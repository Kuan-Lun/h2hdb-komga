import json
from dataclasses import dataclass, field
from typing import Any, Self, cast

from h2hdb import resolve_environment_placeholders


def _required_credential(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Komga API {label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class KomgaConfig:
    base_url: str
    api_username: str = field(repr=False)
    api_password: str = field(repr=False)
    library_id: str
    trigger_scan: bool

    def __post_init__(self) -> None:
        if not isinstance(self.api_username, str) or not self.api_username:
            raise ValueError("Komga API username must be a non-empty string")
        if not isinstance(self.api_password, str) or not self.api_password:
            raise ValueError("Komga API password must be a non-empty string")

    @classmethod
    def from_file(cls, path: str) -> Self:
        with open(path) as f:
            resolved = resolve_environment_placeholders(json.load(f))
        if not isinstance(resolved, dict):
            raise ValueError("Komga configuration must be a JSON object")
        raw = cast(dict[str, Any], resolved)
        api_username = _required_credential(raw, "api_username", "username")
        api_password = _required_credential(raw, "api_password", "password")
        return cls(
            base_url=raw["base_url"],
            api_username=api_username,
            api_password=api_password,
            library_id=raw["library_id"],
            trigger_scan=raw.get("trigger_scan", True),
        )
