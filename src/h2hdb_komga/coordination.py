__all__ = [
    "ACTIVATING_MARKER_NAME",
    "PUBLICATION_LOCK_NAME",
    "LibraryReadCoordinator",
    "LibraryUnavailable",
]

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

PUBLICATION_LOCK_NAME = "publication.lock"
ACTIVATING_MARKER_NAME = "ACTIVATING"


class LibraryUnavailable(RuntimeError):
    """The public library cannot be read during an activation transition."""


def _open_directory_without_symlinks(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    normalized = Path(os.path.abspath(path))
    descriptor = os.open(normalized.anchor, flags)
    try:
        for component in normalized.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_publication_lock(coordination_descriptor: int) -> int:
    # O_NONBLOCK prevents a damaged FIFO leaf from hanging before fstat rejects it.
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(
        PUBLICATION_LOCK_NAME,
        flags,
        dir_fd=coordination_descriptor,
    )
    try:
        lock_stat = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(descriptor)
        raise OSError(errno.EINVAL, "publication lock is not a regular file")
    return descriptor


def _activation_marker_exists(coordination_descriptor: int) -> bool:
    try:
        os.stat(
            ACTIVATING_MARKER_NAME,
            dir_fd=coordination_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LibraryUnavailable(
            "Library activation state cannot be verified"
        ) from error
    return True


class LibraryReadCoordinator:
    """Coordinate a complete Komga sync with ingest library activation."""

    def __init__(self, coordination_root: Path) -> None:
        self.coordination_root = coordination_root

    @contextmanager
    def read(self) -> Iterator[None]:
        coordination_descriptor = -1
        lock_descriptor = -1
        locked = False
        try:
            try:
                coordination_descriptor = _open_directory_without_symlinks(
                    self.coordination_root
                )
                lock_descriptor = _open_publication_lock(coordination_descriptor)
                fcntl.flock(lock_descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                raise LibraryUnavailable(
                    "Library publication is temporarily unavailable"
                ) from error
            if _activation_marker_exists(coordination_descriptor):
                raise LibraryUnavailable("Library publication is activating")
            yield
        finally:
            try:
                if locked:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                if lock_descriptor >= 0:
                    os.close(lock_descriptor)
                if coordination_descriptor >= 0:
                    os.close(coordination_descriptor)
