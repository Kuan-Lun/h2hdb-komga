import io
import json
import multiprocessing
import os
import subprocess
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest
from _shared.h2h_dev_tools import process as owned_process
from h2hdb import (
    CatalogReader,
    CatalogRevisionNotFoundError,
    CoreConfig,
    DatabaseAccessMode,
    DatabaseConfig,
    VNextDatabaseAdminFacade,
)

from h2hdb_komga import cli, config_loader
from h2hdb_komga.config_loader import KomgaConfig
from tests.database_support import DatabaseCase


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ([], "Komga configuration must be a JSON object"),
        ({}, "Komga API username must be a non-empty string"),
    ],
)
def test_module_cli_spawns_worker_and_reports_configuration_error(
    tmp_path: Path, config: object, message: str
) -> None:
    config_path = tmp_path / "komga.json"
    config_path.write_text(json.dumps(config))
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    output_path = tmp_path / "cli.log"
    diagnostics = io.StringIO()
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(cli.__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
    }
    with (
        output_path.open("wb") as output,
        redirect_stdout(diagnostics),
        redirect_stderr(diagnostics),
    ):
        status = owned_process.run_command(
            owned_process.ProcessCommand(
                "Actual Komga module CLI spawn",
                (
                    sys.executable,
                    "-m",
                    "h2hdb_komga",
                    "--komgaconfig",
                    str(config_path),
                    "--h2hdbconfig",
                    str(tmp_path / "never-opened-core.json"),
                    "--timeout-seconds",
                    "10",
                ),
                cwd=tmp_path,
                environment=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
            ),
            hard_deadline=monotonic() + 20,
            termination_reserve_seconds=6,
        )
    output_text = output_path.read_text()
    assert status == 1, output_text
    assert message in output_text, output_text
    assert "AttributeError" not in output_text
    assert "worker exited with status 1" in output_text
    assert "owned_tree_empty=True" in diagnostics.getvalue()
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize("sync_fails", [False, True])
def test_worker_bootstrap_opens_compatible_database_read_only(
    monkeypatch: Any,
    tmp_path: Path,
    sync_fails: bool,
) -> None:
    events: list[str] = []
    supervised_snapshot = tmp_path / "supervised-snapshot"
    original_config = CoreConfig(
        database=DatabaseConfig(
            sql_type="sqlite", access_mode=DatabaseAccessMode.read_write
        )
    )
    komga_config = KomgaConfig(
        base_url="https://komga.invalid",
        api_username="user",
        api_password="password",
        library_id="library-1",
        coordination_root=tmp_path / "coordination",
        trigger_scan=False,
    )

    class FakeCatalogReader:
        def close(self) -> None:
            events.append("catalog-closed")

    reader = FakeCatalogReader()
    opened_configs: list[CoreConfig] = []

    class Admin:
        def __init__(self, config: CoreConfig) -> None:
            assert config.database.access_mode is DatabaseAccessMode.read_only

        def check_readiness(self) -> None:
            events.append("readiness")

        def close(self) -> None:
            events.append("admin-closed")

    def open_compatible_database(config: CoreConfig) -> FakeCatalogReader:
        opened_configs.append(config)
        events.append("opened")
        return reader

    def sync(
        config: KomgaConfig,
        selected_reader: FakeCatalogReader,
        *,
        timeout_seconds: float,
        snapshot_directory: Path | None = None,
    ) -> None:
        assert config is komga_config
        assert selected_reader is reader
        assert timeout_seconds == 17
        assert snapshot_directory == supervised_snapshot
        events.append("synced")
        if sync_fails:
            raise RuntimeError("sync failed")

    monkeypatch.setattr(
        config_loader.KomgaConfig, "from_file", lambda path: komga_config
    )
    monkeypatch.setattr(cli, "load_h2hdb_config", lambda path: original_config)
    monkeypatch.setattr(cli, "VNextDatabaseAdminFacade", Admin)
    monkeypatch.setattr(cli, "VNextCatalogFacade", open_compatible_database)
    monkeypatch.setattr(cli, "sync_komga_library", sync)
    if sync_fails:
        with pytest.raises(RuntimeError, match="sync failed"):
            cli._sync_from_config_paths(
                "komga.json", "h2hdb.json", 17, supervised_snapshot
            )
    else:
        cli._sync_from_config_paths("komga.json", "h2hdb.json", 17, supervised_snapshot)

    assert events == ["readiness", "admin-closed", "opened", "synced", "catalog-closed"]
    assert opened_configs[0].database.access_mode is DatabaseAccessMode.read_only
    assert original_config.database.access_mode is DatabaseAccessMode.read_write


def test_worker_opens_real_schema_without_mutating_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_case: DatabaseCase,
) -> None:
    core_config = database_case.config
    with closing(VNextDatabaseAdminFacade(core_config)) as admin:
        report = admin.initialize()
    assert report.epoch == 3
    assert report.schema_version == 9
    assert report.state == "READY"

    def forbid_full_audit(_admin: VNextDatabaseAdminFacade) -> None:
        pytest.fail("reader startup must not run a full database audit")

    monkeypatch.setattr(VNextDatabaseAdminFacade, "check", forbid_full_audit)
    before = database_case.snapshot()
    komga_config = KomgaConfig(
        base_url="https://komga.invalid",
        api_username="user",
        api_password="password",
        library_id="library-1",
        coordination_root=tmp_path / "coordination",
        trigger_scan=False,
    )
    synchronized = False

    def sync(
        selected_config: KomgaConfig,
        reader: CatalogReader,
        *,
        timeout_seconds: float,
        snapshot_directory: Path | None = None,
    ) -> None:
        nonlocal synchronized
        assert selected_config is komga_config
        assert timeout_seconds == 17
        with pytest.raises(CatalogRevisionNotFoundError):
            reader.get_catalog_revision()
        synchronized = True

    monkeypatch.setattr(
        config_loader.KomgaConfig, "from_file", lambda path: komga_config
    )
    monkeypatch.setattr(cli, "load_h2hdb_config", lambda path: core_config)
    monkeypatch.setattr(cli, "sync_komga_library", sync)

    cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)

    assert synchronized
    assert database_case.snapshot() == before
    assert core_config.database.access_mode is DatabaseAccessMode.read_write


