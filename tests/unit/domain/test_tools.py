import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from incidentpilot.domain.enums import ToolErrorCode, ToolName, ToolStatus
from incidentpilot.domain.tools import (
    MAX_RETRY_AFTER_MS,
    Coverage,
    ToolContext,
    ToolError,
    ToolResponse,
)
from incidentpilot.domain.window import TimeWindow

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"

INVOCATION = UUID("00000000-0000-4000-8000-0000000000d4")
START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
GAP = TimeWindow(start_at=START, end_at=START + timedelta(minutes=5))
SELECTOR = {
    "kind": "deployment_record",
    "evidence_id": UUID("00000000-0000-4000-8000-000000000001"),
}

TRANSIENT = [ToolErrorCode.TIMEOUT, ToolErrorCode.SOURCE_UNAVAILABLE, ToolErrorCode.RATE_LIMITED]
NEEDS_A_CHANGE = [
    ToolErrorCode.INVALID_ARGUMENT,
    ToolErrorCode.OUT_OF_SCOPE,
    ToolErrorCode.STALE_LEASE,
    ToolErrorCode.CANCELLED,
    ToolErrorCode.QUOTA_EXCEEDED,
    ToolErrorCode.CURSOR_INVALID,
    ToolErrorCode.RESULT_TRUNCATED,
    ToolErrorCode.INTERNAL_ERROR,
]


def parse[M: BaseModel](model: type[M], fields: dict[str, Any]) -> M:
    return model.model_validate_json(json.dumps(to_jsonable_python(fields)))


def coverage(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"fraction": 1.0, "samples": 30, "gaps": [], "clock_caveat": None}
    return fields | changes


def error(code: ToolErrorCode = ToolErrorCode.OUT_OF_SCOPE, /, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "code": code,
        "retryable": code.retryable,
        "message": "The service is outside this investigation's scope",
        "retry_after_ms": None,
    }
    return fields | changes


def response(status: str = "ok", /, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": "3.0",
        "invocation_id": INVOCATION,
        "status": status,
        "source": {"adapter": "fixture", "version": "fixture-v1", "snapshot_id": "snap-case-a"},
        "data": {"deployments": []},
        "coverage": coverage(),
        "evidence_refs": [SELECTOR] if status == "ok" else [],
        "truncated": False,
        "next_cursor": None,
        "error": None,
    }
    if status == "partial":
        fields["coverage"] = coverage(fraction=0.5, gaps=[GAP])
    if status == "error":
        fields |= {"data": None, "coverage": None, "error": error()}
    return fields | changes


def context(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "owner_id": UUID("00000000-0000-4000-8000-0000000000a1"),
        "investigation_id": UUID("00000000-0000-4000-8000-0000000000c3"),
        "attempt_id": UUID("00000000-0000-4000-8000-0000000000e5"),
        "lease_epoch": 1,
        "snapshot_id": "snap-case-a",
        "invocation_id": INVOCATION,
        "tool": "get_deployments",
    }
    return fields | changes


# Vocabulary from the spec


def test_error_codes_match_spec_section_8_2() -> None:
    match = re.search(r"Proposed codes: ([^.]+)\.", SPEC.read_text())
    assert match is not None

    assert {code.value for code in ToolErrorCode} == set(re.findall(r"`([A-Z_]+)`", match.group(1)))


def test_statuses_match_spec_section_8_2() -> None:
    match = re.search(r"Tool `status` is one of ([^.]+)\.", SPEC.read_text())
    assert match is not None

    assert {status.value for status in ToolStatus} == set(re.findall(r"`(\w+)`", match.group(1)))


def test_tools_are_the_p0_rows_of_spec_section_8_1() -> None:
    section = SPEC.read_text().split("### 8.1 ")[1].split("### 8.2 ")[0]
    rows = re.findall(r"^\| `(\w+)` \| ([^|]+) \|[^|]+\| ([^|]+) \|$", section, re.MULTILINE)
    p0 = {name for name, writable, access in rows if "Never" not in writable and "P1" not in access}

    assert len(rows) == 7
    assert {tool.value for tool in ToolName} == p0


@pytest.mark.parametrize("code", TRANSIENT)
def test_transient_errors_are_retryable(code: ToolErrorCode) -> None:
    assert code.retryable is True


