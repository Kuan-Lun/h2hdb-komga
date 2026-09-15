from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(repository: Path, *command: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def _git(repository: Path, *arguments: str) -> str:
    result = _run(repository, "git", *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _identity(repository: Path) -> None:
    _git(repository, "config", "user.name", "Workflow Test")
    _git(repository, "config", "user.email", "workflow@example.invalid")
    _git(repository, "config", "commit.gpgsign", "false")


def _commit(repository: Path, filename: str) -> str:
    (repository / filename).write_text(filename + "\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", f"test: add {filename}")
    return _git(repository, "rev-parse", "HEAD")


def _install(repository: Path) -> None:
    result = _run(repository, "bash", "scripts/install-git-hooks.sh")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(params=("main", "master"))
def git_repository(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    primary = str(request.param)
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--bare", f"--initial-branch={primary}", str(remote))
    _git(tmp_path, "clone", str(remote), str(repository))
    _identity(repository)
    for filename in (
        "scripts/detect-primary-branch.sh",
        "scripts/install-git-hooks.sh",
        ".githooks/pre-rebase",
    ):
        target = repository / filename
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(_PROJECT_ROOT / filename, target)
    _commit(repository, "seed")
    _git(repository, "push", "-u", "origin", primary)
    _install(repository)
    return repository


def _merge_task(repository: Path) -> tuple[str, str]:
    primary = _git(repository, "branch", "--show-current")
    _git(repository, "switch", "-c", "task/change")
    _commit(repository, "task-change")
    _git(repository, "switch", primary)
    _git(repository, "merge", "--no-edit", "task/change")
    return primary, _git(repository, "rev-list", "--parents", "-n", "1", "HEAD")


def _advance_remote(repository: Path) -> None:
    peer = repository.parent / "peer"
    remote = _git(repository, "remote", "get-url", "origin")
    _git(repository.parent, "clone", remote, str(peer))
    _identity(peer)
    _commit(peer, "remote-change")
    _git(peer, "push")


def test_installer_repairs_unsafe_settings_on_each_run(git_repository: Path) -> None:
    primary = _git(git_repository, "branch", "--show-current")
    desired = {
        "core.hooksPath": ".githooks",
        f"branch.{primary}.mergeOptions": "--no-ff",
        "pull.rebase": "false",
        f"branch.{primary}.rebase": "false",
        "pull.ff": "only",
    }
    for unsafe_rebase in ("true", "merges"):
        for key, value in {
            "core.hooksPath": "missing",
            f"branch.{primary}.mergeOptions": "--ff",
            "pull.rebase": "true",
            f"branch.{primary}.rebase": unsafe_rebase,
            "pull.ff": "true",
        }.items():
            _git(git_repository, "config", "--local", key, value)
        _install(git_repository)
        for key, value in desired.items():
            assert _git(git_repository, "config", "--local", "--get", key) == value


def test_merge_then_pull_preserves_exact_commit_and_parents(
    git_repository: Path,
) -> None:
    primary, before = _merge_task(git_repository)
    assert len(before.split()) == 3

    _git(git_repository, "pull", "--tags", "origin", primary)

    assert _git(git_repository, "rev-list", "--parents", "-n", "1", "HEAD") == before


def test_pull_rejects_divergence_without_changing_merge(git_repository: Path) -> None:
    primary, before = _merge_task(git_repository)
    _advance_remote(git_repository)

    result = _run(git_repository, "git", "pull", "--tags", "origin", primary)

    assert result.returncode != 0
    assert "fast-forward" in result.stderr
    assert _git(git_repository, "rev-list", "--parents", "-n", "1", "HEAD") == before


@pytest.mark.parametrize("explicit_branch", (None, "short", "full"))
def test_rebase_rejects_primary_even_when_another_branch_is_checked_out(
    git_repository: Path, explicit_branch: str | None
) -> None:
    primary, before = _merge_task(git_repository)
    arguments = ["rebase", "--force-rebase", f"origin/{primary}"]
    if explicit_branch is not None:
        _git(git_repository, "switch", "task/change")
        arguments.append(
            primary if explicit_branch == "short" else f"refs/heads/{primary}"
        )
    checked_out = _git(git_repository, "symbolic-ref", "HEAD")

    result = _run(git_repository, "git", *arguments)

    assert result.returncode != 0
    assert f"Rebasing primary is not allowed: {primary}" in result.stderr
    assert _git(git_repository, "rev-list", "--parents", "-n", "1", primary) == before
    assert _git(git_repository, "symbolic-ref", "HEAD") == checked_out


@pytest.mark.parametrize("configuration_override", (False, True))
def test_pull_rebase_cannot_override_primary_protection(
    git_repository: Path, configuration_override: bool
) -> None:
    primary, before = _merge_task(git_repository)
    _advance_remote(git_repository)
    arguments = ["-c", "pull.ff=true"]
    if configuration_override:
        arguments += ["-c", f"branch.{primary}.rebase=true"]
    arguments += ["pull", "--tags"]
    if not configuration_override:
        arguments.append("--rebase")
    arguments += ["origin", primary]

    result = _run(git_repository, "git", *arguments)

    assert result.returncode != 0
    assert f"Rebasing primary is not allowed: {primary}" in result.stderr
    assert _git(git_repository, "rev-list", "--parents", "-n", "1", "HEAD") == before


def test_task_branch_can_rebase(git_repository: Path) -> None:
    primary = _git(git_repository, "branch", "--show-current")
    initial_primary = _git(git_repository, "rev-parse", primary)
    _git(git_repository, "switch", "-c", "task/change")
    before = _commit(git_repository, "task-change")
    _advance_remote(git_repository)
    _git(git_repository, "fetch", "origin")

    _git(git_repository, "rebase", f"origin/{primary}")

    assert _git(git_repository, "branch", "--show-current") == "task/change"
    assert _git(git_repository, "rev-parse", "HEAD") != before
    assert _git(git_repository, "rev-parse", "HEAD^") == _git(
        git_repository, "rev-parse", f"origin/{primary}"
    )
    assert _git(git_repository, "rev-parse", primary) == initial_primary
    assert (git_repository / "task-change").exists()
    assert (git_repository / "remote-change").exists()
