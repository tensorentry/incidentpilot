"""The CLI's argument handling, before anything connects (erratum E10)."""

import asyncio
import os
import re
from pathlib import Path

import pytest

from incidentpilot import cli
from incidentpilot.application.errors import RequestError
from incidentpilot.domain.enums import RunMode
from incidentpilot.domain.incident import CreateIncident
from incidentpilot.persistence.isolation import LOCAL_OWNER_ID
from incidentpilot.settings import Settings


def exactly(message: str) -> str:
    return rf"^{re.escape(message)}$"


def test_the_owner_defaults_to_the_local_owner() -> None:
    arguments = cli.build_parser().parse_args(["import-snapshots"])

    assert (arguments.owner, arguments.root) == (LOCAL_OWNER_ID, Path("scenarios/observations"))


@pytest.mark.parametrize(
    "argv",
    [
        ["--owner", "everyone", "import-snapshots"],
        ["start-investigation", "not-a-uuid", "--snapshot", "s", "--start", "a", "--end", "b"],
        ["show-investigation", "not-a-uuid"],
        ["cancel-investigation", "not-a-uuid"],
        [
            "create-incident",
            "--service",
            "x",
            "--severity",
            "sev9",
            "--start",
            "a",
            "--end",
            "b",
            "--description",
            "d",
        ],
    ],
    ids=["owner", "incident", "investigation", "cancel", "severity"],
)
def test_malformed_arguments_are_usage_errors(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)

    assert caught.value.code == 2
    assert "invalid" in capsys.readouterr().err


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([])

    assert caught.value.code == 2
    assert "required: command" in capsys.readouterr().err


def test_an_invalid_request_names_fields_without_echoing_input() -> None:
    fields = {
        "description": "canary-secret-value",
        "service": "Not A Service",
        "severity": "sev2",
        "window": {"start_at": "2026-11-04T18:00:00Z", "end_at": "2026-11-04T17:00:00Z"},
    }
    with pytest.raises(RequestError) as caught:
        cli.parse(CreateIncident, fields)

    message = str(caught.value)
    assert message.startswith("invalid request: service: String should match pattern")
    assert "window: Value error, window must end after it starts" in message
    assert "canary-secret-value" not in message
    assert "Not A Service" not in message


def test_running_without_a_database_exits_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INCIDENTPILOT_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    message = "incidentpilot: Invalid configuration: INCIDENTPILOT_DATABASE_URL is required"
    with pytest.raises(SystemExit, match=exactly(message)):
        cli.main(["import-snapshots"])


def test_help_describes_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    # argparse rewraps the text, so compare it with whitespace collapsed.
    assert "It calls the application services directly" in " ".join(capsys.readouterr().out.split())


CREATE = [
    "create-incident",
    "--service",
    "checkout-api",
    "--severity",
    "sev2",
    "--start",
    "a",
    "--end",
    "b",
    "--description",
    "d",
]
START = [
    "start-investigation",
    "00000000-0000-4000-8000-000000000003",
    "--snapshot",
    "s",
    "--start",
    "a",
    "--end",
    "b",
]


