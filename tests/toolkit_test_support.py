"""Expose fixture helpers from this checkout's verified tooling snapshot."""

import runpy
from pathlib import Path

_SHARED_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "scripts/_shared/h2h_dev_tools"
)
runpy.run_path(str(_SHARED_DIRECTORY / "source_loader.py"))["load_package"](
    _SHARED_DIRECTORY, "_shared.h2h_dev_tools"
)

from _shared.h2h_dev_tools.executable_fixture import (  # noqa: E402 -- Verify snapshot before importing shared source.
    write_python_executable as write_python_executable,
)
from _shared.h2h_dev_tools.review_test_support import (  # noqa: E402 -- Verify snapshot before importing shared source.
    synthetic_review_response_source as synthetic_review_response_source,
)
