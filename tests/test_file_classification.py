from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.project_inventory as project_inventory_module
from tools.file_classification import (
    FILE_CLASSIFICATION_KIND,
    FILE_CLASSIFICATION_SCHEMA_VERSION,
    UNKNOWN_CLASSIFICATION,
    classification_from_dict,
    classify_file,
)
from tools.project_inventory import inventory_project
from tools.project_registry import register_project
from tools.scan_policy import ScanPolicyConfig


class FileClassificationTests(unittest.TestCase):
    def test_magic_shebang_extension_and_content_priority_are_deterministic(
        self,
    ) -> None:
        spoofed_pdf = classify_file(
            "scripts/spoofed.pdf",
            b"#!/usr/bin/env python\nprint('not a PDF')\n",
        )
        self.assertEqual(spoofed_pdf.format, "python")
        self.assertEqual(spoofed_pdf.language, "python")
        self.assertEqual(spoofed_pdf.research_role, "automation")
        self.assertEqual(
            spoofed_pdf.reasons["format"]["code"],
            "shebang-interpreter",
        )

        disguised_pdf = classify_file("src/model.py", b"%PDF-1.7\n")
        self.assertEqual(disguised_pdf.format, "pdf")
        self.assertEqual(
            disguised_pdf.reasons["format"]["code"],
            "magic-signature",
        )

        extensionless_notebook = classify_file(
            "notebooks/trial",
            json.dumps({"cells": [], "nbformat": 4}).encode("utf-8"),
        )
        self.assertEqual(extensionless_notebook.format, "notebook")
        self.assertEqual(extensionless_notebook.language, "jupyter_notebook")
        self.assertEqual(extensionless_notebook.research_role, "notebook")

    def test_common_research_paths_get_explainable_roles(self) -> None:
        cases = {
            "README": ("plain_text", "project_documentation"),
            "paper/main.tex": ("latex", "paper"),
            "paper/references.bib": ("bibtex", "bibliography"),
            "configs/train.yaml": ("yaml", "configuration"),
            "data/metrics.csv": ("csv", "dataset"),
            "results/run.log": ("log", "result"),
            "models/best.ckpt": ("model_checkpoint", "model_artifact"),
            "tests/test_model.py": ("python", "test_code"),
        }
        for relative_path, (expected_format, expected_role) in cases.items():
            with self.subTest(relative_path=relative_path):
                sample = b"key: value\n"
                if relative_path.endswith(".ckpt"):
                    sample = b"\x00model\x01"
                classification = classify_file(relative_path, sample)
                self.assertEqual(classification.format, expected_format)
                self.assertEqual(classification.research_role, expected_role)
                self.assertTrue(classification.reasons["format"]["detail"])
                self.assertTrue(
                    classification.reasons["research_role"]["detail"]
                )

    def test_unknown_binary_remains_explicit_and_round_trips(self) -> None:
        classification = classify_file("artifacts/blob.weird", b"\x00\x01\x02")
        self.assertEqual(classification.format, UNKNOWN_CLASSIFICATION)
        self.assertEqual(classification.language, UNKNOWN_CLASSIFICATION)
        self.assertEqual(classification.research_role, UNKNOWN_CLASSIFICATION)
        persisted = classification.as_dict()
        self.assertEqual(
            persisted["schema_version"], FILE_CLASSIFICATION_SCHEMA_VERSION
        )
        self.assertEqual(persisted["kind"], FILE_CLASSIFICATION_KIND)
        self.assertEqual(classification_from_dict(persisted), classification)

        invalid = dict(persisted)
        invalid["schema_version"] = FILE_CLASSIFICATION_SCHEMA_VERSION + 1
        with self.assertRaises(ValueError):
            classification_from_dict(invalid)


    def test_path_only_classification_requires_and_records_policy_reason(self) -> None:
        classification = classify_file(
            "src/model.py",
            None,
            content_access_reason_code="sensitive-path",
            content_access_reason="raw content is not read",
        )
        self.assertEqual(classification.format, "python")
        self.assertEqual(classification.language, "python")
        self.assertEqual(classification.research_role, "source_code")
        self.assertEqual(
            classification.reasons["format"]["source"],
            "policy-path",
        )
        self.assertIn(
            "sensitive-path",
            classification.reasons["format"]["detail"],
        )

        with self.assertRaises(ValueError):
            classify_file("src/model.py", None)


class FileClassificationInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.workspace = root / "workspace"
        self.project = root / "research-project"
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "notebooks").mkdir()
        (self.project / "data").mkdir()
        (self.project / "paper").mkdir()
        (self.project / "models").mkdir()
        (self.project / "README").write_text("Research project\n", encoding="utf-8")
        (self.project / "scripts" / "spoofed.pdf").write_text(
            "#!/usr/bin/env python\nprint('classified locally')\n",
            encoding="utf-8",
        )
        (self.project / "notebooks" / "experiment").write_text(
            json.dumps({"cells": [], "nbformat": 4}),
            encoding="utf-8",
        )
        (self.project / "data" / "metrics.csv").write_text(
            "score\n0.9\n",
            encoding="utf-8",
        )
        (self.project / "paper" / "main.tex").write_text(
            "\\documentclass{article}\n",
            encoding="utf-8",
        )
        (self.project / "models" / "best.ckpt").write_bytes(b"\x00model\x01")
        (self.project / "mystery.opaque").write_bytes(b"\x00\x01\x02")

    def test_inventory_classifies_every_in_scope_file_without_source_writes(
        self,
    ) -> None:
        registration = register_project(self.workspace, self.project)
        before = {
            path.relative_to(self.project).as_posix(): path.read_bytes()
            for path in self.project.rglob("*")
            if path.is_file()
        }

        result = inventory_project(self.workspace, registration.project_id)
        rows = [
            json.loads(line)
            for line in result.manifest_file.read_text(encoding="utf-8").splitlines()
        ]
        files = {
            row["path"]: classification_from_dict(row["classification"])
            for row in rows
            if row["record_type"] == "file"
        }

        self.assertEqual(set(files), set(before))
        self.assertEqual(files["scripts/spoofed.pdf"].format, "python")
        self.assertEqual(files["notebooks/experiment"].format, "notebook")
        self.assertEqual(files["data/metrics.csv"].research_role, "dataset")
        self.assertEqual(files["paper/main.tex"].research_role, "paper")
        self.assertEqual(
            files["models/best.ckpt"].research_role,
            "model_artifact",
        )
        self.assertEqual(files["mystery.opaque"].format, UNKNOWN_CLASSIFICATION)
        self.assertEqual(
            result.classification_summary["classified_files"],
            len(files),
        )
        self.assertEqual(
            {
                path.relative_to(self.project).as_posix(): path.read_bytes()
                for path in self.project.rglob("*")
                if path.is_file()
            },
            before,
        )


    def test_sensitive_and_oversized_files_are_classified_without_prefix_reads(
        self,
    ) -> None:
        protected_project = Path(self.temp_dir.name) / "protected-project"
        protected_project.mkdir()
        source_bytes = {
            ".env": b"SECRET=value\n",
            "oversized.py": b"x = 'this file is deliberately over the limit'\n",
            "small.py": b"x = 1\n",
        }
        for relative_path, content in source_bytes.items():
            (protected_project / relative_path).write_bytes(content)

        registration = register_project(self.workspace, protected_project)
        original_reader = project_inventory_module._read_classification_sample
        sampled_paths: list[str] = []

        def guarded_reader(path: Path, snapshot: object) -> bytes:
            sampled_paths.append(path.name)
            if path.name in {".env", "oversized.py"}:
                raise AssertionError(
                    "B-02 metadata-only files must not be opened for classification"
                )
            return original_reader(path, snapshot)  # type: ignore[arg-type]

        with patch(
            "tools.project_inventory._read_classification_sample",
            side_effect=guarded_reader,
        ):
            result = inventory_project(
                self.workspace,
                registration.project_id,
                policy_config=ScanPolicyConfig(
                    max_content_file_bytes=16,
                    max_raw_external_send_bytes=16,
                ),
            )

        rows = [
            json.loads(line)
            for line in result.manifest_file.read_text(encoding="utf-8").splitlines()
        ]
        files = {
            row["path"]: classification_from_dict(row["classification"])
            for row in rows
            if row["record_type"] == "file"
        }
        self.assertEqual(set(files), set(source_bytes))
        self.assertEqual(sampled_paths, ["small.py"])

        sensitive = files[".env"]
        self.assertEqual(sensitive.format, UNKNOWN_CLASSIFICATION)
        self.assertEqual(sensitive.reasons["format"]["source"], "policy-path")
        self.assertIn("sensitive-path", sensitive.reasons["format"]["detail"])

        oversized = files["oversized.py"]
        self.assertEqual(oversized.format, "python")
        self.assertEqual(oversized.reasons["format"]["source"], "policy-path")
        self.assertIn(
            "content-size-limit",
            oversized.reasons["format"]["detail"],
        )

        allowed = files["small.py"]
        self.assertEqual(allowed.reasons["format"]["source"], "content-or-path")
        self.assertEqual(
            {path.name: path.read_bytes() for path in protected_project.iterdir()},
            source_bytes,
        )


if __name__ == "__main__":
    unittest.main()
