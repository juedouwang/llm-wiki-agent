from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tools import reading_priority as priority_module
from tools.advisory_lock import AdvisoryFileLock, AdvisoryLockTimeoutError
from tools.file_state import FILE_STATE_KIND, FILE_STATE_SCHEMA_VERSION
from tools.project_inventory import inventory_project
from tools.project_layout import LayoutError, UnsupportedSchemaVersionError
from tools.project_registry import register_project
from tools.reading_priority import (
    READING_PRIORITY_KIND,
    READING_PRIORITY_SCHEMA_VERSION,
    READING_PRIORITY_VERSION,
    ReadingPriorityError,
    generate_reading_priority,
    load_current_reading_priority,
    load_reading_priority,
)
from tools.research_core import ResearchCoreService
from tools.scan_policy import ScanPolicyConfig


REPO_ROOT = Path(__file__).parent.parent


class ReadingPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.project = self.root / "research-project"
        for directory in (
            "src",
            "configs",
            "figures",
            "models",
            "data",
            "a",
            "b",
        ):
            (self.project / directory).mkdir(parents=True, exist_ok=True)
        (self.project / "README.md").write_text(
            """# Priority Study

![plot](figures/plot.png)
[config](configs/base.yaml)
Implementation: `src/helper.py`
Sensitive: [token](secret.txt)
Oversized: [table](data/huge.csv)
Weights: [checkpoint](models/checkpoint.pt)
Ambiguous: shared.txt
Rejected: ../outside.txt and https://example.test/remote.csv
Traversal must not fall back: [escape](../unique.txt)
""",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "configs" / "base.yaml").write_text(
            "entrypoint: ../src/helper.py\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "src" / "helper.py").write_text(
            "VALUE = 7\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "figures" / "plot.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"plot"
        )
        (self.project / "models" / "checkpoint.pt").write_bytes(b"weights")
        (self.project / "secret.txt").write_text(
            "do not read\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "data" / "huge.csv").write_bytes(b"x" * 400)
        (self.project / "a" / "shared.txt").write_text("a\n", encoding="utf-8")
        (self.project / "b" / "shared.txt").write_text("b\n", encoding="utf-8")
        (self.project / "unique.txt").write_text("inside\n", encoding="utf-8")
        (self.root / "outside.txt").write_text("outside\n", encoding="utf-8")
        self.policy_config = ScanPolicyConfig(
            max_content_file_bytes=360,
            max_raw_external_send_bytes=128,
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="priority-study",
        )
        self.inventory = inventory_project(
            self.workspace,
            self.registration.project_id,
            policy_config=self.policy_config,
        )

    def source_snapshot(self) -> dict[str, tuple[str, int]]:
        snapshot: dict[str, tuple[str, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for name in filenames:
                path = base / name
                snapshot[path.relative_to(self.project).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
        return snapshot

    def curated_snapshot(self) -> dict[str, bytes]:
        root = self.registration.layout.knowledge_root
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def manifest_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.inventory.manifest_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def write_manifest_rows(self, rows: list[dict[str, object]]) -> None:
        self.inventory.manifest_file.write_text(
            "".join(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for row in rows
            ),
            encoding="utf-8",
            newline="\n",
        )

    def replace_file_state(
        self,
        relative_path: str,
        *,
        processing_status: str,
        read_depth: str,
        reason_code: str,
        reason: str,
    ) -> None:
        rows = self.manifest_rows()
        for row in rows[1:]:
            if row["record_type"] == "file" and row["path"] == relative_path:
                row["file_state"] = {
                    "schema_version": FILE_STATE_SCHEMA_VERSION,
                    "kind": FILE_STATE_KIND,
                    "processing_status": processing_status,
                    "read_depth": read_depth,
                    "reason_code": reason_code,
                    "reason": reason,
                }
                break
        else:
            self.fail(f"missing Manifest file row {relative_path!r}")
        statuses: Counter[str] = Counter()
        depths: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for row in rows[1:]:
            if row["record_type"] != "file":
                continue
            state = row["file_state"]
            statuses[state["processing_status"]] += 1
            depths[state["read_depth"]] += 1
            reasons[state["reason_code"]] += 1
        summary = rows[0]["file_state_summary"]
        summary["processing_statuses"] = dict(sorted(statuses.items()))
        summary["read_depths"] = dict(sorted(depths.items()))
        summary["reasons"] = dict(sorted(reasons.items()))
        self.write_manifest_rows(rows)

    @staticmethod
    def by_path(priority: dict[str, object]) -> dict[str, dict[str, object]]:
        return {record["path"]: record for record in priority["files"]}

    @staticmethod
    def write_sized_file(path: Path, size_bytes: int, *, prefix: bytes = b"") -> None:
        if size_bytes < len(prefix):
            raise ValueError("size_bytes must be at least the prefix length")
        with path.open("wb") as target:
            target.write(prefix)
            if size_bytes > len(prefix):
                target.seek(size_bytes - 1)
                target.write(b"\0")

    def test_references_promote_allowed_targets_and_policy_limits_remain(self) -> None:
        self.replace_file_state(
            "src/helper.py",
            processing_status="processed",
            read_depth="metadata_only",
            reason_code="metadata-indexed",
            reason="metadata was indexed without reading source content",
        )
        source_before = self.source_snapshot()
        curated_before = self.curated_snapshot()

        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        records = self.by_path(result.priority)

        for path in ("figures/plot.png", "configs/base.yaml", "src/helper.py"):
            self.assertEqual(records[path]["priority_tier"], "promoted")
            self.assertEqual(records[path]["deep_read_status"], "selected")
            self.assertEqual(records[path]["recommended_read_depth"], "deep_read")
            self.assertIn("README.md", records[path]["referenced_by"])
        self.assertEqual(records["src/helper.py"]["current_read_depth"], "metadata_only")
        self.assertEqual(records["secret.txt"]["deep_read_status"], "limited")
        self.assertIn("sensitive-path", records["secret.txt"]["reason_codes"])
        self.assertEqual(records["data/huge.csv"]["deep_read_status"], "limited")
        self.assertIn("content-size-limit", records["data/huge.csv"]["reason_codes"])
        self.assertEqual(records["models/checkpoint.pt"]["deep_read_status"], "limited")
        self.assertIn(
            "model-artifact-limited",
            records["models/checkpoint.pt"]["reason_codes"],
        )
        self.assertEqual(
            [entry["path"] for entry in result.priority["promotion_queue"]],
            [
                record["path"]
                for record in result.priority["files"]
                if record["priority_tier"] == "promoted"
            ],
        )
        self.assertEqual(source_before, self.source_snapshot())
        self.assertEqual(curated_before, self.curated_snapshot())
        self.assertTrue(result.priority_file.is_relative_to(self.registration.layout.machine_root))
        self.assertFalse(
            result.priority_file.is_relative_to(self.registration.layout.knowledge_root)
        )

    def test_outside_uri_and_ambiguous_references_do_not_resolve(self) -> None:
        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        records = self.by_path(result.priority)
        self.assertEqual(records["a/shared.txt"]["referenced_by"], [])
        self.assertEqual(records["b/shared.txt"]["referenced_by"], [])
        self.assertEqual(records["unique.txt"]["referenced_by"], [])
        self.assertNotIn("outside.txt", records)
        all_references = {
            source
            for record in result.priority["files"]
            for source in record["referenced_by"]
        }
        self.assertNotIn("https://example.test/remote.csv", all_references)

    def test_normal_read_reference_is_still_promoted_to_deep_read(self) -> None:
        self.replace_file_state(
            "src/helper.py",
            processing_status="processed",
            read_depth="normal_read",
            reason_code="text-extracted",
            reason="source text was read at normal depth",
        )

        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        record = self.by_path(result.priority)["src/helper.py"]
        self.assertEqual(record["priority_tier"], "promoted")
        self.assertEqual(record["current_read_depth"], "normal_read")
        self.assertEqual(record["recommended_read_depth"], "deep_read")
        self.assertIn(
            "src/helper.py",
            [entry["path"] for entry in result.priority["promotion_queue"]],
        )

    def test_loader_rejects_semantically_tampered_rank_and_score(self) -> None:
        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        queue_paths = {
            entry["path"] for entry in result.priority["promotion_queue"]
        }
        swappable_index = next(
            index
            for index in range(len(result.priority["files"]) - 1)
            if result.priority["files"][index]["priority_tier"]
            == result.priority["files"][index + 1]["priority_tier"]
            and result.priority["files"][index]["path"] not in queue_paths
            and result.priority["files"][index + 1]["path"] not in queue_paths
        )

        reordered = json.loads(json.dumps(result.priority))
        reordered["files"][swappable_index], reordered["files"][swappable_index + 1] = (
            reordered["files"][swappable_index + 1],
            reordered["files"][swappable_index],
        )
        reordered["files"][swappable_index]["priority_rank"] = swappable_index + 1
        reordered["files"][swappable_index + 1]["priority_rank"] = swappable_index + 2
        tampered = self.root / "reordered-priority.json"
        tampered.write_text(
            json.dumps(reordered, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ReadingPriorityError, "rank order"):
            load_reading_priority(tampered, project_id=self.registration.project_id)

        rescored = json.loads(json.dumps(result.priority))
        target = next(
            record
            for record in reversed(rescored["files"])
            if record["path"] not in queue_paths
        )
        target["priority_score"] += 1
        tampered.write_text(
            json.dumps(rescored, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(ReadingPriorityError, "score"):
            load_reading_priority(tampered, project_id=self.registration.project_id)

    def test_loader_rejects_bool_and_float_rank_encodings(self) -> None:
        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        cases = (
            ("priority-rank-bool", ("files", 0, "priority_rank"), True),
            ("queue-rank-float", ("promotion_queue", 0, "queue_rank"), 1.0),
        )
        for name, location, value in cases:
            with self.subTest(name=name):
                tampered_payload = json.loads(json.dumps(result.priority))
                collection, index, field = location
                tampered_payload[collection][index][field] = value
                tampered = self.root / f"{name}.json"
                tampered.write_text(
                    json.dumps(
                        tampered_payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                with self.assertRaisesRegex(ReadingPriorityError, "rank"):
                    load_reading_priority(
                        tampered,
                        project_id=self.registration.project_id,
                    )

    def test_current_grounded_loader_rejects_policy_limited_promotion_tamper(self) -> None:
        original_limited_reason = priority_module._limited_reason

        def bypass_oversized_limit(info: object) -> str | None:
            if info.path == "data/huge.csv":
                return None
            return original_limited_reason(info)

        with patch.object(
            priority_module,
            "_limited_reason",
            side_effect=bypass_oversized_limit,
        ):
            result = generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )

        record = self.by_path(result.priority)["data/huge.csv"]
        self.assertEqual(record["priority_tier"], "promoted")
        self.assertEqual(record["deep_read_status"], "selected")
        previous = result.priority_file.read_bytes()
        # Schema-only parsing cannot reconstruct B-02 policy and therefore is
        # intentionally not execution authorization.
        self.assertEqual(
            load_reading_priority(
                result.priority_file,
                project_id=self.registration.project_id,
            ),
            result.priority,
        )
        with self.assertRaisesRegex(ReadingPriorityError, "currently limited"):
            load_current_reading_priority(
                self.workspace,
                self.registration.project_id,
            )
        with self.assertRaisesRegex(ReadingPriorityError, "currently limited"):
            generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )
        self.assertEqual(result.priority_file.read_bytes(), previous)

    def test_structural_loader_recomputes_model_checkpoint_limitation(self) -> None:
        original_limited_reason = priority_module._limited_reason
        original_intrinsic_reason = priority_module._intrinsic_limited_reason

        def bypass_model_limit(info: object) -> str | None:
            if info.path == "models/checkpoint.pt":
                return None
            return original_limited_reason(info)

        def bypass_model_intrinsic(**values: object) -> str | None:
            if values["research_role"] == "model_artifact":
                return None
            return original_intrinsic_reason(**values)

        with (
            patch.object(
                priority_module,
                "_limited_reason",
                side_effect=bypass_model_limit,
            ),
            patch.object(
                priority_module,
                "_intrinsic_limited_reason",
                side_effect=bypass_model_intrinsic,
            ),
        ):
            result = generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )

        record = self.by_path(result.priority)["models/checkpoint.pt"]
        self.assertEqual(record["priority_tier"], "promoted")
        self.assertEqual(record["deep_read_status"], "selected")
        with self.assertRaisesRegex(ReadingPriorityError, "intrinsically limited"):
            load_reading_priority(
                result.priority_file,
                project_id=self.registration.project_id,
            )

    def test_structural_loader_recomputes_processing_and_read_depth_limits(self) -> None:
        original_manifest = self.inventory.manifest_file.read_bytes()
        cases = (
            ("failed", "sampled", "processing-state-limited"),
            ("processed", "ignored", "read-depth-limited"),
        )
        for processing_status, read_depth, expected_reason in cases:
            with self.subTest(
                processing_status=processing_status,
                read_depth=read_depth,
            ):
                self.inventory.manifest_file.write_bytes(original_manifest)
                self.registration.layout.reading_priority_file.unlink(missing_ok=True)
                self.replace_file_state(
                    "src/helper.py",
                    processing_status=processing_status,
                    read_depth=read_depth,
                    reason_code="test-limited-state",
                    reason="test state requires a hard reading limit",
                )
                original_limited_reason = priority_module._limited_reason
                original_intrinsic_reason = priority_module._intrinsic_limited_reason

                def bypass_target_limit(info: object) -> str | None:
                    if info.path == "src/helper.py":
                        return None
                    return original_limited_reason(info)

                def bypass_target_intrinsic(**values: object) -> str | None:
                    reason = original_intrinsic_reason(**values)
                    return None if reason == expected_reason else reason

                with (
                    patch.object(
                        priority_module,
                        "_limited_reason",
                        side_effect=bypass_target_limit,
                    ),
                    patch.object(
                        priority_module,
                        "_intrinsic_limited_reason",
                        side_effect=bypass_target_intrinsic,
                    ),
                ):
                    result = generate_reading_priority(
                        self.workspace,
                        self.registration.project_id,
                    )
                with self.assertRaisesRegex(
                    ReadingPriorityError,
                    "intrinsically limited",
                ):
                    load_reading_priority(
                        result.priority_file,
                        project_id=self.registration.project_id,
                    )
        self.inventory.manifest_file.write_bytes(original_manifest)

    def test_loader_checks_unresolved_path_before_following_a_symlink(self) -> None:
        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        lexical_alias = (
            result.priority_file.parent
            / ".."
            / result.priority_file.parent.name
            / result.priority_file.name
        )
        self.assertNotEqual(lexical_alias, lexical_alias.resolve())

        with patch.object(
            Path,
            "is_symlink",
            autospec=True,
            side_effect=lambda candidate: candidate == lexical_alias,
        ):
            with self.assertRaisesRegex(ReadingPriorityError, "symbolic link"):
                load_reading_priority(
                    lexical_alias,
                    project_id=self.registration.project_id,
                )

    def test_symbolic_link_artifact_fails_closed_without_replacement(self) -> None:
        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        external = self.root / "external-priority.json"
        external.write_bytes(result.priority_file.read_bytes())
        result.priority_file.unlink()
        try:
            os.symlink(external, result.priority_file)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

        before = external.read_bytes()
        with self.assertRaisesRegex(ReadingPriorityError, "symbolic link"):
            load_reading_priority(
                result.priority_file,
                project_id=self.registration.project_id,
            )
        with self.assertRaisesRegex(ReadingPriorityError, "symbolic link"):
            generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )
        self.assertTrue(result.priority_file.is_symlink())
        self.assertEqual(external.read_bytes(), before)

    def test_unchanged_inputs_are_byte_identical_and_loader_is_strict(self) -> None:
        first = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        first_bytes = first.priority_file.read_bytes()
        second = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(second.priority_file.read_bytes(), first_bytes)
        loaded = load_reading_priority(
            second.priority_file,
            project_id=self.registration.project_id,
        )
        self.assertEqual(loaded, second.priority)
        self.assertEqual(
            load_current_reading_priority(
                self.workspace,
                self.registration.project_id,
            ),
            second.priority,
        )
        serialized = (
            json.dumps(
                loaded,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(first_bytes, serialized)

    def test_generation_creates_no_extraction_source_evidence_run_or_curated_state(self) -> None:
        curated_before = self.curated_snapshot()
        generate_reading_priority(self.workspace, self.registration.project_id)
        layout = self.registration.layout
        self.assertFalse(layout.sources_file.exists())
        self.assertFalse(layout.machine_root.joinpath("evidence.jsonl").exists())
        self.assertEqual(list(layout.extracted_dir.iterdir()), [])
        self.assertEqual(list(layout.runs_dir.iterdir()), [])
        self.assertEqual(curated_before, self.curated_snapshot())

    def test_reference_read_is_bounded_when_the_descriptor_appears_to_grow(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        original_read = os.read
        requested_counts: list[int] = []
        returned_counts: list[int] = []
        injected_growth = False

        def growing_read(descriptor: int, count: int) -> bytes:
            nonlocal injected_growth
            requested_counts.append(count)
            payload = original_read(descriptor, count)
            if not payload and not injected_growth:
                injected_growth = True
                payload = b"x" * count
            returned_counts.append(len(payload))
            return payload

        readme_size = (self.project / "README.md").stat().st_size
        with patch.object(priority_module.os, "read", side_effect=growing_read):
            with self.assertRaisesRegex(ReadingPriorityError, "changed after inventory"):
                generate_reading_priority(
                    self.workspace,
                    self.registration.project_id,
                )

        self.assertTrue(injected_growth)
        self.assertTrue(requested_counts)
        self.assertLessEqual(max(requested_counts), readme_size + 1)
        self.assertLessEqual(sum(returned_counts), readme_size + 1)
        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_changed_reference_source_fails_closed_and_preserves_prior_artifact(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        readme = self.project / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("Priority", "Changed!"),
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(ReadingPriorityError, "changed after inventory"):
            generate_reading_priority(self.workspace, self.registration.project_id)

        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_reference_mutation_after_scan_before_commit_fails_closed(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        readme = self.project / "README.md"
        original_write = priority_module._write_atomic_json

        def mutate_then_write(*args: object, **kwargs: object) -> object:
            readme.write_text(
                readme.read_text(encoding="utf-8").replace("Priority", "Mutated!"),
                encoding="utf-8",
                newline="\n",
            )
            return original_write(*args, **kwargs)

        with patch.object(
            priority_module,
            "_write_atomic_json",
            side_effect=mutate_then_write,
        ):
            with self.assertRaisesRegex(ReadingPriorityError, "changed after inventory"):
                generate_reading_priority(
                    self.workspace,
                    self.registration.project_id,
                )
        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_changed_ignore_policy_fails_closed_and_preserves_prior_artifact(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        (self.project / ".llmwikiignore").write_text(
            "figures/\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(ReadingPriorityError, "run inventory first"):
            generate_reading_priority(self.workspace, self.registration.project_id)

        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_manifest_change_during_load_fails_closed_and_preserves_artifact(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        original = priority_module.load_project_manifest

        def load_then_change(*args: object, **kwargs: object) -> object:
            manifest = original(*args, **kwargs)
            with self.inventory.manifest_file.open("ab") as target:
                target.write(b" ")
            return manifest

        with patch.object(
            priority_module,
            "load_project_manifest",
            side_effect=load_then_change,
        ):
            with self.assertRaisesRegex(ReadingPriorityError, "while it was loaded"):
                generate_reading_priority(
                    self.workspace,
                    self.registration.project_id,
                )
        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_policy_is_revalidated_before_every_reference_read_and_replace(self) -> None:
        original = priority_module._scan_policy_for_manifest
        calls = 0

        def counted(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        with patch.object(
            priority_module,
            "_scan_policy_for_manifest",
            side_effect=counted,
        ):
            result = generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )

        self.assertEqual(
            calls,
            2 * result.priority["summary"]["reference_source_count"] + 3,
        )

    def test_policy_change_during_generation_fails_closed_and_preserves_artifact(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        original = priority_module._scan_references

        def scan_then_change(*args: object, **kwargs: object) -> object:
            result = original(*args, **kwargs)
            (self.project / ".llmwikiignore").write_text(
                "figures/\n",
                encoding="utf-8",
                newline="\n",
            )
            return result

        with patch.object(
            priority_module,
            "_scan_references",
            side_effect=scan_then_change,
        ):
            with self.assertRaisesRegex(ReadingPriorityError, "run inventory first"):
                generate_reading_priority(
                    self.workspace,
                    self.registration.project_id,
                )
        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_policy_change_at_atomic_commit_fails_closed_and_preserves_artifact(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        previous = valid.priority_file.read_bytes()
        original = priority_module._write_atomic_json

        def change_then_write(*args: object, **kwargs: object) -> object:
            (self.project / ".llmwikiignore").write_text(
                "figures/\n",
                encoding="utf-8",
                newline="\n",
            )
            return original(*args, **kwargs)

        with patch.object(
            priority_module,
            "_write_atomic_json",
            side_effect=change_then_write,
        ):
            with self.assertRaisesRegex(ReadingPriorityError, "run inventory first"):
                generate_reading_priority(
                    self.workspace,
                    self.registration.project_id,
                )
        self.assertEqual(valid.priority_file.read_bytes(), previous)

    def test_existing_future_legacy_and_malformed_artifacts_are_preserved(self) -> None:
        valid = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        path = valid.priority_file
        future = {
            "schema_version": READING_PRIORITY_SCHEMA_VERSION + 1,
            "kind": READING_PRIORITY_KIND,
            "priority_version": READING_PRIORITY_VERSION,
            "project_id": self.registration.project_id,
        }
        future_bytes = (json.dumps(future) + "\n").encode("utf-8")
        path.write_bytes(future_bytes)
        with self.assertRaises(UnsupportedSchemaVersionError):
            generate_reading_priority(self.workspace, self.registration.project_id)
        self.assertEqual(path.read_bytes(), future_bytes)

        malformed_cases = (
            b'{"schema_version":1,"schema_version":1}\n',
            b'{"schema_version":1,"value":NaN}\n',
            b'{"schema_version":1,"value":"\xff"}\n',
            b'{"kind":"legacy-priority"}\n',
        )
        for payload in malformed_cases:
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                with self.assertRaises(LayoutError):
                    generate_reading_priority(
                        self.workspace,
                        self.registration.project_id,
                    )
                self.assertEqual(path.read_bytes(), payload)

    def test_summary_and_ranks_reconcile_with_every_manifest_file(self) -> None:
        result = generate_reading_priority(
            self.workspace,
            self.registration.project_id,
        )
        priority = result.priority
        summary = priority["summary"]
        files = priority["files"]
        manifest_file_count = self.inventory.record_counts["file"]
        self.assertEqual(len(files), manifest_file_count)
        self.assertEqual(summary["manifest_file_count"], manifest_file_count)
        self.assertEqual(summary["ranked_file_count"], manifest_file_count)
        self.assertEqual(
            [record["priority_rank"] for record in files],
            list(range(1, manifest_file_count + 1)),
        )
        self.assertEqual(
            sum(summary["priority_tiers"].values()),
            manifest_file_count,
        )
        self.assertEqual(
            sum(summary["deep_read_statuses"].values()),
            manifest_file_count,
        )
        self.assertEqual(
            sum(summary["reference_scan_statuses"].values()),
            manifest_file_count,
        )
    def test_service_and_cli_generate_the_same_machine_artifact(self) -> None:
        service = ResearchCoreService(self.workspace)
        service_result = service.prioritize(self.registration.project_id)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REPO_ROOT / "tools" / "project.py"),
                "prioritize",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["project_id"], self.registration.project_id)
        self.assertEqual(payload["priority"]["kind"], READING_PRIORITY_KIND)
        self.assertEqual(payload["priority"], service_result.priority)
        self.assertEqual(
            Path(payload["priority_file"]),
            self.registration.layout.reading_priority_file,
        )

    def test_symlinked_machine_state_ancestor_cannot_redirect_priority_writes(self) -> None:
        layout = self.registration.layout
        layout.machine_state_lock_file.unlink(missing_ok=True)
        layout.indexes_dir.rmdir()
        external = self.root / "external-indexes"
        external.mkdir()
        try:
            os.symlink(external, layout.indexes_dir, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

        with self.assertRaisesRegex(
            ReadingPriorityError,
            "symbolic link|reparse point",
        ):
            generate_reading_priority(
                self.workspace,
                self.registration.project_id,
            )
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((external / "machine-state.lock").exists())
        self.assertFalse((external / "reading-priority.json").exists())

    def test_shared_machine_state_lock_timeout_fails_closed(self) -> None:
        acquired = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def hold_lock() -> None:
            try:
                with AdvisoryFileLock(
                    self.registration.layout.machine_state_lock_file,
                    timeout_seconds=1.0,
                ):
                    acquired.set()
                    release.wait(timeout=2.0)
            except BaseException as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)

        holder = threading.Thread(target=hold_lock, name="priority-lock-holder")
        holder.start()
        self.assertTrue(acquired.wait(timeout=1.0))
        try:
            with self.assertRaisesRegex(ReadingPriorityError, "timed out"):
                generate_reading_priority(
                    self.workspace,
                    self.registration.project_id,
                    lock_timeout_seconds=0.05,
                )
        finally:
            release.set()
            holder.join(timeout=2.0)
        self.assertFalse(holder.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.registration.layout.reading_priority_file.exists())

    def test_shared_lock_is_held_during_reference_read_and_atomic_write(self) -> None:
        for target_name in ("_read_verified_reference_source", "_write_atomic_json"):
            with self.subTest(target_name=target_name):
                original = getattr(priority_module, target_name)
                observed: list[BaseException] = []

                def checked(*args: object, **kwargs: object) -> object:
                    started = threading.Event()

                    def contend() -> None:
                        started.set()
                        try:
                            with AdvisoryFileLock(
                                self.registration.layout.machine_state_lock_file,
                                timeout_seconds=0.02,
                            ):
                                observed.append(
                                    AssertionError("shared lock was not held")
                                )
                        except BaseException as exc:
                            observed.append(exc)

                    contender = threading.Thread(
                        target=contend,
                        name="priority-phase-lock-contender",
                    )
                    contender.start()
                    self.assertTrue(started.wait(timeout=1.0))
                    contender.join(timeout=1.0)
                    self.assertFalse(contender.is_alive())
                    return original(*args, **kwargs)

                with patch.object(priority_module, target_name, side_effect=checked):
                    generate_reading_priority(
                        self.workspace,
                        self.registration.project_id,
                    )
                self.assertTrue(observed)
                self.assertTrue(
                    all(isinstance(exc, AdvisoryLockTimeoutError) for exc in observed),
                    observed,
                )

    def test_large_dataset_collection_is_not_bulk_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            (project / "data").mkdir(parents=True)
            (project / "README.md").write_text(
                "Dataset entry: [first](data/row-00.csv)\n",
                encoding="utf-8",
                newline="\n",
            )
            for index in range(32):
                (project / "data" / f"row-{index:02}.csv").write_text(
                    f"x,y\n{index},{index + 1}\n",
                    encoding="utf-8",
                    newline="\n",
                )
            registration = register_project(
                workspace,
                project,
                project_id="large-dataset",
            )
            inventory_project(workspace, registration.project_id)

            result = generate_reading_priority(workspace, registration.project_id)
            records = self.by_path(result.priority)
            dataset_records = [
                record
                for path, record in records.items()
                if path.startswith("data/")
            ]
            self.assertEqual(len(dataset_records), 32)
            self.assertTrue(
                all(record["deep_read_status"] == "limited" for record in dataset_records)
            )
            self.assertTrue(
                all(
                    "large-dataset-limited" in record["reason_codes"]
                    for record in dataset_records
                )
            )
            self.assertNotIn(
                "data/row-00.csv",
                [entry["path"] for entry in result.priority["promotion_queue"]],
            )

    def test_over_budget_reference_promotion_is_explicitly_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            (project / "figures").mkdir(parents=True)
            (project / "README.md").write_text(
                "![wide](figures/wide.png)\n",
                encoding="utf-8",
                newline="\n",
            )
            (project / "figures" / "wide.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"x" * 192
            )
            registration = register_project(
                workspace,
                project,
                project_id="deferred-promotion",
            )
            inventory_project(workspace, registration.project_id)
            with patch.object(priority_module, "DEEP_READ_MAX_TOTAL_BYTES", 128):
                result = generate_reading_priority(workspace, registration.project_id)
                record = self.by_path(result.priority)["figures/wide.png"]
                self.assertEqual(record["priority_tier"], "promoted")
                self.assertEqual(record["deep_read_status"], "deferred")
                self.assertEqual(record["recommended_read_depth"], "deep_read")
                self.assertIn("deep-read-budget-deferred", record["reason_codes"])
                self.assertEqual(len(result.priority["promotion_queue"]), 1)
                queue_entry = result.priority["promotion_queue"][0]
                self.assertEqual(queue_entry["path"], "figures/wide.png")
                self.assertEqual(queue_entry["deep_read_status"], "deferred")

    def test_default_reference_source_count_boundary_is_exact(self) -> None:
        self.assertEqual(priority_module.REFERENCE_SOURCE_MAX_FILES, 128)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            project.mkdir()
            for index in range(priority_module.REFERENCE_SOURCE_MAX_FILES + 1):
                (project / f"doc-{index:03}.md").write_bytes(b"")
            registration = register_project(
                workspace,
                project,
                project_id="reference-count-boundary",
            )
            inventory_project(workspace, registration.project_id)

            result = generate_reading_priority(workspace, registration.project_id)
            records = self.by_path(result.priority)
            self.assertEqual(
                result.priority["summary"]["reference_source_count"],
                priority_module.REFERENCE_SOURCE_MAX_FILES,
            )
            self.assertEqual(
                records["doc-127.md"]["reference_scan_status"],
                "read",
            )
            self.assertEqual(
                records["doc-128.md"]["reference_scan_status"],
                "deferred-file-count",
            )

    def test_default_reference_file_and_total_byte_boundaries_are_exact(self) -> None:
        self.assertEqual(priority_module.REFERENCE_SOURCE_MAX_FILE_BYTES, 256 * 1024)
        self.assertEqual(priority_module.REFERENCE_SOURCE_MAX_TOTAL_BYTES, 4 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            project.mkdir()
            for index in range(16):
                (project / f"a-{index:02}.md").write_bytes(
                    b"\n" * priority_module.REFERENCE_SOURCE_MAX_FILE_BYTES
                )
            (project / "a-16.md").write_bytes(b"x")
            (project / "z-oversized.md").write_bytes(
                b"\n" * (priority_module.REFERENCE_SOURCE_MAX_FILE_BYTES + 1)
            )
            registration = register_project(
                workspace,
                project,
                project_id="reference-byte-boundary",
            )
            inventory_project(workspace, registration.project_id)

            result = generate_reading_priority(workspace, registration.project_id)
            records = self.by_path(result.priority)
            self.assertEqual(
                result.priority["summary"]["reference_bytes_read"],
                priority_module.REFERENCE_SOURCE_MAX_TOTAL_BYTES,
            )
            self.assertEqual(records["a-15.md"]["reference_scan_status"], "read")
            self.assertEqual(
                records["a-16.md"]["reference_scan_status"],
                "deferred-total-bytes",
            )
            self.assertEqual(
                records["z-oversized.md"]["reference_scan_status"],
                "deferred-file-size",
            )

    def test_default_deep_read_count_boundary_is_exact(self) -> None:
        self.assertEqual(priority_module.DEEP_READ_MAX_FILES, 128)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            project.mkdir()
            for index in range(priority_module.DEEP_READ_MAX_FILES + 1):
                (project / f"image-{index:03}.png").write_bytes(
                    b"\x89PNG\r\n\x1a\n"
                )
            registration = register_project(
                workspace,
                project,
                project_id="deep-count-boundary",
            )
            inventory_project(workspace, registration.project_id)

            result = generate_reading_priority(workspace, registration.project_id)
            records = self.by_path(result.priority)
            self.assertEqual(
                result.priority["summary"]["deep_read_selected_count"],
                priority_module.DEEP_READ_MAX_FILES,
            )
            self.assertEqual(records["image-127.png"]["deep_read_status"], "selected")
            self.assertEqual(records["image-128.png"]["deep_read_status"], "deferred")

    def test_default_deep_read_byte_boundary_is_exact(self) -> None:
        self.assertEqual(priority_module.DEEP_READ_MAX_TOTAL_BYTES, 32 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            project.mkdir()
            half_budget = priority_module.DEEP_READ_MAX_TOTAL_BYTES // 2
            for name in ("a.png", "b.png"):
                self.write_sized_file(
                    project / name,
                    half_budget,
                    prefix=b"\x89PNG\r\n\x1a\n",
                )
            (project / "c.png").write_bytes(b"\x89PNG\r\n\x1a\nx")
            registration = register_project(
                workspace,
                project,
                project_id="deep-byte-boundary",
            )
            inventory_project(
                workspace,
                registration.project_id,
                policy_config=ScanPolicyConfig(
                    max_content_file_bytes=priority_module.DEEP_READ_MAX_TOTAL_BYTES,
                ),
            )

            result = generate_reading_priority(workspace, registration.project_id)
            records = self.by_path(result.priority)
            self.assertEqual(
                result.priority["summary"]["deep_read_selected_bytes"],
                priority_module.DEEP_READ_MAX_TOTAL_BYTES,
            )
            self.assertEqual(records["a.png"]["deep_read_status"], "selected")
            self.assertEqual(records["b.png"]["deep_read_status"], "selected")
            self.assertEqual(records["c.png"]["deep_read_status"], "deferred")

    def test_default_large_dataset_byte_boundary_is_exact(self) -> None:
        self.assertEqual(priority_module.LARGE_DATASET_MIN_BYTES, 64 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "project"
            (project / "data").mkdir(parents=True)
            self.write_sized_file(
                project / "data" / "at-threshold.csv",
                priority_module.LARGE_DATASET_MIN_BYTES,
                prefix=b"x,y\n",
            )
            registration = register_project(
                workspace,
                project,
                project_id="dataset-byte-boundary",
            )
            inventory_project(
                workspace,
                registration.project_id,
                policy_config=ScanPolicyConfig(
                    max_content_file_bytes=priority_module.LARGE_DATASET_MIN_BYTES,
                ),
            )

            result = generate_reading_priority(workspace, registration.project_id)
            record = self.by_path(result.priority)["data/at-threshold.csv"]
            self.assertEqual(record["deep_read_status"], "limited")
            self.assertIn("large-dataset-limited", record["reason_codes"])

    def test_empty_project_produces_a_valid_reconciled_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            project = root / "empty-project"
            project.mkdir()
            registration = register_project(
                workspace,
                project,
                project_id="empty-project",
            )
            inventory_project(workspace, registration.project_id)
            result = generate_reading_priority(workspace, registration.project_id)
            self.assertEqual(result.priority["files"], [])
            self.assertEqual(result.priority["promotion_queue"], [])
            self.assertEqual(result.priority["summary"]["ranked_file_count"], 0)
            self.assertEqual(
                load_reading_priority(
                    result.priority_file,
                    project_id=registration.project_id,
                ),
                result.priority,
            )


if __name__ == "__main__":
    unittest.main()
