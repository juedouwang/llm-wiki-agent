from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.host_context import (
    HOST_CONTEXT_DEFAULT_MAX_BYTES,
    HostContextBudgetError,
    HostContextPackResult,
    build_host_context_pack,
)
from tools.project_layout import UnsupportedSchemaVersionError
from tools.project_registry import ProjectRecordError
from tools.research_core import ResearchCoreService
from tools.source_registry import load_source_registry, sync_source_registry


_TOP_LEVEL_FIELDS = {
    "schema_version",
    "kind",
    "pack_version",
    "project_id",
    "budget",
    "state",
    "tasks",
    "risks",
    "evidence_refs",
    "omissions",
}
_STABLE_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def canonical_bytes(value: object) -> bytes:
    """Independent canonical encoder used to verify the product budget."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class HostContextPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.knowledge = self.root / "knowledge"
        self.service = ResearchCoreService(self.workspace)

    @staticmethod
    def nested_keys(value: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                keys.add(str(key))
                keys.update(HostContextPackTests.nested_keys(child))
        elif isinstance(value, list):
            for child in value:
                keys.update(HostContextPackTests.nested_keys(child))
        return keys

    @staticmethod
    def nested_strings(value: object) -> tuple[str, ...]:
        strings: list[str] = []
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                strings.extend(HostContextPackTests.nested_strings(child))
        elif isinstance(value, list):
            for child in value:
                strings.extend(HostContextPackTests.nested_strings(child))
        return tuple(strings)

    @staticmethod
    def source_snapshot(project: Path) -> dict[str, tuple[str, int, int, int]]:
        snapshot: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in filenames:
                path = base / name
                relative = path.relative_to(project).as_posix()
                metadata = path.stat()
                snapshot[relative] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_mode,
                )
        return snapshot

    @staticmethod
    def omission_count(
        payload: dict[str, Any],
        *,
        section: str,
        reason_code: str,
    ) -> int:
        return sum(
            row["count"]
            for row in payload["omissions"]
            if row["section"] == section and row["reason_code"] == reason_code
        )

    def assert_pack_contract(
        self,
        payload: dict[str, Any],
        *,
        max_bytes: int,
    ) -> None:
        self.assertEqual(set(payload), _TOP_LEVEL_FIELDS)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["kind"], "llmwiki-host-context-pack")
        self.assertEqual(payload["pack_version"], "host-context-pack-v1")
        self.assertIsInstance(payload["project_id"], str)
        self.assertTrue(payload["project_id"])

        budget = payload["budget"]
        self.assertEqual(
            set(budget),
            {"unit", "max_bytes", "used_bytes", "truncated"},
        )
        self.assertEqual(budget["unit"], "canonical-json-utf8-bytes")
        self.assertEqual(budget["max_bytes"], max_bytes)
        self.assertIsInstance(budget["used_bytes"], int)
        self.assertNotIsInstance(budget["used_bytes"], bool)
        self.assertGreater(budget["used_bytes"], 0)
        self.assertLessEqual(budget["used_bytes"], max_bytes)
        self.assertIsInstance(budget["truncated"], bool)

        encoded = canonical_bytes(payload)
        self.assertEqual(budget["used_bytes"], len(encoded))
        self.assertLessEqual(len(encoded), max_bytes)

        self.assertIsInstance(payload["state"], dict)
        self.assertIsInstance(payload["tasks"], list)
        self.assertIsInstance(payload["risks"], list)
        self.assertIsInstance(payload["evidence_refs"], list)
        self.assertIsInstance(payload["omissions"], list)
        for omission in payload["omissions"]:
            self.assertEqual(
                set(omission),
                {"section", "reason_code", "count"},
            )
            self.assertIsInstance(omission["section"], str)
            self.assertRegex(omission["reason_code"], _STABLE_CODE)
            self.assertIsInstance(omission["count"], int)
            self.assertNotIsInstance(omission["count"], bool)
            self.assertGreater(omission["count"], 0)

    def prepare_project(
        self,
        *,
        project_id: str,
        evidence_count: int = 1,
        include_filtered_evidence: bool = False,
        name: str | None = None,
        final_goal: str = "Verify the bounded context protocol",
        current_stage: str = "G-08 validation",
        important_question: str = "Can a host reopen current Evidence safely?",
    ) -> dict[str, Any]:
        project = self.root / f"source-{project_id}"
        project.mkdir(parents=True)

        source_text: dict[str, str] = {}
        for index in range(evidence_count):
            relative = f"notes/note-{index:03d}.md"
            text = f"RAW_EVIDENCE_{index:03d}_{'x' * 72}\n"
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            source_text[relative] = text

        sensitive_secret = None
        stale_original = None
        stale_changed = None
        if include_filtered_evidence:
            sensitive_secret = "G08_SENSITIVE_TOKEN_f0ac63e2"
            sensitive_text = f"TOKEN={sensitive_secret}\n"
            (project / ".env").write_text(
                sensitive_text,
                encoding="utf-8",
                newline="\n",
            )
            source_text[".env"] = sensitive_text

            stale_original = "G08_STALE_ORIGINAL_6a9a8d55\n"
            stale_path = project / "notes" / "stale.md"
            stale_path.write_text(
                stale_original,
                encoding="utf-8",
                newline="\n",
            )
            source_text["notes/stale.md"] = stale_original

        registration = self.service.register(
            project,
            project_id=project_id,
            name=name,
            knowledge_root=self.knowledge,
            final_goal=final_goal,
            current_stage=current_stage,
            important_question=important_question,
            deadline="2026-12-31",
            daily_available_hours=2.5,
        )
        self.service.scan(project_id)
        sync_source_registry(self.workspace, project_id)
        registry = load_source_registry(self.workspace, project_id)

        evidence: dict[str, Any] = {}
        for relative, text in sorted(source_text.items()):
            source = registry.current_by_path[relative]
            content_hash = source.current_content_hash
            assert content_hash is not None
            evidence[relative] = register_evidence(
                self.workspace,
                project_id,
                source_id=source.source_id,
                content_hash=content_hash,
                locator=LineRangeLocator(1, 1),
                excerpt=text,
            ).evidence

        if include_filtered_evidence:
            stale_changed = "G08_STALE_CHANGED_90d461c0_with_new_bytes\n"
            (project / "notes" / "stale.md").write_text(
                stale_changed,
                encoding="utf-8",
                newline="\n",
            )
            self.service.scan(project_id)
            sync_source_registry(self.workspace, project_id)

        return {
            "project": project,
            "registration": registration,
            "evidence": evidence,
            "source_text": source_text,
            "sensitive_secret": sensitive_secret,
            "stale_original": stale_original,
            "stale_changed": stale_changed,
        }

    def test_direct_and_core_service_results_share_the_closed_core_state(self) -> None:
        fixture = self.prepare_project(
            project_id="g-08-core-parity",
            evidence_count=2,
        )

        direct_result = build_host_context_pack(
            self.workspace,
            "g-08-core-parity",
        )
        service_result = self.service.host_context_pack("g-08-core-parity")
        self.assertIsInstance(direct_result, HostContextPackResult)
        self.assertEqual(direct_result.as_dict(), service_result.as_dict())

        payload = direct_result.as_dict()
        self.assert_pack_contract(payload, max_bytes=HOST_CONTEXT_DEFAULT_MAX_BYTES)
        self.assertEqual(
            direct_result.serialized_bytes(),
            canonical_bytes(payload),
        )

        project_context = self.service.project_context("g-08-core-parity").as_dict()
        coverage = self.service.coverage_view("g-08-core-parity").as_dict()["report"]
        self.assertEqual(payload["state"]["project"]["name"], project_context["name"])
        self.assertEqual(
            payload["state"]["project"]["onboarding"],
            project_context["onboarding"],
        )
        self.assertEqual(
            payload["state"]["inventory"]["scan_generation"],
            coverage["manifest"]["scan_generation"],
        )
        self.assertEqual(
            payload["state"]["inventory"]["file_count"],
            coverage["totals"]["file_count"],
        )
        self.assertEqual(
            {row["evidence_id"] for row in payload["evidence_refs"]},
            {item.evidence_id for item in fixture["evidence"].values()},
        )

        payload["state"]["project"]["name"] = "caller mutation"
        self.assertNotEqual(
            direct_result.as_dict()["state"]["project"]["name"],
            "caller mutation",
        )

    def test_multibyte_utf8_is_counted_over_the_complete_canonical_envelope(
        self,
    ) -> None:
        project_name = "量子研究🚀"
        final_goal = "验证多字节预算不会按字符误算"
        fixture = self.prepare_project(
            project_id="g-08-unicode",
            evidence_count=1,
            name=project_name,
            final_goal=final_goal,
            current_stage="证据核验阶段",
            important_question="遗漏原因是否清晰？",
        )

        before = self.source_snapshot(fixture["project"])
        result = build_host_context_pack(self.workspace, "g-08-unicode")
        payload = result.as_dict()
        encoded = canonical_bytes(payload)
        text = encoded.decode("utf-8")

        self.assert_pack_contract(payload, max_bytes=HOST_CONTEXT_DEFAULT_MAX_BYTES)
        self.assertIn(project_name, text)
        self.assertIn(final_goal, text)
        self.assertGreater(len(encoded), len(text))
        self.assertEqual(result.used_bytes, len(encoded))
        self.assertEqual(before, self.source_snapshot(fixture["project"]))

    def test_repeated_build_is_byte_identical_and_source_read_only(self) -> None:
        fixture = self.prepare_project(
            project_id="g-08-deterministic",
            evidence_count=4,
        )
        before = self.source_snapshot(fixture["project"])

        first = build_host_context_pack(
            self.workspace,
            "g-08-deterministic",
            max_bytes=8192,
        )
        second = build_host_context_pack(
            self.workspace,
            "g-08-deterministic",
            max_bytes=8192,
        )

        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.serialized_bytes(), second.serialized_bytes())
        self.assert_pack_contract(first.as_dict(), max_bytes=8192)
        self.assertEqual(before, self.source_snapshot(fixture["project"]))
        self.assertFalse((fixture["project"] / ".llmwiki").exists())
        self.assertFalse((fixture["project"] / "wiki").exists())

    def test_large_project_truncates_deterministically_with_counted_reasons(
        self,
    ) -> None:
        fixture = self.prepare_project(
            project_id="g-08-large",
            evidence_count=32,
        )
        expected_ids = {
            item.evidence_id for item in fixture["evidence"].values()
        }
        full = build_host_context_pack(
            self.workspace,
            "g-08-large",
            max_bytes=1_048_576,
        ).as_dict()
        self.assertEqual(
            {row["evidence_id"] for row in full["evidence_refs"]},
            expected_ids,
        )
        self.assertFalse(full["budget"]["truncated"])
        self.assertGreater(full["budget"]["used_bytes"], 4096)

        first = build_host_context_pack(
            self.workspace,
            "g-08-large",
            max_bytes=4096,
        )
        second = build_host_context_pack(
            self.workspace,
            "g-08-large",
            max_bytes=4096,
        )
        payload = first.as_dict()

        self.assert_pack_contract(payload, max_bytes=4096)
        self.assertTrue(payload["budget"]["truncated"])
        self.assertEqual(first.serialized_bytes(), second.serialized_bytes())
        included_ids = {row["evidence_id"] for row in payload["evidence_refs"]}
        self.assertTrue(included_ids < expected_ids)
        omitted_for_budget = self.omission_count(
            payload,
            section="evidence_refs",
            reason_code="context-budget-exceeded",
        )
        self.assertEqual(omitted_for_budget, len(expected_ids - included_ids))

    def test_sensitive_ignored_and_stale_evidence_are_filtered_without_leaks(
        self,
    ) -> None:
        fixture = self.prepare_project(
            project_id="g-08-filtering",
            evidence_count=2,
            include_filtered_evidence=True,
        )
        result = build_host_context_pack(
            self.workspace,
            "g-08-filtering",
            max_bytes=32768,
        )
        payload = result.as_dict()
        self.assert_pack_contract(payload, max_bytes=32768)

        expected_valid = {
            evidence.evidence_id
            for relative, evidence in fixture["evidence"].items()
            if relative.startswith("notes/note-")
        }
        included = {row["evidence_id"] for row in payload["evidence_refs"]}
        self.assertEqual(included, expected_valid)
        self.assertNotIn(
            fixture["evidence"][".env"].evidence_id,
            included,
        )
        self.assertNotIn(
            fixture["evidence"]["notes/stale.md"].evidence_id,
            included,
        )
        self.assertEqual(
            self.omission_count(
                payload,
                section="evidence_refs",
                reason_code="sensitive-path-filtered",
            ),
            1,
        )
        self.assertEqual(
            self.omission_count(
                payload,
                section="evidence_refs",
                reason_code="evidence-stale",
            ),
            1,
        )
        self.assertEqual(
            payload["state"]["inventory"]["read_depth_counts"]["ignored"],
            1,
        )

        forbidden_reference_keys = {
            "absolute_path",
            "current_path",
            "excerpt",
            "path",
            "project_root",
            "raw_content",
            "text",
        }
        reference_surface = [*payload["evidence_refs"], *payload["omissions"]]
        self.assertTrue(
            self.nested_keys(reference_surface).isdisjoint(forbidden_reference_keys),
            self.nested_keys(reference_surface) & forbidden_reference_keys,
        )

        serialized = result.serialized_bytes().decode("utf-8")
        forbidden_values = (
            ".env",
            "notes/stale.md",
            str(fixture["project"].resolve()),
            str(self.workspace.resolve()),
            fixture["sensitive_secret"],
            fixture["stale_original"],
            fixture["stale_changed"],
            *fixture["source_text"].values(),
        )
        for value in forbidden_values:
            assert value is not None
            self.assertNotIn(value, serialized)

        for omission in payload["omissions"]:
            omission_text = json.dumps(omission, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(".env", omission_text)
            self.assertNotIn("stale.md", omission_text)
            self.assertNotRegex(omission_text, r"[0-9a-f]{64}")

    def test_task_store_absence_is_explicit_and_onboarding_is_not_a_task(self) -> None:
        final_goal = "Finish the deterministic G-08 slice"
        current_stage = "Context assembly"
        self.prepare_project(
            project_id="g-08-no-task-store",
            evidence_count=1,
            final_goal=final_goal,
            current_stage=current_stage,
        )

        payload = build_host_context_pack(
            self.workspace,
            "g-08-no-task-store",
        ).as_dict()

        self.assertEqual(payload["tasks"], [])
        self.assertEqual(
            self.omission_count(
                payload,
                section="tasks",
                reason_code="task-store-unavailable",
            ),
            1,
        )
        onboarding = payload["state"]["project"]["onboarding"]
        self.assertEqual(onboarding["final_goal"], final_goal)
        self.assertEqual(onboarding["current_stage"], current_stage)

    def test_absent_source_and_evidence_registries_have_visible_reasons(self) -> None:
        project = self.root / "source-g-08-empty-registries"
        project.mkdir()
        (project / "README.md").write_text(
            "No Evidence has been registered.\n",
            encoding="utf-8",
            newline="\n",
        )
        self.service.register(
            project,
            project_id="g-08-empty-registries",
            knowledge_root=self.knowledge,
        )
        self.service.scan("g-08-empty-registries")

        without_sources = build_host_context_pack(
            self.workspace,
            "g-08-empty-registries",
        ).as_dict()
        self.assertEqual(without_sources["evidence_refs"], [])
        self.assertEqual(
            self.omission_count(
                without_sources,
                section="evidence_refs",
                reason_code="source-registry-unavailable",
            ),
            1,
        )

        sync_source_registry(self.workspace, "g-08-empty-registries")
        without_evidence = build_host_context_pack(
            self.workspace,
            "g-08-empty-registries",
        ).as_dict()
        self.assertEqual(without_evidence["evidence_refs"], [])
        self.assertEqual(
            self.omission_count(
                without_evidence,
                section="evidence_refs",
                reason_code="evidence-registry-unavailable",
            ),
            1,
        )

    def test_too_small_budget_raises_the_stable_typed_error(self) -> None:
        fixture = self.prepare_project(
            project_id="g-08-small-budget",
            evidence_count=1,
        )
        before = self.source_snapshot(fixture["project"])

        with self.assertRaises(HostContextBudgetError) as raised:
            self.service.host_context_pack(
                "g-08-small-budget",
                max_bytes=512,
            )

        self.assertEqual(raised.exception.reason_code, "context-budget-too-small")
        self.assertIsInstance(raised.exception.minimum_required_bytes, int)
        self.assertGreater(raised.exception.minimum_required_bytes, 512)
        self.assertEqual(before, self.source_snapshot(fixture["project"]))

    def test_pack_never_bulk_loads_curated_wiki_or_calls_external_surfaces(
        self,
    ) -> None:
        fixture = self.prepare_project(
            project_id="g-08-local-only",
            evidence_count=2,
        )
        curated_marker = "G08_CURATED_WIKI_MUST_NOT_BE_BULK_LOADED_2fd4b8f6"
        overview = fixture["registration"].layout.overview_file
        overview.write_text(
            f"# Curated project overview\n\n{curated_marker}\n",
            encoding="utf-8",
            newline="\n",
        )
        before = self.source_snapshot(fixture["project"])

        with ExitStack() as stack:
            guards = (
                stack.enter_context(
                    patch(
                        "tools._utils.call_llm",
                        side_effect=AssertionError("G-08 attempted an LLM call"),
                    )
                ),
                stack.enter_context(
                    patch(
                        "socket.create_connection",
                        side_effect=AssertionError("G-08 attempted network access"),
                    )
                ),
                stack.enter_context(
                    patch(
                        "urllib.request.urlopen",
                        side_effect=AssertionError("G-08 attempted URL access"),
                    )
                ),
                stack.enter_context(
                    patch(
                        "webbrowser.open",
                        side_effect=AssertionError("G-08 attempted Web UI behavior"),
                    )
                ),
            )
            result = build_host_context_pack(
                self.workspace,
                "g-08-local-only",
            )

        for guard in guards:
            guard.assert_not_called()
        self.assertNotIn(curated_marker, result.serialized_bytes().decode("utf-8"))
        self.assertEqual(before, self.source_snapshot(fixture["project"]))

    def test_future_project_schema_fails_closed(self) -> None:
        fixture = self.prepare_project(
            project_id="g-08-future-schema",
            evidence_count=1,
        )
        project_file = fixture["registration"].layout.project_file
        record = json.loads(project_file.read_text(encoding="utf-8"))
        record["schema_version"] = 2
        project_file.write_bytes(canonical_bytes(record))

        with self.assertRaises(ProjectRecordError) as raised:
            build_host_context_pack(
                self.workspace,
                "g-08-future-schema",
            )
        self.assertIsInstance(raised.exception.__cause__, UnsupportedSchemaVersionError)


if __name__ == "__main__":
    unittest.main()