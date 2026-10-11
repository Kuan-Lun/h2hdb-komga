"""Check installed candidate requirements, import origins, and real SQLite reads."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import closing
from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory

# Isolated smoke execution imports only tool sources, never repository runtime src.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _shared.h2h_dev_tools.distribution_check import (
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
    print(
        json.dumps(
            {
                "runtime_requirements": requirements,
                "wheel_origins": origins,
                "sqlite_read_only": "passed",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
