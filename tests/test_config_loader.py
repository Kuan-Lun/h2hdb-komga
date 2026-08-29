import json
from pathlib import Path

import pytest
from h2hdb import EnvironmentPlaceholderError

from h2hdb_komga.config_loader import KomgaConfig


def _write_config(tmp_path: Path, **values: object) -> Path:
    path = tmp_path / "komga-config.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "https://komga.invalid",
                "library_id": "library-1",
                "coordination_root": str(tmp_path / "coordination"),
                "trigger_scan": False,
                **values,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _clear_komga_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOMGA_API_USERNAME", raising=False)
    monkeypatch.delenv("KOMGA_API_PASSWORD", raising=False)


def test_exact_environment_placeholders_resolve_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOMGA_API_USERNAME", "environment-user")
    monkeypatch.setenv("KOMGA_API_PASSWORD", "environment-password")
    path = _write_config(
        tmp_path,
        api_username="${KOMGA_API_USERNAME}",
        api_password="${KOMGA_API_PASSWORD}",
    )

    config = KomgaConfig.from_file(str(path))

    assert config.api_username == "environment-user"
    assert config.api_password == "environment-password"
    assert config.coordination_root == tmp_path / "coordination"


def test_literal_json_credentials_remain_supported(tmp_path: Path) -> None:
    config = KomgaConfig.from_file(
        str(
            _write_config(
                tmp_path,
                api_username="json-user",
                api_password="json-password",
            )
        )
    )

    assert config.api_username == "json-user"
    assert config.api_password == "json-password"


def test_missing_placeholder_environment_variable_is_reported_without_secret(
    tmp_path: Path,
) -> None:
    path = _write_config(
        tmp_path,
        api_username="${KOMGA_API_USERNAME}",
        api_password="literal-secret-must-not-leak",
    )

    with pytest.raises(EnvironmentPlaceholderError) as captured:
        KomgaConfig.from_file(str(path))

    message = str(captured.value)
    assert "KOMGA_API_USERNAME" in message
    assert "literal-secret-must-not-leak" not in message


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"api_username": "configured-user"},
        {"api_password": "configured-password"},
        {"api_username": "", "api_password": "configured-password"},
        {"api_username": "configured-user", "api_password": ""},
    ],
)
def test_credentials_must_resolve_to_a_non_empty_pair(
    tmp_path: Path,
    credentials: dict[str, str],
) -> None:
    path = _write_config(tmp_path, **credentials)

    with pytest.raises(ValueError) as captured:
        KomgaConfig.from_file(str(path))

    message = str(captured.value)
    assert "non-empty string" in message
    assert "configured-user" not in message
    assert "configured-password" not in message


def test_empty_environment_secret_is_rejected_instead_of_falling_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOMGA_API_USERNAME", "environment-user")
    monkeypatch.setenv("KOMGA_API_PASSWORD", "")
    path = _write_config(
        tmp_path,
        api_username="${KOMGA_API_USERNAME}",
        api_password="${KOMGA_API_PASSWORD}",
    )

    with pytest.raises(ValueError, match="password must be a non-empty string"):
        KomgaConfig.from_file(str(path))


def test_coordination_root_is_required(tmp_path: Path) -> None:
    path = tmp_path / "komga-config.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "https://komga.invalid",
                "api_username": "user",
                "api_password": "password",
                "library_id": "library-1",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="coordination root must be a non-empty"):
        KomgaConfig.from_file(str(path))


@pytest.mark.parametrize("coordination_root", ["relative/path", "/"])
def test_coordination_root_must_be_an_absolute_nonroot_path(
    tmp_path: Path,
    coordination_root: str,
) -> None:
    path = _write_config(
        tmp_path,
        api_username="user",
        api_password="password",
        coordination_root=coordination_root,
    )

    with pytest.raises(ValueError, match="coordination root must"):
        KomgaConfig.from_file(str(path))


def test_credentials_are_excluded_from_config_repr() -> None:
    config = KomgaConfig(
        base_url="https://komga.invalid",
        api_username="private-user",
        api_password="private-password",
        library_id="library-1",
        coordination_root=Path("/srv/h2hdb/coordination"),
        trigger_scan=True,
    )

    rendered = repr(config)

    assert "private-user" not in rendered
    assert "private-password" not in rendered
