import argparse
import logging
import math
import multiprocessing
import shutil
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic
from types import FrameType
from typing import Protocol, cast

from h2hdb import (
    CoreConfig,
    DatabaseAccessMode,
    VNextCatalogFacade,
    VNextDatabaseAdminFacade,
)
from h2hdb import (
    load_config as load_h2hdb_config,
)

from .config_loader import KomgaConfig
from .sync import SETTLING_TIMEOUT_SECONDS, sync_komga_library

PROCESS_KILL_GRACE_SECONDS = 1.0
PROCESS_TERMINATE_GRACE_SECONDS = 1.0
PROCESS_POLL_SECONDS = 0.05
TIMEOUT_EXIT_CODE = 124


class _WorkerProcess(Protocol):
    """Operations used by the wall-clock supervisor."""

    exitcode: int | None
    pid: int | None

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def kill(self) -> None: ...

    def terminate(self) -> None: ...

    def close(self) -> None: ...


class _ProcessContext(Protocol):
    """Narrow multiprocessing factory contract, including test contexts."""

    def Process(
        self,
        *,
        target: Callable[..., object],
        args: tuple[object, ...],
        daemon: bool,
    ) -> _WorkerProcess: ...


def _read_only_core_config(config: CoreConfig) -> CoreConfig:
    database = config.database.model_copy(
        update={"access_mode": DatabaseAccessMode.read_only}
    )
    return config.model_copy(update={"database": database})


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )


def _sync_from_config_paths(
    komga_config_path: str,
    h2hdb_config_path: str,
    timeout_seconds: float,
    snapshot_directory: Path | None = None,
) -> None:
    """Bootstrap read-only core and perform one in-process synchronization."""

    _configure_logging()
    komga_config = KomgaConfig.from_file(komga_config_path)
    core_config = _read_only_core_config(load_h2hdb_config(h2hdb_config_path))
    admin = VNextDatabaseAdminFacade(core_config)
    try:
        admin.check_readiness()
    finally:
        admin.close()
    catalog_reader = VNextCatalogFacade(core_config)
    try:
        logging.info("Database readiness confirmed; full audit was not performed")
        sync_komga_library(
            komga_config,
            catalog_reader,
            timeout_seconds=timeout_seconds,
            snapshot_directory=snapshot_directory,
        )
    finally:
        catalog_reader.close()


def _sync_worker(
    komga_config_path: str,
    h2hdb_config_path: str,
    timeout_seconds: float,
    snapshot_directory: Path,
) -> None:
    try:
        _sync_from_config_paths(
            komga_config_path,
            h2hdb_config_path,
            timeout_seconds,
            snapshot_directory,
        )
    except TimeoutError:
        logging.exception("Komga synchronization exceeded its deadline")
        raise SystemExit(TIMEOUT_EXIT_CODE) from None


def _reap_worker(process: _WorkerProcess) -> None:
    if process.is_alive():
        process.terminate()
        process.join(PROCESS_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(PROCESS_KILL_GRACE_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError("Unable to terminate and reap Komga sync worker")
    process.join(0)
    process.close()


def _run_process_with_hard_timeout(
    target: Callable[..., object],
    args: tuple[object, ...],
    *,
    timeout_seconds: float,
    process_context: object | None = None,
) -> None:
    """Own one worker and its snapshot workspace through cancellation and exit.

    Socket read timeouts and executor cancellation are cooperative and cannot
    stop a peer that keeps a response trickling forever. A process boundary is
    the final deadline fence for the CLI, and also covers a blocked database
    gate or worker-thread shutdown.

    If the OS cannot confirm worker exit after termination and kill, its
    workspace is preserved and cleanup fails. Multiprocessing may still wait
    for that child during interpreter shutdown; parent exit is not bounded in
    this unproven-cleanup case.
    """

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    context = cast(
        _ProcessContext,
        process_context or multiprocessing.get_context("spawn"),
    )
    cancelled: int | None = None

    def cancel(signum: int, _frame: FrameType | None) -> None:
        # Do not interrupt Process.start() before multiprocessing records its PID.
        nonlocal cancelled
        if cancelled is None:
            cancelled = signum

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    previous_handlers = {number: signal.getsignal(number) for number in signals}
    directory: Path | None = None
    process: _WorkerProcess | None = None
    cleanup_safe = True
    try:
        for number in signals:
            signal.signal(number, cancel)
        directory = Path(mkdtemp(prefix="h2hdb-komga-snapshot-"))
        process = context.Process(target=target, args=(*args, directory), daemon=False)
        deadline = monotonic() + timeout_seconds
        try:
            cleanup_safe = False
            process.start()
            while process.is_alive():
                if cancelled is not None:
                    raise SystemExit(128 + cancelled)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Komga synchronization exceeded the "
                        f"{timeout_seconds:g}s hard timeout"
                    )
                process.join(min(remaining, PROCESS_POLL_SECONDS))
            exit_code = process.exitcode
        finally:
            if process.pid is not None:
                _reap_worker(process)
            else:
                process.close()
            cleanup_safe = True
        if cancelled is not None:
            raise SystemExit(128 + cancelled)
    finally:
        # A workspace survives an unproven worker exit; deleting it could race I/O.
        try:
            if directory is not None and cleanup_safe:
                shutil.rmtree(directory)
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
    if exit_code == TIMEOUT_EXIT_CODE:
        raise TimeoutError(
            f"Komga synchronization exceeded the {timeout_seconds:g}s hard timeout"
        )
    if exit_code != 0:
        raise RuntimeError(
            f"Komga synchronization worker exited with status {exit_code}"
        )


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--komgaconfig", required=True)
    parser.add_argument("--h2hdbconfig", required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=SETTLING_TIMEOUT_SECONDS,
        help="Hard wall-clock limit for the complete synchronization",
    )
    args = parser.parse_args(arguments)

    _configure_logging()
    _run_process_with_hard_timeout(
        _sync_worker,
        (args.komgaconfig, args.h2hdbconfig, args.timeout_seconds),
        timeout_seconds=args.timeout_seconds,
    )
