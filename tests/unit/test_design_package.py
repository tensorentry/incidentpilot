"""The design spec is frozen; corrections belong in docs/spec-errata.md and ADRs."""

import hashlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    loaded: dict[str, object] = json.loads((REPO_ROOT / "MANIFEST.json").read_text())
    return loaded


def test_spec_matches_pinned_digest(manifest: dict[str, object]) -> None:
    spec = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"
    assert hashlib.sha256(spec.read_bytes()).hexdigest() == manifest["design_sha256"]


def test_manifest_files_exist(manifest: dict[str, object]) -> None:
    files = manifest["files"]
    assert isinstance(files, list)
    missing = [name for name in files if not (REPO_ROOT / name).is_file()]
    assert missing == []
