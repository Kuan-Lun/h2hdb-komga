#!/usr/bin/env python3
"""Run pytest after verifying this repository's shared source snapshot."""

import runpy
from pathlib import Path

_SHARED_DIRECTORY = Path(__file__).resolve().parent / "_shared/h2h_dev_tools"
runpy.run_path(str(_SHARED_DIRECTORY / "source_loader.py"))["load_package"](
    _SHARED_DIRECTORY, "_shared.h2h_dev_tools"
)

from _shared.h2h_dev_tools.pytest_source import main  # noqa: E402 -- Load verified snapshot source before importing it.

if __name__ == "__main__":
    main()
