from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tools.controlled_markdown_persistence import (
    CONTROLLED_MARKDOWN_AUDIT_FILE,
    TrustedHostSessionContext,
)
from tools.evidence_registry import register_evidence
from tools.extraction_schema import LineRangeLocator
from tools.file_classification import classification_from_dict
from tools.file_state import file_state_from_dict
from tools.knowledge_artifacts import KNOWLEDGE_SCHEMA_VERSION, parse_knowledge_page
from tools.project_inventory import PROJECT_MANIFEST_VERSION, load_project_manifest
from tools.project_layout import parse_json_bytes_strict
from tools.project_registry import load_registered_project
from tools.research_cockpit import ResearchCockpit
from tools.research_core import ResearchCoreService
from tools.research_goals import load_goal
from tools.research_mcp import QUERY_TOOL, ResearchMCPAdapter
from tools.research_planning import load_initial_plan
from tools.research_state import load_project_state
from tools.research_tasks import load_tasks
from tools.source_access import locate_source
from tools.source_registry import load_source_registry, sync_source_registry


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "minimal_research_project"
PROJECT_ID = "j-01-r3-preview"
PLAN_DATE = "2026-07-19"
PLAN_GENERATED_AT = "2026-07-19T12:00:00Z"
RENDERED_AT = "2026-07-19T12:01:00Z"
CANARY_TOKEN = b"J01_C07_BINARY_CANARY_DO_NOT_EXTRACT"
CANARY_PAYLOAD = b"\x00model\x01" + CANARY_TOKEN
QUERY_SECRET = "J01_QUERY_SECRET_MUST_NOT_ECHO"

PRODUCT_PATHS = (
    "overview.md",
    "project-map.md",
    "reproduction.md",
    "architecture.md",
    "papers/index.md",
    "methods/index.md",
    "datasets/index.md",
    "experiments/index.md",
    "results/index.md",
    "claims/index.md",
    "open-questions.md",
    "status.md",
    "risks.md",
    "goals.md",
    "plans/backlog.md",
)
QUERY_UNAVAILABLE = {
    "schema_version": 1,
    "ok": False,
    "capability": "query",
    "error": {
        "code": "capability-unavailable",
        "message": "This Core capability is not implemented yet.",
        "retryable": False,
        "details": {
            "available_after": "G-04",
            "status": "not-implemented",
        },
    },
}
COCKPIT_QUERY_UNAVAILABLE = {
    "available": False,
    "error": "capability-unavailable",
    "available_after": "G-04",
}


