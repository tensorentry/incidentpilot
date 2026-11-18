"""§9.3 items 1 and 4: the typed output maps to the report candidate, and a bad or
refused output ends the run with an explicit reason.

Whether a real model can follow the schema is T1.6. These tests show what the pinned
SDK does with the schema and with whatever comes back.
"""

import json
from typing import Any
from uuid import uuid4

import pytest
from agents import Agent, ModelBehaviorError, ModelRefusalError, Runner

from incidentpilot.domain.enums import StopReason
from incidentpilot.domain.report import ReportCandidate
from incidentpilot.model_runtime.sdk import candidate_output, run_config, stop_reason
from tests.sdk_compat.fake import FakeModel, message, refusal


def candidate() -> dict[str, Any]:
    return {
        "diagnostic_outcome": "insufficient_evidence",
        "observations": [
            {
                "observation_id": "obs-1",
                "claim": "checkout-api logged an upstream timeout.",
                "claim_kind": "observed",
                "references": [{"kind": "log_record", "evidence_id": str(uuid4())}],
                "measurement": None,
            }
        ],
        "hypotheses": [],
        "root_causes": [],
        "impact": {"affected_services": [], "measurements": []},
        "alternatives": [],
        "recommendations": [],
        "limitations": ["One log record is not enough to name a cause."],
    }


def investigator(model: FakeModel) -> Agent[None]:
    return Agent(name="investigator", model=model, output_type=candidate_output())


async def finish_with(text: str) -> object:
    model = FakeModel([[message(text)]])
    result = await Runner.run(investigator(model), "investigate", run_config=run_config())
    return result.final_output


def schema_nodes(node: object) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [found for item in node for found in schema_nodes(item)]
    if not isinstance(node, dict):
        return []
    return [node, *(found for value in node.values() for found in schema_nodes(value))]


def test_the_candidate_schema_is_strict() -> None:
    output = candidate_output()
    assert output.is_strict_json_schema()
    assert not output.is_plain_text()

    objects = [node for node in schema_nodes(output.json_schema()) if "properties" in node]
    assert objects
    for node in objects:
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])


def test_strict_mode_turns_each_selector_choice_into_any_of() -> None:
    # Pydantic writes the selector union as oneOf, which strict mode doesn't accept
    # inside an object, so the SDK rewrites it. That's only safe because each choice
    # is told apart by a different `kind`.
    nodes = schema_nodes(candidate_output().json_schema())
    assert not [node for node in nodes if "oneOf" in node]

    definitions = candidate_output().json_schema()["$defs"]
    unions = [node["anyOf"] for node in nodes if "discriminator" in node]
    assert unions
    for choices in unions:
        kinds = [
            definitions[choice["$ref"].removeprefix("#/$defs/")]["properties"]["kind"]["const"]
            for choice in choices
        ]
        assert sorted(kinds) == [
            "deployment_record",
            "log_record",
            "metric_series_window",
            "span_summary",
        ]


async def test_a_valid_answer_arrives_as_a_report_candidate() -> None:
    answer = candidate()

    final = await finish_with(json.dumps(answer))

    assert isinstance(final, ReportCandidate)
    assert final.model_dump(mode="json") == answer


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({**candidate(), "owner_id": str(uuid4())}),
        json.dumps({**candidate(), "diagnostic_outcome": "solved"}),
        json.dumps({key: value for key, value in candidate().items() if key != "limitations"}),
    ],
    ids=["malformed", "extra field", "unknown outcome", "missing field"],
)
async def test_an_answer_off_the_schema_is_a_provider_failure(answer: str) -> None:
    with pytest.raises(ModelBehaviorError) as raised:
        await finish_with(answer)

    assert stop_reason(raised.value) is StopReason.PROVIDER_FAILURE


async def test_a_refusal_is_reported_as_one() -> None:
    model = FakeModel([[refusal("I can't help with that.")]])

    with pytest.raises(ModelRefusalError) as raised:
        await Runner.run(investigator(model), "investigate", run_config=run_config())

    assert raised.value.refusal == "I can't help with that."
    assert stop_reason(raised.value) is StopReason.MODEL_REFUSED
