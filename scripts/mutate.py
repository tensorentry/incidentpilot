"""Mutation testing behind `make mutate`. Exits non-zero if any mutation goes unnoticed.

mutmut covers function bodies but skips module-level tables and enum values, so this
script also flips every state transition and renames every enum value.
"""

import enum
import inspect
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from incidentpilot.domain import enums, transitions

ROOT = Path(__file__).resolve().parents[1]
COPIED = ["src", "tests", "docs", "pyproject.toml", "MANIFEST.json", ".env.example", "README.md"]
TRANSITIONS_FILE = Path("src/incidentpilot/domain/transitions.py")
ENUMS_FILE = Path("src/incidentpilot/domain/enums.py")
TABLES: dict[str, Mapping[Any, frozenset[Any]]] = {
    "INVESTIGATION_TRANSITIONS": transitions.INVESTIGATION_TRANSITIONS,
    "ATTEMPT_TRANSITIONS": transitions.ATTEMPT_TRANSITIONS,
    "MODEL_CALL_TRANSITIONS": transitions.MODEL_CALL_TRANSITIONS,
    "TOOL_CALL_TRANSITIONS": transitions.TOOL_CALL_TRANSITIONS,
}
TESTS_FAILED = 1
ENVIRONMENT = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def mutmut_results(mutmut: str, *options: str) -> list[str]:
    results = subprocess.run(
        [mutmut, "results", *options],
        cwd=ROOT,
        env=ENVIRONMENT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in results.stdout.splitlines() if line.strip()]


def run_mutmut() -> tuple[int, list[str]]:
    shutil.rmtree(ROOT / "mutants", ignore_errors=True)
    mutmut = str(Path(sys.executable).parent / "mutmut")
    subprocess.run(
        [mutmut, "run", "--max-children", "4"],
        cwd=ROOT,
        env=ENVIRONMENT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return len(mutmut_results(mutmut, "--all", "true")), mutmut_results(mutmut)


def pytest_exit_code(copy: Path) -> int:
    for cache in copy.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/domain", "-q", "-x", "-p", "no:cacheprovider"],
        cwd=copy,
        env={**ENVIRONMENT, "PYTHONPATH": str(copy / "src")},
        capture_output=True,
        check=False,
    ).returncode


def override(table: str, source: enum.Enum, targets: frozenset[enum.Enum]) -> str:
    status_type = type(source).__name__
    members = ", ".join(f"{status_type}.{target.name}" for target in targets)
    expression = f"frozenset({{{members}}})" if members else "frozenset()"
    key = f"{status_type}.{source.name}"
    return f"\n{table} = MappingProxyType({{**{table}, {key}: {expression}}})\n"


def edge_mutations(original: str) -> Iterator[tuple[str, str]]:
    for table_name, table in TABLES.items():
        states = list(table)
        for source, target in itertools.product(states, states):
            targets: frozenset[enum.Enum] = frozenset(table[source]) ^ {target}
            yield f"{source}->{target}", original + override(table_name, source, targets)


def enum_mutations(original: str) -> Iterator[tuple[str, str]]:
    for name, status_type in inspect.getmembers(enums, inspect.isclass):
        if issubclass(status_type, enum.StrEnum) and status_type is not enum.StrEnum:
            start = original.index(f"class {name}(StrEnum):")
            for member in status_type:
                line = f'    {member.name} = "{member.value}"\n'
                position = original.index(line, start)
                renamed = f'    {member.name} = "{member.value}_mutated"\n'
                yield (
                    f"{name}.{member.name}",
                    original[:position] + renamed + original[position + len(line) :],
                )


def check_mutations(
    copy: Path, file: Path, mutations: Iterator[tuple[str, str]], original: str
) -> tuple[int, list[str]]:
    path = copy / file
    total = 0
    unnoticed = []
    for name, text in mutations:
        path.write_text(text)
        total += 1
        if pytest_exit_code(copy) != TESTS_FAILED:
            unnoticed.append(name)
    path.write_text(original)
    return total, unnoticed


def main() -> int:
    try:
        total, survivors = run_mutmut()
    except subprocess.CalledProcessError:
        print("ABORT: mutmut failed. Tests must pass before mutation results mean anything")
        return 2
    if total == 0:
        print("ABORT: mutmut produced no mutants, so a pass would mean nothing")
        return 2
    print(f"mutmut: {total - len(survivors)}/{total} killed")

    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory)
        for name in COPIED:
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, copy / name)
            else:
                shutil.copy(source, copy / name)
        transitions_text = (copy / TRANSITIONS_FILE).read_text()
        enums_text = (copy / ENUMS_FILE).read_text()

        if pytest_exit_code(copy) != 0:
            print("ABORT: tests are already failing, so mutation results would mean nothing")
            return 2
        table = transitions.ATTEMPT_TRANSITIONS
        status = enums.AttemptStatus.RUNNING
        no_op = transitions_text + override("ATTEMPT_TRANSITIONS", status, frozenset(table[status]))
        (copy / TRANSITIONS_FILE).write_text(no_op)
        if pytest_exit_code(copy) != 0:
            print("ABORT: a no-op override failed, so the harness itself is broken")
            return 2
        (copy / TRANSITIONS_FILE).write_text(transitions_text)

        edges, missed_edges = check_mutations(
            copy, TRANSITIONS_FILE, edge_mutations(transitions_text), transitions_text
        )
        print(f"state edges: {edges - len(missed_edges)}/{edges} caught")
        values, missed_values = check_mutations(
            copy, ENUMS_FILE, enum_mutations(enums_text), enums_text
        )
        print(f"enum values: {values - len(missed_values)}/{values} caught")

    problems = [*survivors, *missed_edges, *missed_values]
    for problem in problems:
        print(f"NOT CAUGHT: {problem}")
    print("All mutations caught." if not problems else f"{len(problems)} mutations not caught.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
