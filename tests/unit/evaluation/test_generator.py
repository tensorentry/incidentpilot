from functools import cache
from pathlib import Path
from random import Random

import pytest

from incidentpilot.adapters.fixture_bundle import ObservationBundle
from incidentpilot.evaluation.cases import (
    BASELINE,
    CASES,
    GeneratedCase,
    snapshot_id_for,
    synchronous_dependency,
)
from incidentpilot.evaluation.generate import encode, main, write_case
from incidentpilot.evaluation.oracle import Oracle
from incidentpilot.evaluation.telemetry import LATENCY_BOUNDS_MS, latencies

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = REPO_ROOT / "scenarios"


@cache
def case(seed: int = 7) -> GeneratedCase:
    return synchronous_dependency(seed)


# Reproducibility (the acceptance test)


def test_the_same_seed_writes_identical_bytes() -> None:
    again = synchronous_dependency(7)

    assert encode(again.bundle) == encode(case().bundle)
    assert encode(again.oracle) == encode(case().oracle)


def test_a_different_seed_writes_different_telemetry() -> None:
    other = case(8)

    assert other.bundle.manifest.snapshot_id != case().bundle.manifest.snapshot_id
    assert other.bundle.metrics != case().bundle.metrics


def test_written_files_parse_back_to_the_same_case() -> None:
    assert ObservationBundle.model_validate_json(encode(case().bundle)) == case().bundle
    assert Oracle.model_validate_json(encode(case().oracle)) == case().oracle


def test_snapshot_ids_are_opaque_and_stable() -> None:
    snapshot_id = snapshot_id_for("synchronous-dependency", 7)

    assert snapshot_id == case().bundle.manifest.snapshot_id
    assert snapshot_id.startswith("snap-")
    assert "synchronous" not in snapshot_id
    assert snapshot_id != snapshot_id_for("synchronous-dependency", 8)
    assert snapshot_id != snapshot_id_for("other-family", 7)


def test_the_committed_files_match_a_fresh_run() -> None:
    expected = {}
    for build, seed in CASES:
        generated = build(seed)
        name = f"{generated.bundle.manifest.snapshot_id}.json"
        expected[f"observations/{name}"] = encode(generated.bundle)
        expected[f"oracle/{name}"] = encode(generated.oracle)
    committed = {
        str(path.relative_to(SCENARIOS)): path.read_bytes() for path in SCENARIOS.glob("*/*.json")
    }

    assert committed.keys() == expected.keys(), "run `make fixtures` and commit the result"
    for name, content in expected.items():
        assert committed[name] == content, f"{name} is stale: run `make fixtures`"


def test_main_writes_every_case(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path)]) == 0

    written = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.glob("*/*.json"))
    assert written == sorted(
        str(path.relative_to(SCENARIOS)) for path in SCENARIOS.glob("*/*.json")
    )


def test_write_case_pairs_a_bundle_with_its_oracle(tmp_path: Path) -> None:
    root = tmp_path / "new" / "scenarios"
    write_case(case(), root)
    write_case(case(), root)
    name = f"{case().bundle.manifest.snapshot_id}.json"

    assert (root / "observations" / name).read_bytes() == encode(case().bundle)
    assert (root / "oracle" / name).read_bytes() == encode(case().oracle)


def test_main_writes_to_scenarios_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0
    # Compared by name, because macOS paths ignore case.
    assert [path.name for path in tmp_path.iterdir()] == ["scenarios"]
    assert sorted(path.name for path in (tmp_path / "scenarios").iterdir()) == [
        "observations",
        "oracle",
    ]


def test_main_explains_itself(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "Write the committed cases" in capsys.readouterr().out


def test_latencies_count_every_sample_including_overflow() -> None:
    rng = Random(1)  # noqa: S311 - a fixed seed, not a secret
    series = latencies(rng, BASELINE, lambda t: 5000, lambda t: 50, service="checkout-api")
    point = series.points[0]

    assert point.overflow > 0
    assert sum(bucket.count for bucket in point.buckets) + point.overflow == 50
    assert [bucket.le for bucket in point.buckets] == list(LATENCY_BOUNDS_MS)


def test_latencies_put_fast_samples_in_the_first_bucket() -> None:
    rng = Random(1)  # noqa: S311 - a fixed seed, not a secret
    series = latencies(rng, BASELINE, lambda t: 1, lambda t: 50, service="checkout-api")

    assert series.points[0].buckets[0].count == 50
    assert series.points[0].overflow == 0
