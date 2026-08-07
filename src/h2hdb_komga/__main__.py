import argparse
import logging
import multiprocessing
from collections.abc import Callable, Sequence
from typing import Protocol, cast

from h2hdb import (
    H2HDB,
    CoreConfig,
    DatabaseAccessMode,
)
from h2hdb import (
    load_config as load_h2hdb_config,
)

from .config_loader import KomgaConfig
from .sync import SETTLING_TIMEOUT_SECONDS, sync_komga_library

PROCESS_KILL_GRACE_SECONDS = 1.0
TIMEOUT_EXIT_CODE = 124


class _WorkerProcess(Protocol):
    """Operations used by the wall-clock supervisor."""

    exitcode: int | None

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...

    def kill(self) -> None: ...

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
) -> None:
    """Bootstrap read-only core and perform one in-process synchronization."""

    _configure_logging()
    komga_config = KomgaConfig.from_file(komga_config_path)
    core_config = _read_only_core_config(load_h2hdb_config(h2hdb_config_path))
    catalog_reader = H2HDB(core_config)
    catalog_reader.check_compatibility()
    sync_komga_library(
        komga_config,
        catalog_reader,
        timeout_seconds=timeout_seconds,
    )


def _sync_worker(
    komga_config_path: str,
    h2hdb_config_path: str,
    timeout_seconds: float,
) -> None:
    try:
        _sync_from_config_paths(
            komga_config_path,
            h2hdb_config_path,
            timeout_seconds,
        )
    except TimeoutError:
        logging.exception("Komga synchronization exceeded its deadline")
        raise SystemExit(TIMEOUT_EXIT_CODE) from None


def _run_process_with_hard_timeout(
    target: Callable[..., object],
    args: tuple[object, ...],
    *,
    timeout_seconds: float,
    process_context: object | None = None,
) -> None:
    """Run one disposable worker and kill it at the wall-clock deadline.

    Socket read timeouts and executor cancellation are cooperative and cannot
    stop a peer that keeps a response trickling forever. A process boundary is
    the final deadline fence for the CLI, and also covers a blocked database
    gate or worker-thread shutdown.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    context = cast(
        _ProcessContext,
        process_context or multiprocessing.get_context("spawn"),
    )
    process = context.Process(target=target, args=args, daemon=True)
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.kill()
        process.join(PROCESS_KILL_GRACE_SECONDS)
        still_alive = process.is_alive()
        if not still_alive:
            process.close()
        if still_alive:
            raise RuntimeError("Unable to terminate expired Komga sync worker")
        raise TimeoutError(
            f"Komga synchronization exceeded the {timeout_seconds:g}s hard timeout"
        )

    exit_code = process.exitcode
    process.close()
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


if __name__ == "__main__":
    main()
