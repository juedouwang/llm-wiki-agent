from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tools import health


class HealthBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_root = Path(self.temp_dir.name)
        self.wiki_dir = self.repo_root / "wiki"
        self.index_file = self.wiki_dir / "index.md"
        self.log_file = self.wiki_dir / "log.md"
        self.wiki_dir.mkdir(parents=True)

    def write(self, relative_path: str, content: str) -> Path:
        path = self.repo_root / Path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def path_patch(self):
        return patch.multiple(
            health,
            REPO_ROOT=self.repo_root,
            WIKI_DIR=self.wiki_dir,
            INDEX_FILE=self.index_file,
            LOG_FILE=self.log_file,
        )

    def test_frontmatter_is_removed_and_empty_or_short_pages_are_reported(self) -> None:
        empty = self.write(
            "wiki/sources/empty.md",
            "---\ntitle: Empty\ntype: source\n---\n",
        )
        stub = self.write(
            "wiki/sources/stub.md",
            "---\ntitle: Stub\ntype: source\n---\n\nShort note.",
        )
        complete = self.write(
            "wiki/sources/complete.md",
            "---\ntitle: Complete\ntype: source\n---\n\n" + ("Evidence. " * 20),
        )

        self.assertEqual(health.strip_frontmatter(empty.read_text(encoding="utf-8")), "")
        self.assertEqual(
            health.strip_frontmatter(stub.read_text(encoding="utf-8")),
            "Short note.",
        )

        with self.path_patch():
            findings = health.check_empty_files([complete, stub, empty], threshold=50)

        by_name = {Path(item["path"]).name: item for item in findings}
        self.assertEqual(set(by_name), {"empty.md", "stub.md"})
        self.assertEqual(by_name["empty.md"]["status"], "empty")
        self.assertEqual(by_name["empty.md"]["body_bytes"], 0)
        self.assertEqual(by_name["stub.md"]["status"], "stub")
        self.assertEqual(by_name["stub.md"]["body_bytes"], len("Short note."))

    def test_index_sync_reports_stale_and_unlisted_pages(self) -> None:
        overview = self.write("wiki/overview.md", "# Overview\n")
        indexed = self.write("wiki/sources/indexed.md", "# Indexed\n")
        unlisted = self.write("wiki/sources/unlisted.md", "# Unlisted\n")
        self.write(
            "wiki/index.md",
            "# Wiki Index\n\n"
            "- [Overview](overview.md)\n"
            "- [Indexed](sources/indexed.md)\n"
            "- [Stale](sources/stale.md)\n",
        )

        with self.path_patch():
            result = health.check_index_sync([overview, indexed, unlisted])

        self.assertEqual(
            result["in_index_not_on_disk"],
            [str(Path("wiki") / "sources" / "stale.md")],
        )
        self.assertEqual(
            result["on_disk_not_in_index"],
            [str(Path("wiki") / "sources" / "unlisted.md")],
        )

    def test_log_coverage_matches_slug_or_yaml_quoted_title(self) -> None:
        quoted = self.write(
            "wiki/sources/quoted-title.md",
            '---\ntitle: "Few \\"People\\" Laptop"\ntype: source\n---\n\nBody.\n',
        )
        by_slug = self.write(
            "wiki/sources/by-slug.md",
            "---\ntitle: A Different Display Title\ntype: source\n---\n\nBody.\n",
        )
        unlogged = self.write(
            "wiki/sources/unlogged.md",
            "---\ntitle: Unlogged Study\ntype: source\n---\n\nBody.\n",
        )
        self.write(
            "wiki/log.md",
            '# Wiki Log\n\n'
            '## [2026-01-03] ingest | Few "People" Laptop\n\n'
            '## [2026-01-02] ingest | by slug\n',
        )

        self.assertEqual(
            health._parse_frontmatter_title(quoted.read_text(encoding="utf-8")),
            'few "people" laptop',
        )

        with self.path_patch():
            missing = health.check_log_coverage([quoted, by_slug, unlogged])

        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["path"], str(Path("wiki") / "sources" / "unlogged.md"))
        self.assertEqual(missing[0]["slug"], "unlogged")
        self.assertEqual(missing[0]["title"], "unlogged study")

    def test_run_health_and_report_keep_the_structural_contract(self) -> None:
        source = self.write(
            "wiki/sources/logged.md",
            "---\ntitle: Logged Study\ntype: source\n---\n\n" + ("Evidence. " * 20),
        )
        concept = self.write(
            "wiki/concepts/short.md",
            "---\ntitle: Short Concept\ntype: concept\n---\n\nTiny.",
        )
        self.write(
            "wiki/index.md",
            "# Wiki Index\n\n- [Logged Study](sources/logged.md)\n",
        )
        self.write(
            "wiki/log.md",
            "# Wiki Log\n\n## [2026-01-02] ingest | Logged Study\n",
        )
        pages = [source, concept]

        with self.path_patch(), patch.object(
            health,
            "all_wiki_pages",
            return_value=pages,
        ) as page_scan:
            results = health.run_health()

        page_scan.assert_called_once_with(extra_exclude={"health-report.md"})
        self.assertEqual(results["date"], date.today().isoformat())
        self.assertEqual(results["total_pages"], 2)
        self.assertEqual(len(results["empty_files"]), 1)
        self.assertEqual(results["empty_files"][0]["status"], "stub")
        self.assertEqual(
            results["index_sync"]["on_disk_not_in_index"],
            [str(Path("wiki") / "concepts" / "short.md")],
        )
        self.assertEqual(results["log_coverage"], [])

        report = health.format_report(results)
        self.assertIn("# Wiki Health Report", report)
        self.assertIn("Scanned 2 wiki pages.", report)
        self.assertIn("## Empty / Stub Files (1 found)", report)
        self.assertIn("## Index Sync (1 issues)", report)
        self.assertIn("## Log Coverage (0 source pages without log entry)", report)
        self.assertIn(str(Path("wiki") / "concepts" / "short.md"), report)


if __name__ == "__main__":
    unittest.main()
