from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import project_map as project_map_module
from tools.project_inventory import inventory_project
from tools.project_map import (
    PROJECT_MAP_KIND,
    PROJECT_MAP_MAX_CATEGORY_CANDIDATES,
    PROJECT_MAP_MAX_KEY_CANDIDATES,
    PROJECT_MAP_SCHEMA_VERSION,
    PROJECT_MAP_VERSION,
    ProjectMapError,
    build_project_map,
    generate_project_map,
    load_current_project_map,
    load_project_map,
)
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService


class ProjectMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.source = root / "research-project"
        self.source.mkdir()
        self.files = {
            "README.md": "# Demo\nRun `python train.py --config configs/base.yaml`.\n",
            "train.py": "from src.model import Model\nprint(Model)\n",
            "evaluate.py": "print('evaluate')\n",
            "src/__init__.py": "",
            "src/model.py": "class Model: pass\n",
            "configs/base.yaml": "seed: 7\n",
            "requirements.txt": "numpy==2.0\n",
            "scripts/run.sh": "python train.py\n",
            "papers/method.pdf": "%PDF fixture metadata only",
            "results/metrics.csv": "metric,value\naccuracy,0.9\n",
            "data/weights.ckpt": "not a real checkpoint",
        }
        for relative, text in self.files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.registration = register_project(self.workspace, self.source)
        self.project_id = self.registration.project_id
        self.inventory = inventory_project(self.workspace, self.project_id)
        self.source_snapshot = {
            path.relative_to(self.source).as_posix(): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }

    def assert_source_unchanged(self) -> None:
        actual = {
            path.relative_to(self.source).as_posix(): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, self.source_snapshot)

    def test_generates_deterministic_manifest_only_project_outline(self) -> None:
        first = generate_project_map(self.workspace, self.project_id)
        first_bytes = first.project_map_file.read_bytes()
        second = generate_project_map(self.workspace, self.project_id)

        self.assertEqual(first.project_map, second.project_map)
        self.assertEqual(first_bytes, second.project_map_file.read_bytes())
        project_map = first.project_map
        self.assertEqual(project_map["schema_version"], PROJECT_MAP_SCHEMA_VERSION)
        self.assertEqual(project_map["kind"], PROJECT_MAP_KIND)
        self.assertEqual(project_map["map_version"], PROJECT_MAP_VERSION)
        self.assertEqual(project_map["project_id"], self.project_id)
        self.assertEqual(
            project_map["derivation"],
            {
                "mode": "manifest-metadata-only",
                "source_content_read": False,
                "llm_used": False,
            },
        )
        self.assertEqual(
            project_map["manifest"]["sha256"],
            hashlib.sha256(self.inventory.manifest_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            project_map["manifest"]["ordinary_file_count"], len(self.files)
        )
        self.assertEqual(
            project_map["manifest"]["ordinary_byte_count"],
            sum(len(value.encode("utf-8")) for value in self.files.values()),
        )
        self.assertEqual(project_map["directories"][0]["path"], ".")
        self.assertEqual(
            project_map["directories"][0]["recursive_file_count"], len(self.files)
        )
        self.assertEqual(
            [item["path"] for item in project_map["candidates"]["entrypoints"]],
            ["evaluate.py", "train.py", "scripts/run.sh"],
        )
        self.assertEqual(
            [item["path"] for item in project_map["candidates"]["dependencies"]],
            ["requirements.txt"],
        )
        self.assertEqual(
            [item["path"] for item in project_map["candidates"]["configurations"]],
            ["configs/base.yaml"],
        )
        self.assertEqual(
            project_map["candidates"]["run_scripts"][0]["path"], "scripts/run.sh"
        )
        self.assertEqual(
            [item["rank"] for item in project_map["candidates"]["key_files"]],
            list(range(1, len(project_map["candidates"]["key_files"]) + 1)),
        )
        self.assertEqual(load_current_project_map(self.workspace, self.project_id), project_map)
        self.assert_source_unchanged()

    def test_stable_ordering_is_independent_of_manifest_row_order(self) -> None:
        manifest = project_map_module.load_project_manifest(
            self.inventory.manifest_file,
            project_id=self.project_id,
            project_root=self.source,
            required_manifest_version="project-inventory-v4",
        )
        digest = hashlib.sha256(self.inventory.manifest_file.read_bytes()).hexdigest()
        reversed_manifest = project_map_module.ProjectManifest(
            manifest_file=manifest.manifest_file,
            project_id=manifest.project_id,
            project_root=manifest.project_root,
            manifest_version=manifest.manifest_version,
            scan_generation=manifest.scan_generation,
            summary=manifest.summary,
            records=tuple(reversed(manifest.records)),
        )
        self.assertEqual(
            build_project_map(manifest, manifest_sha256=digest),
            build_project_map(reversed_manifest, manifest_sha256=digest),
        )

    def test_generation_never_opens_registered_source_files(self) -> None:
        original_read_bytes = Path.read_bytes
        source_root = self.source.resolve()

        def guarded_read_bytes(path: Path) -> bytes:
            resolved = path.resolve()
            try:
                resolved.relative_to(source_root)
            except ValueError:
                return original_read_bytes(path)
            raise AssertionError(f"source content must not be opened: {resolved}")

        with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
            result = generate_project_map(self.workspace, self.project_id)
        self.assertTrue(result.project_map_file.is_file())

    def test_policy_limited_files_remain_metadata_only_candidates(self) -> None:
        project_map = generate_project_map(self.workspace, self.project_id).project_map
        key_by_path = {
            item["path"]: item for item in project_map["candidates"]["key_files"]
        }
        self.assertIn("data/weights.ckpt", key_by_path)
        manifest = project_map_module.load_project_manifest(
            self.inventory.manifest_file,
            project_id=self.project_id,
            project_root=self.source,
            required_manifest_version="project-inventory-v4",
        )
        weights_record = next(
            record
            for record in manifest.records
            if record["record_type"] == "file" and record["path"] == "data/weights.ckpt"
        )
        self.assertEqual(
            key_by_path["data/weights.ckpt"]["research_role"],
            weights_record["classification"]["research_role"],
        )
        self.assertFalse(project_map["derivation"]["source_content_read"])

    def test_generation_writes_only_machine_index_and_no_curated_markdown(self) -> None:
        before_knowledge = sorted(
            path.relative_to(self.registration.layout.knowledge_root).as_posix()
            for path in self.registration.layout.knowledge_root.rglob("*")
        )
        result = generate_project_map(self.workspace, self.project_id)
        after_knowledge = sorted(
            path.relative_to(self.registration.layout.knowledge_root).as_posix()
            for path in self.registration.layout.knowledge_root.rglob("*")
        )
        self.assertEqual(before_knowledge, after_knowledge)
        self.assertEqual(
            result.project_map_file,
            self.registration.layout.indexes_dir / "project-map.json",
        )
        self.assert_source_unchanged()

    @staticmethod
    def canonical_bytes(payload: dict[str, object]) -> bytes:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    def test_current_loader_rejects_stale_map_and_generation_replaces_it(self) -> None:
        first = generate_project_map(self.workspace, self.project_id)
        first_generation = first.project_map["manifest"]["scan_generation"]
        (self.source / "new-module.py").write_bytes(b"VALUE = 1\n")
        self.inventory = inventory_project(self.workspace, self.project_id)
        self.source_snapshot["new-module.py"] = b"VALUE = 1\n"

        with self.assertRaisesRegex(ProjectMapError, "stale"):
            load_current_project_map(self.workspace, self.project_id)

        replacement = generate_project_map(self.workspace, self.project_id)
        self.assertGreater(
            replacement.project_map["manifest"]["scan_generation"], first_generation
        )
        self.assertEqual(
            load_current_project_map(self.workspace, self.project_id),
            replacement.project_map,
        )
        self.assert_source_unchanged()

    def test_current_loader_and_generator_reject_validly_shaped_tampering(self) -> None:
        result = generate_project_map(self.workspace, self.project_id)
        tampered = json.loads(json.dumps(result.project_map))
        tampered["candidates"]["key_files"][0]["score"] += 1
        result.project_map_file.write_bytes(self.canonical_bytes(tampered))
        self.assertEqual(
            load_project_map(result.project_map_file, project_id=self.project_id),
            tampered,
        )

        with self.assertRaisesRegex(ProjectMapError, "deterministic current truth"):
            load_current_project_map(self.workspace, self.project_id)
        with self.assertRaisesRegex(ProjectMapError, "tampered"):
            generate_project_map(self.workspace, self.project_id)

    def test_loader_fails_closed_for_legacy_future_unknown_duplicate_and_noncanonical(self) -> None:
        result = generate_project_map(self.workspace, self.project_id)
        base = result.project_map
        cases: list[tuple[str, bytes]] = []

        legacy = json.loads(json.dumps(base))
        legacy.pop("schema_version")
        cases.append(("legacy", self.canonical_bytes(legacy)))

        future = json.loads(json.dumps(base))
        future["schema_version"] = PROJECT_MAP_SCHEMA_VERSION + 1
        cases.append(("future", self.canonical_bytes(future)))

        unknown = json.loads(json.dumps(base))
        unknown["unexpected"] = True
        cases.append(("unknown", self.canonical_bytes(unknown)))

        canonical = self.canonical_bytes(base)
        duplicate = canonical.replace(
            b'{\n  "candidates"',
            b'{\n  "schema_version": 1,\n  "candidates"',
            1,
        )
        cases.append(("duplicate", duplicate))
        cases.append(
            (
                "noncanonical",
                json.dumps(base, ensure_ascii=False, sort_keys=False).encode("utf-8"),
            )
        )

        for label, raw in cases:
            with self.subTest(label=label):
                path = result.project_map_file.with_name(f"{label}.json")
                path.write_bytes(raw)
                with self.assertRaises(ProjectMapError):
                    load_project_map(path, project_id=self.project_id)

    def test_manifest_change_before_atomic_replace_never_commits_map(self) -> None:
        original_write = project_map_module._write_atomic_json
        manifest_file = self.inventory.manifest_file
        original_manifest = manifest_file.read_bytes()

        def racing_write(
            path: Path,
            payload: dict[str, object],
            *,
            layout: object,
            before_replace: object,
        ) -> None:
            manifest_file.write_bytes(original_manifest + b"\n")
            original_write(
                path,
                payload,
                layout=layout,
                before_replace=before_replace,
            )

        with mock.patch.object(
            project_map_module, "_write_atomic_json", side_effect=racing_write
        ):
            with self.assertRaisesRegex(ProjectMapError, "Manifest changed"):
                generate_project_map(self.workspace, self.project_id)
        self.assertFalse(self.registration.layout.project_map_file.exists())

    def test_loader_rejects_symlinked_project_map(self) -> None:
        result = generate_project_map(self.workspace, self.project_id)
        outside = result.project_map_file.with_name("outside-map.json")
        outside.write_bytes(result.project_map_file.read_bytes())
        result.project_map_file.unlink()
        try:
            result.project_map_file.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ProjectMapError, "symbolic link"):
            load_project_map(result.project_map_file, project_id=self.project_id)
        with self.assertRaises(ProjectMapError):
            load_current_project_map(self.workspace, self.project_id)

    def test_candidates_are_bounded_and_omissions_are_accounted(self) -> None:
        for index in range(PROJECT_MAP_MAX_CATEGORY_CANDIDATES + 7):
            path = self.source / "configs" / f"sweep-{index:03}.yaml"
            path.write_bytes(f"seed: {index}\n".encode("utf-8"))
            self.source_snapshot[path.relative_to(self.source).as_posix()] = path.read_bytes()
        for index in range(PROJECT_MAP_MAX_KEY_CANDIDATES + 7):
            path = self.source / "papers" / f"paper-{index:03}.md"
            path.write_bytes(f"# Paper {index}\n".encode("utf-8"))
            self.source_snapshot[path.relative_to(self.source).as_posix()] = path.read_bytes()
        self.inventory = inventory_project(self.workspace, self.project_id)

        payload = generate_project_map(self.workspace, self.project_id).project_map
        self.assertEqual(
            len(payload["candidates"]["configurations"]),
            PROJECT_MAP_MAX_CATEGORY_CANDIDATES,
        )
        self.assertGreater(payload["omissions"]["configurations"], 0)
        self.assertEqual(
            len(payload["candidates"]["key_files"]), PROJECT_MAP_MAX_KEY_CANDIDATES
        )
        self.assertGreater(payload["omissions"]["key_files"], 0)
        self.assert_source_unchanged()

    def test_build_rejects_disagreeing_manifest_hash_arguments(self) -> None:
        manifest = project_map_module.load_project_manifest(
            self.inventory.manifest_file,
            project_id=self.project_id,
            project_root=self.source,
            required_manifest_version="project-inventory-v4",
        )
        snapshot = self.inventory.manifest_file.read_bytes()
        with self.assertRaisesRegex(ProjectMapError, "hash arguments disagree"):
            build_project_map(
                manifest,
                manifest_bytes=snapshot,
                manifest_sha256="0" * 64,
            )

    def test_layout_property_and_research_core_facade(self) -> None:
        self.assertEqual(
            self.registration.layout.project_map_file,
            self.registration.layout.indexes_dir / "project-map.json",
        )
        result = ResearchCoreService(self.workspace).project_map(self.project_id)
        self.assertEqual(result.project_map_file, self.registration.layout.project_map_file)
        self.assertEqual(
            result.project_map,
            load_current_project_map(self.workspace, self.project_id),
        )

    def test_generation_preserves_registration_and_source_bytes(self) -> None:
        registration_bytes = self.registration.layout.project_file.read_bytes()
        generate_project_map(self.workspace, self.project_id)
        self.assertEqual(
            self.registration.layout.project_file.read_bytes(), registration_bytes
        )
        self.assert_source_unchanged()

