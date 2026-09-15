import multiprocessing
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest
from h2hdb import (
    CatalogReader,
    CatalogRevisionNotFoundError,
    CoreConfig,
    DatabaseAccessMode,
    DatabaseConfig,
    VNextDatabaseAdminFacade,
)

from h2hdb_komga import __main__ as cli
from h2hdb_komga import config_loader
from h2hdb_komga.config_loader import KomgaConfig


@pytest.mark.parametrize("sync_fails", [False, True])
def test_worker_bootstrap_opens_compatible_database_read_only(
    monkeypatch: Any,
    tmp_path: Path,
    sync_fails: bool,
) -> None:
    events: list[str] = []
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
    ) -> None:
        assert config is komga_config
        assert selected_reader is reader
        assert timeout_seconds == 17
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
            cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)
    else:
        cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)

    assert events == ["readiness", "admin-closed", "opened", "synced", "catalog-closed"]
    assert opened_configs[0].database.access_mode is DatabaseAccessMode.read_only
    assert original_config.database.access_mode is DatabaseAccessMode.read_write


def test_worker_opens_real_sqlite_schema_without_mutating_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    core_config = CoreConfig(
        database=DatabaseConfig(sql_type="sqlite", database=str(database_path))
    )
    with closing(VNextDatabaseAdminFacade(core_config)) as admin:
        report = admin.initialize()
    assert report.epoch == 3
    assert report.schema_version == 7
    assert report.state == "READY"

    def forbid_full_audit(_admin: VNextDatabaseAdminFacade) -> None:
        pytest.fail("reader startup must not run a full database audit")

    monkeypatch.setattr(VNextDatabaseAdminFacade, "check", forbid_full_audit)
    before = database_path.read_bytes()
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
    assert database_path.read_bytes() == before
    assert core_config.database.access_mode is DatabaseAccessMode.read_write


def test_worker_rejects_previous_schema_before_sync_without_mutating_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "prior-catalog.sqlite3"
    core_config = CoreConfig(
        database=DatabaseConfig(sql_type="sqlite", database=str(database_path))
    )
    with closing(VNextDatabaseAdminFacade(core_config)) as admin:
        admin.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE h2hdb_schema_epoch SET schema_version = 5")
    before = database_path.read_bytes()
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
    ) -> None:
        nonlocal synchronized
        del selected_config, reader, timeout_seconds
        synchronized = True

    monkeypatch.setattr(
        config_loader.KomgaConfig, "from_file", lambda path: komga_config
    )
    monkeypatch.setattr(cli, "load_h2hdb_config", lambda path: core_config)
    monkeypatch.setattr(cli, "sync_komga_library", sync)

    with pytest.raises(RuntimeError, match="expected epoch/version"):
        cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)

    assert not synchronized
    assert database_path.read_bytes() == before


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


def test_process_supervisor_kills_a_blocked_worker_at_the_deadline() -> None:
    started_at = monotonic()

    with pytest.raises(TimeoutError, match=r"0.05s hard timeout"):
        cli._run_process_with_hard_timeout(
            sleep,
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
