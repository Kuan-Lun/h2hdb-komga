import fcntl
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest

from h2hdb_komga import cli

_PROBE = r"""
import json
import os
import signal
import sys
from pathlib import Path
from time import sleep

from h2hdb_komga.cli import _run_process_with_hard_timeout
from h2hdb_komga.config_loader import KomgaConfig
from h2hdb_komga.sync import sync_komga_library


class Catalog:
    def __init__(self, root, mode):
        self.root = root
        self.mode = mode

    def get_catalog_revision(self):
        snapshots = list((self.root / "tmp").glob("*/snapshot.sqlite3"))
        assert len(snapshots) == 1
        assert snapshots[0].stat().st_size > 0
        (self.root / "ready.json").write_text(json.dumps({
            "pid": os.getpid(), "snapshot": str(snapshots[0]),
        }))
        if self.mode != "block":
            raise SystemExit(int(self.mode))
        while True:
            sleep(0.05)


def worker(mode, root, snapshot_directory=None):
    def ignore_term(signum, frame):
        (root / "term-received").touch()
    signal.signal(signal.SIGTERM, ignore_term)
    config = KomgaConfig(
        base_url="https://komga.invalid", api_username="synthetic",
        api_password="synthetic", library_id="library",
        coordination_root=root / "coordination", trigger_scan=False,
    )
    options = {} if snapshot_directory is None else {
        "snapshot_directory": snapshot_directory,
    }
    sync_komga_library(config, Catalog(root, mode), client=object(), **options)


if __name__ == "__main__":
    _run_process_with_hard_timeout(
        worker, (sys.argv[1], Path(sys.argv[2])),
        timeout_seconds=float(sys.argv[3]),
    )
"""


def _wait_for_ready(
    root: Path, supervisor: subprocess.Popen[bytes]
) -> dict[str, object]:
    ready = root / "ready.json"
    deadline = monotonic() + 10
    while monotonic() < deadline and supervisor.poll() is None:
        if ready.exists():
            try:
                value = json.loads(ready.read_text())
            except json.JSONDecodeError:
                continue
            assert isinstance(value, dict)
            return value
        sleep(0.01)
    pytest.fail(
        f"worker did not reach real snapshot/lock boundary: {supervisor.poll()}"
    )


@pytest.mark.parametrize("stop", ["deadline", "SIGTERM", "SIGINT", "SIGHUP"])
def test_supervisor_reaps_worker_before_removing_snapshot_and_releasing_lock(
    tmp_path: Path, stop: str
) -> None:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    lock = coordination / "publication.lock"
    lock.touch()
    environment = {**os.environ, "TMPDIR": str(temporary)}
    deadline = "3" if stop == "deadline" else "60"
    worker_pid: int | None = None
    with (tmp_path / "supervisor.log").open("wb") as output:
        supervisor = subprocess.Popen(
            [sys.executable, str(script), "block", str(tmp_path), deadline],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            ready = _wait_for_ready(tmp_path, supervisor)
            assert isinstance(ready["pid"], int)
            worker_pid = ready["pid"]
            assert isinstance(ready["snapshot"], str)
            snapshot = Path(ready["snapshot"])
            with lock.open("rb") as descriptor:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = monotonic()
            if stop != "deadline":
                supervisor.send_signal(getattr(signal, stop))
                sleep(0.1)
                supervisor.send_signal(getattr(signal, stop))
            supervisor.wait(timeout=6)
            assert monotonic() - started < 6
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                worker_survived = False
            else:
                worker_survived = True
            (tmp_path / "outcome.json").write_text(
                json.dumps(
                    {
                        "supervisor_status": supervisor.returncode,
                        "worker_pid": worker_pid,
                        "worker_survived": worker_survived,
                        "snapshot_survived": snapshot.parent.exists(),
                    }
                )
            )
            assert supervisor.returncode == (
                1 if stop == "deadline" else 128 + getattr(signal, stop)
            )
            with pytest.raises(ProcessLookupError):
                os.kill(worker_pid, 0)
            assert not snapshot.parent.exists()
            assert list(temporary.iterdir()) == []
            assert (tmp_path / "term-received").exists()
            with lock.open("rb") as descriptor:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
            supervisor.wait(timeout=5)
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("status", [0, 7, cli.TIMEOUT_EXIT_CODE])
def test_supervisor_removes_snapshot_after_success_or_worker_failure(
    tmp_path: Path, status: int
) -> None:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE)
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    (coordination / "publication.lock").touch()
    result = subprocess.run(
        [sys.executable, str(script), str(status), str(tmp_path), "10"],
        env={**os.environ, "TMPDIR": str(temporary)},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == (0 if status == 0 else 1), result.stderr
    assert (tmp_path / "ready.json").exists()
    assert list(temporary.iterdir()) == []
    if status == cli.TIMEOUT_EXIT_CODE:
        assert b"TimeoutError" in result.stderr
    elif status:
        assert b"worker exited with status 7" in result.stderr


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_signal_during_start_is_deferred_and_unproven_cleanup_preserves_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    events: list[str] = []

    class Process:
        pid: int | None = None
        exitcode: int | None = None

        def start(self) -> None:
            os.kill(os.getpid(), signal.SIGTERM)
            self.pid = 1
            (workspace / "snapshot.sqlite3").touch()
            events.append("started")

        def join(self, timeout: float | None = None) -> None:
            assert timeout is not None and timeout <= 1
            events.append("joined")

        def is_alive(self) -> bool:
            return self.exitcode is None

        def terminate(self) -> None:
            events.append("terminated")

        def kill(self) -> None:
            events.append("killed")
            if not cleanup_fails:
                self.exitcode = -signal.SIGKILL

        def close(self) -> None:
            assert self.exitcode is not None
            events.append("closed")

    class Context:
        def Process(self, **_kwargs: object) -> Process:
            return Process()

    previous = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(cli, "mkdtemp", lambda **_kwargs: str(workspace))
    if cleanup_fails:
        with pytest.raises(RuntimeError, match="terminate and reap"):
            cli._run_process_with_hard_timeout(
                sleep, (), timeout_seconds=60, process_context=Context()
            )
        assert (workspace / "snapshot.sqlite3").exists()
        assert "closed" not in events
    else:
        with pytest.raises(SystemExit) as error:
            cli._run_process_with_hard_timeout(
                sleep, (), timeout_seconds=60, process_context=Context()
            )
        assert error.value.code == 128 + signal.SIGTERM
        assert not workspace.exists()
        assert events[-1] == "closed"
    assert events[:3] == ["started", "terminated", "joined"]
    assert "killed" in events
    assert signal.getsignal(signal.SIGTERM) is previous


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_supervisor_rejects_nonfinite_or_nonpositive_deadline(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        cli._run_process_with_hard_timeout(sleep, (), timeout_seconds=timeout)
