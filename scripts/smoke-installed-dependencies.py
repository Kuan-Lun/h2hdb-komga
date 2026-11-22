"""Check installed candidate requirements, import origins, and real SQLite reads."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from contextlib import closing
from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory

# Isolated smoke execution imports only tool sources, never repository runtime src.
sys.path.insert(0, str(Path(__file__).resolve().parent))
_SHARED_DIRECTORY = Path(__file__).resolve().parent / "_shared/h2h_dev_tools"
runpy.run_path(str(_SHARED_DIRECTORY / "source_loader.py"))["load_package"](
    _SHARED_DIRECTORY, "_shared.h2h_dev_tools"
)

from _shared.h2h_dev_tools.distribution_check import (  # noqa: E402 -- Load verified snapshot source before importing it.
    check_runtime_requirements,
    check_wheel_origin,
)


def check_sqlite_reader() -> None:
    from h2hdb import (
        CatalogRevisionNotFoundError,
        CoreConfig,
        DatabaseAccessMode,
        DatabaseConfig,
        VNextCatalogFacade,
        VNextDatabaseAdminFacade,
    )

    with TemporaryDirectory(prefix="h2hdb-komga-wheel-") as temporary:
        config = CoreConfig(
            database=DatabaseConfig(
                sql_type="sqlite", database=str(Path(temporary) / "catalog.sqlite3")
            )
        )
        with closing(VNextDatabaseAdminFacade(config)) as admin:
            report = admin.initialize()
        assert (report.epoch, report.schema_version, report.state) == (3, 9, "READY")
        read_only = config.model_copy(
            update={
                "database": config.database.model_copy(
                    update={"access_mode": DatabaseAccessMode.read_only}
                )
            }
        )
        with closing(VNextDatabaseAdminFacade(read_only)) as admin:
            admin.check_readiness()
        with closing(VNextCatalogFacade(read_only)) as reader:
            try:
                reader.get_catalog_revision()
            except CatalogRevisionNotFoundError:
                pass
            else:
                raise AssertionError("fresh SQLite unexpectedly contains a publication")


def check_module_worker() -> None:
    with TemporaryDirectory(prefix="h2hdb-komga-wheel-cli-") as temporary:
        workspace = Path(temporary)
        invalid_config = workspace / "komga.json"
        invalid_config.write_text("[]", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "h2hdb_komga",
                "--komgaconfig",
                str(invalid_config),
                "--h2hdbconfig",
                str(workspace / "never-opened-core.json"),
                "--timeout-seconds",
                "10",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 1, result.stderr
        assert "Komga configuration must be a JSON object" in result.stderr, (
            result.stderr
        )
        assert "worker exited with status 1" in result.stderr, result.stderr


def main() -> None:
    assert sys.flags.isolated and not sys.flags.optimize, (
        "run with python -I, without -O"
    )
    requirements = check_runtime_requirements(distribution("h2hdb-komga"))
    assert "h2hdb" in requirements, (
        "candidate metadata must declare its core requirement"
    )
    origins = {
        "h2hdb-komga": check_wheel_origin("h2hdb-komga", "h2hdb_komga", local=True),
        "h2hdb": check_wheel_origin("h2hdb", "h2hdb", local=False),
    }
    subprocess.run(
        [sys.executable, "-I", "-m", "h2hdb_komga", "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    check_sqlite_reader()
    check_module_worker()
    print(
        json.dumps(
            {
                "runtime_requirements": requirements,
                "wheel_origins": origins,
                "sqlite_read_only": "passed",
                "module_spawn": "passed",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