class R3MinusC07EndToEndAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _source_snapshot(
        source: Path,
    ) -> tuple[
        dict[str, tuple[int, int]],
        dict[str, tuple[str, int, int, int]],
    ]:
        directories: dict[str, tuple[int, int]] = {}
        files: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(source):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            relative_directory = (
                "." if base == source else base.relative_to(source).as_posix()
            )
            directory_stat = base.stat()
            directories[relative_directory] = (
                directory_stat.st_mtime_ns,
                stat.S_IMODE(directory_stat.st_mode),
            )
            for filename in filenames:
                path = base / filename
                metadata = path.stat()
                files[path.relative_to(source).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    stat.S_IMODE(metadata.st_mode),
                )
        return directories, files

    def _assert_no_canary_in_outputs(self, *roots: Path) -> None:
        for root in roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                self.assertNotIn(
                    CANARY_TOKEN,
                    path.read_bytes(),
                    path.relative_to(root).as_posix(),
                )

    def _load_and_validate_audit(
        self,
        *,
        audit_file: Path,
        current_pages: dict[str, bytes],
        source: Path,
        workspace: Path,
        knowledge_parent: Path,
    ) -> tuple[dict[str, object], ...]:
        payload = audit_file.read_bytes()
        self.assertTrue(payload)
        lines = payload.splitlines(keepends=True)
        self.assertTrue(all(line.endswith(b"\n") for line in lines))

        records: list[dict[str, object]] = []
        for sequence, line in enumerate(lines, start=1):
            record = parse_json_bytes_strict(
                line[:-1],
                label=f"J-01 controlled Markdown audit record {sequence}",
            )
            self.assertIsInstance(record, dict)
            canonical = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
            self.assertEqual(line, canonical)
            self.assertEqual(record["sequence"], sequence)
            records.append(record)

        phases_by_transaction: dict[str, list[str]] = {}
        committed_by_path: dict[str, list[dict[str, object]]] = {}
        for record in records:
            transaction_id = record["transaction_id"]
            self.assertIsInstance(transaction_id, str)
            phases_by_transaction.setdefault(transaction_id, []).append(record["phase"])
            if record["phase"] == "committed":
                committed_by_path.setdefault(record["path"], []).append(record)
        self.assertTrue(phases_by_transaction)
        self.assertTrue(
            all(phases == ["prepared", "committed"] for phases in phases_by_transaction.values())
        )

        for relative_path, page_bytes in current_pages.items():
            current_hash = hashlib.sha256(page_bytes).hexdigest()
            matching = [
                record
                for record in committed_by_path.get(relative_path, [])
                if record["output_sha256"] == current_hash
                and record["observed_after_sha256"] == current_hash
                and record["commit_state"] == "committed"
            ]
            self.assertTrue(
                matching,
                f"current page lacks a committed F-05B audit binding: {relative_path}",
            )

        forbidden_keys = {
            "body",
            "content",
            "excerpt",
            "output_bytes",
            "proposed_bytes",
            "source_bytes",
        }
        for record in records:
            self.assertTrue(forbidden_keys.isdisjoint(record))

        audit_text = payload.decode("utf-8", errors="strict")
        self.assertNotIn(CANARY_TOKEN.decode("ascii"), audit_text)
        self.assertNotIn(QUERY_SECRET, audit_text)
        self.assertNotIn("# Minimal Reproducible Study", audit_text)
        folded_audit = audit_text.casefold()
        for absolute_root in (source, workspace, knowledge_parent):
            for spelling in {
                str(absolute_root.resolve()),
                absolute_root.resolve().as_posix(),
            }:
                self.assertNotIn(spelling.casefold(), folded_audit)
        return tuple(records)

    def _exercise_chain(self, run_number: int) -> dict[str, object]:
        with tempfile.TemporaryDirectory(
            prefix=f"llmwiki-j01-r3-e2e-{run_number}-"
        ) as temp_dir:
            root = Path(temp_dir)
            source = root / "minimal_research_project"
            workspace = root / "workspace"
            knowledge_parent = root / "knowledge"
            shutil.copytree(FIXTURE_ROOT, source)
            (source / "model.ckpt").write_bytes(CANARY_PAYLOAD)
            before = self._source_snapshot(source)

            service = ResearchCoreService(workspace)
            with ExitStack() as stack:
                guards = (
                    stack.enter_context(
                        patch(
                            "tools._utils.call_llm",
                            side_effect=AssertionError(
                                "J-01 deterministic acceptance attempted an LLM call"
                            ),
                        )
                    ),
                    stack.enter_context(
                        patch(
                            "socket.create_connection",
                            side_effect=AssertionError(
                                "J-01 deterministic acceptance attempted network access"
                            ),
                        )
                    ),
                    stack.enter_context(
                        patch(
                            "urllib.request.urlopen",
                            side_effect=AssertionError(
                                "J-01 deterministic acceptance attempted URL access"
                            ),
                        )
                    ),
                    stack.enter_context(
                        patch(
                            "webbrowser.open",
                            side_effect=AssertionError(
                                "J-01 deterministic acceptance attempted browser behavior"
                            ),
                        )
                    ),
                )

                understand = service.project_understand(
                    source,
                    project_id=PROJECT_ID,
                    name="J-01 R3 Preview",
                    knowledge_root=knowledge_parent,
                    final_goal="Reproduce the deterministic research fixture",
                    current_stage="classification",
                    important_question="Which local evidence supports the result?",
                    deadline="2026-12-31",
                    daily_available_hours=2.0,
                )
                self.assertEqual(understand.status, "succeeded")
                self.assertTrue(
                    all(stage["status"] == "succeeded" for stage in understand.record["stages"])
                )

                registration = load_registered_project(workspace, PROJECT_ID)
                manifest_before = load_project_manifest(
                    registration.layout.manifest_file,
                    project_id=PROJECT_ID,
                    project_root=source,
                    required_manifest_version=PROJECT_MANIFEST_VERSION,
                )
                self.assertEqual(manifest_before.scan_generation, 1)
                canary_record = next(
                    record
                    for record in manifest_before.file_records
                    if record["path"] == "model.ckpt"
                )
                canary_classification = classification_from_dict(
                    canary_record["classification"]
                )
                canary_state = file_state_from_dict(canary_record["file_state"])
                self.assertEqual(canary_classification.format, "model_checkpoint")
                self.assertEqual(canary_classification.research_role, "model_artifact")
                self.assertEqual(canary_state.processing_status, "discovered")
                self.assertEqual(canary_state.read_depth, "sampled")
                self.assertEqual(canary_state.reason_code, "classification-sample")
                self.assertEqual(
                    canary_state.reason,
                    "a bounded local prefix was read for deterministic classification; "
                    "content extraction has not been attempted",
                )
                reading_priority = json.loads(
                    registration.layout.reading_priority_file.read_text(encoding="utf-8")
                )
                canary_priority = next(
                    record
                    for record in reading_priority["files"]
                    if record["path"] == "model.ckpt"
                )
                self.assertEqual(canary_priority["deep_read_status"], "limited")
                self.assertEqual(canary_priority["recommended_read_depth"], "sampled")
                self.assertEqual(canary_priority["reference_bytes_read"], 0)
                self.assertIn("model-artifact-limited", canary_priority["reason_codes"])

                sync = sync_source_registry(workspace, PROJECT_ID)
                self.assertEqual(sync.scan_generation, 1)
                source_registry = load_source_registry(workspace, PROJECT_ID)
                readme = source_registry.current_by_path["README.md"]
                self.assertRegex(readme.source_id, r"^src-[0-9a-f]{32}$")
                self.assertEqual(readme.current_version, 1)
                readme_bytes = (source / "README.md").read_bytes()
                readme_hash = hashlib.sha256(readme_bytes).hexdigest()
                self.assertEqual(readme.current_content_hash, readme_hash)

                locator = LineRangeLocator(1, 1)
                excerpt = readme_bytes.splitlines(keepends=True)[0]
                evidence = register_evidence(
                    workspace,
                    PROJECT_ID,
                    source_id=readme.source_id,
                    content_hash=readme.current_content_hash,
                    locator=locator,
                    excerpt=excerpt,
                ).evidence
                self.assertRegex(evidence.evidence_id, r"^evd-[0-9a-f]{64}$")
                self.assertEqual(evidence.source_id, readme.source_id)
                self.assertEqual(evidence.source_version, readme.current_version)
                self.assertEqual(evidence.content_hash, readme.current_content_hash)
                self.assertEqual(evidence.locator, locator)
                self.assertEqual(
                    evidence.excerpt_hash,
                    hashlib.sha256(excerpt).hexdigest(),
                )

                location = locate_source(
                    workspace,
                    PROJECT_ID,
                    readme.source_id,
                )
                self.assertEqual(location.current_path, "README.md")
                self.assertEqual(location.current_version, readme.current_version)
                self.assertEqual(location.content_hash, readme.current_content_hash)
                self.assertEqual(location.absolute_path, (source / "README.md").resolve())

                opened = service.source_open_view(PROJECT_ID, evidence.evidence_id)
                opened_payload = opened.as_dict()
                self.assertEqual(opened_payload["evidence_id"], evidence.evidence_id)
                self.assertEqual(
                    opened_payload["evidence_source_version"],
                    readme.current_version,
                )
                self.assertEqual(opened_payload["source"]["source_id"], readme.source_id)
                self.assertEqual(opened_payload["source"]["current_path"], "README.md")
                self.assertEqual(
                    opened_payload["source"]["content_hash"],
                    readme.current_content_hash,
                )
                self.assertEqual(opened_payload["locator"], locator.as_dict())
                self.assertEqual(opened_payload["excerpt"].encode("utf-8"), excerpt)
                self.assertTrue(opened_payload["content_hash_verified"])
                self.assertTrue(opened_payload["excerpt_hash_verified"])

                query_result = ResearchMCPAdapter(workspace).call_tool(
                    QUERY_TOOL,
                    {
                        "project_id": PROJECT_ID,
                        "question": QUERY_SECRET,
                    },
                )
                query_payload = query_result.structuredContent
                self.assertEqual(query_payload, QUERY_UNAVAILABLE)
                self.assertNotIn(
                    QUERY_SECRET,
                    json.dumps(query_payload, ensure_ascii=False, sort_keys=True),
                )

                reconcile = service.project_reconcile(PROJECT_ID, dirty_paths=())
                reconcile_payload = reconcile.as_dict()
                self.assertEqual(reconcile_payload["status"], "reconciled")
                self.assertEqual(reconcile_payload["mode"], "full-scan")
                self.assertEqual(
                    reconcile_payload["source_of_truth"],
                    "manifest-and-hash",
                )
                self.assertEqual(reconcile_payload["manifest"]["scan_generation"], 2)
                self.assertEqual(reconcile_payload["hints"]["status"], "absent")
                self.assertEqual(
                    reconcile_payload["hints"]["explicit_dirty_path_count"],
                    0,
                )
                self.assertEqual(
                    reconcile_payload["hints"]["combined_dirty_path_count"],
                    0,
                )

                goals_before = registration.layout.goals_file.read_bytes()
                tasks_before = registration.layout.tasks_file.read_bytes()
                planning = service.plan(
                    PROJECT_ID,
                    generated_at=PLAN_GENERATED_AT,
                    plan_date=PLAN_DATE,
                )
                self.assertFalse(planning.state_created)
                self.assertTrue(planning.state_rebuilt)
                self.assertEqual(registration.layout.goals_file.read_bytes(), goals_before)
                self.assertEqual(registration.layout.tasks_file.read_bytes(), tasks_before)

                goal = load_goal(workspace, PROJECT_ID)
                tasks = load_tasks(workspace, PROJECT_ID)
                state = load_project_state(workspace, PROJECT_ID)
                initial_plan = load_initial_plan(workspace, PROJECT_ID)
                self.assertEqual(goal.status, "draft")
                self.assertEqual(initial_plan.status, "draft")
                self.assertTrue(tasks.tasks)
                self.assertTrue(all(task.status == "draft" for task in tasks.tasks))
                self.assertTrue(all(not task.executable for task in tasks.tasks))
                self.assertEqual(initial_plan.state_artifact_id, state.artifact_id)
                self.assertEqual(
                    planning.initial_plan.state_artifact_id,
                    state.artifact_id,
                )

                render = service.knowledge_render(
                    PROJECT_ID,
                    rendered_at=RENDERED_AT,
                    plan_date=PLAN_DATE,
                    persist=True,
                    host_context=TrustedHostSessionContext(
                        host_id="codex",
                        actor_type="host-agent",
                        actor_id="j-01-acceptance",
                        session_id=f"j-01-session-{run_number}",
                    ),
                    decision_id_prefix=f"j-01-render-{run_number}",
                    authorized_at=RENDERED_AT,
                )
                self.assertEqual(render.status, "succeeded")

                cockpit_snapshot = ResearchCockpit(workspace).project_snapshot(PROJECT_ID)
                capabilities = cockpit_snapshot["capabilities"]
                self.assertEqual(capabilities["query"], COCKPIT_QUERY_UNAVAILABLE)
                self.assertEqual(capabilities["c07"], "deferred/not_started")
                self.assertFalse(capabilities["source_bytes_reopened"])

            for guard in guards:
                guard.assert_not_called()

            knowledge_root = registration.layout.knowledge_root
            expected_paths = set(PRODUCT_PATHS) | {
                "index.md",
                f"plans/daily/{PLAN_DATE}.md",
            }
            for relative_path in expected_paths:
                self.assertTrue(
                    (knowledge_root / relative_path).is_file(),
                    relative_path,
                )

            current_pages: dict[str, bytes] = {}
            parsed_paths: list[str] = []
            for page_path in sorted(knowledge_root.rglob("*.md")):
                relative_path = page_path.relative_to(knowledge_root).as_posix()
                page_bytes = page_path.read_bytes()
                parsed = parse_knowledge_page(page_bytes, path=relative_path)
                self.assertEqual(
                    parsed.frontmatter.schema_version,
                    KNOWLEDGE_SCHEMA_VERSION,
                )
                self.assertEqual(parsed.frontmatter.project_id, PROJECT_ID)
                current_pages[relative_path] = page_bytes
                parsed_paths.append(relative_path)
            self.assertTrue(expected_paths.issubset(current_pages))

            web_index = (
                registration.layout.runs_dir
                / understand.run_id
                / "web"
                / "index.html"
            )
            self.assertTrue(web_index.is_file())

            audit_file = (
                registration.layout.indexes_dir / CONTROLLED_MARKDOWN_AUDIT_FILE
            )
            audit_records = self._load_and_validate_audit(
                audit_file=audit_file,
                current_pages=current_pages,
                source=source,
                workspace=workspace,
                knowledge_parent=knowledge_parent,
            )

            self._assert_no_canary_in_outputs(workspace, knowledge_parent)
            cockpit_json = json.dumps(
                cockpit_snapshot,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            self.assertNotIn(CANARY_TOKEN, cockpit_json)
            self.assertNotIn(QUERY_SECRET.encode("utf-8"), cockpit_json)

            self.assertEqual(before, self._source_snapshot(source))
            self.assertFalse((source / ".llmwiki").exists())
            self.assertFalse((source / "wiki").exists())
            self.assertFalse((source / f"{source.name}-wiki").exists())

            return {
                "understand": {
                    "status": understand.status,
                    "stages": tuple(
                        (stage["stage_id"], stage["status"])
                        for stage in understand.record["stages"]
                    ),
                    "web_index": web_index.is_file(),
                },
                "knowledge": {
                    "required_paths": tuple(sorted(expected_paths)),
                    "all_markdown_paths": tuple(parsed_paths),
                    "schema_version": KNOWLEDGE_SCHEMA_VERSION,
                },
                "source_trace": {
                    "source_id_valid": re.fullmatch(
                        r"src-[0-9a-f]{32}", readme.source_id
                    )
                    is not None,
                    "evidence_id_valid": re.fullmatch(
                        r"evd-[0-9a-f]{64}", evidence.evidence_id
                    )
                    is not None,
                    "path": location.current_path,
                    "version": location.current_version,
                    "content_hash": location.content_hash,
                    "locator": locator.as_dict(),
                    "excerpt_hash_verified": opened_payload["excerpt_hash_verified"],
                },
                "query": query_payload,
                "reconcile": {
                    "status": reconcile_payload["status"],
                    "mode": reconcile_payload["mode"],
                    "source_of_truth": reconcile_payload["source_of_truth"],
                    "scan_generation": reconcile_payload["manifest"][
                        "scan_generation"
                    ],
                    "hint_status": reconcile_payload["hints"]["status"],
                },
                "planning": {
                    "goal_status": goal.status,
                    "task_statuses": tuple(task.status for task in tasks.tasks),
                    "task_executable": tuple(task.executable for task in tasks.tasks),
                    "plan_status": initial_plan.status,
                    "state_refreshed": planning.state_rebuilt,
                    "daily_path": f"plans/daily/{PLAN_DATE}.md",
                },
                "render": {
                    "status": render.status,
                    "pages": tuple(
                        sorted(
                            (page.path, page.action, page.reason_code)
                            for page in render.pages
                        )
                    ),
                },
                "audit": {
                    "record_count": len(audit_records),
                    "committed_count": sum(
                        record["phase"] == "committed"
                        for record in audit_records
                    ),
                    "current_page_count": len(current_pages),
                },
                "c07": {
                    "classification": canary_classification.format,
                    "research_role": canary_classification.research_role,
                    "processing_status": canary_state.processing_status,
                    "read_depth": canary_state.read_depth,
                    "reason_code": canary_state.reason_code,
                    "deep_read_status": canary_priority["deep_read_status"],
                    "capability": capabilities["c07"],
                    "canary_leaked": False,
                },
                "cockpit_query": capabilities["query"],
            }

    def test_r3_minus_c07_preview_is_repeatable_safe_and_complete(self) -> None:
        first = self._exercise_chain(1)
        second = self._exercise_chain(2)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
