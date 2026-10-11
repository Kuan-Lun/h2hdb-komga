"""Local shell entry points preserve repository path bytes and Git authority."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_GATE_ENTRIES = ("check-fast.sh", "check-full.sh")
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX shell entry points")


def _environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return environment


@pytest.fixture(params=("checkout\ninside", "checkout\n\n"))
def repository(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    name = request.param
    assert isinstance(name, str)
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ("git", "init", "-b", "main", str(root)),
        env=_environment(),
        check=True,
        capture_output=True,
        timeout=10,
    )
    return root


def _copy(root: Path, relative: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ROOT / relative, destination)


def _shell(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize("entry", _GATE_ENTRIES)
def test_quality_gate_uses_its_source_root_with_line_breaks(
    repository: Path,
    tmp_path: Path,
    entry: str,
) -> None:
    for name in _GATE_ENTRIES:
        _copy(repository, f"scripts/{name}")
    log = tmp_path / "calls.jsonl"
    for relative in (
        ".venv/bin/ruff",
        ".venv/bin/mypy",
        ".venv/bin/python",
        "node_modules/.bin/markdownlint-cli2",
        "bin/node",
    ):
        executable = repository / relative
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(log)!r}, 'a') as output:\n"
            "    output.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv}) + '\\n')\n"
        )
        executable.chmod(0o755)
    environment = _environment()
    environment["PATH"] = str(repository / "bin") + os.pathsep + os.environ["PATH"]

    nested = repository / "nested"
    nested.mkdir()
    result = subprocess.run(
        ("bash", str(repository / "scripts" / entry)),
        cwd=nested,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls
    assert all(call["cwd"] == str(repository) for call in calls)
    for call in calls:
        arguments = call["argv"]
        if "--repository" in arguments:
            assert arguments[arguments.index("--repository") + 1] in (
                ".",
                str(repository),
            )


@pytest.mark.parametrize(
    "entry,shared",
    (
        ("detect-primary-branch.sh", "primary-branch.sh"),
        ("git-flow-merge.sh", "git-flow-merge.sh"),
        ("review-merge-candidate.sh", "review-merge-candidate.sh"),
        ("install-git-hooks.sh", "install-git-hooks.sh"),
    ),
)
def test_shared_shell_entry_forwards_exact_source_root(
    repository: Path,
    tmp_path: Path,
    entry: str,
    shared: str,
) -> None:
    _copy(repository, f"scripts/{entry}")
    _shell(
        repository,
        f"scripts/_shared/h2h_dev_tools/{shared}",
        'printf \'%s\\0%s\\0\' "$1" "$PWD" > "$PATH_RECEIPT"\n',
    )
    receipt = tmp_path / "root.bin"
    result = subprocess.run(
        ("bash", str(repository / "scripts" / entry)),
        cwd=tmp_path,
        env=_environment() | {"PATH_RECEIPT": str(receipt)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert receipt.read_bytes().split(b"\0") == [
        os.fsencode(repository),
        os.fsencode(repository),
        b"",
    ]


def test_commit_hook_preserves_relative_message_and_git_authority(
    repository: Path,
    tmp_path: Path,
) -> None:
    for relative in (
        ".githooks/commit-msg",
        "scripts/_shared/h2h_dev_tools/commit-message.sh",
    ):
        _copy(repository, relative)
    (tmp_path / "message").write_text("fix: preserve source path\n")
    result = subprocess.run(
        ("bash", str(repository / ".githooks/commit-msg"), "message"),
        cwd=tmp_path,
        env=_environment()
        | {"GIT_DIR": str(repository / ".git"), "GIT_WORK_TREE": str(repository)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rebase_hook_preserves_primary_protection_with_line_breaks(
    repository: Path,
) -> None:
    for relative in (
        ".githooks/pre-rebase",
        "scripts/detect-primary-branch.sh",
        "scripts/_shared/h2h_dev_tools/primary-branch.sh",
    ):
        _copy(repository, relative)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Path Test",
            "-c",
            "user.email=path@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test: initialize",
        ),
        env=_environment(),
        check=True,
        capture_output=True,
        timeout=10,
    )
    result = subprocess.run(
        ("bash", str(repository / ".githooks/pre-rebase"), "upstream", "main"),
        cwd=repository,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert "Rebasing primary is not allowed" in result.stderr


@pytest.mark.parametrize("hook", ("pre-commit", "pre-merge-commit"))
def test_gate_hooks_dispatch_from_git_root_with_line_breaks(
    repository: Path,
    tmp_path: Path,
    hook: str,
) -> None:
    _copy(repository, f".githooks/{hook}")
    _shell(repository, "scripts/detect-primary-branch.sh", "printf 'main\\n'\n")
    if hook == "pre-commit":
        subprocess.run(
            ("git", "-C", str(repository), "symbolic-ref", "HEAD", "refs/heads/task"),
            env=_environment(),
            check=True,
            capture_output=True,
            timeout=10,
        )
    _shell(
        repository,
        ".venv/bin/python",
        "if [[ \"$1\" == scripts/check_change_scope.py ]]; then printf 'full\\n'; fi\n",
    )
    for gate in ("check-fast.sh", "check-full.sh"):
        _shell(
            repository, f"scripts/{gate}", 'printf \'%s\\0\' "$PWD" > "$PATH_RECEIPT"\n'
        )
    receipt = tmp_path / "gate-root.bin"
    result = subprocess.run(
        ("bash", str(repository / ".githooks" / hook)),
        cwd=tmp_path,
        env=_environment()
        | {
            "GIT_DIR": str(repository / ".git"),
            "GIT_WORK_TREE": str(repository),
            "PATH_RECEIPT": str(receipt),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert receipt.read_bytes() == os.fsencode(repository) + b"\0"


@pytest.mark.parametrize(
    "entry", ("format.sh", "rebuild-env.sh", "install-git-hooks.sh")
)
def test_mutating_entries_do_not_select_a_truncated_neighbor(
    tmp_path: Path,
    entry: str,
) -> None:
    target, neighbor = tmp_path / "checkout\n\n", tmp_path / "checkout"
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "relative.whl").write_bytes(b"explicit caller input")
    for root in (target, neighbor):
        root.mkdir()
        subprocess.run(
            ("git", "init", "-b", "main", str(root)),
            env=_environment(),
            check=True,
            capture_output=True,
            timeout=10,
        )
        _copy(root, f"scripts/{entry}")
        if entry == "format.sh":
            for executable in (".venv/bin/ruff", "node_modules/.bin/markdownlint-cli2"):
                _shell(root, executable, ': > "$PWD/modified-by-entry"\n')
        else:
            _shell(
                root,
                f"scripts/_shared/h2h_dev_tools/{entry}",
                'printf \'%s\\0\' "$PWD" "$@" > "$1/modified-by-entry"\n',
            )
    arguments = ("--source", "h2hdb=relative.whl") if entry == "rebuild-env.sh" else ()
    result = subprocess.run(
        ("bash", str(target / "scripts" / entry), *arguments),
        cwd=target if entry == "format.sh" else caller,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (target / "modified-by-entry").exists()
    assert not (neighbor / "modified-by-entry").exists()
    if entry == "rebuild-env.sh":
        received = (target / "modified-by-entry").read_bytes().split(b"\0")
        assert received[0] == os.fsencode(caller)
        assert received[1] == os.fsencode(target)
        assert received[-3:] == [b"--source", b"h2hdb=relative.whl", b""]
        assert (
            Path(os.fsdecode(received[0])) / "relative.whl"
        ).read_bytes() == b"explicit caller input"


@pytest.mark.parametrize("entry", ["rebuild-env.sh", "detect-primary-branch.sh"])
@pytest.mark.parametrize("foreign", [False, True])
def test_relative_entry_ignores_cdpath_search_and_output(
    repository: Path, tmp_path: Path, entry: str, foreign: bool
) -> None:
    neighbor = tmp_path / "foreign"
    neighbor.mkdir()
    subprocess.run(
        ("git", "init", "-b", "main", str(neighbor)),
        env=_environment(),
        check=True,
        capture_output=True,
        timeout=10,
    )
    shared = "primary-branch.sh" if entry == "detect-primary-branch.sh" else entry
    for root in (repository, neighbor):
        _copy(root, f"scripts/{entry}")
        _shell(
            root,
            f"scripts/_shared/h2h_dev_tools/{shared}",
            "printf '%s\\0' \"$1\" > \"$1/selected-root\"\nprintf 'main\\n'\n",
        )
    result = subprocess.run(
        ("bash", f"scripts/{entry}"),
        cwd=repository,
        env=_environment() | {"CDPATH": str(neighbor) if foreign else "."},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "main\n"
    assert (repository / "selected-root").read_bytes() == os.fsencode(
        repository
    ) + b"\0"
    assert not (neighbor / "selected-root").exists()
