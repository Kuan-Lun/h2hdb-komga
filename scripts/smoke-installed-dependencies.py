"""Check installed candidate requirements, import origins, and real SQLite reads."""

from __future__ import annotations

import importlib
import json
import sys
from contextlib import closing
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.requirements import Requirement


def check_runtime_requirements(package: Distribution) -> dict[str, str]:
    selected: dict[str, str] = {}
    for raw in package.requires or ():
        requirement = Requirement(raw)
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": ""}
        ):
            continue
        try:
            dependency = distribution(requirement.name)
        except PackageNotFoundError as error:
            raise RuntimeError(
                f"{package.metadata['Name']} requires {requirement}; package is missing"
            ) from error
        if dependency.version not in requirement.specifier:
            raise RuntimeError(
                f"{package.metadata['Name']} requires {requirement}; "
                f"installed {requirement.name}=={dependency.version}"
            )
        selected[requirement.name] = dependency.version
    return selected


def check_wheel_origin(project: str, module_name: str, *, local: bool) -> str:
    package = distribution(project)
    module = importlib.import_module(module_name)
    if module.__file__ is None:
        raise RuntimeError(f"{project} has no import origin")
    actual = Path(module.__file__).resolve()
    record_name = module_name + "/__init__.py"
    expected = Path(str(package.locate_file(record_name))).resolve()
    if actual != expected:
        raise RuntimeError(
            f"{project} import disagrees with installed metadata: {actual}"
        )
    if package.files is None or not any(
        str(item) == record_name for item in package.files
    ):
        raise RuntimeError(f"{project} has no installed package RECORD")
    direct = json.loads(package.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable", False):
        raise RuntimeError(
            f"{project} must come from a wheel, not an editable checkout"
        )
    if local and not actual.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError(f"{project} must be installed inside the smoke environment")
    return str(actual)


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
        assert (report.epoch, report.schema_version, report.state) == (3, 8, "READY")
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
