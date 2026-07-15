from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.project_layout import CURRENT_SCHEMA_VERSION
from tools.raw_md import RawMdBuilder, RawMdConfig, sha256_file


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "minimal_research_project"
FIXED_CONVERSION_TIME = "2026-01-02T10:00:00+08:00"
EXPECTED_FILES = {
    "README.md": "markdown-wrap",
    "configs/train.yaml": "code-wrap",
    "data/metrics.csv": "code-wrap",
    "notebooks/experiment.ipynb": "fallback-ipynb",
    "paper/main.tex": "text-wrap",
    "paper/references.bib": "text-wrap",
    "results/run.log": "text-wrap",
    "src/model.py": "code-wrap",
}
CONTENT_MARKERS = {
    "README.md": "Minimal Reproducible Study",
    "configs/train.yaml": "seed: 7",
    "data/metrics.csv": "proposed,0.84,0.36",
    "notebooks/experiment.ipynb": "baseline_accuracy=0.80",
    "paper/main.tex": "four percentage points",
    "paper/references.bib": "smith2024tiny",
    "results/run.log": "training_complete",
    "src/model.py": "def score",
}


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RawMdBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.temp_root = Path(self.temp_dir.name)
        self.project_root = self.temp_root / "minimal_research_project"
        self.wiki_root = self.temp_root / "minimal_research_project-wiki"
        shutil.copytree(FIXTURE_ROOT, self.project_root)

    def build_raw_md(self) -> dict:
        builder = RawMdBuilder(
            RawMdConfig(
                project_root=self.project_root,
                wiki_root=self.wiki_root,
                scope="primary",
            )
        )
        builder.generated_at = FIXED_CONVERSION_TIME

        # Keep this baseline independent of optional MarkItDown installation details.
        # The notebook must still be readable through the built-in deterministic fallback.
        with patch.object(
            builder,
            "_get_markitdown",
            side_effect=RuntimeError("MarkItDown disabled by deterministic baseline test"),
        ):
            return builder.run()

    def test_manifest_covers_the_minimal_research_project(self) -> None:
        manifest = self.build_raw_md()
        entries = {
            entry["source_path"]: entry
            for entry in manifest["entries"]
            if entry["kind"] == "file"
        }

        self.assertEqual(set(EXPECTED_FILES), set(entries))
        self.assertEqual(manifest["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["scope"], "primary")
        self.assertEqual(manifest["generated_at"], FIXED_CONVERSION_TIME)
        self.assertEqual(manifest["counts"]["files"], len(EXPECTED_FILES))
        self.assertEqual(manifest["counts"]["directories"], 0)
        self.assertEqual(manifest["counts"]["ok"], len(EXPECTED_FILES))
        self.assertEqual(manifest["counts"]["metadata_only"], 0)
        self.assertEqual(manifest["counts"]["skipped"], 0)

        for source_path, expected_converter in EXPECTED_FILES.items():
            with self.subTest(source_path=source_path):
                entry = entries[source_path]
                self.assertEqual(entry["status"], "ok")
                self.assertEqual(entry["converter"], expected_converter)
                self.assertEqual(
                    entry["source_hash"],
                    sha256_file(self.project_root / Path(source_path)),
                )
                self.assertTrue(entry["output_path"].startswith("raw-md/primary/"))

        manifest_path = self.wiki_root / "state" / "raw-md-manifest.json"
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            manifest,
        )

    def test_generated_pages_preserve_traceable_paths_and_content(self) -> None:
        manifest = self.build_raw_md()
        entries = {
            entry["source_path"]: entry
            for entry in manifest["entries"]
            if entry["kind"] == "file"
        }

        for source_path, marker in CONTENT_MARKERS.items():
            with self.subTest(source_path=source_path):
                output_path = self.wiki_root / Path(entries[source_path]["output_path"])
                self.assertTrue(output_path.is_file())
                page = output_path.read_text(encoding="utf-8")
                self.assertIn(
                    f"schema_version: {CURRENT_SCHEMA_VERSION}",
                    page,
                )
                self.assertIn(f'source_path: "{source_path}"', page)
                self.assertIn(f"- Source path: `{source_path}`", page)
                self.assertIn(marker, page)

        notebook_page = (
            self.wiki_root
            / Path(entries["notebooks/experiment.ipynb"]["output_path"])
        ).read_text(encoding="utf-8")
        self.assertIn("# Experiment Note", notebook_page)
        self.assertIn("### Code Cell 2", notebook_page)
        self.assertIn("### Output 2", notebook_page)

        report_path = self.wiki_root / "reports" / "raw-md-report.md"
        report = report_path.read_text(encoding="utf-8")
        self.assertIn(
            f"- Schema version: `{CURRENT_SCHEMA_VERSION}`",
            report,
        )
        self.assertIn("## Summary", report)
        self.assertIn("## Converted Files", report)
        self.assertIn("`src/model.py`", report)
        self.assertIn("This command does not call an LLM API.", report)

    def test_machine_state_directory_is_excluded_from_source_evidence(self) -> None:
        private_state = (
            self.project_root
            / ".llmwiki"
            / "projects"
            / "tiny-study"
            / "indexes"
            / "private.json"
        )
        private_state.parent.mkdir(parents=True)
        private_state.write_text(
            '{"local_path": "do-not-ingest"}\n',
            encoding="utf-8",
            newline="\n",
        )

        manifest = self.build_raw_md()
        file_paths = {
            entry["source_path"]
            for entry in manifest["entries"]
            if entry["kind"] == "file"
        }
        skipped = [
            entry
            for entry in manifest["entries"]
            if entry["kind"] == "dir" and entry["source_path"] == ".llmwiki"
        ]

        self.assertNotIn(
            ".llmwiki/projects/tiny-study/indexes/private.json",
            file_paths,
        )
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["status"], "skipped")
        self.assertIn("excluded directory", skipped[0]["reason"])

    def test_repeated_build_is_stable_and_does_not_modify_sources(self) -> None:
        source_hashes_before = tree_hashes(self.project_root)

        first_manifest = self.build_raw_md()
        first_output_hashes = tree_hashes(self.wiki_root)
        second_manifest = self.build_raw_md()
        second_output_hashes = tree_hashes(self.wiki_root)

        self.assertEqual(tree_hashes(self.project_root), source_hashes_before)
        self.assertEqual(second_manifest, first_manifest)
        self.assertEqual(second_output_hashes, first_output_hashes)


if __name__ == "__main__":
    unittest.main()
