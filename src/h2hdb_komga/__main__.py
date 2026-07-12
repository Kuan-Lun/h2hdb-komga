import argparse
import json
import logging
from types import TracebackType

from h2hdb import H2HDBConfig
from h2hdb import load_config as load_h2hdb_config

from .config_loader import KomgaConfig
from .komga import scan_komga_library


class Configs:
    def __init__(self, komga: KomgaConfig, h2hdb: H2HDBConfig) -> None:
        self.komga = komga
        self.h2hdb = h2hdb


def load_configs(komga_config_path: str, h2hdb_config_path: str) -> Configs:
    with open(komga_config_path) as f:
        user_config = json.load(f)

    komga_config = KomgaConfig(
        user_config["base_url"],
        user_config["api_username"],
        user_config["api_password"],
        user_config["library_id"],
        user_config.get("trigger_scan", True),
    )

    h2hdb_config = load_h2hdb_config(h2hdb_config_path)

    return Configs(komga_config, h2hdb_config)


class UpdateKomga:
    def __init__(self, komgaconfig: KomgaConfig, h2hdbconfig: H2HDBConfig) -> None:
        self.komgaconfig = komgaconfig
        self.h2hdbconfig = h2hdbconfig

    def __enter__(self) -> UpdateKomga:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    def update(self) -> None:
        scan_komga_library(self.komgaconfig, self.h2hdbconfig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--komgaconfig")
    parser.add_argument("--h2hdbconfig")
    args = parser.parse_args()

    if args.komgaconfig is None:
        raise ValueError("No komga config file provided")
    if args.h2hdbconfig is None:
        raise ValueError("No h2hdb config file provided")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    configs = load_configs(args.komgaconfig, args.h2hdbconfig)

    with UpdateKomga(configs.komga, configs.h2hdb) as uk:
        uk.update()
