#!/usr/bin/env python3
"""Portable descriptor-backed advisory file locks.

Lock files are stable coordination points: they are created when needed, held by
an open descriptor, and deliberately left in place after release.  A small
process-local registry adds same-thread reentrancy and makes different threads
in one process contend consistently before the platform lock is attempted.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import stat
import threading
import time
from typing import Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_LOCK_RETRY_SECONDS = 0.02
_LOCK_BYTE = b"\0"
_LOCK_LENGTH = 1


class AdvisoryLockError(RuntimeError):
    """Raised when a stable advisory lock cannot be prepared or acquired."""


class AdvisoryLockTimeoutError(AdvisoryLockError):
    """Raised when an advisory lock remains unavailable through its deadline."""


@dataclass
class _HeldLock:
    owner_thread_id: int
    depth: int = 1
    descriptor: int | None = None


@dataclass(frozen=True)
class _LockLease:
    key: str
    state: _HeldLock


_REGISTRY_CONDITION = threading.Condition()
_HELD_LOCKS: dict[str, _HeldLock] = {}


def _positive_finite_seconds(value: float, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{label} must be a positive finite number")
    return float(value)


def _normalize_lock_path(lock_file: str | Path) -> tuple[Path, str]:
    try:
        path = Path(lock_file).expanduser()
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AdvisoryLockError(
            f"invalid advisory lock path {lock_file!r}: {exc}"
        ) from exc
    key = os.path.normcase(os.path.normpath(os.fspath(absolute)))
    return absolute, key


def _validate_existing_lock_file(lock_file: Path) -> None:
    try:
        current = os.lstat(lock_file)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AdvisoryLockError(
            f"could not inspect advisory lock file {lock_file}: {exc}"
        ) from exc
    if stat.S_ISLNK(current.st_mode):
        raise AdvisoryLockError(
            f"advisory lock file must not be a symbolic link: {lock_file}"
        )
    if not stat.S_ISREG(current.st_mode):
        raise AdvisoryLockError(
            f"advisory lock file must be a regular file: {lock_file}"
        )


def _close_descriptor_no_raise(descriptor: int) -> None:
    try:
        os.close(descriptor)
        return
    except Exception:
        pass
    try:
        os.fdopen(descriptor, "rb", closefd=True).close()
    except Exception:
        # Closing is best-effort cleanup. Reporting a committed operation as a
        # failure would invite an unsafe retry, and process exit is the final
        # platform backstop for an otherwise unreleasable descriptor.
        pass


def _reset_registry_after_fork() -> None:
    global _REGISTRY_CONDITION

    inherited_descriptors = {
        state.descriptor
        for state in _HELD_LOCKS.values()
        if state.descriptor is not None
    }
    _HELD_LOCKS.clear()
    # A condition may be inherited while owned by a thread that does not exist in
    # the child. Replace it rather than trying to acquire the inherited mutex.
    _REGISTRY_CONDITION = threading.Condition()
    for descriptor in inherited_descriptors:
        _close_descriptor_no_raise(descriptor)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_registry_after_fork)


def _open_lock_descriptor(lock_file: Path) -> int:
    _validate_existing_lock_file(lock_file)
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_file, flags, 0o600)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", None):
            raise AdvisoryLockError(
                f"advisory lock file must not be a symbolic link: {lock_file}"
            ) from exc
        raise AdvisoryLockError(
            f"could not open advisory lock file {lock_file}: {exc}"
        ) from exc

    try:
        opened = os.fstat(descriptor)
        current = os.lstat(lock_file)
        if stat.S_ISLNK(current.st_mode):
            raise AdvisoryLockError(
                f"advisory lock file must not be a symbolic link: {lock_file}"
            )
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
            raise AdvisoryLockError(
                f"advisory lock file must be a regular file: {lock_file}"
            )
        if not os.path.samestat(opened, current):
            raise AdvisoryLockError(
                f"advisory lock file changed while it was being opened: {lock_file}"
            )
        if opened.st_size < _LOCK_LENGTH:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.write(descriptor, _LOCK_BYTE) != len(_LOCK_BYTE):
                raise AdvisoryLockError(
                    f"could not initialize advisory lock file {lock_file}"
                )
        return descriptor
    except AdvisoryLockError:
        _close_descriptor_no_raise(descriptor)
        raise
    except OSError as exc:
        _close_descriptor_no_raise(descriptor)
        raise AdvisoryLockError(
            f"could not validate advisory lock file {lock_file}: {exc}"
        ) from exc


def _try_lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, _LOCK_LENGTH)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, _LOCK_LENGTH)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _is_lock_contention(exc: OSError) -> bool:
    contention_errnos = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EDEADLK", errno.EACCES),
        getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
    }
    return exc.errno in contention_errnos or getattr(exc, "winerror", None) in {
        33,  # ERROR_LOCK_VIOLATION
        36,  # ERROR_SHARING_BUFFER_EXCEEDED
    }


def _timeout_error(lock_file: Path, timeout_seconds: float) -> AdvisoryLockTimeoutError:
    return AdvisoryLockTimeoutError(
        f"timed out after {timeout_seconds:g} seconds waiting for advisory lock: "
        f"{lock_file}"
    )


def _claim_process_slot(
    key: str,
    lock_file: Path,
    *,
    deadline: float,
    timeout_seconds: float,
) -> tuple[_LockLease, bool]:
    owner_thread_id = threading.get_ident()
    waited = False
    with _REGISTRY_CONDITION:
        while True:
            state = _HELD_LOCKS.get(key)
            if state is None:
                if waited and time.monotonic() >= deadline:
                    raise _timeout_error(lock_file, timeout_seconds)
                state = _HeldLock(owner_thread_id=owner_thread_id)
                _HELD_LOCKS[key] = state
                return _LockLease(key=key, state=state), False
            if state.owner_thread_id == owner_thread_id:
                state.depth += 1
                return _LockLease(key=key, state=state), True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _timeout_error(lock_file, timeout_seconds)
            waited = True
            _REGISTRY_CONDITION.wait(timeout=remaining)


def _acquire_platform_lock(
    descriptor: int,
    lock_file: Path,
    *,
    deadline: float,
    timeout_seconds: float,
    retry_seconds: float,
) -> None:
    first_attempt = True
    while True:
        if not first_attempt and time.monotonic() >= deadline:
            raise _timeout_error(lock_file, timeout_seconds)
        first_attempt = False
        try:
            _try_lock_descriptor(descriptor)
            return
        except OSError as exc:
            if not _is_lock_contention(exc):
                raise AdvisoryLockError(
                    f"could not acquire advisory lock {lock_file}: {exc}"
                ) from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _timeout_error(lock_file, timeout_seconds) from exc
            time.sleep(min(retry_seconds, remaining))


def _abandon_process_slot(lease: _LockLease) -> None:
    with _REGISTRY_CONDITION:
        if _HELD_LOCKS.get(lease.key) is lease.state:
            del _HELD_LOCKS[lease.key]
            _REGISTRY_CONDITION.notify_all()


def _acquire_lock(
    lock_file: Path,
    key: str,
    *,
    timeout_seconds: float,
    retry_seconds: float,
) -> _LockLease:
    deadline = time.monotonic() + timeout_seconds
    lease, reentrant = _claim_process_slot(
        key,
        lock_file,
        deadline=deadline,
        timeout_seconds=timeout_seconds,
    )
    if reentrant:
        return lease

    descriptor: int | None = None
    try:
        descriptor = _open_lock_descriptor(lock_file)
        _acquire_platform_lock(
            descriptor,
            lock_file,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            retry_seconds=retry_seconds,
        )
        with _REGISTRY_CONDITION:
            if _HELD_LOCKS.get(key) is not lease.state:
                raise AdvisoryLockError(
                    f"advisory lock ownership changed unexpectedly: {lock_file}"
                )
            lease.state.descriptor = descriptor
        return lease
    except BaseException:
        if descriptor is not None:
            _close_descriptor_no_raise(descriptor)
        _abandon_process_slot(lease)
        raise


def _current_thread_advisory_lock_descriptor(
    lock_file: str | Path,
) -> int | None:
    """Return a borrowed descriptor for an exact same-thread held lock.

    This is an internal coordination primitive for code which has to compose
    the advisory lock with a descriptor-pinned transaction.  The returned
    descriptor remains owned by :class:`AdvisoryFileLock`; callers must never
    close or unlock it.  A lock held by another thread, another process, or a
    different lexical path is deliberately reported as absent.
    """

    _path, key = _normalize_lock_path(lock_file)
    owner_thread_id = threading.get_ident()
    with _REGISTRY_CONDITION:
        state = _HELD_LOCKS.get(key)
        if state is None or state.owner_thread_id != owner_thread_id:
            return None
        return state.descriptor


def _release_lock_no_raise(lease: _LockLease) -> None:
    descriptor: int | None = None
    try:
        with _REGISTRY_CONDITION:
            if _HELD_LOCKS.get(lease.key) is not lease.state:
                return
            lease.state.depth -= 1
            if lease.state.depth > 0:
                return
            descriptor = lease.state.descriptor
            lease.state.descriptor = None

        if descriptor is not None:
            try:
                _unlock_descriptor(descriptor)
            except Exception:
                pass
            _close_descriptor_no_raise(descriptor)
    finally:
        if lease.state.depth <= 0:
            _abandon_process_slot(lease)


class AdvisoryFileLock:
    """A stable, exclusive advisory lock with bounded acquisition time."""

    def __init__(
        self,
        lock_file: str | Path,
        *,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        retry_seconds: float = DEFAULT_LOCK_RETRY_SECONDS,
    ) -> None:
        self.path, self._key = _normalize_lock_path(lock_file)
        self.timeout_seconds = _positive_finite_seconds(
            timeout_seconds,
            label="timeout_seconds",
        )
        self.retry_seconds = _positive_finite_seconds(
            retry_seconds,
            label="retry_seconds",
        )
        self._leases: list[_LockLease] = []
        self._instance_lock = threading.Lock()

    def acquire(self) -> AdvisoryFileLock:
        """Acquire the lock or raise after the configured timeout."""

        lease = _acquire_lock(
            self.path,
            self._key,
            timeout_seconds=self.timeout_seconds,
            retry_seconds=self.retry_seconds,
        )
        with self._instance_lock:
            self._leases.append(lease)
        return self

    def release(self) -> None:
        """Release one acquisition without allowing cleanup errors to escape."""

        with self._instance_lock:
            if not self._leases:
                return
            lease = self._leases.pop()
        try:
            _release_lock_no_raise(lease)
        except Exception:
            # Release must not transform a successfully committed operation into
            # a reported failure. The descriptor cleanup above remains best-effort.
            pass

    def __enter__(self) -> AdvisoryFileLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.release()
        return False


@contextmanager
def advisory_file_lock(
    lock_file: str | Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    retry_seconds: float = DEFAULT_LOCK_RETRY_SECONDS,
) -> Iterator[AdvisoryFileLock]:
    """Hold a stable advisory lock for the duration of a context manager."""

    lock = AdvisoryFileLock(
        lock_file,
        timeout_seconds=timeout_seconds,
        retry_seconds=retry_seconds,
    )
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
