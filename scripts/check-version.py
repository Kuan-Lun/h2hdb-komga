#!/usr/bin/env python3
"""Run the shared version policy implementation."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    shared_dir = Path(__file__).resolve().parent / "_shared/h2h_dev_tools"
    runpy.run_path(str(shared_dir / "source_loader.py"))["load_package"](
        shared_dir, "_shared.h2h_dev_tools"
    )
    runpy.run_module("_shared.h2h_dev_tools.version_policy", run_name="__main__")
