"""Install CI tools using this checkout's declared requirements."""

import runpy
from pathlib import Path

_SHARED_DIRECTORY = Path(__file__).resolve().parent / "_shared/h2h_dev_tools"
runpy.run_path(str(_SHARED_DIRECTORY / "source_loader.py"))["load_package"](
    _SHARED_DIRECTORY, "_shared.h2h_dev_tools"
)

from _shared.h2h_dev_tools.ci_dependencies import main  # noqa: E402 -- Load verified snapshot source before importing it.

if __name__ == "__main__":
    main(Path(__file__).resolve().parents[1])
