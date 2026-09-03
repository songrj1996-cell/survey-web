"""Cross-platform advisory locks for already-open lock-file descriptors."""

from __future__ import annotations

import os


if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire_exclusive_file_lock(descriptor: int) -> None:
    """Block until an exclusive process lock is held for ``descriptor``."""

    if os.name == "nt":
        if os.fstat(descriptor).st_size < 1:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(descriptor, fcntl.LOCK_EX)


def release_file_lock(descriptor: int) -> None:
    """Release a lock acquired by :func:`acquire_exclusive_file_lock`."""

    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(descriptor, fcntl.LOCK_UN)