def without(argv: list[str], option: str) -> list[str]:
    at = argv.index(option)
    return argv[:at] + argv[at + 2 :]


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        *(
            (CREATE, option)
            for option in ["--service", "--severity", "--start", "--end", "--description"]
        ),
        *((START, option) for option in ["--snapshot", "--start", "--end"]),
    ],
)
def test_every_required_option_is_required(
    argv: list[str], option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(without(argv, option))

    assert caught.value.code == 2
    assert f"the following arguments are required: {option}" in capsys.readouterr().err


def test_an_unknown_mode_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([*START, "--mode", "turbo"])

    assert caught.value.code == 2
    assert "argument --mode: invalid choice: 'turbo'" in capsys.readouterr().err


def help_text(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit):
        cli.main([*argv, "--help"])
    return " ".join(capsys.readouterr().out.split())


def test_help_names_the_program_and_each_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    text = help_text(capsys)

    assert text.startswith("usage: incidentpilot [-h] [--owner OWNER]")
    for command, summary in [
        ("import-snapshots", "register observation files"),
        ("create-incident", "report an incident"),
        ("start-investigation", "queue an investigation"),
        ("show-investigation", "state, configuration and events"),
        ("cancel-investigation", "stop queued or running work"),
        ("worker", "run offline_demo investigations"),
    ]:
        assert f"{command} {summary}" in text


def test_help_explains_the_options_that_need_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    create = help_text(capsys, "create-incident")
    start = help_text(capsys, "start-investigation")

    assert "--start START UTC start, such as 2026-11-04T18:15:00Z" in create
    assert "--key KEY idempotency key; reuse it to retry safely" in create
    assert "--mode {offline_demo,live,replay} default: this process's --key KEY" in start
    assert "--key KEY idempotency key; reuse it to retry safely" in start
    worker = help_text(capsys, "worker")

    assert "--jobs JOBS stop after this many; default: never" in worker
    assert (
        "--provider-delay PROVIDER_DELAY seconds the scripted provider takes to answer each request"
    ) in worker


def test_validation_errors_give_the_field_path_and_separate_problems() -> None:
    fields = {
        "description": "d",
        "service": "checkout-api",
        "severity": "sev2",
        "window": {"start_at": "yesterday", "end_at": "today"},
    }
    with pytest.raises(RequestError) as caught:
        cli.parse(CreateIncident, fields)

    assert re.fullmatch(
        r"invalid request: window\.start_at: Input should be a valid datetime[^;]*;"
        r" window\.end_at: Input should be a valid datetime.*",
        str(caught.value),
    )


def test_a_worker_runs_forever_on_default_leases_unless_told_otherwise() -> None:
    arguments = cli.build_parser().parse_args(["worker"])

    assert (
        arguments.worker_id,
        arguments.jobs,
        arguments.lease_seconds,
        arguments.provider_delay,
    ) == (f"worker-{os.getpid()}", None, 30.0, 0.0)


def test_worker_options_take_numbers() -> None:
    argv = ["--worker-id", "w-1", "--jobs", "2", "--lease-seconds", "1.5"]
    arguments = cli.build_parser().parse_args(["worker", *argv, "--provider-delay", "0.25"])

    assert (arguments.worker_id, arguments.jobs, arguments.lease_seconds) == ("w-1", 2, 1.5)
    assert arguments.provider_delay == 0.25


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--jobs", "0"], "argument --jobs: must be positive: 0"),
        (["--jobs", "1.5"], "argument --jobs: invalid int value: '1.5'"),
        (["--lease-seconds", "0"], "argument --lease-seconds: must be positive: 0"),
        (["--lease-seconds", "-1"], "argument --lease-seconds: must be positive: -1"),
        (["--lease-seconds", "soon"], "argument --lease-seconds: invalid float value: 'soon'"),
    ],
)
def test_worker_counts_and_leases_must_be_positive(
    argv: list[str],
    error: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # With no database, a value wrongly accepted fails on configuration instead of
    # starting a worker.
    monkeypatch.delenv("INCIDENTPILOT_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as caught:
        cli.main(["worker", *argv])

    assert caught.value.code == 2
    assert capsys.readouterr().err.endswith(f"error: {error}\n")


@pytest.mark.parametrize("mode", [RunMode.LIVE, RunMode.REPLAY])
def test_only_the_offline_demo_has_a_worker(mode: RunMode) -> None:
    arguments = cli.build_parser().parse_args(["worker"])
    settings = Settings.model_construct(mode=mode)

    message = "only offline_demo has a worker until the live investigator (T4.7)"
    with pytest.raises(RequestError, match=exactly(message)):
        asyncio.run(cli.work("unused", settings, arguments))
