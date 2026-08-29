import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, cast

from h2hdb import resolve_environment_placeholders


def _required_credential(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Komga API {label} must be a non-empty string")
    return value


def _required_path(raw: dict[str, Any], key: str, label: str) -> Path:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Komga {label} must be a non-empty string")
    return Path(value)


@dataclass(frozen=True, slots=True)
class KomgaConfig:
    base_url: str
    api_username: str = field(repr=False)
    api_password: str = field(repr=False)
    library_id: str
    coordination_root: Path
    trigger_scan: bool

    def __post_init__(self) -> None:
        if not isinstance(self.api_username, str) or not self.api_username:
            raise ValueError("Komga API username must be a non-empty string")
        if not isinstance(self.api_password, str) or not self.api_password:
            raise ValueError("Komga API password must be a non-empty string")
        if not isinstance(self.coordination_root, Path):
            raise ValueError("Komga coordination root must be a path")
        if not self.coordination_root.is_absolute():
            raise ValueError("Komga coordination root must be an absolute path")
        normalized = Path(os.path.abspath(self.coordination_root))
        if normalized == Path(normalized.anchor):
            raise ValueError("Komga coordination root must not be the filesystem root")
        object.__setattr__(self, "coordination_root", normalized)

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
            coordination_root=_required_path(
                raw,
                "coordination_root",
                "coordination root",
            ),
            trigger_scan=raw.get("trigger_scan", True),
        )
