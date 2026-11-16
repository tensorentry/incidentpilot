"""Settings shared by every test run."""

import shutil
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Give each run its own temporary root unless one was chosen.

    Concurrent runs, as mutation testing starts, otherwise share one root and prune
    each other's old folders mid-run, which fails whichever run loses the race.
    """
    if config.option.basetemp is None:
        config.option.basetemp = tempfile.mkdtemp(prefix="incidentpilot-pytest-")
        config.add_cleanup(lambda: shutil.rmtree(Path(config.option.basetemp), ignore_errors=True))
