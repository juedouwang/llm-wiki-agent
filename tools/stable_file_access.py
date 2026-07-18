#!/usr/bin/env python3
"""Descriptor-bound reads for regular files beneath a trusted local root.

The helpers in this module open a lexical path once, prove the opened descriptor
still resolves beneath the trusted root before reading any bytes, and then verify
that the descriptor and pathname stayed stable for the whole read.  Callers may
also reject every symbolic-link/reparse-point redirection for machine-state files.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_READ_CHUNK_BYTES = 1024 * 1024


class StableFileAccessError(Exception):
    """Base error for fail-closed descriptor-bound file access."""

    reason_code = "stable-file-access-error"


class StableFileMissingError(StableFileAccessError):
    reason_code = "stable-file-missing"


class StableFileBoundaryError(StableFileAccessError):
    reason_code = "stable-file-outside-trusted-root"


class StableFileRedirectionError(StableFileAccessError):
    reason_code = "stable-file-redirection"


class StableFileTypeError(StableFileAccessError):
    reason_code = "stable-file-not-regular"


class StableFileChangedError(StableFileAccessError):
    reason_code = "stable-file-changed-during-read"


class StableFileReadError(StableFileAccessError):
    reason_code = "stable-file-read-failed"


class StableFileVerificationUnavailableError(StableFileAccessError):
    """Raised when the platform cannot prove descriptor/path identity."""

    reason_code = "stable-file-verification-unavailable"


class StableFileCommitUnknownError(StableFileAccessError):
    """Raised when replacement may be visible but cannot be proven current."""

    reason_code = "stable-file-commit-state-unknown"


class StableFileLockTimeoutError(StableFileAccessError):
    """Raised when a root-bound stable lock cannot be acquired in time."""

    reason_code = "stable-file-lock-timeout"


@dataclass(frozen=True)
class StableFileWriteResult:
    """Explicit atomic-write outcome; pre-commit failures still raise."""

    wrote: bool
    commit_state: str

    def __post_init__(self) -> None:
        allowed = {"unchanged", "committed", "committed-durability-unknown"}
        if self.commit_state not in allowed:
            raise ValueError(f"unsupported stable-file commit_state: {self.commit_state}")
        if self.wrote != (self.commit_state != "unchanged"):
            raise ValueError("wrote must agree with stable-file commit_state")

    def __bool__(self) -> bool:
        return self.wrote


@dataclass(frozen=True)
class StableFileReadResult:
    """Stable descriptor-bound bytes/hash observation."""

    lexical_path: Path
    final_path: Path
    size_bytes: int
    content_sha256: str
    data: bytes | None


class StableDirectoryLease:
    """Pinned trusted-root identity retained across a complete transaction."""

    def __init__(
        self,
        *,
        root: Path,
        identity: tuple[int, int],
        descriptor: int | None = None,
        windows_handle: int | None = None,
    ) -> None:
        self.root = root
        self.identity = identity
        self._descriptor = descriptor
        self._windows_handle = windows_handle
        self._lock_descriptor: int | None = None
        self._lock_path: Path | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._closed = False

    @property
    def descriptor(self) -> int:
        if self._closed or self._descriptor is None:
            raise StableFileChangedError(
                f"trusted-root descriptor is unavailable: {self.root}"
            )
        return self._descriptor

    @property
    def windows_handle(self) -> int:
        if self._closed or self._windows_handle is None:
            raise StableFileChangedError(
                f"trusted-root handle is unavailable: {self.root}"
            )
        return self._windows_handle

    def revalidate(self) -> Path:
        """Prove that the pinned directory still owns its lexical path."""

        if self._closed:
            raise StableFileChangedError(
                f"trusted-root lease is already closed: {self.root}"
            )
        try:
            current = os.lstat(self.root)
            resolved = self.root.resolve(strict=True)
        except OSError as exc:
            raise StableFileChangedError(
                f"trusted root disappeared while leased: {self.root}: {exc}"
            ) from exc
        if _is_reparse_point(current) or _path_key(resolved) != _path_key(self.root):
            raise StableFileRedirectionError(
                f"trusted root changed to a redirection while leased: {self.root}"
            )
        if not stat.S_ISDIR(current.st_mode):
            raise StableFileTypeError(
                f"trusted root changed to a non-directory while leased: {self.root}"
            )
        current_identity = _identity(current)
        if current_identity is None:
            raise StableFileVerificationUnavailableError(
                f"trusted root no longer exposes stable identity: {self.root}"
            )
        if current_identity != self.identity:
            raise StableFileChangedError(
                f"trusted root identity changed while leased: {self.root}"
            )
        if os.name == "nt":
            final_path = _windows_handle_final_path(self.windows_handle)
            if _path_key(final_path) != _path_key(self.root):
                raise StableFileChangedError(
                    f"trusted root moved while leased: {self.root}"
                )
        else:
            final_path = _assert_pinned_directory(
                self.descriptor,
                root=self.root,
                opened_identity=self.identity,
            )
        if self._lock_descriptor is not None:
            assert self._lock_path is not None
            assert self._lock_identity is not None
            try:
                opened_lock = os.fstat(self._lock_descriptor)
                current_lock = os.lstat(self._lock_path)
            except OSError as exc:
                raise StableFileChangedError(
                    f"stable lock file changed while held: {self._lock_path}: {exc}"
                ) from exc
            if _is_reparse_point(current_lock):
                raise StableFileRedirectionError(
                    f"stable lock file changed to a redirection: {self._lock_path}"
                )
            if not stat.S_ISREG(opened_lock.st_mode) or not stat.S_ISREG(
                current_lock.st_mode
            ):
                raise StableFileTypeError(
                    f"stable lock file is not regular: {self._lock_path}"
                )
            if _identity(opened_lock) != self._lock_identity or _identity(
                current_lock
            ) != self._lock_identity:
                raise StableFileChangedError(
                    f"stable lock file identity changed while held: {self._lock_path}"
                )
        return final_path

    def bind_lock(self, descriptor: int, path: Path) -> None:
        if self._lock_descriptor is not None:
            raise StableFileChangedError(
                f"trusted-root lease already owns a stable lock: {self.root}"
            )
        identity = _identity(os.fstat(descriptor))
        if identity is None:
            raise StableFileVerificationUnavailableError(
                f"stable lock file has no usable identity: {path}"
            )
        self._lock_descriptor = descriptor
        self._lock_path = path
        self._lock_identity = identity
        self.revalidate()

    def unbind_lock(self) -> None:
        self._lock_descriptor = None
        self._lock_path = None
        self._lock_identity = None

    def close(self) -> None:
        if self._closed:
            return
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        if self._windows_handle is not None:
            _close_windows_handle(self._windows_handle)
            self._windows_handle = None
        self._closed = True


class StableFileLease:
    """Open descriptor lease whose pathname, identity, and content can be rechecked."""

    def __init__(
        self,
        *,
        descriptor: int,
        lexical_path: Path,
        final_path: Path,
        resolved_root: Path,
        reject_redirection: bool,
        descriptor_signature: tuple[object, ...],
        path_signature: tuple[object, ...],
        size_bytes: int,
        content_sha256: str,
        root_lease: StableDirectoryLease,
    ) -> None:
        self._descriptor = descriptor
        self.lexical_path = lexical_path
        self.final_path = final_path
        self._resolved_root = resolved_root
        self._reject_redirection = reject_redirection
        self._descriptor_signature = descriptor_signature
        self._path_signature = path_signature
        self.size_bytes = size_bytes
        self.content_sha256 = content_sha256
        self._root_lease = root_lease
        self._closed = False

    @property
    def observation(self) -> StableFileReadResult:
        """Return the immutable observation established when the lease was opened."""

        return StableFileReadResult(
            lexical_path=self.lexical_path,
            final_path=self.final_path,
            size_bytes=self.size_bytes,
            content_sha256=self.content_sha256,
            data=None,
        )

    def revalidate(self) -> StableFileReadResult:
        """Re-hash and prove that the leased descriptor still owns the same path."""

        if self._closed:
            raise StableFileChangedError(
                f"stable file lease is already closed: {self.lexical_path}"
            )
        self._root_lease.revalidate()
        descriptor_before = os.fstat(self._descriptor)
        if _signature(descriptor_before) != self._descriptor_signature:
            raise StableFileChangedError(
                f"leased file changed before revalidation: {self.lexical_path}"
            )
        path_before = _path_metadata(
            self.lexical_path,
            reject_redirection=self._reject_redirection,
        )
        _assert_same_identity(
            descriptor_before,
            path_before,
            target=self.lexical_path,
        )
        if _signature(path_before) != self._path_signature:
            raise StableFileChangedError(
                f"leased file pathname changed before revalidation: {self.lexical_path}"
            )
        final_before = _verified_descriptor_final_path(
            self._descriptor,
            target=self.lexical_path,
            resolved_root=self._resolved_root,
            reject_redirection=self._reject_redirection,
        )
        if _path_key(final_before) != _path_key(self.final_path):
            raise StableFileChangedError(
                f"leased descriptor path changed before revalidation: {self.lexical_path}"
            )

        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
        except OSError as exc:
            raise StableFileReadError(
                f"could not rewind leased file {self.lexical_path}: {exc}"
            ) from exc
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(self._descriptor, _READ_CHUNK_BYTES)
            except OSError as exc:
                raise StableFileReadError(
                    f"could not re-read leased file {self.lexical_path}: {exc}"
                ) from exc
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)

        descriptor_after = os.fstat(self._descriptor)
        if _signature(descriptor_after) != self._descriptor_signature:
            raise StableFileChangedError(
                f"leased file changed during revalidation: {self.lexical_path}"
            )
        if total != self.size_bytes or digest.hexdigest() != self.content_sha256:
            raise StableFileChangedError(
                f"leased file content changed during revalidation: {self.lexical_path}"
            )
        path_after = _path_metadata(
            self.lexical_path,
            reject_redirection=self._reject_redirection,
        )
        _assert_same_identity(
            descriptor_after,
            path_after,
            target=self.lexical_path,
        )
        if _signature(path_after) != self._path_signature:
            raise StableFileChangedError(
                f"leased file pathname changed during revalidation: {self.lexical_path}"
            )
        final_after = _verified_descriptor_final_path(
            self._descriptor,
            target=self.lexical_path,
            resolved_root=self._resolved_root,
            reject_redirection=self._reject_redirection,
        )
        if _path_key(final_after) != _path_key(self.final_path):
            raise StableFileChangedError(
                f"leased descriptor path changed during revalidation: {self.lexical_path}"
            )
        return self.observation

    def close(self) -> None:
        if not self._closed:
            os.close(self._descriptor)
            self._closed = True


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _absolute_lexical(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_within(candidate: Path, root: Path) -> bool:
    candidate_key = _path_key(candidate)
    root_key = _path_key(root)
    try:
        return os.path.commonpath((candidate_key, root_key)) == root_key
    except ValueError:
        return False


def _identity(metadata: os.stat_result) -> tuple[int, int] | None:
    device = int(getattr(metadata, "st_dev", 0))
    inode = int(getattr(metadata, "st_ino", 0))
    if device <= 0 or inode <= 0:
        return None
    return device, inode


def _signature(metadata: os.stat_result) -> tuple[object, ...]:
    signature: tuple[object, ...] = (
        stat.S_IFMT(metadata.st_mode),
        _identity(metadata),
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    if os.name != "nt":
        signature += (metadata.st_ctime_ns,)
    return signature


def _is_reparse_point(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (
            reparse_flag
            and getattr(metadata, "st_file_attributes", 0) & reparse_flag
        )
    )


def _preflight_no_redirection(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise StableFileBoundaryError(
            f"file path is outside the trusted lexical root: {target}"
        ) from exc

    current = root
    components = (root,)
    if relative.parts:
        built: list[Path] = []
        for part in relative.parts:
            current = current / part
            built.append(current)
        components += tuple(built)

    for current in components:
        is_leaf = _path_key(current) == _path_key(target)
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise StableFileMissingError(f"file path is unavailable: {current}") from exc
        except OSError as exc:
            raise StableFileReadError(
                f"could not inspect file path {current}: {exc}"
            ) from exc
        if _is_reparse_point(metadata):
            raise StableFileRedirectionError(
                f"file path must not traverse a symbolic link or reparse point: {current}"
            )
        if is_leaf:
            if not stat.S_ISREG(metadata.st_mode):
                raise StableFileTypeError(f"file path is not a regular file: {current}")
        elif not stat.S_ISDIR(metadata.st_mode):
            raise StableFileTypeError(
                f"file ancestor is not a directory: {current}"
            )
        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise StableFileReadError(
                f"could not resolve file path {current}: {exc}"
            ) from exc
        if _path_key(resolved) != _path_key(current):
            raise StableFileRedirectionError(
                f"file path resolves through redirection: {current}"
            )


def _windows_descriptor_path(descriptor: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = msvcrt.get_osfhandle(descriptor)
    if handle == -1:
        raise OSError("could not obtain the operating-system file handle")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    required = get_final_path(handle, None, 0, 0)
    if required == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return _absolute_lexical(value)


def _linux_descriptor_path(descriptor: int) -> Path:
    value = os.readlink(f"/proc/self/fd/{descriptor}")
    if value.endswith(" (deleted)"):
        raise OSError("opened file was deleted before verification")
    return _absolute_lexical(value)


def _darwin_descriptor_path(descriptor: int) -> Path:
    import fcntl

    # F_GETPATH is stable on Darwin but is not exported by Python's fcntl module.
    value = fcntl.fcntl(descriptor, 50, b"\0" * 4096)
    raw = value.split(b"\0", 1)[0]
    if not raw:
        raise OSError("could not obtain the opened file path")
    return _absolute_lexical(os.fsdecode(raw))


def _descriptor_final_path(descriptor: int) -> Path | None:
    if os.name == "nt":
        return _windows_descriptor_path(descriptor)
    if sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        return _linux_descriptor_path(descriptor)
    if sys.platform == "darwin":
        return _darwin_descriptor_path(descriptor)
    return None


def _verified_descriptor_final_path(
    descriptor: int,
    *,
    target: Path,
    resolved_root: Path,
    reject_redirection: bool,
) -> Path:
    try:
        final_path = _descriptor_final_path(descriptor)
    except OSError as exc:
        raise StableFileReadError(
            f"could not verify opened descriptor path for {target}: {exc}"
        ) from exc
    if final_path is None:
        raise StableFileVerificationUnavailableError(
            "the platform cannot obtain the opened descriptor's final path: "
            f"{target}"
        )
    if not _is_within(final_path, resolved_root):
        raise StableFileBoundaryError(
            f"opened descriptor resolves outside the trusted root: {target}"
        )
    if reject_redirection and _path_key(final_path) != _path_key(target):
        raise StableFileRedirectionError(
            f"opened descriptor resolves through redirection: {target}"
        )
    return final_path


def _path_metadata(target: Path, *, reject_redirection: bool) -> os.stat_result:
    try:
        metadata = os.lstat(target) if reject_redirection else os.stat(target)
    except FileNotFoundError as exc:
        raise StableFileChangedError(
            f"file path disappeared during descriptor verification: {target}"
        ) from exc
    except OSError as exc:
        raise StableFileReadError(
            f"could not inspect file path during descriptor verification: {target}: {exc}"
        ) from exc
    if reject_redirection and _is_reparse_point(metadata):
        raise StableFileRedirectionError(
            f"file path changed to a symbolic link or reparse point: {target}"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise StableFileTypeError(f"file path is not a regular file: {target}")
    return metadata


def _assert_same_identity(
    opened: os.stat_result,
    path_metadata: os.stat_result,
    *,
    target: Path,
) -> None:
    opened_identity = _identity(opened)
    path_identity = _identity(path_metadata)
    if opened_identity is None or path_identity is None:
        raise StableFileVerificationUnavailableError(
            "the platform did not expose stable file identity for descriptor/path "
            f"verification: {target}"
        )
    if opened_identity != path_identity:
        raise StableFileChangedError(
            f"opened descriptor no longer matches the requested path: {target}"
        )



def _close_windows_handle(handle: int) -> None:
    import ctypes

    if not ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle):
        raise StableFileReadError(
            f"could not close trusted-root handle: {ctypes.WinError(ctypes.get_last_error())}"
        )


def _validated_root_identity(root: Path) -> tuple[int, int]:
    try:
        metadata = os.lstat(root)
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise StableFileReadError(f"trusted root is unavailable: {root}: {exc}") from exc
    if _is_reparse_point(metadata) or _path_key(resolved) != _path_key(root):
        raise StableFileRedirectionError(
            f"trusted root must not be a symbolic link or reparse point: {root}"
        )
    if not stat.S_ISDIR(metadata.st_mode) or not resolved.is_dir():
        raise StableFileTypeError(f"trusted root is not a directory: {root}")
    identity = _identity(metadata)
    if identity is None:
        raise StableFileVerificationUnavailableError(
            f"the platform did not expose stable identity for trusted root: {root}"
        )
    return identity


@contextmanager
def lease_stable_directory(trusted_root: str | Path) -> Iterator[StableDirectoryLease]:
    """Pin one non-redirected trusted root across a complete operation."""

    root = _absolute_lexical(trusted_root)
    initial_identity = _validated_root_identity(root)
    lease: StableDirectoryLease | None = None
    try:
        if os.name == "nt":
            handle = _open_windows_pinned_directory(root)
            lease = StableDirectoryLease(
                root=root,
                identity=initial_identity,
                windows_handle=handle,
            )
        else:
            flags = os.O_RDONLY
            for optional_flag in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW"):
                flags |= getattr(os, optional_flag, 0)
            try:
                descriptor = os.open(root, flags)
            except OSError as exc:
                raise StableFileReadError(
                    f"could not pin trusted root {root}: {exc}"
                ) from exc
            try:
                opened_identity = _identity(os.fstat(descriptor))
                if opened_identity is None:
                    raise StableFileVerificationUnavailableError(
                        f"the platform did not expose stable identity for trusted root: {root}"
                    )
                if opened_identity != initial_identity:
                    raise StableFileChangedError(
                        f"trusted root changed while it was being pinned: {root}"
                    )
                lease = StableDirectoryLease(
                    root=root,
                    identity=initial_identity,
                    descriptor=descriptor,
                )
            except Exception:
                os.close(descriptor)
                raise
        lease.revalidate()
        yield lease
    finally:
        if lease is not None:
            lease.close()


def _relative_file_parts(root: Path, target: Path) -> tuple[str, ...]:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise StableFileBoundaryError(
            f"file path is outside the trusted lexical root: {target}"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise StableFileBoundaryError(f"file path is not a valid root-relative path: {target}")
    return tuple(relative.parts)


def _open_posix_regular_from_lease(
    lease: StableDirectoryLease,
    target: Path,
    *,
    reject_redirection: bool,
) -> int:
    parts = _relative_file_parts(lease.root, target)
    parent_descriptor = os.dup(lease.descriptor)
    expected_parent = lease.root
    try:
        for part in parts[:-1]:
            flags = os.O_RDONLY
            for optional_flag in ("O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK"):
                flags |= getattr(os, optional_flag, 0)
            if reject_redirection:
                flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                child_descriptor = os.open(part, flags, dir_fd=parent_descriptor)
            except FileNotFoundError as exc:
                raise StableFileMissingError(
                    f"file path is unavailable: {expected_parent / part}"
                ) from exc
            except OSError as exc:
                if reject_redirection and exc.errno == getattr(errno, "ELOOP", None):
                    raise StableFileRedirectionError(
                        f"file path traverses a symbolic link: {expected_parent / part}"
                    ) from exc
                raise StableFileReadError(
                    f"could not open file ancestor {expected_parent / part}: {exc}"
                ) from exc
            os.close(parent_descriptor)
            parent_descriptor = child_descriptor
            metadata = os.fstat(parent_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise StableFileTypeError(
                    f"file ancestor is not a directory: {expected_parent / part}"
                )
            final_path = _descriptor_final_path(parent_descriptor)
            if final_path is None:
                raise StableFileVerificationUnavailableError(
                    f"the platform cannot verify file ancestor: {expected_parent / part}"
                )
            expected_parent /= part
            if not _is_within(final_path, lease.root):
                raise StableFileBoundaryError(
                    f"file ancestor resolves outside the trusted root: {expected_parent}"
                )
            if reject_redirection and _path_key(final_path) != _path_key(expected_parent):
                raise StableFileRedirectionError(
                    f"file ancestor resolves through redirection: {expected_parent}"
                )

        flags = os.O_RDONLY
        for optional_flag in ("O_BINARY", "O_CLOEXEC", "O_NONBLOCK"):
            flags |= getattr(os, optional_flag, 0)
        if reject_redirection:
            flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(parts[-1], flags, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            raise StableFileMissingError(f"file path is unavailable: {target}") from exc
        except OSError as exc:
            if reject_redirection and exc.errno == getattr(errno, "ELOOP", None):
                raise StableFileRedirectionError(
                    f"file path must not be a symbolic link: {target}"
                ) from exc
            raise StableFileReadError(f"could not open file path {target}: {exc}") from exc
    finally:
        os.close(parent_descriptor)


def _open_windows_regular_from_lease(
    lease: StableDirectoryLease,
    target: Path,
    *,
    reject_redirection: bool,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    if reject_redirection:
        _preflight_no_redirection(lease.root, target)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flags = file_attribute_normal
    if reject_redirection:
        flags |= file_flag_open_reparse_point
    handle = create_file(
        str(target),
        generic_read | file_read_attributes | synchronize,
        file_share_read,
        None,
        open_existing,
        flags,
        None,
    )
    if handle == invalid_handle_value:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise StableFileMissingError(f"file path is unavailable: {target}")
        raise StableFileReadError(
            f"could not open stable file path {target}: {ctypes.WinError(error)}"
        )
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _open_regular_from_lease(
    lease: StableDirectoryLease,
    target: Path,
    *,
    reject_redirection: bool,
) -> tuple[int, os.stat_result, os.stat_result, Path]:
    lease.revalidate()
    if os.name == "nt":
        descriptor = _open_windows_regular_from_lease(
            lease,
            target,
            reject_redirection=reject_redirection,
        )
    else:
        descriptor = _open_posix_regular_from_lease(
            lease,
            target,
            reject_redirection=reject_redirection,
        )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise StableFileTypeError(f"opened path is not a regular file: {target}")
        final_path = _verified_descriptor_final_path(
            descriptor,
            target=target,
            resolved_root=lease.root,
            reject_redirection=reject_redirection,
        )
        path_metadata = _path_metadata(
            target,
            reject_redirection=reject_redirection,
        )
        _assert_same_identity(opened, path_metadata, target=target)
        lease.revalidate()
        return descriptor, opened, path_metadata, final_path
    except Exception:
        os.close(descriptor)
        raise


def _read_open_descriptor(
    descriptor: int,
    *,
    target: Path,
    capture_bytes: bool,
) -> tuple[int, str, bytes | None]:
    digest = hashlib.sha256()
    total = 0
    chunks: list[bytes] | None = [] if capture_bytes else None
    while True:
        try:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        except OSError as exc:
            raise StableFileReadError(f"could not read file path {target}: {exc}") from exc
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    return total, digest.hexdigest(), b"".join(chunks) if chunks is not None else None

def _read_stable_regular_file_with_lease(
    lease: StableDirectoryLease,
    target: Path,
    *,
    reject_redirection: bool,
    capture_bytes: bool,
) -> StableFileReadResult:
    descriptor = -1
    try:
        descriptor, opened_before, path_before, final_path = _open_regular_from_lease(
            lease,
            target,
            reject_redirection=reject_redirection,
        )
        total, digest, data = _read_open_descriptor(
            descriptor,
            target=target,
            capture_bytes=capture_bytes,
        )
        opened_after = os.fstat(descriptor)
        if _signature(opened_after) != _signature(opened_before):
            raise StableFileChangedError(f"file changed while it was being read: {target}")
        if total != opened_before.st_size:
            raise StableFileChangedError(
                f"file size changed while it was being read: {target}"
            )
        path_after = _path_metadata(target, reject_redirection=reject_redirection)
        _assert_same_identity(opened_after, path_after, target=target)
        if _signature(path_after) != _signature(path_before):
            raise StableFileChangedError(
                f"file pathname changed while it was being read: {target}"
            )
        final_after = _verified_descriptor_final_path(
            descriptor,
            target=target,
            resolved_root=lease.root,
            reject_redirection=reject_redirection,
        )
        if _path_key(final_after) != _path_key(final_path):
            raise StableFileChangedError(
                f"opened descriptor path changed while it was being read: {target}"
            )
        lease.revalidate()
        return StableFileReadResult(
            lexical_path=target,
            final_path=final_path,
            size_bytes=total,
            content_sha256=digest,
            data=data,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_stable_regular_file(
    trusted_root: str | Path,
    path: str | Path,
    *,
    reject_redirection: bool,
    capture_bytes: bool = True,
    root_lease: StableDirectoryLease | None = None,
) -> StableFileReadResult:
    """Read/hash one regular file through one pinned trusted-root identity."""

    if not isinstance(reject_redirection, bool):
        raise TypeError("reject_redirection must be a bool")
    if not isinstance(capture_bytes, bool):
        raise TypeError("capture_bytes must be a bool")
    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(path)
    _relative_file_parts(root, target)

    if root_lease is not None:
        if not isinstance(root_lease, StableDirectoryLease):
            raise TypeError("root_lease must be a StableDirectoryLease")
        if _path_key(root_lease.root) != _path_key(root):
            raise StableFileBoundaryError(
                f"root lease does not match requested trusted root: {root}"
            )
        return _read_stable_regular_file_with_lease(
            root_lease,
            target,
            reject_redirection=reject_redirection,
            capture_bytes=capture_bytes,
        )

    with lease_stable_directory(root) as lease:
        return _read_stable_regular_file_with_lease(
            lease,
            target,
            reject_redirection=reject_redirection,
            capture_bytes=capture_bytes,
        )


@contextmanager
def lease_stable_regular_file(
    trusted_root: str | Path,
    path: str | Path,
    *,
    reject_redirection: bool,
) -> Iterator[StableFileLease]:
    """Hash and retain one immutable file plus its pinned trusted-root lease."""

    if not isinstance(reject_redirection, bool):
        raise TypeError("reject_redirection must be a bool")
    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(path)
    _relative_file_parts(root, target)

    descriptor = -1
    stable_lease: StableFileLease | None = None
    with lease_stable_directory(root) as root_lease:
        try:
            descriptor, opened_before, path_before, final_path = _open_regular_from_lease(
                root_lease,
                target,
                reject_redirection=reject_redirection,
            )
            total, digest, _data = _read_open_descriptor(
                descriptor,
                target=target,
                capture_bytes=False,
            )
            opened_after = os.fstat(descriptor)
            if _signature(opened_after) != _signature(opened_before):
                raise StableFileChangedError(
                    f"file changed while it was being leased: {target}"
                )
            if total != opened_before.st_size:
                raise StableFileChangedError(
                    f"file size changed while it was being leased: {target}"
                )
            path_after = _path_metadata(
                target,
                reject_redirection=reject_redirection,
            )
            _assert_same_identity(opened_after, path_after, target=target)
            if _signature(path_after) != _signature(path_before):
                raise StableFileChangedError(
                    f"file pathname changed while it was being leased: {target}"
                )
            final_after = _verified_descriptor_final_path(
                descriptor,
                target=target,
                resolved_root=root_lease.root,
                reject_redirection=reject_redirection,
            )
            if _path_key(final_after) != _path_key(final_path):
                raise StableFileChangedError(
                    f"opened descriptor path changed while it was being leased: {target}"
                )
            root_lease.revalidate()
            stable_lease = StableFileLease(
                descriptor=descriptor,
                lexical_path=target,
                final_path=final_path,
                resolved_root=root_lease.root,
                reject_redirection=reject_redirection,
                descriptor_signature=_signature(opened_after),
                path_signature=_signature(path_after),
                size_bytes=total,
                content_sha256=digest,
                root_lease=root_lease,
            )
            descriptor = -1
            yield stable_lease
        finally:
            if stable_lease is not None:
                stable_lease.close()
            if descriptor >= 0:
                os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset : offset + _READ_CHUNK_BYTES])
        except OSError as exc:
            raise StableFileReadError(f"could not write atomic temporary file: {exc}") from exc
        if written <= 0:
            raise StableFileReadError("atomic temporary-file write made no progress")
        offset += written
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise StableFileReadError(f"could not flush atomic temporary file: {exc}") from exc


def _assert_pinned_directory(
    descriptor: int,
    *,
    root: Path,
    opened_identity: tuple[int, int],
) -> Path:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise StableFileTypeError(f"pinned trusted root is not a directory: {root}")
    identity = _identity(metadata)
    if identity is None:
        raise StableFileVerificationUnavailableError(
            f"the platform did not expose stable identity for trusted root: {root}"
        )
    if identity != opened_identity:
        raise StableFileChangedError(f"trusted root identity changed during write: {root}")
    try:
        final_path = _descriptor_final_path(descriptor)
    except OSError as exc:
        raise StableFileReadError(
            f"could not verify pinned trusted-root descriptor {root}: {exc}"
        ) from exc
    if final_path is None:
        raise StableFileVerificationUnavailableError(
            f"the platform cannot obtain the pinned trusted-root path: {root}"
        )
    if _path_key(final_path) != _path_key(root):
        raise StableFileChangedError(
            f"trusted root moved while an atomic write was in progress: {root}"
        )
    return final_path


def _write_atomic_posix(
    lease: StableDirectoryLease,
    target: Path,
    payload: bytes,
) -> str:
    required_dir_fd = (os.open, os.replace, os.unlink)
    if not all(function in os.supports_dir_fd for function in required_dir_fd):
        raise StableFileVerificationUnavailableError(
            "the platform cannot perform handle-relative atomic replacement"
        )
    root = lease.root
    root_descriptor = lease.descriptor
    temporary_descriptor = -1
    temporary_name: str | None = None
    replaced = False
    try:
        lease.revalidate()
        temporary_name = f".stable-write.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
            flags |= getattr(os, optional_flag, 0)
        try:
            temporary_descriptor = os.open(
                temporary_name,
                flags,
                0o600,
                dir_fd=root_descriptor,
            )
        except OSError as exc:
            raise StableFileReadError(
                f"could not create atomic temporary file inside {root}: {exc}"
            ) from exc
        temporary_before = os.fstat(temporary_descriptor)
        if not stat.S_ISREG(temporary_before.st_mode):
            raise StableFileTypeError("atomic temporary descriptor is not a regular file")
        if _identity(temporary_before) is None:
            raise StableFileVerificationUnavailableError(
                "the platform did not expose stable temporary-file identity"
            )
        _write_all(temporary_descriptor, payload)
        lease.revalidate()
        try:
            os.replace(
                temporary_name,
                target.name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
        except OSError as exc:
            raise StableFileReadError(
                f"could not atomically replace stable file {target}: {exc}"
            ) from exc
        temporary_name = None
        replaced = True

        try:
            opened_after = os.fstat(temporary_descriptor)
            path_after = os.stat(
                target.name,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            _assert_same_identity(opened_after, path_after, target=target)
            if _signature(opened_after) != _signature(path_after):
                raise StableFileChangedError(
                    f"atomic replacement target changed before verification: {target}"
                )
            lease.revalidate()
        except Exception as exc:
            raise StableFileCommitUnknownError(
                f"atomic replacement may be visible but could not be verified: {target}: {exc}"
            ) from exc
        try:
            os.fsync(root_descriptor)
        except OSError:
            return "committed-durability-unknown"
        return "committed"
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name is not None and not replaced:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _windows_handle_final_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    required = get_final_path(handle, None, 0, 0)
    if required == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return _absolute_lexical(value)


def _open_windows_pinned_directory(root: Path) -> int:
    import ctypes
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(root),
        file_read_attributes | synchronize,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        raise StableFileReadError(
            f"could not pin trusted root {root}: {ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        final_path = _windows_handle_final_path(handle)
        if _path_key(final_path) != _path_key(root):
            raise StableFileRedirectionError(
                f"trusted root resolves through redirection: {root}"
            )
        attributes = ctypes.WinDLL("kernel32", use_last_error=True).GetFileAttributesW(
            str(root)
        )
        if attributes == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        file_attribute_directory = 0x00000010
        file_attribute_reparse_point = 0x00000400
        if not attributes & file_attribute_directory:
            raise StableFileTypeError(f"trusted root is not a directory: {root}")
        if attributes & file_attribute_reparse_point:
            raise StableFileRedirectionError(
                f"trusted root must not be a reparse point: {root}"
            )
        return int(handle)
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _create_windows_temporary(root: Path) -> tuple[int, Path]:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_write = 0x40000000
    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    file_share_delete = 0x00000004
    create_new = 1
    file_attribute_normal = 0x00000080
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    for _ in range(128):
        path = root / f".stable-write.{uuid.uuid4().hex}.tmp"
        handle = create_file(
            str(path),
            generic_write | delete_access | file_read_attributes,
            file_share_delete,
            None,
            create_new,
            file_attribute_normal,
            None,
        )
        if handle != invalid_handle_value:
            try:
                descriptor = msvcrt.open_osfhandle(
                    int(handle),
                    os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
            except Exception:
                kernel32.CloseHandle(handle)
                raise
            return descriptor, path
        error = ctypes.get_last_error()
        if error not in {80, 183}:
            raise StableFileReadError(
                f"could not create atomic temporary file in {root}: {ctypes.WinError(error)}"
            )
    raise StableFileReadError("could not allocate a unique atomic temporary filename")


def _write_atomic_windows(
    lease: StableDirectoryLease,
    target: Path,
    payload: bytes,
) -> str:
    root = lease.root
    temporary_descriptor = -1
    temporary_path: Path | None = None
    replaced = False
    try:
        lease.revalidate()
        temporary_descriptor, temporary_path = _create_windows_temporary(root)
        temporary_final = _descriptor_final_path(temporary_descriptor)
        if temporary_final is None:
            raise StableFileVerificationUnavailableError(
                "Windows could not obtain the atomic temporary-file path"
            )
        if _path_key(temporary_final.parent) != _path_key(root):
            raise StableFileBoundaryError(
                f"atomic temporary file was created outside the pinned root: {temporary_final}"
            )
        temporary_before = os.fstat(temporary_descriptor)
        if _identity(temporary_before) is None:
            raise StableFileVerificationUnavailableError(
                "Windows did not expose stable temporary-file identity"
            )
        _write_all(temporary_descriptor, payload)
        lease.revalidate()
        try:
            os.replace(temporary_path, target)
        except OSError as exc:
            raise StableFileReadError(
                f"could not atomically replace stable file {target}: {exc}"
            ) from exc
        replaced = True
        try:
            final_path = _descriptor_final_path(temporary_descriptor)
            if final_path is None or _path_key(final_path) != _path_key(target):
                raise StableFileChangedError(
                    f"atomic temporary descriptor did not become target {target}"
                )
            path_after = os.stat(target, follow_symlinks=False)
            opened_after = os.fstat(temporary_descriptor)
            _assert_same_identity(opened_after, path_after, target=target)
            if _signature(opened_after) != _signature(path_after):
                raise StableFileChangedError(
                    f"atomic replacement target changed before verification: {target}"
                )
            lease.revalidate()
        except Exception as exc:
            raise StableFileCommitUnknownError(
                f"atomic replacement may be visible but could not be verified: {target}: {exc}"
            ) from exc
        return "committed"
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_path is not None and not replaced:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_atomic_stable_file_with_lease(
    lease: StableDirectoryLease,
    target: Path,
    payload: bytes,
) -> StableFileWriteResult:
    try:
        observation = _read_stable_regular_file_with_lease(
            lease,
            target,
            reject_redirection=True,
            capture_bytes=True,
        )
    except StableFileMissingError:
        observation = None
    if observation is not None and observation.data == payload:
        return StableFileWriteResult(wrote=False, commit_state="unchanged")

    if os.name == "nt":
        commit_state = _write_atomic_windows(lease, target, payload)
    else:
        commit_state = _write_atomic_posix(lease, target, payload)
    return StableFileWriteResult(wrote=True, commit_state=commit_state)


def write_atomic_stable_file(
    trusted_root: str | Path,
    path: str | Path,
    payload: bytes,
    *,
    root_lease: StableDirectoryLease | None = None,
) -> StableFileWriteResult:
    """Atomically replace one direct-child file with explicit commit semantics."""

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(path)
    if target.parent != root or target.name in {"", ".", ".."}:
        raise StableFileBoundaryError(
            f"atomic stable-file destination must be a direct child of {root}: {target}"
        )
    if root_lease is not None:
        if not isinstance(root_lease, StableDirectoryLease):
            raise TypeError("root_lease must be a StableDirectoryLease")
        if _path_key(root_lease.root) != _path_key(root):
            raise StableFileBoundaryError(
                f"root lease does not match atomic-write trusted root: {root}"
            )
        return _write_atomic_stable_file_with_lease(root_lease, target, payload)
    with lease_stable_directory(root) as lease:
        return _write_atomic_stable_file_with_lease(lease, target, payload)


def _open_windows_lock_descriptor(target: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_always = 4
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(target),
        generic_read | generic_write | file_read_attributes | synchronize,
        file_share_read | file_share_write,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        raise StableFileReadError(
            f"could not open stable lock file {target}: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _open_stable_lock_descriptor(
    lease: StableDirectoryLease,
    lock_file: Path,
) -> int:
    if lock_file.parent != lease.root or lock_file.name in {"", ".", ".."}:
        raise StableFileBoundaryError(
            f"stable lock file must be a direct child of {lease.root}: {lock_file}"
        )
    lease.revalidate()
    if os.name == "nt":
        descriptor = _open_windows_lock_descriptor(lock_file)
    else:
        flags = os.O_RDWR | os.O_CREAT
        for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW"):
            flags |= getattr(os, optional_flag, 0)
        try:
            descriptor = os.open(
                lock_file.name,
                flags,
                0o600,
                dir_fd=lease.descriptor,
            )
        except OSError as exc:
            if exc.errno == getattr(errno, "ELOOP", None):
                raise StableFileRedirectionError(
                    f"stable lock file must not be a symbolic link: {lock_file}"
                ) from exc
            raise StableFileReadError(
                f"could not open stable lock file {lock_file}: {exc}"
            ) from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(lock_file)
        if _is_reparse_point(current):
            raise StableFileRedirectionError(
                f"stable lock file must not be a symbolic link or reparse point: {lock_file}"
            )
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
            raise StableFileTypeError(
                f"stable lock file must be a regular file: {lock_file}"
            )
        _assert_same_identity(opened, current, target=lock_file)
        final_path = _descriptor_final_path(descriptor)
        if final_path is None:
            raise StableFileVerificationUnavailableError(
                f"the platform cannot verify stable lock file path: {lock_file}"
            )
        if _path_key(final_path) != _path_key(lock_file):
            raise StableFileRedirectionError(
                f"stable lock file resolves through redirection: {lock_file}"
            )
        lease.bind_lock(descriptor, lock_file)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _try_platform_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_platform_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _lock_contention(exc: OSError) -> bool:
    return exc.errno in {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EDEADLK", errno.EACCES),
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    } or getattr(exc, "winerror", None) in {33, 36}


@contextmanager
def exclusive_stable_file_lock(
    trusted_root: str | Path,
    lock_file: str | Path,
    *,
    timeout_seconds: float,
    retry_seconds: float = 0.01,
) -> Iterator[StableDirectoryLease]:
    """Hold a persistent OS lock and its pinned trusted root for one transaction."""

    for value, label in (
        (timeout_seconds, "timeout_seconds"),
        (retry_seconds, "retry_seconds"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{label} must be a positive finite number")
    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(lock_file)
    descriptor = -1
    acquired = False
    with lease_stable_directory(root) as lease:
        try:
            descriptor = _open_stable_lock_descriptor(lease, target)
            deadline = time.monotonic() + float(timeout_seconds)
            while True:
                try:
                    _try_platform_lock(descriptor)
                    acquired = True
                    break
                except OSError as exc:
                    if not _lock_contention(exc):
                        raise StableFileReadError(
                            f"could not acquire stable lock {target}: {exc}"
                        ) from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StableFileLockTimeoutError(
                            f"timed out waiting for stable lock: {target}"
                        ) from exc
                    time.sleep(min(float(retry_seconds), remaining))
            if os.fstat(descriptor).st_size < 1:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.write(descriptor, b"\0") != 1:
                    raise StableFileReadError(
                        f"could not initialize stable lock file: {target}"
                    )
                os.fsync(descriptor)
            lease.revalidate()
            yield lease
        finally:
            if descriptor >= 0:
                if acquired:
                    try:
                        _unlock_platform_lock(descriptor)
                    except OSError:
                        pass
                lease.unbind_lock()
                os.close(descriptor)
