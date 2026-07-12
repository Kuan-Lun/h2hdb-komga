import argparse
import logging

from h2hdb import load_config as load_h2hdb_config

from .config_loader import KomgaConfig
from .sync import sync_komga_library


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--komgaconfig", required=True)
    parser.add_argument("--h2hdbconfig", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    komga_config = KomgaConfig.from_file(args.komgaconfig)
    h2hdb_config = load_h2hdb_config(args.h2hdbconfig)
    sync_komga_library(komga_config, h2hdb_config)


if __name__ == "__main__":
    main()
