"""Installed METADATA constraints must agree with the core actually tested."""

from __future__ import annotations

import runpy
import tomllib
from collections.abc import Callable
from importlib.metadata import Distribution, version
from pathlib import Path
from typing import cast

import pytest
from packaging.requirements import Requirement


def _checker() -> Callable[[Distribution], dict[str, str]]:
    namespace = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts" / "smoke-installed-dependencies.py")
    )
    return cast(
        Callable[[Distribution], dict[str, str]],
        namespace["check_runtime_requirements"],
    )


def _metadata(tmp_path: Path, requirements: tuple[str, ...]) -> Distribution:
    folder = tmp_path / "candidate-0.1.0.dist-info"
    folder.mkdir()
    contents = "Metadata-Version: 2.4\nName: candidate\nVersion: 0.1.0\n"
    contents += "".join(
        f"Requires-Dist: {requirement}\n" for requirement in requirements
    )
    (folder / "METADATA").write_text(contents, encoding="utf-8")
    return Distribution.at(folder)


def test_installed_metadata_accepts_actual_core_and_ignores_inactive_markers(
    tmp_path: Path,
) -> None:
    package = _metadata(
        tmp_path,
        ("h2hdb>=0.39.0,<0.40.0", "missing-inactive-package; python_version < '3.14'"),
    )
    assert _checker()(package) == {"h2hdb": version("h2hdb")}


@pytest.mark.parametrize(
    "requirement", ["h2hdb>=0.36.0,<0.39.0", "h2hdb>=0.40.0,<0.41.0"]
)
def test_installed_metadata_rejects_wrong_core_lane(
    tmp_path: Path, requirement: str
) -> None:
    with pytest.raises(RuntimeError, match="installed h2hdb=="):
        _checker()(_metadata(tmp_path, (requirement,)))


def test_installed_metadata_rejects_missing_dependency(tmp_path: Path) -> None:
    package = _metadata(tmp_path, ("missing-h2hdb-komga-smoke-dependency>=1",))
    with pytest.raises(RuntimeError, match="package is missing"):
        _checker()(package)


@pytest.mark.parametrize(
    ("core_version", "accepted"),
    [
        ("0.35.9", False),
        ("0.36.0", False),
        ("0.37.0", False),
        ("0.38.3", False),
        ("0.39.0", True),
        ("0.39.1", True),
        ("0.40.0", False),
    ],
)
def test_manifest_supports_verified_catalog_contract_lanes(
    core_version: str,
    accepted: bool,
) -> None:
    document = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    requirements = [Requirement(raw) for raw in document["project"]["dependencies"]]
    core_requirements = [item for item in requirements if item.name == "h2hdb"]
    assert len(core_requirements) == 1
    assert (core_version in core_requirements[0].specifier) is accepted
