from __future__ import annotations

import hashlib
import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tools import development_dashboard as dashboard


ROADMAP_FIXTURE = """
# Research assistant roadmap

## Completed work

| Task | Status | Commit | Checkpoint |
|---|---|---|---|
| A-01 Baseline complete | completed | `abcdef1` | `checkpoint/a-01-baseline` |
| B-01 Foundation phase | phase A complete | `bcdefa2` | `checkpoint/b-01-foundation` |

## Tasks

| ID | Minimum change | Acceptance | Delta |
|---|---|---|---|
| A-01 `P0/S` | Align the repository | Baseline passes | unknown to aligned |
| B-01 `P0/M` | Build the scan foundation | Fixtures pass | absent to available |
| J-03 `P0/L` | Local read-only dashboard | Loopback only | Markdown to visual |

### R0：Engineering baseline (已完成)

```text
A-01
```

### R0.5: Project foundation

```text
B-01
```

### R1: Basic chain

```text
A-01 -> B-01
```

### R2: Understanding

```text
B-01
```

### R3: First useful product

```text
J-03
```

### R4: Verified query

```text
A-01
```

### R5: Incremental maintenance

```text
B-01
```

### R6: Planning loop

```text
J-03
```
"""


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout.strip()


def write_file(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def init_repository(parent: Path) -> Path:
    repo = parent / "repo"
    repo.mkdir()
    run_git(repo, "init", "-b", dashboard.BASE_BRANCH)
    run_git(repo, "config", "user.name", "Dashboard Tests")
    run_git(repo, "config", "user.email", "dashboard@example.invalid")
    write_file(repo, "docs/research-assistant-roadmap.md", ROADMAP_FIXTURE)
    write_file(repo, "tracked.txt", "base\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "chore: establish baseline")
    return repo


def file_fingerprints(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def fake_git_state(
    *,
    branch: str = dashboard.BASE_BRANCH,
    clean: bool = True,
    ahead: int = 0,
    tags: dict[str, str] | None = None,
) -> dict[str, object]:
    full_hash = "a" * 40
    commit = {
        "hash": full_hash,
        "short_hash": full_hash[:12],
        "committed_at": "2026-07-17T06:00:00+00:00",
        "subject": "feat(j-03): add dashboard foundation",
        "files": [
            {
                "path": "tools/development_dashboard.py",
                "additions": 120,
                "deletions": 2,
                "binary": False,
            }
        ],
        "task_ids": ["J-03"],
    }
    return {
        "repository_name": "repo",
        "branch": branch,
        "head": {
            "hash": full_hash,
            "short_hash": full_hash[:12],
            "subject": commit["subject"],
            "committed_at": commit["committed_at"],
        },
        "clean": clean,
        "status_entries": [] if clean else [{"status": " M", "path": "work.txt"}],
        "omitted_status_count": 0,
        "base_branch": dashboard.BASE_BRANCH,
        "ahead": ahead,
        "behind": 0,
        "current_changes": dashboard.aggregate_files([]),
        "commits": [commit],
        "tags": tags or {},
    }


def unit_catalog_fixture(
    *,
    commit: str = "a" * 40,
    path: str = "docs/development-dashboard.md",
    checkpoints: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": dashboard.SCHEMA_VERSION,
        "kind": dashboard.UNIT_CATALOG_KIND,
        "units": [
            {
                "task_id": "J-03",
                "unit_id": "J-03B",
                "title": "Unit evidence visualization",
                "summary": "Group bounded validation evidence by unit.",
                "commits": [commit],
                "checkpoints": (
                    ["checkpoint/j-03-unit-visualization"]
                    if checkpoints is None
                    else checkpoints
                ),
                "artifacts": [
                    {
                        "artifact_id": "dashboard-doc",
                        "label": "Dashboard documentation",
                        "description": "Committed dashboard contract.",
                        "path": path,
                        "commit": commit,
                    }
                ],
            }
        ],
    }


def progress_record_fixture(
    *,
    task_id: str = "J-03",
    unit_id: str = "J-03B",
    commit: str = "a" * 12,
    status: str = "passed",
) -> dict[str, object]:
    return {
        "record_id": f"{unit_id.lower()}-record",
        "recorded_at": "2026-07-17T06:15:00Z",
        "task_id": task_id,
        "unit_id": unit_id,
        "status": status,
        "summary": "Focused dashboard validation passed.",
        "branch": "task/j-03-unit-visualization",
        "commit": commit,
        "checks": [
            {
                "id": "dashboard-tests",
                "status": "passed",
                "summary": "focused tests passed",
            }
        ],
    }


class RoadmapParsingTests(unittest.TestCase):
    def test_fixture_distinguishes_complete_partial_and_milestones(self) -> None:
        parsed = dashboard.parse_roadmap(ROADMAP_FIXTURE)

        self.assertEqual(set(parsed["tasks"]), {"A-01", "B-01", "J-03"})
        self.assertFalse(parsed["tasks"]["A-01"]["completion"]["partial"])
        self.assertTrue(parsed["tasks"]["B-01"]["completion"]["partial"])
        self.assertIsNone(parsed["tasks"]["J-03"]["completion"])
        self.assertEqual(
            [item["id"] for item in parsed["milestones"]],
            ["R0", "R0.5", "R1", "R2", "R3", "R4", "R5", "R6"],
        )
        self.assertTrue(parsed["milestones"][0]["accepted"])
        self.assertEqual(parsed["milestones"][4]["task_ids"], ["J-03"])

    def test_repository_roadmap_has_expected_dashboard_contract(self) -> None:
        text = (
            dashboard.REPO_ROOT / "docs" / "research-assistant-roadmap.md"
        ).read_text(encoding="utf-8")
        parsed = dashboard.parse_roadmap(text)

        self.assertIn("J-03", parsed["tasks"])
        self.assertGreaterEqual(len(parsed["tasks"]), 70)
        self.assertEqual(
            [item["id"] for item in parsed["milestones"]],
            ["R0", "R0.5", "R1", "R2", "R3", "R4", "R5", "R6"],
        )

    def test_branch_alias_and_active_status_transitions(self) -> None:
        active = dashboard.branch_unit("task/j-03-dashboard-foundation")
        self.assertEqual(active["task_id"], "J-03")
        self.assertEqual(active["unit_id"], "J-03A")

        scenarios = [
            (True, 0, None, "authorized"),
            (False, 0, None, "in_progress"),
            (True, 1, None, "awaiting_review"),
            (True, 0, {"status": "awaiting_review"}, "awaiting_review"),
            (True, 0, {"status": "failed"}, "needs_changes"),
            (True, 0, {"status": "blocked"}, "blocked"),
        ]
        for clean, ahead, progress, expected in scenarios:
            with self.subTest(expected=expected):
                state = fake_git_state(clean=clean, ahead=ahead)
                self.assertEqual(dashboard.active_status(state, progress), expected)

    def test_parses_p08_and_f01_subunits_from_branches_and_checkpoints(self) -> None:
        cases = [
            ("task/p-08-agent-native-architecture", "P-08", "P-08"),
            ("task/f-01a-knowledge-artifact-contract", "F-01", "F-01A"),
            ("task/f-01b-canonical-knowledge-layout", "F-01", "F-01B"),
            ("task/j-03-unit-visualization", "J-03", "J-03B"),
        ]
        for reference, task_id, unit_id in cases:
            with self.subTest(reference=reference):
                parsed = dashboard.branch_unit(reference)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed["task_id"], task_id)
                self.assertEqual(parsed["unit_id"], unit_id)

                checkpoint = reference.replace("task/", "checkpoint/", 1)
                parsed_checkpoint = dashboard.checkpoint_unit(checkpoint)
                self.assertIsNotNone(parsed_checkpoint)
                self.assertEqual(parsed_checkpoint["task_id"], task_id)
                self.assertEqual(parsed_checkpoint["unit_id"], unit_id)
                self.assertEqual(dashboard.checkpoint_task(checkpoint), task_id)

        commit_log = (
            f"\x1e{'b' * 40}\x1f{'b' * 12}\x1f2026-07-17T06:00:00+00:00"
            "\x1ffeat(p-08): establish agent-native architecture\n"
        )
        self.assertEqual(dashboard.parse_git_log(commit_log)[0]["task_ids"], ["P-08"])


class GitAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = init_repository(Path(self.temp_dir.name))

    def test_collects_branch_commits_checkpoints_and_file_deltas(self) -> None:
        base_hash = run_git(self.repo, "rev-parse", "HEAD")
        run_git(self.repo, "tag", "checkpoint/a-01-baseline", base_hash)
        run_git(self.repo, "switch", "-c", "task/j-03-dashboard-foundation")
        write_file(self.repo, "tracked.txt", "base\ncommitted\n")
        run_git(self.repo, "add", "tracked.txt")
        run_git(self.repo, "commit", "-m", "feat(j-03): add dashboard data")
        run_git(self.repo, "tag", "checkpoint/j-03-dashboard-foundation")
        write_file(self.repo, "tracked.txt", "base\ncommitted\nworking\n")
        write_file(self.repo, "untracked.txt", "local\n")

        state = dashboard.collect_git_state(self.repo)

        self.assertEqual(state["branch"], "task/j-03-dashboard-foundation")
        self.assertFalse(state["clean"])
        self.assertEqual(state["ahead"], 1)
        self.assertEqual(state["behind"], 0)
        self.assertIn("checkpoint/a-01-baseline", state["tags"])
        self.assertIn("checkpoint/j-03-dashboard-foundation", state["tags"])
        task_commits = [item for item in state["commits"] if "J-03" in item["task_ids"]]
        self.assertEqual(len(task_commits), 1)
        self.assertEqual(task_commits[0]["subject"], "feat(j-03): add dashboard data")
        changes = {item["path"]: item for item in state["current_changes"]["files"]}
        self.assertIn("tracked.txt", changes)
        self.assertIn("untracked.txt", changes)
        self.assertGreaterEqual(changes["tracked.txt"]["additions"], 2)
        self.assertTrue(changes["untracked.txt"]["binary"])


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.path = self.root / ".llmwiki" / "development-dashboard" / "progress.json"

    def append(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "task_id": "J-03",
            "unit_id": "J-03A",
            "status": "in_progress",
            "summary": "Dashboard implementation is underway.",
            "branch": "task/j-03-dashboard-foundation",
            "commit": "abcdef123456",
            "checks": [{"id": "ruff", "status": "passed", "summary": "clean"}],
            "recorded_at": datetime(2026, 7, 17, 6, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return dashboard.append_record(self.path, **values)

    def test_missing_ledger_and_round_trip_use_schema_v1(self) -> None:
        missing = dashboard.load_ledger(self.path)
        self.assertEqual(missing["schema_version"], 1)
        self.assertEqual(missing["records"], [])

        record = self.append()
        loaded = dashboard.load_ledger(self.path)

        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(loaded["kind"], dashboard.LEDGER_KIND)
        self.assertEqual(loaded["records"], [record])
        self.assertEqual(record["recorded_at"], "2026-07-17T06:00:00Z")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_atomic_write_does_not_replace_existing_file_on_serialization_error(
        self,
    ) -> None:
        original = {
            "schema_version": 1,
            "kind": dashboard.LEDGER_KIND,
            "records": [],
        }
        dashboard.atomic_json(self.path, original)
        before = self.path.read_bytes()

        with patch.object(dashboard.json, "dump", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                dashboard.atomic_json(self.path, {"replacement": True})

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_missing_and_future_schema_versions_fail_closed(self) -> None:
        payloads = [
            {"kind": dashboard.LEDGER_KIND, "records": []},
            {
                "schema_version": dashboard.SCHEMA_VERSION + 1,
                "kind": dashboard.LEDGER_KIND,
                "records": [],
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(dashboard.DashboardStateError):
                    dashboard.load_ledger(self.path)

    def test_invalid_task_status_and_check_are_rejected(self) -> None:
        invalid = [
            {"task_id": "j03"},
            {"task_id": "F-01", "unit_id": "J-03A"},
            {"unit_id": "J-03-ESCAPE"},
            {"status": "complete"},
            {"checks": [{"id": "ruff", "status": "unknown", "summary": ""}]},
            {"checks": [{"id": "INVALID CHECK", "status": "passed", "summary": ""}]},
        ]
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(dashboard.DashboardStateError):
                    self.append(**override)

    def test_optional_catalog_is_strict_and_append_preserves_prior_records(
        self,
    ) -> None:
        first = self.append()
        payload = dashboard.load_ledger(self.path)
        payload["unit_catalog"] = unit_catalog_fixture()
        dashboard.atomic_json(self.path, dashboard.validate_ledger(payload))
        before = dashboard.load_ledger(self.path)

        second = self.append(
            unit_id="J-03B",
            branch="task/j-03-unit-visualization",
            commit="a" * 12,
        )
        after = dashboard.load_ledger(self.path)

        self.assertEqual(after["records"], [first, second])
        self.assertEqual(after["records"][0], before["records"][0])
        self.assertEqual(after["unit_catalog"], before["unit_catalog"])

        base = {
            "schema_version": dashboard.SCHEMA_VERSION,
            "kind": dashboard.LEDGER_KIND,
            "records": [],
            "unit_catalog": unit_catalog_fixture(),
        }
        invalid_payloads = []
        for location, key, value in [
            (("unit_catalog",), "extra", True),
            (("unit_catalog", "units", 0), "extra", True),
            (("unit_catalog", "units", 0, "artifacts", 0), "extra", True),
        ]:
            candidate = json.loads(json.dumps(base))
            target = candidate
            for part in location:
                target = target[part]
            target[key] = value
            invalid_payloads.append(candidate)

        future = json.loads(json.dumps(base))
        future["unit_catalog"]["schema_version"] = dashboard.SCHEMA_VERSION + 1
        invalid_payloads.append(future)
        short_commit = json.loads(json.dumps(base))
        short_commit["unit_catalog"]["units"][0]["commits"] = ["a" * 12]
        invalid_payloads.append(short_commit)
        undeclared_artifact_commit = json.loads(json.dumps(base))
        undeclared_artifact_commit["unit_catalog"]["units"][0]["artifacts"][0][
            "commit"
        ] = "b" * 40
        invalid_payloads.append(undeclared_artifact_commit)
        mismatched_unit = json.loads(json.dumps(base))
        mismatched_unit["unit_catalog"]["units"][0]["task_id"] = "F-01"
        invalid_payloads.append(mismatched_unit)

        for candidate in invalid_payloads:
            with self.subTest(candidate=candidate):
                with self.assertRaises(dashboard.DashboardStateError):
                    dashboard.validate_ledger(candidate)

    def test_artifact_paths_reject_workspace_external_and_protected_targets(
        self,
    ) -> None:
        self.assertEqual(
            dashboard.validate_artifact_path("docs/development-dashboard.md"),
            "docs/development-dashboard.md",
        )
        invalid = [
            "",
            "/etc/passwd",
            "C:/Windows/win.ini",
            r"\\server\share\file.txt",
            "https://example.invalid/file.txt",
            "../tracked.txt",
            "docs/../tracked.txt",
            r"docs\tracked.txt",
            "raw/source.md",
            "docs/raw/source.md",
            ".git/config",
            "docs/.git/config",
            ".llmwiki/state.json",
            "docs/.llmwiki/state.json",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(dashboard.DashboardStateError):
                    dashboard.validate_artifact_path(value)

    def test_builder_rejects_state_paths_outside_llmwiki(self) -> None:
        repo = init_repository(self.root)
        outside = self.root / "outside-progress.json"

        with self.assertRaises(dashboard.DashboardStateError):
            dashboard.DashboardSnapshotBuilder(repo, state_path=outside)
        with self.assertRaises(dashboard.DashboardStateError):
            dashboard.DashboardSnapshotBuilder(repo, state_path=Path("outside.json"))


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = init_repository(Path(self.temp_dir.name))
        self.clock = lambda: datetime(2026, 7, 17, 6, 30, tzinfo=timezone.utc)

    def test_snapshot_is_versioned_path_safe_read_only_and_aggregated(self) -> None:
        before = file_fingerprints(self.repo)
        state = fake_git_state(
            branch="task/j-03-dashboard-foundation",
            clean=False,
            tags={"checkpoint/j-03-dashboard-foundation": "a" * 40},
        )
        builder = dashboard.DashboardSnapshotBuilder(self.repo, clock=self.clock)

        with patch.object(dashboard, "collect_git_state", return_value=state):
            snapshot = builder.build(use_cache=False)

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["kind"], dashboard.SNAPSHOT_KIND)
        self.assertEqual(snapshot["generated_at"], "2026-07-17T06:30:00Z")
        self.assertEqual(snapshot["active_unit"]["task_id"], "J-03")
        self.assertEqual(snapshot["active_unit"]["unit_id"], "J-03A")
        self.assertEqual(snapshot["active_unit"]["status"], "in_progress")
        task = next(item for item in snapshot["tasks"] if item["id"] == "J-03")
        self.assertEqual(task["changes"]["file_count"], 1)
        self.assertEqual(task["changes"]["additions"], 120)
        self.assertEqual(snapshot["summary"]["checkpoint_count"], 1)
        self.assertNotIn(str(self.repo), json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(file_fingerprints(self.repo), before)

    def test_checkpoint_without_full_roadmap_completion_is_partial(self) -> None:
        state = fake_git_state(tags={"checkpoint/j-03-dashboard-foundation": "a" * 40})
        builder = dashboard.DashboardSnapshotBuilder(self.repo, clock=self.clock)

        with patch.object(dashboard, "collect_git_state", return_value=state):
            snapshot = builder.build(use_cache=False)

        task = next(item for item in snapshot["tasks"] if item["id"] == "J-03")
        self.assertEqual(task["status"], "partial")
        self.assertEqual(
            task["checkpoints"][0]["name"], "checkpoint/j-03-dashboard-foundation"
        )
        statuses = {item["id"]: item["status"] for item in snapshot["tasks"]}
        self.assertEqual(statuses["A-01"], "completed")
        self.assertEqual(statuses["B-01"], "partial")

    def test_snapshot_groups_catalog_records_checks_commits_checkpoints_and_artifacts(
        self,
    ) -> None:
        builder = dashboard.DashboardSnapshotBuilder(self.repo, clock=self.clock)
        ledger = {
            "schema_version": dashboard.SCHEMA_VERSION,
            "kind": dashboard.LEDGER_KIND,
            "records": [progress_record_fixture()],
            "unit_catalog": unit_catalog_fixture(),
        }
        dashboard.atomic_json(builder.state_path, dashboard.validate_ledger(ledger))
        state = fake_git_state(tags={"checkpoint/j-03-unit-visualization": "a" * 40})

        with patch.object(dashboard, "collect_git_state", return_value=state):
            snapshot = builder.build(use_cache=False)

        task = next(item for item in snapshot["tasks"] if item["id"] == "J-03")
        unit = next(item for item in task["units"] if item["unit_id"] == "J-03B")
        group = next(
            item for item in snapshot["unit_groups"] if item["task_id"] == "J-03"
        )
        self.assertEqual(snapshot["summary"]["unit_count"], 1)
        self.assertEqual(snapshot["summary"]["artifact_count"], 1)
        self.assertEqual(group["units"][0]["unit_id"], "J-03B")
        self.assertEqual(unit["records"][0]["record_id"], "j-03b-record")
        self.assertEqual(unit["checks"][0]["id"], "dashboard-tests")
        self.assertEqual(unit["explicit_commits"][0]["commit"], "a" * 40)
        self.assertEqual(unit["explicit_commits"][0]["sources"], ["catalog", "record"])
        self.assertEqual(unit["checkpoints"][0]["unit_id"], "J-03B")
        self.assertRegex(
            unit["artifacts"][0]["route"],
            r"^/api/artifacts/[0-9a-f]{64}$",
        )
        self.assertEqual(unit["status"], "completed")


class ArtifactPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = init_repository(Path(self.temp_dir.name))

    def commit_bytes(self, relative: str, content: bytes, subject: str) -> str:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        run_git(self.repo, "add", relative)
        run_git(self.repo, "commit", "-m", subject)
        return run_git(self.repo, "rev-parse", "HEAD")

    def test_opaque_route_serves_only_bounded_strict_utf8_from_explicit_commits(
        self,
    ) -> None:
        committed_body = "committed preview\n".encode()
        text_commit = self.commit_bytes(
            "docs/unit-preview.txt",
            committed_body,
            "feat(j-03): add committed preview",
        )
        (self.repo / "docs/unit-preview.txt").write_text(
            "workspace override\n", encoding="utf-8"
        )
        binary_commit = self.commit_bytes(
            "docs/non-utf8.bin",
            b"\xff\xfe",
            "feat(j-03): add non utf8 fixture",
        )
        large_commit = self.commit_bytes(
            "docs/too-large.txt",
            b"x" * (dashboard.MAX_ARTIFACT_BYTES + 1),
            "feat(j-03): add oversized fixture",
        )

        catalog = unit_catalog_fixture(
            commit=text_commit, path="docs/unit-preview.txt", checkpoints=[]
        )
        unit = catalog["units"][0]
        unit["commits"] = [text_commit, binary_commit, large_commit]
        unit["artifacts"] = [
            {
                "artifact_id": "preview",
                "label": "Preview",
                "description": "Committed UTF-8 preview.",
                "path": "docs/unit-preview.txt",
                "commit": text_commit,
            },
            {
                "artifact_id": "non-utf8",
                "label": "Binary",
                "description": "Encoding rejection fixture.",
                "path": "docs/non-utf8.bin",
                "commit": binary_commit,
            },
            {
                "artifact_id": "too-large",
                "label": "Large",
                "description": "Size rejection fixture.",
                "path": "docs/too-large.txt",
                "commit": large_commit,
            },
        ]
        builder = dashboard.DashboardSnapshotBuilder(self.repo)
        ledger = {
            "schema_version": dashboard.SCHEMA_VERSION,
            "kind": dashboard.LEDGER_KIND,
            "records": [],
            "unit_catalog": catalog,
        }
        dashboard.atomic_json(builder.state_path, dashboard.validate_ledger(ledger))
        snapshot = builder.build(use_cache=False)
        artifacts = {
            item["artifact_id"]: item
            for item in next(
                item for item in snapshot["units"] if item["unit_id"] == "J-03B"
            )["artifacts"]
        }

        server = dashboard.create_server(builder, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]

            def request(path: str) -> tuple[int, dict[str, str], bytes]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", path, headers={"Host": "127.0.0.1"})
                response = connection.getresponse()
                body = response.read()
                headers = {
                    name.casefold(): value for name, value in response.getheaders()
                }
                status = response.status
                connection.close()
                return status, headers, body

            status, headers, body = request(artifacts["preview"]["route"])
            self.assertEqual(status, 200)
            self.assertEqual(body, committed_body)
            self.assertNotIn(b"workspace override", body)
            self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
            self.assertEqual(headers["cache-control"], "no-store")

            status, _, body = request(artifacts["non-utf8"]["route"])
            self.assertEqual(status, 415)
            self.assertEqual(json.loads(body)["error"], "artifact_not_utf8")

            status, _, body = request(artifacts["too-large"]["route"])
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["error"], "artifact_too_large")

            for route in [
                artifacts["preview"]["route"] + "?path=tracked.txt",
                "/api/artifacts/" + "0" * 64,
            ]:
                with self.subTest(route=route):
                    status, _, body = request(route)
                    self.assertEqual(status, 404)
                    self.assertIn(
                        json.loads(body)["error"],
                        {"not_found", "artifact_not_found"},
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class HostValidationTests(unittest.TestCase):
    def test_accepts_only_well_formed_loopback_hosts(self) -> None:
        loopback_addresses = [
            (None, None, None, None, ("127.0.0.1", 0)),
            (None, None, None, None, ("::1", 0, 0, 0)),
        ]
        with patch.object(
            dashboard.socket, "getaddrinfo", return_value=loopback_addresses
        ):
            accepted = ["127.0.0.1", "127.0.0.1:8765", "localhost", "[::1]:8765"]
            for host in accepted:
                with self.subTest(host=host):
                    self.assertTrue(dashboard.request_host_allowed(host))

        rejected = [
            None,
            "",
            "0.0.0.0",
            "192.0.2.1",
            "example.com",
            "127.0.0.1@evil.example",
            "user@127.0.0.1",
            "127.0.0.1/path",
            "127.0.0.1\\path",
            "127.0.0.1?query",
            "127.0.0.1#fragment",
            "127.0.0.1:invalid",
            "127.0.0.1 8765",
        ]
        for host in rejected:
            with self.subTest(host=host):
                self.assertFalse(dashboard.request_host_allowed(host))

    def test_bind_host_rejects_non_loopback_interfaces(self) -> None:
        self.assertEqual(dashboard.validate_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(dashboard.validate_bind_host("::1"), "::1")
        for host in ["0.0.0.0", "::", "192.0.2.10", "example.com"]:
            with self.subTest(host=host):
                with self.assertRaises(dashboard.DashboardError):
                    dashboard.validate_bind_host(host)


class HttpDashboardTests(unittest.TestCase):
    class Builder:
        def build(self) -> dict[str, object]:
            return {
                "schema_version": 1,
                "kind": dashboard.SNAPSHOT_KIND,
                "summary": {"task_count": 1},
            }

    def setUp(self) -> None:
        self.server = dashboard.create_server(
            self.Builder(),
            asset_root=dashboard.DEFAULT_ASSET_ROOT,
            host="127.0.0.1",
            port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(
        self, method: str, path: str, *, host: str = "127.0.0.1"
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        headers = {name.casefold(): value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, headers, body

    def test_serves_fixed_assets_and_read_only_apis_with_security_headers(self) -> None:
        routes = [
            ("/", "text/html"),
            ("/assets/app.js", "text/javascript"),
            ("/assets/styles.css", "text/css"),
            ("/assets/favicon.svg", "image/svg+xml"),
            ("/api/health", "application/json"),
            ("/api/status", "application/json"),
        ]
        for path, content_type in routes:
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertIn(content_type, headers["content-type"])
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertIn("default-src 'self'", headers["content-security-policy"])

    def test_rejects_external_or_malformed_hosts(self) -> None:
        for host in ["0.0.0.0", "example.com", "127.0.0.1@evil.example"]:
            with self.subTest(host=host):
                status, _, body = self.request("GET", "/api/health", host=host)
                self.assertEqual(status, 421)
                self.assertEqual(json.loads(body)["error"], "misdirected_request")

    def test_write_methods_are_disabled(self) -> None:
        for method in ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
            with self.subTest(method=method):
                status, _, body = self.request(method, "/api/status")
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body)["error"], "read_only")

    def test_unknown_traversal_and_repository_paths_are_not_served(self) -> None:
        for path in [
            "/missing",
            "/../AGENTS.md",
            "/tools/development_dashboard.py",
            "/api/files?path=AGENTS.md",
        ]:
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body)["error"], "not_found")


class FrontendAssetTests(unittest.TestCase):
    def test_assets_are_self_contained_and_avoid_dynamic_html_injection(self) -> None:
        root = dashboard.DEFAULT_ASSET_ROOT
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        styles = (root / "styles.css").read_text(encoding="utf-8")

        for name, text in {"html": html, "script": script, "styles": styles}.items():
            with self.subTest(asset=name):
                self.assertNotRegex(text, r"https?://")
                self.assertNotIn("//cdn", text.casefold())
        for dangerous in ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval("]:
            self.assertNotIn(dangerous, script)
        for marker in [
            "function openUnit(",
            "unit-button",
            "artifact-viewer",
            "api\\/artifacts",
            "content.textContent",
        ]:
            self.assertIn(marker, script)
        for marker in [
            ".unit-button-grid",
            ".unit-detail",
            ".check-list",
            ".artifact-viewer",
        ]:
            self.assertIn(marker, styles)
        for element_id in [
            "summary-grid",
            "active-content",
            "milestone-rail",
            "task-search",
            "status-filter",
            "area-filter",
            "task-list",
            "task-detail",
            "validation-list",
            "checkpoint-list",
        ]:
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('src="/assets/app.js"', html)
        self.assertIn('href="/assets/styles.css"', html)


if __name__ == "__main__":
    unittest.main()
