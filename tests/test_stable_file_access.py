from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import stable_file_access as stable_file_access_module
from tools.stable_file_access import (
    StableFileAccessError,
    StableFileBoundaryError,
    StableFileChangedError,
    StableFileCommitUnknownError,
    StableFileLockTimeoutError,
    StableFileMissingError,
    StableFileRedirectionError,
    StableFileTypeError,
    StableFileVerificationUnavailableError,
    exclusive_stable_file_lock,
    lease_stable_regular_file,
    read_stable_regular_file,
    write_atomic_stable_file,
)


class StableFileAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.trusted = self.root / "trusted"
        self.trusted.mkdir()
        self.source = self.trusted / "source.txt"
        self.payload = b"descriptor-bound bytes\n"
        self.source.write_bytes(self.payload)

    def test_stable_read_returns_exact_bytes_and_digest(self) -> None:
        result = read_stable_regular_file(
            self.trusted,
            self.source,
            reject_redirection=False,
            capture_bytes=True,
        )

        self.assertEqual(result.data, self.payload)
        self.assertEqual(result.size_bytes, len(self.payload))
        self.assertEqual(
            result.content_sha256,
            hashlib.sha256(self.payload).hexdigest(),
        )
        self.assertTrue(result.final_path.is_absolute())

        hash_only = read_stable_regular_file(
            self.trusted,
            self.source,
            reject_redirection=True,
            capture_bytes=False,
        )
        self.assertIsNone(hash_only.data)
        self.assertEqual(hash_only.content_sha256, result.content_sha256)

    def test_descriptor_outside_root_is_rejected_before_any_read(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_bytes(self.payload)

        with patch(
            "tools.stable_file_access._descriptor_final_path",
            return_value=outside.resolve(),
        ), patch("tools.stable_file_access.os.read") as read_mock:
            with self.assertRaises(StableFileBoundaryError):
                read_stable_regular_file(
                    self.trusted,
                    self.source,
                    reject_redirection=False,
                )

        read_mock.assert_not_called()

    def test_missing_descriptor_path_primitive_fails_before_any_read(self) -> None:
        with patch(
            "tools.stable_file_access._descriptor_final_path",
            return_value=None,
        ), patch("tools.stable_file_access.os.read") as read_mock:
            with self.assertRaises(StableFileVerificationUnavailableError):
                read_stable_regular_file(
                    self.trusted,
                    self.source,
                    reject_redirection=False,
                )

        read_mock.assert_not_called()

    def test_missing_stable_file_identity_fails_before_any_read(self) -> None:
        with patch(
            "tools.stable_file_access._identity",
            return_value=None,
        ), patch("tools.stable_file_access.os.read") as read_mock:
            with self.assertRaises(StableFileVerificationUnavailableError):
                read_stable_regular_file(
                    self.trusted,
                    self.source,
                    reject_redirection=False,
                )

        read_mock.assert_not_called()

    def test_machine_state_redirection_is_rejected_before_any_read(self) -> None:
        other = self.trusted / "other.txt"
        other.write_bytes(self.payload)

        with patch(
            "tools.stable_file_access._descriptor_final_path",
            return_value=other.resolve(),
        ), patch("tools.stable_file_access.os.read") as read_mock:
            with self.assertRaises(StableFileRedirectionError):
                read_stable_regular_file(
                    self.trusted,
                    self.source,
                    reject_redirection=True,
                )

        read_mock.assert_not_called()

    def test_boolean_controls_fail_closed(self) -> None:
        for value in (None, 0, 1, "false", "true"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    read_stable_regular_file(
                        self.trusted,
                        self.source,
                        reject_redirection=value,  # type: ignore[arg-type]
                    )
        with self.assertRaises(TypeError):
            read_stable_regular_file(
                self.trusted,
                self.source,
                reject_redirection=False,
                capture_bytes="true",  # type: ignore[arg-type]
            )

    def test_missing_and_non_regular_paths_fail_closed(self) -> None:
        self.source.unlink()
        with self.assertRaises(StableFileMissingError):
            read_stable_regular_file(
                self.trusted,
                self.source,
                reject_redirection=False,
            )

        directory = self.trusted / "directory"
        directory.mkdir()
        with self.assertRaises(StableFileTypeError):
            read_stable_regular_file(
                self.trusted,
                directory,
                reject_redirection=True,
            )

    def test_internal_symlink_is_allowed_only_for_source_mode(self) -> None:
        link = self.trusted / "internal-link.txt"
        try:
            os.symlink(self.source, link)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        result = read_stable_regular_file(
            self.trusted,
            link,
            reject_redirection=False,
        )
        self.assertEqual(result.data, self.payload)
        self.assertEqual(result.final_path, self.source.resolve())
        with self.assertRaises(StableFileRedirectionError):
            read_stable_regular_file(
                self.trusted,
                link,
                reject_redirection=True,
            )

    def test_path_replacement_after_open_is_rejected(self) -> None:
        original_read = os.read
        replaced = False

        def read_then_replace(descriptor: int, count: int) -> bytes:
            nonlocal replaced
            chunk = original_read(descriptor, count)
            if not replaced:
                replaced = True
                opened_path = self.trusted / "opened-original.txt"
                try:
                    self.source.replace(opened_path)
                    self.source.write_bytes(b"replacement bytes\n")
                except OSError as exc:
                    self.skipTest(f"open-file replacement unavailable: {exc}")
            return chunk

        with patch("tools.stable_file_access.os.read", side_effect=read_then_replace):
            with self.assertRaises(StableFileChangedError):
                read_stable_regular_file(
                    self.trusted,
                    self.source,
                    reject_redirection=False,
                )

    def test_file_change_during_read_is_rejected(self) -> None:
        payload = b"A" * (2 * 1024 * 1024)
        self.source.write_bytes(payload)
        original_read = os.read
        changed = False

        def read_then_change(descriptor: int, count: int) -> bytes:
            nonlocal changed
            chunk = original_read(descriptor, count)
            if not changed:
                changed = True
                try:
                    self.source.write_bytes(b"B" * (len(payload) + 1))
                except OSError as exc:
                    self.skipTest(f"concurrent source write unavailable: {exc}")
            return chunk

        with patch("tools.stable_file_access.os.read", side_effect=read_then_change):
            with self.assertRaises(StableFileChangedError):
                read_stable_regular_file(
                    self.trusted,
                    self.source,
                    reject_redirection=False,
                )

    def test_redirected_trusted_root_is_always_rejected(self) -> None:
        link = self.root / "trusted-link"
        try:
            os.symlink(self.trusted, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symbolic links unavailable: {exc}")

        with self.assertRaises(StableFileRedirectionError):
            read_stable_regular_file(
                link,
                link / self.source.name,
                reject_redirection=False,
            )

    def test_partial_zero_identity_is_unavailable(self) -> None:
        self.assertIsNone(
            stable_file_access_module._identity(
                SimpleNamespace(st_dev=1, st_ino=0)  # type: ignore[arg-type]
            )
        )
        self.assertIsNone(
            stable_file_access_module._identity(
                SimpleNamespace(st_dev=0, st_ino=1)  # type: ignore[arg-type]
            )
        )

    @unittest.skipUnless(
        getattr(os, "O_NONBLOCK", 0),
        "O_NONBLOCK is unavailable on this platform",
    )
    def test_lease_open_is_nonblocking_until_regular_type_is_proven(self) -> None:
        real_open = os.open
        observed_flags: list[int] = []

        def open_and_capture(path, flags, *args, **kwargs):
            observed_flags.append(flags)
            return real_open(path, flags, *args, **kwargs)

        with patch("tools.stable_file_access.os.open", side_effect=open_and_capture):
            with lease_stable_regular_file(
                self.trusted,
                self.source,
                reject_redirection=True,
            ) as lease:
                self.assertEqual(lease.content_sha256, hashlib.sha256(self.payload).hexdigest())

        self.assertTrue(observed_flags)
        self.assertTrue(observed_flags[0] & os.O_NONBLOCK)

    def test_lease_revalidation_detects_content_change(self) -> None:
        with lease_stable_regular_file(
            self.trusted,
            self.source,
            reject_redirection=True,
        ) as lease:
            try:
                self.source.write_bytes(b"changed after initial verification\n")
            except OSError as exc:
                self.skipTest(f"concurrent lease mutation unavailable: {exc}")
            with self.assertRaises(StableFileChangedError):
                lease.revalidate()

    def test_atomic_write_pins_root_against_parent_replacement(self) -> None:
        target = self.trusted / "registry.jsonl"
        target.write_bytes(b"old registry\n")
        outside = self.root / "outside"
        outside.mkdir()
        outside_target = outside / target.name
        sentinel = b"outside sentinel must not change\n"
        outside_target.write_bytes(sentinel)
        displaced = self.root / "trusted-displaced"
        real_write_all = stable_file_access_module._write_all
        attack_attempted = False
        attack_succeeded = False

        def write_while_replacing_root(descriptor: int, payload: bytes) -> None:
            nonlocal attack_attempted, attack_succeeded
            if not attack_attempted:
                attack_attempted = True
                try:
                    self.trusted.rename(displaced)
                except OSError:
                    pass
                else:
                    try:
                        os.symlink(outside, self.trusted, target_is_directory=True)
                    except OSError:
                        displaced.rename(self.trusted)
                    else:
                        attack_succeeded = True
            real_write_all(descriptor, payload)

        error: StableFileAccessError | None = None
        wrote: bool | None = None
        try:
            with patch(
                "tools.stable_file_access._write_all",
                side_effect=write_while_replacing_root,
            ):
                try:
                    wrote = write_atomic_stable_file(
                        self.trusted,
                        target,
                        b"new registry\n",
                    )
                except StableFileAccessError as exc:
                    error = exc
        finally:
            if self.trusted.is_symlink():
                self.trusted.unlink()
            if displaced.exists():
                displaced.rename(self.trusted)

        self.assertTrue(attack_attempted)
        self.assertEqual(outside_target.read_bytes(), sentinel)
        if attack_succeeded:
            self.assertIsNotNone(error)
            self.assertEqual(target.read_bytes(), b"old registry\n")
        else:
            self.assertIsNone(error)
            self.assertTrue(wrote)
            self.assertEqual(target.read_bytes(), b"new registry\n")

    def test_atomic_write_pins_root_against_ordinary_directory_replacement(self) -> None:
        target = self.source
        displaced = self.root / "trusted-displaced-ordinary"
        replacement_payload = b"replacement-root sentinel\n"
        new_payload = b"new registry bytes\n"
        real_write_all = stable_file_access_module._write_all
        attack_attempted = False
        attack_succeeded = False

        def write_while_replacing_root(descriptor: int, payload: bytes) -> None:
            nonlocal attack_attempted, attack_succeeded
            if not attack_attempted:
                attack_attempted = True
                try:
                    self.trusted.rename(displaced)
                except OSError:
                    pass
                else:
                    self.trusted.mkdir()
                    (self.trusted / target.name).write_bytes(replacement_payload)
                    attack_succeeded = True
            real_write_all(descriptor, payload)

        error: StableFileAccessError | None = None
        result = None
        replacement_observed: bytes | None = None
        try:
            with patch(
                "tools.stable_file_access._write_all",
                side_effect=write_while_replacing_root,
            ):
                try:
                    result = write_atomic_stable_file(
                        self.trusted,
                        target,
                        new_payload,
                    )
                except StableFileAccessError as exc:
                    error = exc
            if attack_succeeded:
                replacement_observed = (self.trusted / target.name).read_bytes()
        finally:
            if attack_succeeded:
                shutil.rmtree(self.trusted)
                displaced.rename(self.trusted)

        self.assertTrue(attack_attempted)
        if attack_succeeded:
            self.assertIsNotNone(error)
            self.assertEqual(replacement_observed, replacement_payload)
            self.assertEqual(target.read_bytes(), self.payload)
        else:
            self.assertIsNone(error)
            self.assertIsNotNone(result)
            self.assertTrue(result)
            self.assertEqual(target.read_bytes(), new_payload)

    def test_post_replace_verification_failure_is_explicitly_commit_unknown(self) -> None:
        replacement = b"replacement became visible\n"
        real_assert_same_identity = stable_file_access_module._assert_same_identity

        def fail_after_replacement(opened, current, *, target: Path) -> None:
            if target == self.source and self.source.read_bytes() == replacement:
                raise StableFileChangedError("forced post-replace verification failure")
            real_assert_same_identity(opened, current, target=target)

        with patch.object(
            stable_file_access_module,
            "_assert_same_identity",
            side_effect=fail_after_replacement,
        ):
            with self.assertRaises(StableFileCommitUnknownError):
                write_atomic_stable_file(self.trusted, self.source, replacement)

        self.assertEqual(self.source.read_bytes(), replacement)

    def test_persistent_lock_serializes_holders_and_remains_on_disk(self) -> None:
        lock_file = self.trusted / "registry.lock"
        ready = threading.Event()
        release = threading.Event()
        holder_error: list[BaseException] = []

        def hold_lock() -> None:
            try:
                with exclusive_stable_file_lock(
                    self.trusted,
                    lock_file,
                    timeout_seconds=1.0,
                ):
                    ready.set()
                    release.wait(timeout=5.0)
            except BaseException as exc:  # pragma: no cover - surfaced below
                holder_error.append(exc)
                ready.set()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        self.assertTrue(ready.wait(timeout=2.0), "lock holder did not start")
        try:
            self.assertEqual(holder_error, [])
            with self.assertRaises(StableFileLockTimeoutError):
                with exclusive_stable_file_lock(
                    self.trusted,
                    lock_file,
                    timeout_seconds=0.02,
                    retry_seconds=0.005,
                ):
                    self.fail("contended lock must not be yielded")
        finally:
            release.set()
            holder.join(timeout=2.0)
        self.assertFalse(holder.is_alive())
        self.assertEqual(holder_error, [])
        with exclusive_stable_file_lock(
            self.trusted,
            lock_file,
            timeout_seconds=1.0,
        ):
            self.assertTrue(lock_file.is_file())
        self.assertTrue(lock_file.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows share-mode contract")
    def test_windows_atomic_temporary_denies_concurrent_writable_open(self) -> None:
        descriptor, temporary_path = stable_file_access_module._create_windows_temporary(
            self.trusted
        )
        try:
            with self.assertRaises(OSError):
                with temporary_path.open("r+b"):
                    pass
        finally:
            os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    @unittest.skipUnless(os.name == "nt", "Windows share-mode contract")
    def test_windows_stable_lease_denies_write_replace_and_delete(self) -> None:
        replacement = self.trusted / "replacement.txt"
        replacement.write_bytes(b"replacement\n")

        with lease_stable_regular_file(
            self.trusted,
            self.source,
            reject_redirection=True,
        ) as lease:
            with self.assertRaises(OSError):
                with self.source.open("r+b"):
                    pass
            with self.assertRaises(OSError):
                os.replace(replacement, self.source)
            with self.assertRaises(OSError):
                self.source.unlink()
            lease.revalidate()

        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(replacement.read_bytes(), b"replacement\n")

    def test_real_symlink_escape_is_rejected_when_available(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_bytes(self.payload)
        link = self.trusted / "escape.txt"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaises((StableFileBoundaryError, StableFileRedirectionError)):
            read_stable_regular_file(
                self.trusted,
                link,
                reject_redirection=False,
            )
        with self.assertRaises(StableFileRedirectionError):
            read_stable_regular_file(
                self.trusted,
                link,
                reject_redirection=True,
            )


    def test_atomic_write_replaces_direct_child_and_is_idempotent(self) -> None:
        replacement = b"replacement machine state\n"

        self.assertTrue(
            write_atomic_stable_file(self.trusted, self.source, replacement)
        )
        self.assertEqual(self.source.read_bytes(), replacement)
        self.assertFalse(
            write_atomic_stable_file(self.trusted, self.source, replacement)
        )
        self.assertEqual(list(self.trusted.glob(".stable-write.*.tmp")), [])

        created = self.trusted / "created.jsonl"
        self.assertTrue(write_atomic_stable_file(self.trusted, created, b"{}\n"))
        self.assertEqual(created.read_bytes(), b"{}\n")

    def test_atomic_write_requires_a_direct_child(self) -> None:
        nested = self.trusted / "nested" / "state.jsonl"
        outside = self.root / "outside.jsonl"

        with self.assertRaises(StableFileBoundaryError):
            write_atomic_stable_file(self.trusted, nested, b"{}\n")
        with self.assertRaises(StableFileBoundaryError):
            write_atomic_stable_file(self.trusted, outside, b"{}\n")
        self.assertFalse(nested.exists())
        self.assertFalse(outside.exists())

    def test_atomic_write_rejects_redirected_target_without_touching_target(self) -> None:
        outside = self.root / "outside-state.jsonl"
        sentinel = b"outside sentinel\n"
        outside.write_bytes(sentinel)
        self.source.unlink()
        try:
            os.symlink(outside, self.source)
        except OSError as exc:
            self.source.write_bytes(self.payload)
            self.skipTest(f"symbolic links unavailable: {exc}")

        with self.assertRaises(StableFileRedirectionError):
            write_atomic_stable_file(self.trusted, self.source, b"new state\n")

        self.assertEqual(outside.read_bytes(), sentinel)
        self.assertTrue(self.source.is_symlink())

    def test_atomic_write_rejects_redirected_trusted_root(self) -> None:
        link = self.root / "trusted-write-link"
        try:
            os.symlink(self.trusted, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symbolic links unavailable: {exc}")

        with self.assertRaises(StableFileRedirectionError):
            write_atomic_stable_file(link, link / "state.jsonl", b"{}\n")
        self.assertFalse((self.trusted / "state.jsonl").exists())

if __name__ == "__main__":
    unittest.main()
