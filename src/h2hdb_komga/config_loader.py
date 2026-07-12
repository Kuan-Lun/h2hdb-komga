import json
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class KomgaConfig:
    base_url: str
    api_username: str
    api_password: str
    library_id: str
    trigger_scan: bool

    @classmethod
    def from_file(cls, path: str) -> Self:
        with open(path) as f:
            raw = json.load(f)
        return cls(
            base_url=raw["base_url"],
            api_username=raw["api_username"],
            api_password=raw["api_password"],
            library_id=raw["library_id"],
            trigger_scan=raw.get("trigger_scan", True),
        )
