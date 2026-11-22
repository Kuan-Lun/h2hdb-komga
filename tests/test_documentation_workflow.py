from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("offline_code_review_cli")

_ROOT = Path(__file__).resolve().parents[1]


_REVIEW_ENTRY = r"""from pathlib import Path
import os
import runpy
import sys

sys.dont_write_bytecode = True
shared_dir = Path(__file__).resolve().parent / "_shared/h2h_dev_tools"
runpy.run_path(str(shared_dir / "source_loader.py"))["load_package"](
    shared_dir, "_shared.h2h_dev_tools"
)
from _shared.h2h_dev_tools.code_review import main

trace = Path(os.environ["DOCUMENTATION_WORKFLOW_TRACE"])
with trace.open("a", encoding="utf-8") as stream:
    stream.write(sys.argv[1] + "\n")
main(Path(__file__).resolve().parents[1])
"""


def _run(repository: Path, *command: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "WORKFLOW_MERGE_TASK_REF"
    }
    environment.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        DOCUMENTATION_WORKFLOW_TRACE=str(repository.parent / "checks.log"),
    )
    return subprocess.run(
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _git(repository: Path, *arguments: str) -> str:
    result = _run(repository, "git", *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _write(repository: Path, name: str, content: str) -> None:
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _commit(repository: Path, message: str) -> None:
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message)


def _repository(tmp_path: Path, *, install_hooks: bool = True) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    for name, value in (
        ("user.name", "Documentation Workflow"),
        ("user.email", "documentation@example.invalid"),
        ("commit.gpgsign", "false"),
        ("workflow.primaryBranch", "main"),
    ):
        _git(repository, "config", name, value)
    for name in (
        "AGENTS.md",
        "scripts/detect-primary-branch.sh",
        "scripts/git-flow-merge.sh",
        "scripts/review-merge-candidate.sh",
        "scripts/check_change_scope.py",
        "scripts/check-docs.py",
        "scripts/check-version.py",
        ".githooks/pre-commit",
        ".githooks/pre-merge-commit",
        ".githooks/commit-msg",
        ".markdownlint-cli2.jsonc",
    ):
        destination = repository / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_ROOT / name, destination)
    shutil.copytree(_ROOT / "scripts/_shared", repository / "scripts/_shared")
    _write(repository, "scripts/review-code.py", _REVIEW_ENTRY)
    _write(repository, ".gitignore", ".venv/\nnode_modules/\n__pycache__/\n")
    _write(repository, "README.md", "# Fixture\n")
    tools = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]
    policy = tools["h2h"]["version"]
    dedicated_release = str(policy.get("dedicated-release-commit", False)).lower()
    _write(
        repository,
        "pyproject.toml",
        '[project]\nname = "workflow-fixture"\nversion = "0.1.0"\n'
        '[tool.h2h.version]\nrelease-paths = ["src/**"]\n'
        f"dedicated-release-commit = {dedicated_release}\n"
        'development-paths = ["README.md", "docs/**", "tests/**", "scripts/**", "AGENTS.md"]\n'
        + "[tool.h2h-dev-tools]\ncomponents = "
        + json.dumps(tools["h2h-dev-tools"]["components"])
        + "\n",
    )
    _write(
        repository,
        ".venv/bin/python",
        f'#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} "$@"\n',
    )
    (repository / ".venv/bin/python").chmod(0o755)
    (repository / "node_modules").symlink_to(
        _ROOT / "node_modules", target_is_directory=True
    )
    for profile in ("fast", "full"):
        path = repository / "scripts" / f"check-{profile}.sh"
        path.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            f'printf "{profile}\\n" >> "$DOCUMENTATION_WORKFLOW_TRACE"\n'
            f'if [[ "${{DOCUMENTATION_FAIL_PROFILE:-}}" == "{profile}" ]]; '
            "then exit 42; fi\n"
            'if [[ "${DOCUMENTATION_INVALIDATE_REVIEW:-}" == 1 ]]; then\n'
            '    rm -f "$(git rev-parse --git-path h2h-review/receipts)"/*.json\n'
            "fi\n"
            'if [[ "${DOCUMENTATION_CHANGE_CANDIDATE:-}" == 1 ]]; then\n'
            "    printf 'changed = True\\n' > tests/gate-change.py\n"
            "    git add tests/gate-change.py\n"
            "fi\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    _commit(repository, "chore: seed isolated workflow")
    if install_hooks:
        _git(repository, "config", "core.hooksPath", ".githooks")
    _git(repository, "switch", "-c", "docs/task")
    return repository


