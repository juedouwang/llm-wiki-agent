from __future__ import annotations

import json
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.scan_policy import (
    DEFAULT_MAX_CONTENT_FILE_BYTES,
    DEFAULT_MAX_RAW_EXTERNAL_SEND_BYTES,
    SCAN_POLICY_KIND,
    SCAN_POLICY_SCHEMA_VERSION,
    SCAN_POLICY_VERSION,
    ScanPolicyConfig,
    ScanPolicyConfigError,
    ScanPolicyPathError,
    load_scan_policy,
)


class ScanPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "research-project"
        self.root.mkdir()

    def write_ignore(self, text: str) -> Path:
        path = self.root / ".llmwikiignore"
        path.write_text(text, encoding="utf-8")
        return path

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def test_default_boundary_excludes_noise_but_keeps_research_material(self) -> None:
        policy = load_scan_policy(
            self.root, config=ScanPolicyConfig(case_sensitive=True)
        )

        node_modules = policy.decide_path("node_modules", is_directory=True)
        cache_file = policy.decide_path("src/__pycache__/model.pyc", is_directory=False)
        result = policy.decide_path("results/ablation_curve.pdf", is_directory=False)
        data = policy.decide_path("data/train/images.tar", is_directory=False)
        build = policy.decide_path("build/paper/final.pdf", is_directory=False)

        self.assertFalse(node_modules.included)
        self.assertFalse(node_modules.traverse)
        self.assertEqual(node_modules.reason_code, "default-exclude")
        self.assertFalse(cache_file.included)
        self.assertTrue(result.included)
        self.assertTrue(data.included)
        self.assertTrue(build.included)

    def test_ignore_file_last_match_wins_and_preserves_explanation(self) -> None:
        self.write_ignore(
            "# generated data\ndata/\n!data/important.csv\n*.tmp\n!node_modules/research/**\n"
        )
        policy = load_scan_policy(
            self.root, config=ScanPolicyConfig(case_sensitive=True)
        )

        directory = policy.decide_path("data", is_directory=True)
        ignored = policy.decide_path("data/raw/chunk.csv", is_directory=False)
        included = policy.decide_path("data/important.csv", is_directory=False)
        temporary = policy.decide_path("notes/draft.tmp", is_directory=False)
        default_override = policy.decide_path(
            "node_modules/research/adapter.md",
            is_directory=False,
        )

        self.assertFalse(directory.included)
        self.assertTrue(directory.traverse)
        self.assertIn("possible re-included descendant", directory.reason)
        self.assertEqual(ignored.reason_code, "ignore-exclude")
        self.assertEqual(ignored.matched_rule.line_number, 2)
        self.assertTrue(included.included)
        self.assertEqual(included.reason_code, "ignore-include")
        self.assertEqual(included.matched_rule.line_number, 3)
        self.assertFalse(temporary.included)
        self.assertEqual(temporary.matched_rule.line_number, 4)
        self.assertTrue(default_override.included)
        self.assertEqual(default_override.reason_code, "ignore-include")
        self.assertEqual(default_override.matched_rule.line_number, 5)

    def test_explicit_precedence_and_protected_exclusions_are_deterministic(
        self,
    ) -> None:
        self.write_ignore("private/\n")
        config = ScanPolicyConfig(
            include_patterns=(
                "node_modules/research/**",
                "private/public.md",
                ".git/config",
            ),
            exclude_patterns=("private/**",),
            case_sensitive=True,
        )
        policy = load_scan_policy(self.root, config=config)

        default_parent = policy.decide_path("node_modules", is_directory=True)
        re_included = policy.decide_path(
            "node_modules/research/paper.md",
            is_directory=False,
        )
        overlap = policy.decide_path("private/public.md", is_directory=False)
        protected = policy.decide_path(".git/config", is_directory=False)
        protected_worktree_file = policy.decide_path(".git", is_directory=False)

        self.assertFalse(default_parent.included)
        self.assertTrue(default_parent.traverse)
        self.assertTrue(re_included.included)
        self.assertEqual(re_included.reason_code, "explicit-include")
        self.assertFalse(overlap.included)
        self.assertEqual(overlap.reason_code, "explicit-exclude")
        self.assertFalse(protected.included)
        self.assertEqual(protected.reason_code, "protected-exclude")
        self.assertFalse(protected_worktree_file.included)

    def test_root_anchoring_basename_and_double_star_globs(self) -> None:
        self.write_ignore("/root-only/\n*.bak\nartifacts/**/scratch.*\n")
        policy = load_scan_policy(
            self.root, config=ScanPolicyConfig(case_sensitive=True)
        )

        self.assertFalse(
            policy.decide_path("root-only/file.md", is_directory=False).included
        )
        self.assertTrue(
            policy.decide_path("nested/root-only/file.md", is_directory=False).included
        )
        self.assertFalse(
            policy.decide_path("nested/copy.bak", is_directory=False).included
        )
        self.assertFalse(
            policy.decide_path(
                "artifacts/run/plots/scratch.csv",
                is_directory=False,
            ).included
        )

    def test_windows_style_paths_casefold_and_unicode_nfc_are_normalized(self) -> None:
        self.write_ignore("Caf?/\n")
        policy = load_scan_policy(
            self.root, config=ScanPolicyConfig(case_sensitive=False)
        )
        decomposed_upper = unicodedata.normalize("NFD", "CAF?")

        ignored = policy.decide_path(
            f"notes\\{decomposed_upper}\\paper.md",
            is_directory=False,
        )
        protected = policy.decide_path(
            "nested\\.GIT\\config",
            is_directory=False,
        )

        self.assertFalse(ignored.included)
        self.assertEqual(ignored.reason_code, "ignore-exclude")
        self.assertFalse(protected.included)
        self.assertEqual(protected.reason_code, "protected-exclude")

    def test_symbolic_link_ignore_file_fails_closed(self) -> None:
        ignore = self.root / ".llmwikiignore"

        with patch.object(Path, "is_symlink", autospec=True) as is_symlink:
            is_symlink.side_effect = lambda candidate: candidate == ignore
            with self.assertRaisesRegex(
                ScanPolicyConfigError,
                "must not be a symbolic link",
            ):
                load_scan_policy(self.root)

    def test_config_rejects_conflicts_and_invalid_values(self) -> None:
        invalid_options = (
            {"include_patterns": ("data/**",), "exclude_patterns": ("data/**",)},
            {"include_patterns": "data/**"},
            {"exclude_patterns": ("../outside",)},
            {"exclude_patterns": ("C:/outside",)},
            {"max_content_file_bytes": 0},
            {
                "max_content_file_bytes": 10,
                "max_raw_external_send_bytes": 11,
            },
            {"follow_symlinks": "yes"},
            {"case_sensitive": "yes"},
            {"external_send_mode": "unknown"},
            {"external_send_mode": "allowlist"},
            {"external_include_patterns": ("papers/**",)},
            {
                "external_send_mode": "allowlist",
                "external_include_patterns": ("papers/**",),
                "external_exclude_patterns": ("papers/**",),
            },
        )
        for options in invalid_options:
            with self.subTest(options=options):
                with self.assertRaises(ScanPolicyConfigError):
                    ScanPolicyConfig(**options)

    def test_invalid_ignore_file_fails_closed_without_writing(self) -> None:
        ignore = self.root / ".llmwikiignore"
        ignore.write_bytes(b"valid/\n\xff\n")
        before = self.snapshot()

        with self.assertRaises(ScanPolicyConfigError):
            load_scan_policy(self.root)

        self.assertEqual(self.snapshot(), before)

    def test_sensitive_and_large_files_remain_accounted_for_but_content_is_blocked(
        self,
    ) -> None:
        config = ScanPolicyConfig(
            sensitive_patterns=("private-notes/",),
            max_content_file_bytes=100,
            max_raw_external_send_bytes=50,
            external_send_mode="safe",
            case_sensitive=True,
        )
        policy = load_scan_policy(self.root, config=config)

        secret = policy.decide_file("configs/.env", size_bytes=12)
        custom_secret = policy.decide_file("private-notes/idea.md", size_bytes=12)
        large = policy.decide_file("models/model.ckpt", size_bytes=101)
        medium = policy.decide_file("papers/paper.pdf", size_bytes=51)
        small = policy.decide_file("src/model.py", size_bytes=50)

        self.assertTrue(secret.boundary.included)
        self.assertTrue(secret.sensitive)
        self.assertEqual(secret.local_content_access, "metadata_only")
        self.assertEqual(secret.raw_external_send, "blocked")
        self.assertEqual(secret.external_reason_code, "sensitive-path")
        self.assertTrue(custom_secret.sensitive)
        self.assertEqual(large.local_reason_code, "content-size-limit")
        self.assertEqual(large.local_content_access, "metadata_only")
        self.assertEqual(medium.local_content_access, "allowed")
        self.assertEqual(medium.external_reason_code, "external-size-limit")
        self.assertEqual(small.raw_external_send, "allowed")

    def test_external_send_modes_and_path_rules(self) -> None:
        default_policy = load_scan_policy(self.root)
        self.assertEqual(
            default_policy.decide_file("paper.md", size_bytes=1).external_reason_code,
            "external-local-only",
        )

        local_only = load_scan_policy(
            self.root,
            config=ScanPolicyConfig(
                external_send_mode="local-only",
                case_sensitive=True,
            ),
        )
        self.assertEqual(
            local_only.decide_file("paper.md", size_bytes=1).external_reason_code,
            "external-local-only",
        )

        allowlist = load_scan_policy(
            self.root,
            config=ScanPolicyConfig(
                external_send_mode="allowlist",
                external_include_patterns=("papers/**",),
                external_exclude_patterns=("papers/private/**",),
                case_sensitive=True,
            ),
        )
        allowed = allowlist.decide_file("papers/public/paper.md", size_bytes=1)
        denied = allowlist.decide_file("notes/idea.md", size_bytes=1)
        explicit_denied = allowlist.decide_file(
            "papers/private/review.md",
            size_bytes=1,
        )

        self.assertEqual(allowed.raw_external_send, "allowed")
        self.assertEqual(denied.external_reason_code, "external-not-allowlisted")
        self.assertEqual(
            explicit_denied.external_reason_code,
            "external-explicit-exclude",
        )

    def test_symlink_policy_blocks_default_outside_cycle_and_duplicate_targets(
        self,
    ) -> None:
        inside = self.root / "real-data"
        inside.mkdir()
        outside = self.root.parent / "outside-data"
        outside.mkdir()

        default_policy = load_scan_policy(self.root)
        disabled = default_policy.assess_symlink_target("link", inside)
        self.assertFalse(disabled.follow)
        self.assertEqual(disabled.reason_code, "symlink-follow-disabled")

        policy = load_scan_policy(
            self.root,
            config=ScanPolicyConfig(follow_symlinks=True),
        )
        safe = policy.assess_symlink_target(
            "safe-link",
            inside,
            ancestor_realpaths=(self.root,),
        )
        external = policy.assess_symlink_target("outside-link", outside)
        cycle = policy.assess_symlink_target(
            "loop",
            self.root,
            ancestor_realpaths=(self.root,),
        )
        duplicate = policy.assess_symlink_target(
            "alias",
            inside,
            visited_realpaths=(inside,),
        )

        self.assertTrue(safe.follow)
        self.assertEqual(safe.reason_code, "symlink-follow-safe")
        self.assertFalse(external.follow)
        self.assertEqual(external.reason_code, "symlink-target-outside-project")
        self.assertFalse(cycle.follow)
        self.assertEqual(cycle.reason_code, "symlink-cycle")
        self.assertFalse(duplicate.follow)
        self.assertEqual(duplicate.reason_code, "symlink-target-already-visited")

    def test_loading_and_decisions_do_not_modify_source_tree(self) -> None:
        self.write_ignore("scratch/\n!scratch/keep.md\n")
        (self.root / "README.md").write_text("# Study\n", encoding="utf-8")
        before = self.snapshot()

        policy = load_scan_policy(self.root)
        policy.decide_path("scratch/file.txt", is_directory=False)
        policy.decide_file("README.md", size_bytes=8)
        policy.as_dict()

        self.assertEqual(self.snapshot(), before)

    def test_policy_serialization_is_versioned_and_machine_readable(self) -> None:
        self.write_ignore("cache/\n")
        policy = load_scan_policy(
            self.root,
            config=ScanPolicyConfig(
                exclude_patterns=("private/**",),
                case_sensitive=True,
            ),
        )
        payload = policy.as_dict()
        round_tripped = json.loads(json.dumps(payload))

        self.assertEqual(round_tripped["schema_version"], SCAN_POLICY_SCHEMA_VERSION)
        self.assertEqual(round_tripped["kind"], SCAN_POLICY_KIND)
        self.assertEqual(round_tripped["policy_version"], SCAN_POLICY_VERSION)
        self.assertEqual(round_tripped["ignore_file"], ".llmwikiignore")
        self.assertTrue(round_tripped["effective_case_sensitive"])
        self.assertEqual(
            round_tripped["config"]["max_content_file_bytes"],
            DEFAULT_MAX_CONTENT_FILE_BYTES,
        )
        self.assertEqual(
            round_tripped["config"]["max_raw_external_send_bytes"],
            DEFAULT_MAX_RAW_EXTERNAL_SEND_BYTES,
        )
        self.assertEqual(
            round_tripped["rules"]["explicit_excludes"][0]["pattern"],
            "private/**",
        )

    def test_paths_and_file_sizes_fail_closed(self) -> None:
        policy = load_scan_policy(self.root)
        with self.assertRaises(ScanPolicyConfigError):
            load_scan_policy(self.root, config={})
        invalid_paths = ("", ".", "../escape", "/absolute", "C:/absolute")
        for path in invalid_paths:
            with self.subTest(path=path):
                with self.assertRaises(ScanPolicyPathError):
                    policy.decide_path(path, is_directory=False)
        for size in (-1, 1.5, True):
            with self.subTest(size=size):
                with self.assertRaises(ScanPolicyPathError):
                    policy.decide_file("paper.md", size_bytes=size)


if __name__ == "__main__":
    unittest.main()