@pytest.mark.parametrize("code", NEEDS_A_CHANGE)
def test_other_errors_are_not_retryable(code: ToolErrorCode) -> None:
    assert code.retryable is False


def test_every_error_code_has_a_tested_retry_decision() -> None:
    assert set(TRANSIENT) | set(NEEDS_A_CHANGE) == set(ToolErrorCode)
    assert not set(TRANSIENT) & set(NEEDS_A_CHANGE)


# Trusted context


def test_the_context_holds_every_value_section_8_1_keeps_from_the_model() -> None:
    match = re.search(r"`model_writable` scope never includes ([^.]+)\.", SPEC.read_text())
    assert match is not None
    named = match.group(1)
    in_context = {"investigation_id", "attempt_id", "owner ID", "lease epoch", "snapshot ID"}

    assert all(term in named for term in in_context)
    assert {"investigation_id", "attempt_id", "owner_id", "lease_epoch", "snapshot_id"} <= set(
        ToolContext.model_fields
    )


def test_a_context_is_accepted() -> None:
    parsed = parse(ToolContext, context())

    assert parsed.tool is ToolName.GET_DEPLOYMENTS


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_epoch": 0},
        {"tool": "simulate_rollback"},
        {"tool": "search_runbooks"},
        {"snapshot_id": ""},
        {"budget_usd": 1},
    ],
    ids=["epoch-zero", "never-registered", "p1-tool", "no-snapshot", "extra-field"],
)
def test_malformed_contexts_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(ToolContext, context(**changes))


# Errors


@pytest.mark.parametrize("code", list(ToolErrorCode))
def test_every_code_makes_a_valid_error(code: ToolErrorCode) -> None:
    assert parse(ToolError, error(code)).retryable is code.retryable


def test_a_permanent_error_cannot_claim_to_be_retryable() -> None:
    with pytest.raises(ValidationError, match="Value error, OUT_OF_SCOPE is never retryable \\["):
        parse(ToolError, error(retryable=True))


def test_a_transient_error_cannot_claim_to_be_permanent() -> None:
    with pytest.raises(ValidationError, match="Value error, TIMEOUT is retryable \\["):
        parse(ToolError, error(ToolErrorCode.TIMEOUT, retryable=False))


def test_only_a_retryable_error_suggests_when_to_retry() -> None:
    parse(ToolError, error(ToolErrorCode.RATE_LIMITED, retry_after_ms=MAX_RETRY_AFTER_MS))
    parse(ToolError, error(ToolErrorCode.RATE_LIMITED, retry_after_ms=1))
    with pytest.raises(ValidationError, match="only a retryable error suggests when to retry"):
        parse(ToolError, error(retry_after_ms=100))


@pytest.mark.parametrize(
    "changes",
    [
        {"retry_after_ms": 0},
        {"retry_after_ms": MAX_RETRY_AFTER_MS + 1},
        {"message": ""},
        {"message": "x" * 501},
        {"code": "NOT_FOUND"},
    ],
)
def test_malformed_errors_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(ToolError, error(ToolErrorCode.RATE_LIMITED, **changes))


# Coverage


@pytest.mark.parametrize(
    ("fraction", "gaps", "complete"),
    [(1.0, [], True), (0.99, [], False), (0.5, [GAP], False), (0.0, [], False)],
)
def test_coverage_is_complete_only_without_loss(
    fraction: float, gaps: list[TimeWindow], complete: bool
) -> None:
    assert parse(Coverage, coverage(fraction=fraction, gaps=gaps)).complete is complete


def test_coverage_with_gaps_cannot_be_complete() -> None:
    with pytest.raises(ValidationError, match="coverage with gaps can't be complete"):
        parse(Coverage, coverage(gaps=[GAP]))


@pytest.mark.parametrize(
    "changes",
    [
        {"fraction": 1.01},
        {"fraction": -0.01},
        {"samples": -1},
        {"clock_caveat": ""},
        {"gaps": [GAP] * 51},
    ],
)
def test_malformed_coverage_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(Coverage, coverage(**({"fraction": 0.5} | changes)))


def test_a_clock_caveat_does_not_make_coverage_incomplete() -> None:
    assert parse(Coverage, coverage(clock_caveat="Source clock may lag")).complete is True


# Envelope


