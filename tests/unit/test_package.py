from importlib.metadata import version

import incidentpilot


def test_version_matches_installed_metadata() -> None:
    assert incidentpilot.__version__ == version("incidentpilot")
