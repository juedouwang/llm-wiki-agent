from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from tools.hierarchical_understanding import (
    HIERARCHICAL_UNDERSTANDING_KIND,
    HIERARCHICAL_UNDERSTANDING_VERSION,
    ChunkObservation,
    HierarchicalUnderstandingError,
    build_hierarchical_understanding,
    generate_hierarchical_understanding,
    load_current_hierarchical_understanding,
    load_hierarchical_understanding,
)
from tools.project_inventory import inventory_project, load_project_manifest
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService


class HierarchicalUnderstandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.source = root / "research"
        self.workspace.mkdir()
        self.source.mkdir()
        (self.source / "src").mkdir()
        (self.source / "configs").mkdir()
        (self.source / "src" / "model.py").write_text("class Model:\n    pass\n", encoding="utf-8")
        (self.source / "src" / "train.py").write_text("from model import Model\n", encoding="utf-8")
        (self.source / "configs" / "base.yaml").write_text("seed: 7\n", encoding="utf-8")
        self.registration = register_project(self.workspace, self.source)
        self.project_id = self.registration.project_id
        self.inventory = inventory_project(self.workspace, self.project_id)
        self.manifest = load_project_manifest(
            self.inventory.manifest_file,
            project_id=self.project_id,
            project_root=self.source,
            required_manifest_version="project-inventory-v4",
        )
        self.manifest_bytes = self.inventory.manifest_file.read_bytes()
        self.evidence_a = "evd-" + "a" * 64
        self.evidence_b = "evd-" + "b" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def observation(self, path: str, chunk_id: str, module: str, summary: str, evidence: tuple[str, ...] = ()) -> ChunkObservation:
        return ChunkObservation(path, chunk_id, module, summary, 100, 25, evidence or (self.evidence_a,))

    def test_metadata_fallback_is_explicit_and_current(self) -> None:
        result = generate_hierarchical_understanding(self.workspace, self.project_id)
        payload = result.understanding
        self.assertEqual(payload["kind"], HIERARCHICAL_UNDERSTANDING_KIND)
        self.assertEqual(payload["understanding_version"], HIERARCHICAL_UNDERSTANDING_VERSION)
        self.assertEqual(payload["derivation"]["mode"], "manifest-metadata-fallback")
        self.assertEqual(payload["coverage"]["semantic_files"], 0)
        self.assertEqual(payload["coverage"]["metadata_only_files"], 3)
        self.assertTrue(all(node["metadata_only"] for node in payload["files"]))
        self.assertEqual(load_current_hierarchical_understanding(self.workspace, self.project_id), payload)
        self.assertEqual(result.understanding_file, self.registration.layout.hierarchical_understanding_file)

    def test_explicit_observations_close_through_all_levels(self) -> None:
        observations = [
            self.observation("src/model.py", "model#chunk-1", "src", "Model defines the model boundary.", (self.evidence_a,)),
            self.observation("src/model.py", "model#chunk-2", "src", "The class has no learned parameters.", (self.evidence_b,)),
            self.observation("src/train.py", "train#chunk-1", "src", "Training imports the model module.", (self.evidence_a, self.evidence_b)),
        ]
        first = generate_hierarchical_understanding(self.workspace, self.project_id, observations=observations)
        second = generate_hierarchical_understanding(self.workspace, self.project_id, observations=observations)
        self.assertEqual(first.understanding, second.understanding)
        self.assertEqual(first.understanding_file.read_bytes(), second.understanding_file.read_bytes())
        payload = first.understanding
        self.assertEqual(payload["derivation"]["mode"], "explicit-chunk-observations")
        self.assertEqual(payload["coverage"]["semantic_files"], 2)
        self.assertEqual(payload["coverage"]["metadata_only_files"], 1)
        self.assertEqual(payload["coverage"]["selected_chunks"], 3)
        self.assertEqual(len(payload["modules"]), 2)  # src plus metadata fallback for configs
        source_file = next(item for item in payload["files"] if item["path"] == "src/model.py")
        self.assertEqual(source_file["evidence_ids"], [self.evidence_a, self.evidence_b])
        src_module = next(item for item in payload["modules"] if item["module_id"] == "src")
        self.assertEqual(src_module["file_ids"], sorted(item["id"] for item in payload["files"] if item["path"] in {"src/model.py", "src/train.py"}))
        self.assertEqual(payload["project"]["chunk_ids"], sorted(item["id"] for item in payload["chunks"]))

    def test_input_validation_is_fail_closed(self) -> None:
        with self.assertRaises(HierarchicalUnderstandingError):
            build_hierarchical_understanding(self.manifest, manifest_bytes=self.manifest_bytes, observations=[self.observation("missing.py", "x#chunk-1", "x", "bad")])
        with self.assertRaises(HierarchicalUnderstandingError):
            self.observation("src/model.py", "x#chunk-1", "src", "bad", ("evd-" + "z" * 63,))
        duplicate = self.observation("src/model.py", "same#chunk-1", "src", "one")
        with self.assertRaises(HierarchicalUnderstandingError):
            build_hierarchical_understanding(self.manifest, manifest_bytes=self.manifest_bytes, observations=[duplicate, duplicate])
        with self.assertRaises(HierarchicalUnderstandingError):
            build_hierarchical_understanding(self.manifest, manifest_bytes=self.manifest_bytes, observations=[] , manifest_sha256="0" * 64)

    def test_budget_and_omission_counters_are_deterministic(self) -> None:
        observations = [self.observation("src/model.py", f"chunk-{i}#chunk-1", "src", f"summary {i}") for i in range(5)]
        limits = {"max_chunks": 2, "max_files": 3, "max_modules": 2, "max_chunks_per_file": 2, "max_evidence_ids_per_node": 128, "max_summary_utf8_bytes": 4096, "max_total_summary_utf8_bytes": 4096}
        payload = build_hierarchical_understanding(self.manifest, manifest_bytes=self.manifest_bytes, observations=observations, limits=limits)
        self.assertLessEqual(len(payload["chunks"]), 2)
        self.assertGreater(payload["omissions"]["chunks"], 0)
        self.assertEqual(payload["limits"], limits)

    def test_loader_rejects_noncanonical_tamper_and_stale_manifest(self) -> None:
        result = generate_hierarchical_understanding(self.workspace, self.project_id)
        raw = result.understanding_file.read_bytes()
        self.assertEqual(load_hierarchical_understanding(result.understanding_file, project_id=self.project_id), result.understanding)
        result.understanding_file.write_bytes(raw.replace(b'"kind": "llmwiki-hierarchical-understanding"', b'"kind": "tampered"'))
        with self.assertRaises(HierarchicalUnderstandingError):
            load_hierarchical_understanding(result.understanding_file, project_id=self.project_id)
        result.understanding_file.write_bytes(raw)
        (self.source / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
        inventory_project(self.workspace, self.project_id)
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "stale"):
            load_current_hierarchical_understanding(self.workspace, self.project_id)

    def test_manifest_binding_and_source_are_protected(self) -> None:
        before = {path.relative_to(self.source).as_posix(): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        service = ResearchCoreService(self.workspace)
        result = service.hierarchical_understanding(self.project_id, observations=[self.observation("src/model.py", "x#chunk-1", "src", "model")])
        after = {path.relative_to(self.source).as_posix(): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result.project_id, self.project_id)
        self.assertEqual(result.understanding["manifest"]["sha256"], hashlib.sha256(self.manifest_bytes).hexdigest())


    @staticmethod
    def canonical(payload: dict[str, object]) -> bytes:
        return (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8")

    def test_strict_json_schema_and_unknown_fields_fail_closed(self) -> None:
        result = generate_hierarchical_understanding(self.workspace, self.project_id)
        original = result.understanding_file.read_bytes()
        duplicate = original.replace(b"{\n", b'{\n  "schema_version": 1,\n', 1)
        result.understanding_file.write_bytes(duplicate)
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "duplicate JSON key"):
            load_hierarchical_understanding(result.understanding_file)

        result.understanding_file.write_bytes(json.dumps(result.understanding).encode("utf-8"))
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "canonical"):
            load_hierarchical_understanding(result.understanding_file)

        future = deepcopy(result.understanding)
        future["schema_version"] = 2
        result.understanding_file.write_bytes(self.canonical(future))
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "legacy or future"):
            load_hierarchical_understanding(result.understanding_file)

        unknown = deepcopy(result.understanding)
        unknown["unexpected"] = True
        result.understanding_file.write_bytes(self.canonical(unknown))
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "exactly"):
            load_hierarchical_understanding(result.understanding_file)

    def test_stable_ids_and_aggregate_summaries_reject_tamper(self) -> None:
        observation = self.observation(
            "src/model.py", "model#chunk-1", "src", "Original grounded summary."
        )
        result = generate_hierarchical_understanding(
            self.workspace, self.project_id, observations=[observation]
        )
        chunk_tamper = deepcopy(result.understanding)
        chunk_tamper["chunks"][0]["summary"] = "Changed grounded summary."
        result.understanding_file.write_bytes(self.canonical(chunk_tamper))
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "chunk ID"):
            load_hierarchical_understanding(result.understanding_file)

        aggregate_tamper = deepcopy(result.understanding)
        aggregate_tamper["project"]["summary"] = "Validly shaped but invented."
        result.understanding_file.write_bytes(self.canonical(aggregate_tamper))
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "summary closure"):
            load_hierarchical_understanding(result.understanding_file)

    def test_current_loader_rejects_manifest_projection_tamper(self) -> None:
        result = generate_hierarchical_understanding(
            self.workspace,
            self.project_id,
            observations=[self.observation("src/model.py", "model#1", "src", "Model summary")],
        )
        tampered = deepcopy(result.understanding)
        semantic = next(node for node in tampered["files"] if node["path"] == "src/model.py")
        semantic["classification"]["language"] = "tampered-language"
        result.understanding_file.write_bytes(self.canonical(tampered))
        self.assertEqual(
            load_hierarchical_understanding(result.understanding_file)["files"],
            tampered["files"],
        )
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "current Manifest"):
            load_current_hierarchical_understanding(self.workspace, self.project_id)

    def test_custom_summary_and_evidence_limits_are_enforced_at_every_level(self) -> None:
        evidence = tuple("evd-" + character * 64 for character in ("a", "b", "c"))
        limits = {
            "max_chunks": 4,
            "max_files": 3,
            "max_modules": 3,
            "max_chunks_per_file": 4,
            "max_evidence_ids_per_node": 1,
            "max_summary_utf8_bytes": 16,
            "max_total_summary_utf8_bytes": 16,
        }
        payload = build_hierarchical_understanding(
            self.manifest,
            manifest_bytes=self.manifest_bytes,
            observations=[
                self.observation(
                    "src/model.py", "model#bounded", "src", "A summary longer than sixteen bytes", evidence
                )
            ],
            limits=limits,
        )
        for collection in ("chunks", "files", "modules"):
            for node in payload[collection]:
                self.assertLessEqual(len(node["summary"].encode("utf-8")), 16)
                self.assertLessEqual(len(node["evidence_ids"]), 1)
        self.assertLessEqual(len(payload["project"]["summary"].encode("utf-8")), 16)
        self.assertLessEqual(len(payload["project"]["evidence_ids"]), 1)
        self.assertGreater(payload["omissions"]["summary_bytes"], 0)
        self.assertGreater(payload["omissions"]["evidence_ids"], 0)

    def test_shared_file_across_modules_does_not_duplicate_project_input(self) -> None:
        payload = build_hierarchical_understanding(
            self.manifest,
            manifest_bytes=self.manifest_bytes,
            observations=[
                self.observation("src/model.py", "model#a", "module-a", "Module A view"),
                self.observation("src/model.py", "model#b", "module-b", "Module B view"),
            ],
        )
        self.assertEqual(payload["project"]["input"], {"utf8_bytes": 200, "token_estimate": 50})
        self.assertEqual(len(payload["modules"]), 4)  # two semantic modules plus src/config fallbacks

    def test_empty_manifest_has_bounded_metadata_only_project(self) -> None:
        empty_workspace = Path(self.temp.name) / "empty-workspace"
        empty_source = Path(self.temp.name) / "empty-source"
        empty_workspace.mkdir()
        empty_source.mkdir()
        registration = register_project(empty_workspace, empty_source)
        inventory = inventory_project(empty_workspace, registration.project_id)
        result = generate_hierarchical_understanding(empty_workspace, registration.project_id)
        self.assertEqual(result.understanding["coverage"]["manifest_files"], 0)
        self.assertEqual(result.understanding["files"], [])
        self.assertEqual(result.understanding["modules"], [])
        self.assertTrue(result.understanding["project"]["metadata_only"])
        self.assertEqual(inventory.manifest_file, result.manifest_file)

    def test_manifest_race_before_replace_fails_without_publication(self) -> None:
        target = self.registration.layout.hierarchical_understanding_file

        def race_write(path: Path, payload: object, *, layout: object, before_replace: object) -> None:
            self.inventory.manifest_file.write_bytes(self.manifest_bytes + b" ")
            before_replace()

        with patch("tools.hierarchical_understanding._write_atomic", side_effect=race_write):
            with self.assertRaisesRegex(HierarchicalUnderstandingError, "Manifest changed"):
                generate_hierarchical_understanding(self.workspace, self.project_id)
        self.assertFalse(target.exists())

    def test_symlinked_artifact_is_rejected(self) -> None:
        result = generate_hierarchical_understanding(self.workspace, self.project_id)
        backing = result.understanding_file.with_name("understanding-backing.json")
        backing.write_bytes(result.understanding_file.read_bytes())
        result.understanding_file.unlink()
        try:
            result.understanding_file.symlink_to(backing)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        with self.assertRaisesRegex(HierarchicalUnderstandingError, "symbolic link"):
            load_hierarchical_understanding(result.understanding_file)


if __name__ == "__main__":
    unittest.main()

