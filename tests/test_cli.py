import multiprocessing
from time import monotonic, sleep
from typing import Any

import pytest
from h2hdb import CoreConfig, DatabaseAccessMode, DatabaseConfig

from h2hdb_komga import __main__ as cli
from h2hdb_komga import config_loader
from h2hdb_komga.config_loader import KomgaConfig


def test_worker_bootstrap_forces_read_only_checks_compatibility_and_never_migrates(
    monkeypatch: Any,
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
        trigger_scan=False,
    )

    class FakeH2HDB:
        def __init__(self, config: CoreConfig) -> None:
            self.config = config
            events.append("constructed")

        def check_compatibility(self) -> None:
            events.append("checked")

        def migrate(self) -> None:
            raise AssertionError("Komga must never migrate the core schema")

    readers: list[FakeH2HDB] = []

    def make_reader(config: CoreConfig) -> FakeH2HDB:
        reader = FakeH2HDB(config)
        readers.append(reader)
        return reader

    def sync(
        config: KomgaConfig,
        reader: FakeH2HDB,
        *,
        timeout_seconds: float,
    ) -> None:
        assert config is komga_config
        assert reader is readers[0]
        assert timeout_seconds == 17
        events.append("synced")

    monkeypatch.setattr(
        config_loader.KomgaConfig, "from_file", lambda path: komga_config
    )
    monkeypatch.setattr(cli, "load_h2hdb_config", lambda path: original_config)
    monkeypatch.setattr(cli, "H2HDB", make_reader)
    monkeypatch.setattr(cli, "sync_komga_library", sync)
    cli._sync_from_config_paths("komga.json", "h2hdb.json", 17)

    assert events == ["constructed", "checked", "synced"]
    assert readers[0].config.database.access_mode is DatabaseAccessMode.read_only
    assert original_config.database.access_mode is DatabaseAccessMode.read_write


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

    with pytest.raises(TimeoutError, match="0.05s hard timeout"):
        cli._run_process_with_hard_timeout(
            sleep,
            (10,),
            timeout_seconds=0.05,
            process_context=multiprocessing.get_context("spawn"),
        )

    assert monotonic() - started_at < 2
