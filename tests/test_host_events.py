from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import webbrowser

from tools import advisory_lock, host_events
from tools.project import main as project_main
from tools.project_registry import register_project
from tools.research_core import ResearchCoreService


class SequenceClock:
    def __init__(self, *moments: datetime) -> None:
        self._moments = list(moments)

    def __call__(self) -> datetime:
        if not self._moments:
            raise AssertionError("host-event clock was called more often than expected")
        return self._moments.pop(0)


class HostEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "assistant-workspace"
        self.knowledge = self.root / "knowledge"
        self.project = self.root / "research-project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "README.md").write_text(
            "# Host Event Study\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.project / "src" / "model.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
            newline="\n",
        )
        self.registration = register_project(
            self.workspace,
            self.project,
            project_id="host-event-study",
            knowledge_root=self.knowledge,
        )
        self.layout = self.registration.layout

    @staticmethod
    def canonical_json_line(value: object) -> bytes:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )

    def ledger_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.layout.events_file.read_text(encoding="utf-8").splitlines()
        ]

    def queue_payload(self) -> dict[str, object]:
        value = json.loads(self.layout.dirty_paths_file.read_text(encoding="utf-8"))
        self.assertIsInstance(value, dict)
        return value

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = project_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def source_snapshot(self) -> tuple[tuple[str, ...], dict[str, tuple[str, int, int, int]]]:
        directories: list[str] = []
        files: dict[str, tuple[str, int, int, int]] = {}
        for directory, dirnames, filenames in os.walk(self.project):
            dirnames.sort()
            filenames.sort()
            base = Path(directory)
            for dirname in dirnames:
                path = base / dirname
                if not path.is_symlink():
                    directories.append(path.relative_to(self.project).as_posix())
            for filename in filenames:
                path = base / filename
                if path.is_symlink():
                    continue
                stat = path.stat()
                files[path.relative_to(self.project).as_posix()] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_mode,
                )
        return tuple(sorted(directories)), files

    def submit(
        self,
        *,
        event_id: str,
        producer: str = "codex",
        occurred_at: str = "2026-07-16T08:00:00Z",
        operation: str = "modified",
        paths: object = ("src/model.py",),
        clock: object | None = None,
    ):
        return host_events.submit_host_event(
            self.workspace,
            self.registration.project_id,
            event_id=event_id,
            producer=producer,
            occurred_at=occurred_at,
            operation=operation,
            paths=paths,
            clock=clock,
        )

    def test_layout_append_only_ledger_and_exact_closed_schemas(self) -> None:
        self.assertEqual(
            self.layout.events_file,
            self.layout.machine_root / "events.jsonl",
        )
        self.assertEqual(
            self.layout.dirty_paths_file,
            self.layout.indexes_dir / "dirty-paths.json",
        )

        first_clock = SequenceClock(datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc))
        first = self.submit(
            event_id="evt-001",
            occurred_at="2026-07-16T08:30:00+08:00",
            paths=(r"src\model.py", "README.md", "src/model.py"),
            clock=first_clock,
        )
        first_event = {
            "schema_version": 1,
            "kind": "llmwiki-host-event",
            "event_version": "host-event-v1",
            "record_type": "dirty-path-event",
            "project_id": self.registration.project_id,
            "sequence": 1,
            "event_id": "evt-001",
            "producer": "codex",
            "occurred_at": "2026-07-16T00:30:00.000000Z",
            "ingested_at": "2026-07-16T09:00:00.000000Z",
            "operation": "modified",
            "paths": ["README.md", "src/model.py"],
        }
        first_ledger = self.canonical_json_line(first_event)
        self.assertEqual(self.layout.events_file.read_bytes(), first_ledger)

        first_queue = {
            "schema_version": 1,
            "kind": "llmwiki-dirty-path-queue",
            "queue_version": "dirty-paths-v1",
            "project_id": self.registration.project_id,
            "ledger_event_count": 1,
            "ledger_last_sequence": 1,
            "ledger_sha256": hashlib.sha256(first_ledger).hexdigest(),
            "dirty_path_count": 2,
            "dirty_paths": [
                {
                    "path": "README.md",
                    "first_sequence": 1,
                    "last_sequence": 1,
                    "event_count": 1,
                    "operations": ["modified"],
                    "producers": ["codex"],
                },
                {
                    "path": "src/model.py",
                    "first_sequence": 1,
                    "last_sequence": 1,
                    "event_count": 1,
                    "operations": ["modified"],
                    "producers": ["codex"],
                },
            ],
        }
        self.assertEqual(self.queue_payload(), first_queue)
        self.assertEqual(
            self.layout.dirty_paths_file.read_bytes(),
            self.canonical_json_line(first_queue),
        )
        self.assertEqual(
            first.as_dict(),
            {
                "schema_version": 1,
                "kind": "llmwiki-host-event-submit-result",
                "project_id": self.registration.project_id,
                "event_id": "evt-001",
                "sequence": 1,
                "disposition": "appended",
                "ledger_relative_path": "events.jsonl",
                "queue_relative_path": "indexes/dirty-paths.json",
                "projection_updated": True,
                "queue": first_queue,
            },
        )

        second_clock = SequenceClock(datetime(2026, 7, 16, 9, 5, tzinfo=timezone.utc))
        second = self.submit(
            event_id="evt-002",
            producer="claude-code",
            occurred_at="2026-07-16T01:15:00Z",
            operation="moved",
            paths=("src/model_v2.py", "src/model.py"),
            clock=second_clock,
        )
        second_event = {
            "schema_version": 1,
            "kind": "llmwiki-host-event",
            "event_version": "host-event-v1",
            "record_type": "dirty-path-event",
            "project_id": self.registration.project_id,
            "sequence": 2,
            "event_id": "evt-002",
            "producer": "claude-code",
            "occurred_at": "2026-07-16T01:15:00.000000Z",
            "ingested_at": "2026-07-16T09:05:00.000000Z",
            "operation": "moved",
            "paths": ["src/model.py", "src/model_v2.py"],
        }
        second_ledger = first_ledger + self.canonical_json_line(second_event)
        ledger_after = self.layout.events_file.read_bytes()
        self.assertTrue(ledger_after.startswith(first_ledger))
        self.assertEqual(ledger_after, second_ledger)
        self.assertEqual(second.sequence, 2)

        second_queue = {
            "schema_version": 1,
            "kind": "llmwiki-dirty-path-queue",
            "queue_version": "dirty-paths-v1",
            "project_id": self.registration.project_id,
            "ledger_event_count": 2,
            "ledger_last_sequence": 2,
            "ledger_sha256": hashlib.sha256(second_ledger).hexdigest(),
            "dirty_path_count": 3,
            "dirty_paths": [
                {
                    "path": "README.md",
                    "first_sequence": 1,
                    "last_sequence": 1,
                    "event_count": 1,
                    "operations": ["modified"],
                    "producers": ["codex"],
                },
                {
                    "path": "src/model.py",
                    "first_sequence": 1,
                    "last_sequence": 2,
                    "event_count": 2,
                    "operations": ["modified", "moved"],
                    "producers": ["claude-code", "codex"],
                },
                {
                    "path": "src/model_v2.py",
                    "first_sequence": 2,
                    "last_sequence": 2,
                    "event_count": 1,
                    "operations": ["moved"],
                    "producers": ["claude-code"],
                },
            ],
        }
        self.assertEqual(self.queue_payload(), second_queue)
        loaded = host_events.load_dirty_path_queue(
            self.workspace,
            self.registration.project_id,
        )
        self.assertFalse(loaded.projection_rebuilt)
        self.assertEqual(loaded.queue.as_dict(), second_queue)

    def test_duplicate_is_canonical_idempotency_and_changed_payload_collides(self) -> None:
        original = self.submit(
            event_id="same-id",
            occurred_at="2026-07-16T08:00:00+08:00",
            paths=("src/model.py", "README.md"),
            clock=SequenceClock(datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)),
        )
        ledger_before = self.layout.events_file.read_bytes()
        queue_before = self.layout.dirty_paths_file.read_bytes()

        def clock_must_not_run() -> datetime:
            raise AssertionError("idempotent duplicate unexpectedly consumed the clock")

        duplicate = self.submit(
            event_id="same-id",
            occurred_at="2026-07-16T00:00:00Z",
            paths=(r"README.md", r"src\model.py", "README.md"),
            clock=clock_must_not_run,
        )
        self.assertEqual(duplicate.disposition, "duplicate")
        self.assertEqual(duplicate.sequence, original.sequence)
        self.assertFalse(duplicate.projection_updated)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)
        self.assertEqual(self.layout.dirty_paths_file.read_bytes(), queue_before)

        collisions = (
            {"producer": "claude-code"},
            {"occurred_at": "2026-07-16T00:00:01Z"},
            {"operation": "deleted"},
            {"paths": ("src/other.py",)},
        )
        baseline = {
            "producer": "codex",
            "occurred_at": "2026-07-16T00:00:00Z",
            "operation": "modified",
            "paths": ("README.md", "src/model.py"),
        }
        for changed in collisions:
            with self.subTest(changed=changed):
                values = {**baseline, **changed}
                with self.assertRaises(host_events.HostEventConflictError) as caught:
                    self.submit(
                        event_id="same-id",
                        producer=values["producer"],
                        occurred_at=values["occurred_at"],
                        operation=values["operation"],
                        paths=values["paths"],
                        clock=clock_must_not_run,
                    )
                self.assertEqual(caught.exception.reason_code, "host-event-conflict")
                self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)
                self.assertEqual(self.layout.dirty_paths_file.read_bytes(), queue_before)

    def test_out_of_order_occurrence_keeps_contiguous_ingestion_sequence(self) -> None:
        self.assertEqual(
            host_events.HOST_EVENT_OPERATIONS,
            ("created", "modified", "deleted", "moved", "unknown"),
        )
        occurred = (
            "2026-07-16T12:00:00Z",
            "2026-07-16T08:00:00Z",
            "2026-07-16T10:00:00Z",
            "2026-07-16T07:00:00Z",
            "2026-07-16T11:00:00Z",
        )
        clock = SequenceClock(
            *(
                datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)
                + timedelta(seconds=index)
                for index in range(5)
            )
        )
        for index, (operation, timestamp) in enumerate(
            zip(host_events.HOST_EVENT_OPERATIONS, occurred, strict=True),
            start=1,
        ):
            paths = (
                (f"old/path-{index}.txt", f"new/path-{index}.txt")
                if operation == "moved"
                else (f"path-{index}.txt",)
            )
            result = self.submit(
                event_id=f"evt-{index}",
                occurred_at=timestamp,
                operation=operation,
                paths=paths,
                clock=clock,
            )
            self.assertEqual(result.sequence, index)

        rows = self.ledger_rows()
        self.assertEqual([row["sequence"] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual(
            [row["operation"] for row in rows],
            list(host_events.HOST_EVENT_OPERATIONS),
        )
        self.assertEqual(
            [row["occurred_at"] for row in rows],
            [timestamp.replace("Z", ".000000Z") for timestamp in occurred],
        )
        self.assertNotEqual(
            [row["occurred_at"] for row in rows],
            sorted(row["occurred_at"] for row in rows),
        )
        self.assertEqual(
            [row["ingested_at"] for row in rows],
            sorted(row["ingested_at"] for row in rows),
        )
        self.assertEqual(
            self.queue_payload()["ledger_last_sequence"],
            len(rows),
        )

        state_before = (
            self.layout.events_file.read_bytes(),
            self.layout.dirty_paths_file.read_bytes(),
        )
        with self.assertRaises(host_events.HostEventError):
            self.submit(event_id="bad-operation", operation="renamed")
        self.assertEqual(
            state_before,
            (
                self.layout.events_file.read_bytes(),
                self.layout.dirty_paths_file.read_bytes(),
            ),
        )

    def test_paths_are_normalized_and_unsafe_or_protected_paths_are_rejected(self) -> None:
        invalid_paths: tuple[object, ...] = (
            "",
            ".",
            "..",
            "../escape.txt",
            "nested/../../escape.txt",
            "./README.md",
            "src/./model.py",
            "/etc/passwd",
            r"C:\Windows\system.ini",
            r"\\server\share\file.txt",
            ".git/config",
            ".Git/config",
            ".hg/store",
            ".svn/entries",
            ".llmwiki/project.yaml",
            "control" + chr(0) + "name.txt",
            self.project / "README.md",
        )
        for index, path in enumerate(invalid_paths):
            with self.subTest(path=repr(path)):
                with self.assertRaises(host_events.HostEventError):
                    self.submit(event_id=f"unsafe-{index}", paths=(path,))
                self.assertFalse(self.layout.events_file.exists())
                self.assertFalse(self.layout.dirty_paths_file.exists())

        normalized = self.submit(
            event_id="normalized",
            paths=(
                r"nested\folder\note.txt",
                "nested//folder/note.txt",
                "cafe" + chr(0x0301) + ".txt",
                "café.txt",
            ),
            clock=SequenceClock(datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)),
        )
        self.assertEqual(normalized.queue.dirty_path_count, 2)
        self.assertEqual(
            self.ledger_rows()[0]["paths"],
            ["café.txt", "nested/folder/note.txt"],
        )

    def test_projection_rebuilds_after_deletion_and_current_schema_tampering(self) -> None:
        self.submit(
            event_id="evt-a",
            operation="created",
            paths=("a.txt", "shared.txt"),
            clock=SequenceClock(datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)),
        )
        self.submit(
            event_id="evt-b",
            producer="claude-code",
            operation="deleted",
            paths=("b.txt", "shared.txt"),
            clock=SequenceClock(datetime(2026, 7, 16, 9, 1, tzinfo=timezone.utc)),
        )
        expected_bytes = self.layout.dirty_paths_file.read_bytes()
        expected_queue = self.queue_payload()
        ledger_before = self.layout.events_file.read_bytes()

        self.layout.dirty_paths_file.unlink()
        rebuilt = host_events.load_dirty_path_queue(
            self.workspace,
            self.registration.project_id,
        )
        self.assertTrue(rebuilt.projection_rebuilt)
        self.assertEqual(rebuilt.queue.as_dict(), expected_queue)
        self.assertEqual(self.layout.dirty_paths_file.read_bytes(), expected_bytes)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

        tampered = dict(expected_queue)
        tampered["ledger_sha256"] = "0" * 64
        self.layout.dirty_paths_file.write_bytes(self.canonical_json_line(tampered))
        repaired = host_events.load_dirty_path_queue(
            self.workspace,
            self.registration.project_id,
        )
        self.assertTrue(repaired.projection_rebuilt)
        self.assertEqual(repaired.queue.as_dict(), expected_queue)
        self.assertEqual(self.layout.dirty_paths_file.read_bytes(), expected_bytes)

        self.layout.dirty_paths_file.write_bytes(b"{not-json\n")
        repaired_corruption = host_events.load_dirty_path_queue(
            self.workspace,
            self.registration.project_id,
        )
        self.assertTrue(repaired_corruption.projection_rebuilt)
        self.assertEqual(self.layout.dirty_paths_file.read_bytes(), expected_bytes)
        stable = host_events.load_dirty_path_queue(
            self.workspace,
            self.registration.project_id,
        )
        self.assertFalse(stable.projection_rebuilt)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_before)

    def test_locked_ledger_and_state_snapshot_share_a_consistent_projection(
        self,
    ) -> None:
        self.submit(
            event_id="evt-a",
            operation="created",
            paths=("README.md", "src/model.py"),
            clock=SequenceClock(datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)),
        )
        self.submit(
            event_id="evt-b",
            producer="claude-code",
            operation="deleted",
            paths=("src/model.py", "src/old.py"),
            clock=SequenceClock(datetime(2026, 7, 16, 9, 1, tzinfo=timezone.utc)),
        )
        ledger_bytes = self.layout.events_file.read_bytes()
        event_lock = self.layout.events_file.with_name(
            self.layout.events_file.name + ".lock"
        )
        stable_lock_stat = event_lock.stat()

        with host_events.locked_host_event_ledger(
            self.workspace,
            self.registration.project_id,
        ) as locked:
            self.assertTrue(event_lock.is_file())
            self.assertEqual(locked.project_id, self.registration.project_id)
            self.assertEqual(locked.event_count, 2)
            self.assertEqual(locked.last_sequence, 2)
            self.assertEqual(locked.serialized_bytes, ledger_bytes)
            self.assertEqual(
                locked.ledger_sha256,
                hashlib.sha256(ledger_bytes).hexdigest(),
            )
        self.assertTrue(event_lock.is_file())
        self.assertTrue(os.path.samestat(stable_lock_stat, event_lock.stat()))

        real_write = host_events._write_queue_atomic
        observed_writes: list[tuple[Path, host_events.DirtyPathQueue]] = []

        def write_while_locked(
            queue_file: Path,
            queue: host_events.DirtyPathQueue,
        ) -> bool:
            self.assertTrue(event_lock.is_file())
            observed_writes.append((queue_file, queue))
            return real_write(queue_file, queue)

        with patch.object(
            host_events,
            "_write_queue_atomic",
            side_effect=write_while_locked,
        ):
            state = host_events.snapshot_host_event_state(
                self.workspace,
                self.registration.project_id,
            )

        self.assertTrue(event_lock.is_file())
        self.assertTrue(os.path.samestat(stable_lock_stat, event_lock.stat()))
        self.assertFalse(state.projection_rebuilt)
        self.assertEqual(state.ledger, locked)
        self.assertEqual(
            state.queue,
            host_events.DirtyPathQueue.from_ledger(state.ledger),
        )
        self.assertEqual(
            observed_writes,
            [(self.layout.dirty_paths_file, state.queue)],
        )
        self.assertEqual(
            (
                state.queue.project_id,
                state.queue.ledger_event_count,
                state.queue.ledger_last_sequence,
                state.queue.ledger_sha256,
            ),
            (
                state.ledger.project_id,
                state.ledger.event_count,
                state.ledger.last_sequence,
                state.ledger.ledger_sha256,
            ),
        )

    def test_advisory_lock_errors_preserve_the_host_event_error_contract(
        self,
    ) -> None:
        event_lock = self.layout.events_file.with_name(
            self.layout.events_file.name + ".lock"
        )
        event_lock.mkdir()

        with self.assertRaises(host_events.HostEventLockError) as raised:
            self.submit(event_id="evt-lock-error")

        self.assertEqual(raised.exception.reason_code, "host-event-lock-failed")
        self.assertFalse(self.layout.events_file.exists())
        self.assertFalse(self.layout.dirty_paths_file.exists())

    def test_release_cleanup_does_not_mask_a_successfully_committed_event(
        self,
    ) -> None:
        event_lock = self.layout.events_file.with_name(
            self.layout.events_file.name + ".lock"
        )
        with patch.object(
            advisory_lock,
            "_unlock_descriptor",
            side_effect=OSError("injected host-event unlock failure"),
        ) as unlock:
            committed = self.submit(
                event_id="evt-release-cleanup",
                clock=SequenceClock(
                    datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
                ),
            )

        self.assertEqual(committed.disposition, "appended")
        self.assertEqual(committed.sequence, 1)
        self.assertEqual(
            [row["event_id"] for row in self.ledger_rows()],
            ["evt-release-cleanup"],
        )
        self.assertEqual(committed.queue.ledger_event_count, 1)
        self.assertTrue(event_lock.is_file())
        unlock.assert_called_once()

        duplicate = self.submit(event_id="evt-release-cleanup")
        self.assertEqual(duplicate.disposition, "duplicate")
        self.assertEqual(duplicate.sequence, committed.sequence)

    def test_state_snapshot_repairs_malformed_and_stale_dirty_path_projection(
        self,
    ) -> None:
        self.submit(
            event_id="evt-repair",
            operation="moved",
            paths=("src/model.py", "src/model-v2.py"),
            clock=SequenceClock(datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)),
        )
        expected = host_events.snapshot_host_event_state(
            self.workspace,
            self.registration.project_id,
        )
        self.assertFalse(expected.projection_rebuilt)
        expected_ledger_bytes = self.layout.events_file.read_bytes()
        expected_queue_bytes = expected.queue.serialized_bytes()
        stale_queue = host_events.DirtyPathQueue(
            project_id=self.registration.project_id,
            ledger_event_count=0,
            ledger_last_sequence=0,
            ledger_sha256=hashlib.sha256(b"").hexdigest(),
            dirty_paths=(),
        )

        for label, payload in (
            ("malformed", b"{not-json\n"),
            ("stale", stale_queue.serialized_bytes()),
        ):
            with self.subTest(projection=label):
                self.layout.dirty_paths_file.write_bytes(payload)
                repaired = host_events.snapshot_host_event_state(
                    self.workspace,
                    self.registration.project_id,
                )

                self.assertTrue(repaired.projection_rebuilt)
                self.assertEqual(repaired.ledger, expected.ledger)
                self.assertEqual(repaired.queue, expected.queue)
                self.assertEqual(
                    self.layout.dirty_paths_file.read_bytes(),
                    expected_queue_bytes,
                )
                self.assertEqual(
                    self.layout.events_file.read_bytes(),
                    expected_ledger_bytes,
                )

                stable = host_events.snapshot_host_event_state(
                    self.workspace,
                    self.registration.project_id,
                )
                self.assertFalse(stable.projection_rebuilt)
                self.assertEqual(stable.ledger, repaired.ledger)
                self.assertEqual(stable.queue, repaired.queue)

    def test_legacy_and_future_ledger_or_queue_state_fail_closed(self) -> None:
        self.submit(
            event_id="evt-schema",
            clock=SequenceClock(datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)),
        )
        valid_event = self.ledger_rows()[0]
        valid_ledger = self.layout.events_file.read_bytes()
        valid_queue = self.layout.dirty_paths_file.read_bytes()

        for label, mutation in (
            (
                "legacy",
                {key: value for key, value in valid_event.items() if key != "schema_version"},
            ),
            ("future", {**valid_event, "schema_version": 2}),
        ):
            with self.subTest(state="ledger", version=label):
                payload = self.canonical_json_line(mutation)
                self.layout.events_file.write_bytes(payload)
                with self.assertRaises(host_events.HostEventStateError):
                    host_events.load_dirty_path_queue(
                        self.workspace,
                        self.registration.project_id,
                    )
                with self.assertRaises(host_events.HostEventStateError):
                    self.submit(event_id=f"after-{label}")
                self.assertEqual(self.layout.events_file.read_bytes(), payload)
                self.assertEqual(self.layout.dirty_paths_file.read_bytes(), valid_queue)
                self.layout.events_file.write_bytes(valid_ledger)

        valid_queue_payload = json.loads(valid_queue)
        for label, mutation in (
            (
                "legacy",
                {
                    key: value
                    for key, value in valid_queue_payload.items()
                    if key != "schema_version"
                },
            ),
            ("future", {**valid_queue_payload, "schema_version": 2}),
        ):
            with self.subTest(state="queue", version=label):
                payload = self.canonical_json_line(mutation)
                self.layout.dirty_paths_file.write_bytes(payload)
                with self.assertRaises(host_events.DirtyPathQueueError):
                    host_events.load_dirty_path_queue(
                        self.workspace,
                        self.registration.project_id,
                    )
                self.assertEqual(self.layout.events_file.read_bytes(), valid_ledger)
                self.assertEqual(self.layout.dirty_paths_file.read_bytes(), payload)
                self.layout.dirty_paths_file.write_bytes(valid_queue)

    def test_queue_write_failure_leaves_ledger_recoverable_on_idempotent_retry(self) -> None:
        real_replace = os.replace

        def fail_queue_replace(source: object, destination: object) -> None:
            if Path(destination).resolve() == self.layout.dirty_paths_file:
                raise OSError("injected dirty queue replace failure")
            real_replace(source, destination)

        with patch.object(host_events.os, "replace", side_effect=fail_queue_replace):
            with self.assertRaises(host_events.DirtyPathQueueError):
                self.submit(
                    event_id="evt-retry",
                    paths=("retry.txt",),
                    clock=SequenceClock(
                        datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
                    ),
                )

        ledger_after_failure = self.layout.events_file.read_bytes()
        self.assertEqual(len(ledger_after_failure.splitlines()), 1)
        self.assertEqual(self.ledger_rows()[0]["sequence"], 1)
        self.assertFalse(self.layout.dirty_paths_file.exists())

        def retry_clock_must_not_run() -> datetime:
            raise AssertionError("idempotent recovery retry unexpectedly used the clock")

        recovered = self.submit(
            event_id="evt-retry",
            paths=("retry.txt",),
            clock=retry_clock_must_not_run,
        )
        self.assertEqual(recovered.disposition, "duplicate")
        self.assertEqual(recovered.sequence, 1)
        self.assertTrue(recovered.projection_updated)
        self.assertEqual(self.layout.events_file.read_bytes(), ledger_after_failure)
        self.assertTrue(self.layout.dirty_paths_file.is_file())
        self.assertEqual(self.queue_payload()["ledger_event_count"], 1)
        self.assertEqual(
            list(self.layout.indexes_dir.glob(".dirty-paths.json.*.tmp")),
            [],
        )

    def test_research_core_and_event_cli_share_the_same_path_free_contract(self) -> None:
        service = ResearchCoreService(self.workspace)
        direct = service.host_event_submit(
            self.registration.project_id,
            event_id="evt-core",
            producer="codex",
            occurred_at="2026-07-16T08:00:00Z",
            operation="modified",
            paths=("src/model.py",),
        )
        self.assertEqual(direct.disposition, "appended")

        code, stdout, stderr = self.invoke_cli(
            [
                "event",
                "submit",
                self.registration.project_id,
                "--event-id",
                "evt-cli",
                "--producer",
                "claude-code",
                "--occurred-at",
                "2026-07-16T07:00:00Z",
                "--operation",
                "deleted",
                "--path",
                "README.md",
                "--path",
                r"src\model.py",
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        submitted_payload = json.loads(stdout)
        self.assertTrue(submitted_payload.pop("ok"))
        self.assertEqual(submitted_payload["kind"], "llmwiki-host-event-submit-result")
        self.assertEqual(submitted_payload["sequence"], 2)
        self.assertEqual(submitted_payload["disposition"], "appended")
        self.assertNotIn(str(self.workspace), json.dumps(submitted_payload))
        self.assertNotIn(str(self.project), json.dumps(submitted_payload))

        code, stdout, stderr = self.invoke_cli(
            [
                "event",
                "show",
                self.registration.project_id,
                "--workspace-root",
                str(self.workspace),
                "--json",
            ]
        )
        self.assertEqual((code, stderr), (0, ""))
        shown_payload = json.loads(stdout)
        self.assertTrue(shown_payload.pop("ok"))
        direct_queue = service.dirty_path_queue(self.registration.project_id)
        function_queue = host_events.load_dirty_path_queue(
            self.workspace,
            self.registration.project_id,
        )
        self.assertEqual(shown_payload, direct_queue.as_dict())
        self.assertEqual(shown_payload, function_queue.as_dict())
        self.assertEqual(shown_payload["queue"]["ledger_event_count"], 2)
        self.assertEqual(shown_payload["queue"]["ledger_last_sequence"], 2)

    def test_events_are_source_read_only_create_no_knowledge_and_use_no_external_io(self) -> None:
        source_before = self.source_snapshot()
        knowledge_files_before = sorted(
            path.relative_to(self.layout.knowledge_root).as_posix()
            for path in self.layout.knowledge_root.rglob("*")
            if path.is_file()
        )
        machine_files_before = {
            path.relative_to(self.layout.machine_root).as_posix()
            for path in self.layout.machine_root.rglob("*")
            if path.is_file()
        }

        with ExitStack() as stack:
            guards = (
                stack.enter_context(
                    patch(
                        "tools._utils.call_llm",
                        side_effect=AssertionError("H-04 attempted an LLM call"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        socket,
                        "create_connection",
                        side_effect=AssertionError("H-04 attempted network access"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        urllib.request,
                        "urlopen",
                        side_effect=AssertionError("H-04 attempted URL access"),
                    )
                ),
                stack.enter_context(
                    patch.object(
                        webbrowser,
                        "open",
                        side_effect=AssertionError("H-04 attempted browser behavior"),
                    )
                ),
            )
            service = ResearchCoreService(self.workspace)
            service.host_event_submit(
                self.registration.project_id,
                event_id="evt-local-only",
                producer="codex",
                occurred_at="2026-07-16T08:00:00Z",
                operation="unknown",
                paths=("src/model.py", "deleted/missing.txt"),
            )
            service.dirty_path_queue(self.registration.project_id)

        for guard in guards:
            guard.assert_not_called()
        self.assertEqual(self.source_snapshot(), source_before)
        knowledge_files_after = sorted(
            path.relative_to(self.layout.knowledge_root).as_posix()
            for path in self.layout.knowledge_root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(knowledge_files_after, knowledge_files_before)
        machine_files_after = {
            path.relative_to(self.layout.machine_root).as_posix()
            for path in self.layout.machine_root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(
            machine_files_after - machine_files_before,
            {"events.jsonl", "events.jsonl.lock", "indexes/dirty-paths.json"},
        )
        self.assertFalse(self.layout.manifest_file.exists())
        self.assertFalse(self.layout.sources_file.exists())
        self.assertFalse(self.layout.evidence_file.exists())
        self.assertFalse(self.layout.overview_file.exists())
        self.assertEqual(
            list(self.layout.machine_root.rglob("*.lock")),
            [self.layout.machine_root / "events.jsonl.lock"],
        )
        self.assertEqual(list(self.layout.machine_root.rglob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