@pytest.mark.parametrize(
    ("schema_version", "state", "message"),
    [
        (5, "READY", "expected epoch/version"),
        (6, "READY", "expected epoch/version"),
        (7, "READY", "expected epoch/version"),
        (8, "READY", "expected epoch/version"),
        (10, "READY", "expected epoch/version"),
        (11, "READY", "expected epoch/version"),
        (9, "BUILDING", "not READY"),
    ],
)
def test_worker_rejects_incompatible_schema_before_sync_without_mutating_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int,
    state: str,
    message: str,
    database_case: DatabaseCase,
) -> None:
    core_config = database_case.config
    with closing(VNextDatabaseAdminFacade(core_config)) as admin:
        admin.initialize()
    database_case.execute(
        "UPDATE h2hdb_schema_epoch SET schema_version = %s, state = %s, "
        "ready_at = CASE WHEN %s = 'BUILDING' THEN NULL ELSE ready_at END",
        (schema_version, state, state),
    )
    before = database_case.snapshot()
    komga_config = KomgaConfig(
        base_url="https://komga.invalid",
        api_username="user",
        api_password="password",
        library_id="library-1",
        coordination_root=tmp_path / "coordination",
        trigger_scan=False,
    )
    synchronized = False

    def sync(
        selected_config: KomgaConfig,
        reader: CatalogReader,
        *,
        timeout_seconds: float,
        snapshot_directory: Path | None = None,
    ) -> None:
        nonlocal synchronized
        del selected_config, reader, timeout_seconds
        synchronized = True

    monkeypatch.setattr(
        config_loader.KomgaConfig, "from_file", lambda path: komga_config
    )
    monkeypatch.setattr(cli, "load_h2hdb_config", lambda path: core_config)
    monkeypatch.setattr(cli, "sync_komga_library", sync)

    with pytest.raises(RuntimeError, match=message):
        cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)

    assert not synchronized
    assert database_case.snapshot() == before


def test_cli_runs_bootstrap_inside_hard_deadline_worker(monkeypatch: Any) -> None:
    calls: list[tuple[object, ...]] = []

    def run(
        target: object,
        args: tuple[object, ...],
        *,
        timeout_seconds: float,
    ) -> None:
        calls.append((target, args, timeout_seconds))

    monkeypatch.setattr(cli, "_run_process_with_hard_timeout", run)

    cli.main(
        [
            "--komgaconfig",
            "komga.json",
            "--h2hdbconfig",
            "h2hdb.json",
            "--timeout-seconds",
            "23.5",
        ]
    )

    assert calls == [(cli._sync_worker, ("komga.json", "h2hdb.json", 23.5), 23.5)]


def _sleep_worker(seconds: float, _snapshot_directory: Path) -> None:
    sleep(seconds)


def test_process_supervisor_kills_a_blocked_worker_at_the_deadline() -> None:
    started_at = monotonic()

    with pytest.raises(TimeoutError, match=r"0.05s hard timeout"):
        cli._run_process_with_hard_timeout(
            _sleep_worker,
            (10,),
            timeout_seconds=0.05,
            process_context=multiprocessing.get_context("spawn"),
        )

    assert monotonic() - started_at < 2


@pytest.mark.parametrize("failure", ["readiness", "catalog"])
def test_worker_closes_admin_when_admission_or_catalog_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    events: list[str] = []
    original_config = CoreConfig()
    komga_config = KomgaConfig(
        base_url="https://komga.invalid",
        api_username="user",
        api_password="password",
        library_id="library-1",
        coordination_root=tmp_path / "coordination",
        trigger_scan=False,
    )

    class Admin:
        def __init__(self, config: CoreConfig) -> None:
            assert config.database.access_mode is DatabaseAccessMode.read_only

        def check_readiness(self) -> None:
            events.append("readiness")
            if failure == "readiness":
                raise RuntimeError("readiness failed")

        def close(self) -> None:
            events.append("admin-closed")

    def fail_catalog(_config: CoreConfig) -> CatalogReader:
        events.append("catalog")
        raise RuntimeError("catalog failed")

    def forbid_sync(*_args: object, **_kwargs: object) -> None:
        pytest.fail("failed admission must not start Komga synchronization")

    monkeypatch.setattr(
        config_loader.KomgaConfig, "from_file", lambda _path: komga_config
    )
    monkeypatch.setattr(cli, "load_h2hdb_config", lambda _path: original_config)
    monkeypatch.setattr(cli, "VNextDatabaseAdminFacade", Admin)
    monkeypatch.setattr(cli, "VNextCatalogFacade", fail_catalog)
    monkeypatch.setattr(cli, "sync_komga_library", forbid_sync)
    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)
    assert events == ["readiness", "admin-closed"] + (
        ["catalog"] if failure == "catalog" else []
    )
