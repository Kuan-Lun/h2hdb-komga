import fcntl
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from h2hdb_komga.coordination import LibraryReadCoordinator, LibraryUnavailable


def _coordination_root(tmp_path: Path) -> Path:
    root = tmp_path / "coordination"
    root.mkdir()
    return root


def _symlink_lock(lock_path: Path, tmp_path: Path) -> None:
    target = tmp_path / "outside-lock"
    target.touch()
    lock_path.symlink_to(target)


def _fifo_lock(lock_path: Path, tmp_path: Path) -> None:
    del tmp_path
    os.mkfifo(lock_path)


def _directory_lock(lock_path: Path, tmp_path: Path) -> None:
    del tmp_path
    lock_path.mkdir()


@pytest.mark.parametrize(
    "make_lock",
    [_symlink_lock, _fifo_lock, _directory_lock],
    ids=["symlink", "fifo", "directory"],
)
def test_nonregular_publication_lock_fails_closed(
    tmp_path: Path,
    make_lock: Callable[[Path, Path], None],
) -> None:
    coordination_root = _coordination_root(tmp_path)
    make_lock(coordination_root / "publication.lock", tmp_path)

    with pytest.raises(LibraryUnavailable, match="temporarily unavailable"):
        with LibraryReadCoordinator(coordination_root).read():
            pytest.fail("unsafe publication lock was accepted")


def test_symlinked_coordination_root_fails_closed(tmp_path: Path) -> None:
    coordination_root = _coordination_root(tmp_path)
    (coordination_root / "publication.lock").touch()
    linked_root = tmp_path / "linked-coordination"
    linked_root.symlink_to(coordination_root, target_is_directory=True)

    with pytest.raises(LibraryUnavailable, match="temporarily unavailable"):
        with LibraryReadCoordinator(linked_root).read():
            pytest.fail("symlinked coordination root was accepted")


def test_missing_publication_lock_fails_closed(tmp_path: Path) -> None:
    coordination_root = _coordination_root(tmp_path)

    with pytest.raises(LibraryUnavailable, match="temporarily unavailable"):
        with LibraryReadCoordinator(coordination_root).read():
            pytest.fail("missing publication lock was accepted")


def test_shared_publication_lock_is_released_on_context_exit(tmp_path: Path) -> None:
    coordination_root = _coordination_root(tmp_path)
    lock_path = coordination_root / "publication.lock"
    lock_path.touch()

    with LibraryReadCoordinator(coordination_root).read():
        pass

    descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("active_marker", [False, True])
def test_parent_traversal_does_not_bypass_publication_fencing(
    tmp_path: Path, *, active_marker: bool
) -> None:
    decoy = tmp_path / "decoy"
    actual = tmp_path / "actual"
    for root in (decoy, actual):
        (root / "coordination").mkdir(parents=True)
        (root / "coordination" / "publication.lock").touch()
    (actual / "child").mkdir()
    (decoy / "link").symlink_to(actual / "child", target_is_directory=True)
    unsafe = decoy / "link" / ".." / "coordination"
    assert unsafe.resolve() == actual / "coordination"
    with (actual / "coordination" / "publication.lock").open("rb") as locked:
        if active_marker:
            (actual / "coordination" / "ACTIVATING").touch()
        else:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LibraryUnavailable):
            with LibraryReadCoordinator(unsafe).read():
                pytest.fail("parent traversal selected an unfenced decoy root")
