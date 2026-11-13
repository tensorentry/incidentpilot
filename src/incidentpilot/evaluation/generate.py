"""Write the committed cases to `scenarios/`: `python -m incidentpilot.evaluation.generate`.

Files are canonical JSON, so the same seed always writes the same bytes and a
test can check the committed files against a fresh run.
"""

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel

from incidentpilot.domain.canonical import canonical_json
from incidentpilot.evaluation.cases import CASES, GeneratedCase
from incidentpilot.evaluation.leakage import LeakError, forbidden_terms, leaks


def encode(model: BaseModel) -> bytes:
    return canonical_json(model.model_dump()) + b"\n"


def write_case(case: GeneratedCase, root: Path) -> None:
    """Write both files, or neither if the bundle would leak its answer."""
    name = f"{case.bundle.manifest.snapshot_id}.json"
    found = leaks(encode(case.bundle).decode(), forbidden_terms(case.family, case.oracle))
    if found:
        raise LeakError(f"{name} would show the investigator: {', '.join(found)}")
    for folder, model in (("observations", case.bundle), ("oracle", case.oracle)):
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / name).write_bytes(encode(model))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("scenarios"))
    root = parser.parse_args(argv).root
    for build, seed in CASES:
        write_case(build(seed), root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