def _checks(repository: Path) -> list[str]:
    path = repository.parent / "checks.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_documentation_commit_checks_staged_content_without_program_tools(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _write(repository, "README.md", "# Staged documentation\n")
    _git(repository, "add", "README.md")
    _write(repository, "README.md", "# Unstaged\n\n### Invalid heading jump\n")
    _write(repository, "tests/unstaged.py", "not valid Python !\n")

    _git(repository, "commit", "-m", "docs: update guide")

    assert _checks(repository) == []
    assert _git(repository, "show", "HEAD:README.md") == "# Staged documentation"


@pytest.mark.parametrize(
    "path", ("tests/example.py", "scripts/example.py", "AGENTS.md")
)
def test_mixed_commit_dispatches_to_fast_profile(tmp_path: Path, path: str) -> None:
    repository = _repository(tmp_path)
    _write(repository, "README.md", "# Updated guide\n")
    _write(repository, path, "changed\n")

    _commit(repository, "chore: update implementation and guide")

    assert _checks(repository) == ["fast"]


def test_invalid_documentation_blocks_commit_without_program_checks(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    before = _git(repository, "rev-parse", "HEAD")
    _write(repository, "README.md", "# Guide\n\n### Invalid heading jump\n")
    _git(repository, "add", "README.md")

    result = _run(repository, "git", "commit", "-m", "docs: invalid guide")

    assert result.returncode != 0
    assert "MD001" in result.stdout + result.stderr
    assert _git(repository, "rev-parse", "HEAD") == before
    assert _checks(repository) == []


@pytest.mark.parametrize("classifier", ("raise SystemExit(23)\n", "print('unknown')\n"))
def test_classifier_failure_or_unknown_profile_blocks_commit(
    tmp_path: Path, classifier: str
) -> None:
    repository = _repository(tmp_path)
    before = _git(repository, "rev-parse", "HEAD")
    _write(repository, "README.md", "# Updated guide\n")
    _git(repository, "add", "README.md")
    _write(repository, "scripts/check_change_scope.py", classifier)

    result = _run(repository, "git", "commit", "-m", "docs: update guide")

    assert result.returncode != 0
    assert _git(repository, "rev-parse", "HEAD") == before
    assert _checks(repository) == []


@pytest.mark.parametrize("code_first", (False, True))
@pytest.mark.parametrize("advance_primary", (False, True))
def test_merge_dispatch_uses_complete_candidate_not_last_commit(
    tmp_path: Path, code_first: bool, advance_primary: bool
) -> None:
    repository = _repository(tmp_path)
    if code_first:
        _write(repository, "tests/example.py", "value = 1\n")
        _commit(repository, "test: add example")
    _write(repository, "README.md", "# Updated guide\n")
    _commit(repository, "docs: update guide")
    if advance_primary:
        _git(repository, "switch", "main")
        _git(repository, "switch", "-c", "docs/parallel")
        _write(repository, "docs/parallel.md", "# Parallel guide\n")
        _commit(repository, "docs: add parallel guide")
        result = _run(repository, "scripts/git-flow-merge.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        _git(repository, "switch", "docs/task")
    (repository.parent / "checks.log").write_text("", encoding="utf-8")

    result = _run(repository, "scripts/git-flow-merge.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _checks(repository) == (
        ["run", "verify", "full", "verify"] if code_first else []
    )
    assert _git(repository, "branch", "--show-current") == "main"
    assert (
        len(_git(repository, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    )
    assert not _git(repository, "branch", "--list", "docs/task")
    assert _git(repository, "status", "--porcelain") == ""


@pytest.mark.parametrize("failure", ("documentation", "full"))
def test_failed_selected_merge_profile_aborts_and_retains_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    repository = _repository(tmp_path, install_hooks=False)
    primary = _git(repository, "rev-parse", "main")
    if failure == "documentation":
        _write(repository, "README.md", "# Guide\n\n### Invalid heading jump\n")
    else:
        _write(repository, "tests/example.py", "value = 1\n")
        monkeypatch.setenv("DOCUMENTATION_FAIL_PROFILE", "full")
    _commit(repository, "test: construct failing candidate")
    task = _git(repository, "rev-parse", "HEAD")
    _git(repository, "config", "core.hooksPath", ".githooks")

    result = _run(repository, "scripts/git-flow-merge.sh")

    assert result.returncode != 0
    assert (
        "merge commit or release gate failed; task branch was retained" in result.stderr
    )
    assert _git(repository, "rev-parse", "main") == primary
    assert _git(repository, "rev-parse", "docs/task") == task
    assert _git(repository, "branch", "--show-current") == "docs/task"
    assert _git(repository, "status", "--porcelain") == ""
    assert not (repository / ".git/MERGE_HEAD").exists()
    assert _checks(repository) == (
        ["run", "verify", "full"] if failure == "full" else []
    )
    if failure == "documentation":
        assert "MD001" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "entrypoint",
    (
        (".githooks/pre-merge-commit",),
        (".githooks/pre-commit",),
        ("git", "commit", "--no-edit"),
    ),
)
def test_full_merge_gate_requires_review_before_program_checks(
    tmp_path: Path, entrypoint: tuple[str, ...]
) -> None:
    repository = _repository(tmp_path)
    _write(repository, "tests/example.py", "value = 1\n")
    _commit(repository, "test: add example")
    _git(repository, "switch", "main")
    _git(repository, "merge", "--no-ff", "--no-commit", "docs/task")
    (repository.parent / "checks.log").write_text("", encoding="utf-8")

    result = _run(repository, *entrypoint)

    assert result.returncode != 0
    assert "h2h code review:" in result.stderr
    assert _checks(repository) == ["verify"]


@pytest.mark.parametrize("failure", ["finding", "nonzero"])
def test_review_failure_recovers_only_after_normal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    repository = _repository(tmp_path)
    _write(repository, "tests/example.py", "value = 1\n")
    _commit(repository, "test: add example")
    primary = _git(repository, "rev-parse", "main")
    task = _git(repository, "rev-parse", "HEAD")
    (repository.parent / "checks.log").write_text("", encoding="utf-8")
    monkeypatch.setenv("DOCUMENTATION_REVIEW_FAILURE", failure)

    result = _run(repository, "scripts/git-flow-merge.sh")

    assert result.returncode != 0
    assert _checks(repository) == ["run"]
    assert _git(repository, "rev-parse", "main") == primary
    assert _git(repository, "rev-parse", "docs/task") == task
    metadata = repository / ".git"
    admission = metadata / "h2h-merge-workflow.lock"
    review = metadata / "h2h-review"
    assert not list((review / "receipts").glob("*.json"))
    if failure == "finding":
        assert "code review failed" in result.stderr
        assert "Code review completed with findings" in result.stderr
        assert _git(repository, "branch", "--show-current") == "docs/task"
        assert not (metadata / "MERGE_HEAD").exists()
        assert not admission.exists()
        assert not list((review / "locks").glob("*.unproven.json"))
        (run,) = (review / "runs").iterdir()
        plan = json.loads((run / "plan.json").read_text())
        stages = [f"shard-{index:04d}" for index in range(len(plan["shards"]))]
        if len(stages) > 1:
            stages.append("integration")
        for stage in stages:
            document = json.loads((run / stage / "result.json").read_text())
            assert document["verdict"] == "changes_requested"
            assert document["findings"]
    else:
        assert _git(repository, "branch", "--show-current") == "main"
        assert _git(repository, "rev-parse", "MERGE_HEAD") == task
        assert admission.is_dir()
        (marker,) = (review / "locks").glob("*.unproven.json")
        retained = json.loads(marker.read_text())
        assert retained["kind"] == "review-completion-unproven"
        assert retained["target"]["parents"] == [primary, task]
        (pending,) = review.glob("pending-*")
        assert not (pending / "completion.json").exists()
        diagnostic = json.loads((pending / "completion.json.failure.json").read_text())
        assert diagnostic["authoritative"] is False
        assert diagnostic["exit_code"] == 125
        assert diagnostic["cleanup_state"] == "unknown"
        assert diagnostic["boundary"] == "code-review-main"
        assert diagnostic["exceptions"][0]["type"].endswith(".ReviewCompletionUnproven")
        assert any(
            error["type"].endswith(".ExecutionError")
            and "exited with 1" in error["message"]
            for error in diagnostic["exceptions"]
        )
        (run,) = (review / "runs").iterdir()
        admitted = [path for path in run.glob("shard-*") if path.is_dir()]
        assert admitted
        assert not (run / "integration").exists()
        assert any(
            "fixture reviewer crashed" in log.read_text()
            for log in run.glob("shard-*/stderr.log")
        )
        assert all(not (shard / "result.json").exists() for shard in admitted)

        marker_bytes = marker.read_bytes()
        owner_bytes = (admission / "owner.json").read_bytes()
        candidate_tree = _git(repository, "write-tree")
        blocked = _run(repository, "scripts/git-flow-merge.sh", "--cleanup-only")
        assert blocked.returncode != 0
        assert "admission is already held" in blocked.stderr
        assert marker.read_bytes() == marker_bytes
        assert (admission / "owner.json").read_bytes() == owner_bytes
        assert _git(repository, "write-tree") == candidate_tree
        assert _git(repository, "rev-parse", "MERGE_HEAD") == task
        assert _git(repository, "rev-parse", "main") == primary
        assert _git(repository, "rev-parse", "docs/task") == task
        assert _checks(repository) == ["run"]


@pytest.mark.parametrize("invalidation", ("receipt", "tree"))
def test_full_merge_gate_rechecks_review_after_program_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    repository = _repository(tmp_path)
    _write(repository, "tests/example.py", "value = 1\n")
    _commit(repository, "test: add example")
    primary = _git(repository, "rev-parse", "main")
    task = _git(repository, "rev-parse", "HEAD")
    (repository.parent / "checks.log").write_text("", encoding="utf-8")
    monkeypatch.setenv(
        "DOCUMENTATION_INVALIDATE_REVIEW"
        if invalidation == "receipt"
        else "DOCUMENTATION_CHANGE_CANDIDATE",
        "1",
    )

    result = _run(repository, "scripts/git-flow-merge.sh")

    assert result.returncode != 0
    assert "h2h code review:" in result.stderr
    assert _checks(repository) == ["run", "verify", "full", "verify"]
    assert _git(repository, "rev-parse", "main") == primary
    assert _git(repository, "rev-parse", "docs/task") == task
    merge_head = repository / ".git/MERGE_HEAD"
    admission = repository / ".git/h2h-merge-workflow.lock"
    if invalidation == "receipt":
        assert _git(repository, "branch", "--show-current") == "docs/task"
        assert not merge_head.exists()
        assert not admission.exists()
        assert _git(repository, "status", "--porcelain") == ""
        return

    # A gate's staged change is external to the captured candidate. Preserve
    # those bytes and the original workflow's admission instead of aborting it.
    assert "Candidate changed outside this workflow" in result.stderr
    assert _git(repository, "branch", "--show-current") == "main"
    assert merge_head.read_text(encoding="ascii") == task + "\n"
    staged = _run(repository, "git", "show", ":tests/gate-change.py")
    assert staged.returncode == 0
    assert staged.stdout == "changed = True\n"
    assert (repository / "tests/gate-change.py").read_bytes() == b"changed = True\n"
    index = _git(repository, "write-tree")
    owner_bytes = (admission / "owner.json").read_bytes()
    owner = json.loads(owner_bytes)
    assert owner["repository"] == str(repository)
    assert isinstance(owner["owner_pid"], int) and owner["owner_pid"] > 0
    assert len(owner["token"]) == 64 and set(owner["token"]) <= set("0123456789abcdef")
    identity = admission.stat()

    blocked = _run(repository, "scripts/git-flow-merge.sh", "--cleanup-only")

    assert blocked.returncode != 0
    assert "admission is already held" in blocked.stderr
    assert (admission.stat().st_dev, admission.stat().st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )
    assert (admission / "owner.json").read_bytes() == owner_bytes
    assert merge_head.read_text(encoding="ascii") == task + "\n"
    assert _git(repository, "write-tree") == index
    assert (repository / "tests/gate-change.py").read_bytes() == b"changed = True\n"
    assert _git(repository, "branch", "--show-current") == "main"
    assert _git(repository, "rev-parse", "main") == primary
    assert _git(repository, "rev-parse", "docs/task") == task
    assert _checks(repository) == ["run", "verify", "full", "verify"]


def test_merge_uses_repository_python_when_path_python_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    _write(repository, "tests/example.py", "value = 1\n")
    _commit(repository, "test: add example")
    (repository.parent / "checks.log").write_text("", encoding="utf-8")
    host_python = tmp_path / "host-bin/python3"
    host_python.parent.mkdir()
    host_python.write_text(
        "#!/usr/bin/env bash\necho 'system Python must not run' >&2\nexit 85\n",
        encoding="utf-8",
    )
    host_python.chmod(0o755)
    monkeypatch.setenv("PATH", f"{host_python.parent}{os.pathsep}{os.environ['PATH']}")

    result = _run(repository, "scripts/git-flow-merge.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "system Python must not run" not in result.stderr
    assert _checks(repository) == ["run", "verify", "full", "verify"]


@pytest.mark.parametrize(
    "transitions",
    (
        (("0.1.1", "chore: mix a version change into a normal commit"),),
        (
            ("0.1.2", "chore(release): bump version to 0.1.2"),
            ("0.1.1", "chore(release): bump version to 0.1.1"),
        ),
    ),
)
def test_repository_release_policy_rejects_invalid_task_version_commits(
    tmp_path: Path, transitions: tuple[tuple[str, str], ...]
) -> None:
    repository = _repository(tmp_path)
    _write(repository, "src/runtime.py", "value = 1\n")
    _commit(repository, "fix: add fixture runtime")
    primary = _git(repository, "rev-parse", "main")
    manifest = repository / "pyproject.toml"
    previous = "0.1.0"
    for version, message in transitions:
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                f'version = "{previous}"', f'version = "{version}"'
            ),
            encoding="utf-8",
        )
        _commit(repository, message)
        previous = version

    result = _run(repository, "scripts/git-flow-merge.sh")

    assert result.returncode != 0
    assert "version transition requires its dedicated release commit" in result.stderr
    assert _git(repository, "rev-parse", "main") == primary
    assert _git(repository, "branch", "--show-current") == "docs/task"
    assert not (repository / ".git/MERGE_HEAD").exists()


def test_merge_message_exemption_requires_a_real_merge(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    before = _git(repository, "rev-parse", "HEAD")
    _write(repository, "README.md", "# Updated guide\n")
    _git(repository, "add", "README.md")

    rejected = _run(repository, "git", "commit", "-m", "Merge misleading subject")

    assert rejected.returncode != 0
    assert "Commit message must follow Conventional Commits" in rejected.stderr
    assert _git(repository, "rev-parse", "HEAD") == before
    assert _git(repository, "diff", "--cached", "--name-only") == "README.md"
    _commit(repository, "docs: update guide")
    task = _git(repository, "rev-parse", "HEAD")

    merged = _run(repository, "scripts/git-flow-merge.sh")

    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert _git(repository, "show", "-s", "--format=%P", "HEAD").split() == [
        before,
        task,
    ]
    assert _git(repository, "show", "-s", "--format=%s", "HEAD").startswith("Merge ")
