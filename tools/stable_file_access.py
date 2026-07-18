#!/usr/bin/env python3
"""Descriptor-bound reads for regular files beneath a trusted local root.

The helpers in this module open a lexical path once, prove the opened descriptor
still resolves beneath the trusted root before reading any bytes, and then verify
that the descriptor and pathname stayed stable for the whole read.  Callers may
also reject every symbolic-link/reparse-point redirection for machine-state files.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
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


@dataclass(frozen=True)
class StableFileReadResult:
    """Stable descriptor-bound bytes/hash observation."""

    lexical_path: Path
    final_path: Path
    size_bytes: int
    content_sha256: str
    data: bytes | None


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


def read_stable_regular_file(
    trusted_root: str | Path,
    path: str | Path,
    *,
    reject_redirection: bool,
    capture_bytes: bool = True,
) -> StableFileReadResult:
    """Read/hash one stable regular file without crossing ``trusted_root``.

    The descriptor's final path is checked before the first byte is read. On
    platforms without a descriptor-path primitive or stable file identity, the
    operation fails closed before reading. The same descriptor is used for the
    complete read and checked again before close.
    """

    if not isinstance(reject_redirection, bool):
        raise TypeError("reject_redirection must be a bool")
    if not isinstance(capture_bytes, bool):
        raise TypeError("capture_bytes must be a bool")

    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(path)
    if not _is_within(target, root):
        raise StableFileBoundaryError(
            f"file path is outside the trusted lexical root: {target}"
        )
    try:
        root_metadata = os.lstat(root)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise StableFileReadError(f"trusted root is unavailable: {root}: {exc}") from exc
    if _is_reparse_point(root_metadata) or _path_key(resolved_root) != _path_key(root):
        raise StableFileRedirectionError(
            f"trusted root must not be a symbolic link or reparse point: {root}"
        )
    if not stat.S_ISDIR(root_metadata.st_mode) or not resolved_root.is_dir():
        raise StableFileTypeError(f"trusted root is not a directory: {root}")

    if reject_redirection:
        _preflight_no_redirection(root, target)
    else:
        try:
            resolved_before = target.resolve(strict=True)
        except FileNotFoundError as exc:
            raise StableFileMissingError(f"file path is unavailable: {target}") from exc
        except OSError as exc:
            raise StableFileReadError(f"could not resolve file path {target}: {exc}") from exc
        if not _is_within(resolved_before, resolved_root):
            raise StableFileBoundaryError(
                f"file path resolves outside the trusted root: {target}"
            )
        if not resolved_before.is_file():
            raise StableFileTypeError(f"file path is not a regular file: {target}")

    flags = os.O_RDONLY
    # O_NONBLOCK prevents a raced FIFO/device replacement from blocking before
    # the descriptor type check; it is inert for ordinary regular files.
    for optional_flag in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    if reject_redirection:
        flags |= getattr(os, "O_NOFOLLOW", 0)

    descriptor = -1
    try:
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError as exc:
            raise StableFileMissingError(f"file path is unavailable: {target}") from exc
        except OSError as exc:
            raise StableFileReadError(f"could not open file path {target}: {exc}") from exc

        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise StableFileTypeError(f"opened path is not a regular file: {target}")

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

        path_before = _path_metadata(target, reject_redirection=reject_redirection)
        _assert_same_identity(opened_before, path_before, target=target)

        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture_bytes else None
        total = 0
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
        try:
            final_after = _descriptor_final_path(descriptor)
        except OSError as exc:
            raise StableFileReadError(
                f"could not reverify opened descriptor path for {target}: {exc}"
            ) from exc
        if final_after is None:
            raise StableFileVerificationUnavailableError(
                "the platform cannot reverify the opened descriptor's final path: "
                f"{target}"
            )
        if not _is_within(final_after, resolved_root):
            raise StableFileBoundaryError(
                f"opened descriptor moved outside the trusted root: {target}"
            )
        if _path_key(final_after) != _path_key(final_path):
            raise StableFileChangedError(
                f"opened descriptor path changed while reading: {target}"
            )

        payload = b"".join(chunks) if chunks is not None else None
        return StableFileReadResult(
            lexical_path=target,
            final_path=final_path,
            size_bytes=total,
            content_sha256=digest.hexdigest(),
            data=payload,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)




@contextmanager
def lease_stable_regular_file(
    trusted_root: str | Path,
    path: str | Path,
    *,
    reject_redirection: bool,
) -> Iterator[StableFileLease]:
    """Open, hash, and retain one verified regular-file descriptor.

    The caller may perform a related machine-state commit while the descriptor is
    retained and then call :meth:`StableFileLease.revalidate` to close the
    verification-to-commit gap. The lease never permits writes through the source
    descriptor.
    """

    if not isinstance(reject_redirection, bool):
        raise TypeError("reject_redirection must be a bool")

    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(path)
    if not _is_within(target, root):
        raise StableFileBoundaryError(
            f"file path is outside the trusted lexical root: {target}"
        )
    try:
        root_metadata = os.lstat(root)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise StableFileReadError(f"trusted root is unavailable: {root}: {exc}") from exc
    if _is_reparse_point(root_metadata) or _path_key(resolved_root) != _path_key(root):
        raise StableFileRedirectionError(
            f"trusted root must not be a symbolic link or reparse point: {root}"
        )
    if not stat.S_ISDIR(root_metadata.st_mode) or not resolved_root.is_dir():
        raise StableFileTypeError(f"trusted root is not a directory: {root}")

    if reject_redirection:
        _preflight_no_redirection(root, target)
    else:
        try:
            resolved_before = target.resolve(strict=True)
        except FileNotFoundError as exc:
            raise StableFileMissingError(f"file path is unavailable: {target}") from exc
        except OSError as exc:
            raise StableFileReadError(f"could not resolve file path {target}: {exc}") from exc
        if not _is_within(resolved_before, resolved_root):
            raise StableFileBoundaryError(
                f"file path resolves outside the trusted root: {target}"
            )
        if not resolved_before.is_file():
            raise StableFileTypeError(f"file path is not a regular file: {target}")

    flags = os.O_RDONLY
    # O_NONBLOCK prevents a raced FIFO/device replacement from blocking before
    # the retained descriptor is proven to be a regular file.
    for optional_flag in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    if reject_redirection:
        flags |= getattr(os, "O_NOFOLLOW", 0)

    descriptor = -1
    lease: StableFileLease | None = None
    try:
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError as exc:
            raise StableFileMissingError(f"file path is unavailable: {target}") from exc
        except OSError as exc:
            raise StableFileReadError(f"could not open file path {target}: {exc}") from exc

        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise StableFileTypeError(f"opened path is not a regular file: {target}")
        final_path = _verified_descriptor_final_path(
            descriptor,
            target=target,
            resolved_root=resolved_root,
            reject_redirection=reject_redirection,
        )
        path_before = _path_metadata(target, reject_redirection=reject_redirection)
        _assert_same_identity(opened_before, path_before, target=target)

        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            except OSError as exc:
                raise StableFileReadError(f"could not read file path {target}: {exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)

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
            resolved_root=resolved_root,
            reject_redirection=reject_redirection,
        )
        if _path_key(final_after) != _path_key(final_path):
            raise StableFileChangedError(
                f"opened descriptor path changed while reading: {target}"
            )

        lease = StableFileLease(
            descriptor=descriptor,
            lexical_path=target,
            final_path=final_path,
            resolved_root=resolved_root,
            reject_redirection=reject_redirection,
            descriptor_signature=_signature(opened_after),
            path_signature=_signature(path_after),
            size_bytes=total,
            content_sha256=digest.hexdigest(),
        )
        descriptor = -1
        try:
            yield lease
        finally:
            lease.close()
    finally:
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


def _write_atomic_posix(root: Path, target: Path, payload: bytes) -> None:
    required_dir_fd = (os.open, os.replace, os.unlink)
    if not all(function in os.supports_dir_fd for function in required_dir_fd):
        raise StableFileVerificationUnavailableError(
            "the platform cannot perform handle-relative atomic replacement"
        )
    root_flags = os.O_RDONLY
    for optional_flag in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW"):
        root_flags |= getattr(os, optional_flag, 0)
    root_descriptor = -1
    temporary_descriptor = -1
    temporary_name: str | None = None
    try:
        try:
            root_descriptor = os.open(root, root_flags)
        except OSError as exc:
            raise StableFileReadError(f"could not pin trusted root {root}: {exc}") from exc
        root_metadata = os.fstat(root_descriptor)
        root_identity = _identity(root_metadata)
        if root_identity is None:
            raise StableFileVerificationUnavailableError(
                f"the platform did not expose stable identity for trusted root: {root}"
            )
        _assert_pinned_directory(
            root_descriptor,
            root=root,
            opened_identity=root_identity,
        )

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
        _assert_pinned_directory(
            root_descriptor,
            root=root,
            opened_identity=root_identity,
        )
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
        _assert_pinned_directory(
            root_descriptor,
            root=root,
            opened_identity=root_identity,
        )
        try:
            os.fsync(root_descriptor)
        except OSError as exc:
            raise StableFileReadError(
                f"could not flush trusted-root directory after atomic write: {exc}"
            ) from exc
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name is not None and root_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if root_descriptor >= 0:
            os.close(root_descriptor)


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

    delete_access = 0x00010000
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
        delete_access | file_read_attributes | synchronize,
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
    share_all = 0x00000001 | 0x00000002 | 0x00000004
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
            share_all,
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


def _write_atomic_windows(root: Path, target: Path, payload: bytes) -> None:
    import ctypes

    root_handle = _open_windows_pinned_directory(root)
    temporary_descriptor = -1
    temporary_path: Path | None = None
    replaced = False
    try:
        if _path_key(_windows_handle_final_path(root_handle)) != _path_key(root):
            raise StableFileChangedError(f"trusted root moved before atomic write: {root}")
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
        if _path_key(_windows_handle_final_path(root_handle)) != _path_key(root):
            raise StableFileChangedError(f"trusted root moved during atomic write: {root}")
        try:
            os.replace(temporary_path, target)
        except OSError as exc:
            raise StableFileReadError(
                f"could not atomically replace stable file {target}: {exc}"
            ) from exc
        replaced = True
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
        if _path_key(_windows_handle_final_path(root_handle)) != _path_key(root):
            raise StableFileChangedError(f"trusted root moved after atomic write: {root}")
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_path is not None and not replaced:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(root_handle)


def write_atomic_stable_file(
    trusted_root: str | Path,
    path: str | Path,
    payload: bytes,
) -> bool:
    """Atomically replace one direct-child file without following a raced root.

    The machine-state root is pinned before temporary creation and replacement.
    POSIX uses directory-descriptor-relative operations; Windows holds a directory
    handle with DELETE access and without delete sharing so the root and its
    ancestor path cannot be replaced during the operation.
    """

    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    root = _absolute_lexical(trusted_root)
    target = _absolute_lexical(path)
    if target.parent != root or target.name in {"", ".", ".."}:
        raise StableFileBoundaryError(
            f"atomic stable-file destination must be a direct child of {root}: {target}"
        )
    try:
        observation = read_stable_regular_file(
            root,
            target,
            reject_redirection=True,
            capture_bytes=True,
        )
    except StableFileMissingError:
        observation = None
    if observation is not None and observation.data == payload:
        return False

    if os.name == "nt":
        _write_atomic_windows(root, target, payload)
    else:
        _write_atomic_posix(root, target, payload)
    return True
