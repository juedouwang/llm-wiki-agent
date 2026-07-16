from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tools import advisory_lock


class AdvisoryFileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.lock_file = self.root / "state.lock"

    def test_lock_file_is_stable_across_releases_and_reacquisition(self) -> None:
        self.assertFalse(self.lock_file.exists())

        with advisory_lock.advisory_file_lock(self.lock_file):
            held = os.stat(self.lock_file, follow_symlinks=False)
            self.assertTrue(stat.S_ISREG(held.st_mode))

        released = os.stat(self.lock_file, follow_symlinks=False)
        self.assertTrue(os.path.samestat(held, released))

        with advisory_lock.advisory_file_lock(self.lock_file):
            reacquired = os.stat(self.lock_file, follow_symlinks=False)
            self.assertTrue(os.path.samestat(held, reacquired))

        final = os.stat(self.lock_file, follow_symlinks=False)
        self.assertTrue(os.path.samestat(held, final))

    def test_same_thread_reentrancy_uses_the_existing_lock(self) -> None:
        lock = advisory_lock.AdvisoryFileLock(
            self.lock_file,
            timeout_seconds=0.1,
        )

        with lock:
            with lock:
                with advisory_lock.advisory_file_lock(
                    self.lock_file,
                    timeout_seconds=0.1,
                ):
                    self.assertTrue(self.lock_file.is_file())

        self.assertTrue(self.lock_file.is_file())

    def test_different_threads_in_one_process_contend_for_the_lock(self) -> None:
        started = threading.Event()
        acquired = threading.Event()
        errors: list[BaseException] = []

        def worker() -> None:
            started.set()
            try:
                with advisory_lock.advisory_file_lock(
                    self.lock_file,
                    timeout_seconds=1.0,
                ):
                    acquired.set()
            except BaseException as exc:  # pragma: no cover - assertion below
                errors.append(exc)

        thread = threading.Thread(target=worker)
        with advisory_lock.advisory_file_lock(self.lock_file):
            thread.start()
            self.assertTrue(started.wait(timeout=1.0))
            self.assertFalse(acquired.wait(timeout=0.1))

        self.assertTrue(acquired.wait(timeout=1.0))
        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_thread_contention_times_out_within_a_bounded_interval(self) -> None:
        timeout_seconds = 0.1
        started = threading.Event()
        elapsed: list[float] = []
        errors: list[BaseException] = []

        def worker() -> None:
            started.set()
            began = time.monotonic()
            try:
                with advisory_lock.advisory_file_lock(
                    self.lock_file,
                    timeout_seconds=timeout_seconds,
                ):
                    errors.append(AssertionError("contended lock was acquired"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                elapsed.append(time.monotonic() - began)

        thread = threading.Thread(target=worker)
        with advisory_lock.advisory_file_lock(self.lock_file):
            thread.start()
            self.assertTrue(started.wait(timeout=1.0))
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], advisory_lock.AdvisoryLockTimeoutError)
        self.assertEqual(len(elapsed), 1)
        self.assertGreaterEqual(elapsed[0], timeout_seconds * 0.75)
        self.assertLess(elapsed[0], timeout_seconds + 1.0)

    def test_cross_process_contention_times_out_then_acquires_after_release(
        self,
    ) -> None:
        child_code = r"""
import sys
from pathlib import Path

from tools.advisory_lock import advisory_file_lock

lock_file = Path(sys.argv[1])
with advisory_file_lock(lock_file, timeout_seconds=2.0):
    print("READY", flush=True)
    if sys.stdin.readline().strip() != "RELEASE":
        raise RuntimeError("parent did not signal release")
"""
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", child_code, str(self.lock_file)],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertIsNotNone(process.stdout)
        ready_lines: queue.Queue[str] = queue.Queue(maxsize=1)

        def read_ready_line() -> None:
            assert process.stdout is not None
            ready_lines.put(process.stdout.readline())

        reader = threading.Thread(target=read_ready_line, daemon=True)
        reader.start()
        try:
            try:
                ready = ready_lines.get(timeout=5.0)
            except queue.Empty:
                process.kill()
                _, stderr = process.communicate(timeout=5.0)
                self.fail(f"lock-holder subprocess did not become ready: {stderr}")

            if ready.strip() != "READY":
                _, stderr = process.communicate(timeout=5.0)
                self.fail(
                    "lock-holder subprocess failed before readiness: "
                    f"stdout={ready!r}, stderr={stderr!r}"
                )

            with self.assertRaises(advisory_lock.AdvisoryLockTimeoutError):
                with advisory_lock.advisory_file_lock(
                    self.lock_file,
                    timeout_seconds=0.15,
                ):
                    self.fail("cross-process contender acquired a held lock")

            _, stderr = process.communicate("RELEASE\n", timeout=5.0)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5.0)

        with advisory_lock.advisory_file_lock(
            self.lock_file,
            timeout_seconds=1.0,
        ):
            self.assertTrue(self.lock_file.is_file())
        self.assertTrue(self.lock_file.is_file())

    def test_symbolic_link_and_non_regular_lock_paths_are_rejected(self) -> None:
        directory_lock = self.root / "directory.lock"
        directory_lock.mkdir()
        with self.assertRaises(advisory_lock.AdvisoryLockError):
            with advisory_lock.advisory_file_lock(directory_lock):
                self.fail("non-regular lock path was accepted")

        target = self.root / "target.lock"
        target.write_bytes(b"\0")
        symlink_lock = self.root / "symlink.lock"
        try:
            symlink_lock.symlink_to(target)
        except (NotImplementedError, OSError):
            return
        with self.assertRaises(advisory_lock.AdvisoryLockError):
            with advisory_lock.advisory_file_lock(symlink_lock):
                self.fail("symbolic-link lock path was accepted")

    def test_release_cleanup_error_does_not_escape_or_leave_the_lock_busy(self) -> None:
        committed: list[str] = []
        with patch.object(
            advisory_lock,
            "_unlock_descriptor",
            side_effect=OSError("injected unlock failure"),
        ) as unlock:
            with advisory_lock.advisory_file_lock(self.lock_file):
                committed.append("done")

        self.assertEqual(committed, ["done"])
        unlock.assert_called_once()
        self.assertTrue(self.lock_file.is_file())
        with advisory_lock.advisory_file_lock(
            self.lock_file,
            timeout_seconds=0.2,
        ):
            pass


if __name__ == "__main__":
    unittest.main()