def test_the_spec_sample_fits_once_errata_e3_selectors_replace_strings() -> None:
    blocks = re.findall(r"```json\n(.*?)```", SPEC.read_text(), re.DOTALL)
    sample = json.loads(next(block for block in blocks if '"invocation_id"' in block))
    with pytest.raises(ValidationError):
        ToolResponse.model_validate_json(json.dumps(sample))

    sample["invocation_id"] = str(INVOCATION)
    sample["evidence_refs"] = [
        {
            "kind": "metric_series_window",
            "evidence_id": ref["evidence_id"],
            "window": {"start_at": "2026-11-04T18:15:00Z", "end_at": "2026-11-04T18:16:00Z"},
        }
        for ref in sample["evidence_refs"]
    ]
    assert ToolResponse.model_validate_json(json.dumps(sample)).status is ToolStatus.OK


@pytest.mark.parametrize("status", list(ToolStatus))
def test_each_status_has_a_valid_shape(status: ToolStatus) -> None:
    assert parse(ToolResponse, response(status.value)).status is status


def test_an_error_without_a_source_is_accepted() -> None:
    parse(ToolResponse, response("error", source=None))


@pytest.mark.parametrize(
    ("status", "changes", "message"),
    [
        ("ok", {"next_cursor": "abc"}, "a result with a next page is truncated"),
        ("error", {"error": None}, "an error response needs an error"),
        ("error", {"data": {}}, "an error response carries no data, coverage or evidence"),
        (
            "error",
            {"coverage": coverage()},
            "an error response carries no data, coverage or evidence",
        ),
        (
            "error",
            {"evidence_refs": [SELECTOR]},
            "an error response carries no data, coverage or evidence",
        ),
        ("error", {"truncated": True}, "an error response has no pages"),
        ("ok", {"error": error()}, "status ok has no error"),
        ("partial", {"error": error()}, "status partial has no error"),
        ("ok", {"source": None}, "status ok needs a source, data and coverage"),
        ("empty", {"data": None}, "status empty needs a source, data and coverage"),
        ("partial", {"coverage": None}, "status partial needs a source, data and coverage"),
        ("partial", {"coverage": coverage()}, "a partial response has incomplete coverage"),
        ("ok", {"coverage": coverage(fraction=0.9)}, "status ok needs complete coverage"),
        ("empty", {"coverage": coverage(fraction=0.9)}, "status empty needs complete coverage"),
        ("ok", {"evidence_refs": []}, "an ok response cites the evidence it returned"),
        (
            "empty",
            {"evidence_refs": [SELECTOR]},
            "an empty response has no evidence and no more pages",
        ),
        ("empty", {"truncated": True}, "an empty response has no evidence and no more pages"),
    ],
)
def test_responses_must_fit_their_status(
    status: str, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=f"Value error, {re.escape(message)} \\["):
        parse(ToolResponse, response(status, **changes))


def test_a_truncated_page_may_offer_a_cursor() -> None:
    parse(ToolResponse, response("ok", truncated=True, next_cursor="c2Vjb25k"))
    parse(ToolResponse, response("partial", truncated=True, next_cursor="c2Vjb25k"))


def test_a_truncated_page_may_have_no_cursor() -> None:
    parse(ToolResponse, response("ok", truncated=True))


def test_a_partial_response_may_have_no_evidence() -> None:
    assert parse(ToolResponse, response("partial")).evidence_refs == []


def test_an_incomplete_response_without_gaps_is_partial() -> None:
    parse(ToolResponse, response("partial", coverage=coverage(fraction=0.9)))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2.0"},
        {"invocation_id": "example-call-001"},
        {"next_cursor": "has spaces"},
        {"next_cursor": "a" * 513},
        {"evidence_refs": ["metric:request_count:18:15"]},
        {"source": {"adapter": "Fixture", "version": "fixture-v1", "snapshot_id": "snap-case-a"}},
        {"status": "unknown"},
        {"owner_id": "00000000-0000-4000-8000-0000000000a1"},
    ],
    ids=[
        "old-schema",
        "non-uuid-invocation",
        "cursor-with-spaces",
        "long-cursor",
        "string-selector",
        "bad-adapter-name",
        "unknown-status",
        "extra-field",
    ],
)
def test_malformed_responses_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(ToolResponse, response("ok", **changes))
