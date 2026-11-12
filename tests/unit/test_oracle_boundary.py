"""Erratum E12: no worker-side code may reach `incidentpilot.evaluation` or oracle files.

Every module outside `evaluation/` is parsed, so the check needs no import and
can't be skipped by a module that fails to load. It catches every import form,
relative imports, and string references such as `importlib` targets or paths.
"""

import ast
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
EVALUATION = "incidentpilot.evaluation"
FORBIDDEN_TEXT = (EVALUATION, "scenarios/oracle")


def module_name(path: Path, root: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def imported_names(source: str, module: str, is_package: bool) -> Iterator[str]:
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{base}.{node.module}" if node.module else base
            else:
                base = node.module or ""
            yield base
            # `from incidentpilot import evaluation` imports a submodule by name.
            yield from (f"{base}.{alias.name}" for alias in node.names)


def string_constants(source: str) -> Iterator[str]:
    """Every string the code can use. Docstrings are skipped: they never execute."""
    tree = ast.parse(source)
    documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, documented)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def problems(source: str, module: str, is_package: bool = False) -> list[str]:
    found = [
        f"imports {name}"
        for name in imported_names(source, module, is_package)
        if name == EVALUATION or name.startswith(f"{EVALUATION}.")
    ]
    found += [
        f"mentions {text}"
        for value in string_constants(source)
        for text in FORBIDDEN_TEXT
        if text in value
    ]
    return found


def violations(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        module, is_package = module_name(path, root)
        if module == EVALUATION or module.startswith(f"{EVALUATION}."):
            continue
        for problem in problems(path.read_text(), module, is_package):
            found.append(f"{path.relative_to(root)}: {problem}")
    return found


def test_no_worker_side_module_reaches_evaluation() -> None:
    assert violations(SOURCE_ROOT) == []


def test_the_check_covers_real_modules_on_both_sides() -> None:
    modules = {module_name(path, SOURCE_ROOT)[0] for path in SOURCE_ROOT.rglob("*.py")}

    assert {EVALUATION, f"{EVALUATION}.oracle", "incidentpilot.adapters.fixture_bundle"} <= modules
    assert len(modules) > 15


@pytest.mark.parametrize(
    "source",
    [
        "import incidentpilot.evaluation",
        "import incidentpilot.evaluation.oracle as o",
        "from incidentpilot import evaluation",
        "from incidentpilot.evaluation.oracle import Oracle",
        "from ..evaluation import oracle",
        "from .. import evaluation",
        "from ..evaluation.oracle import Oracle",
        "def load():\n    from incidentpilot.evaluation import oracle",
        "import importlib\nimportlib.import_module('incidentpilot.evaluation.oracle')",
        "PATH = 'scenarios/oracle/case.json'",
    ],
)
def test_every_route_to_the_oracle_is_caught(source: str) -> None:
    assert problems(source, "incidentpilot.adapters.fixture_bundle") != []


def test_relative_imports_resolve_from_a_package() -> None:
    assert problems("from . import evaluation", "incidentpilot.adapters", is_package=True) == []
    assert problems("from .. import evaluation", "incidentpilot.adapters", is_package=True) != []
    assert problems("from . import evaluation", "incidentpilot", is_package=True) != []


def test_docstrings_may_name_the_boundary() -> None:
    source = '"""Nothing here may import incidentpilot.evaluation."""\n'
    function = 'def f():\n    """Reads scenarios/oracle in the grader only."""\n'

    assert problems(source, "incidentpilot.adapters", is_package=True) == []
    assert problems(function, "incidentpilot.adapters.fixture_bundle") == []
    assert problems("x = 1\nNOTE = 'incidentpilot.evaluation'\n", "incidentpilot.x") != []


@pytest.mark.parametrize(
    "source",
    [
        "from incidentpilot.domain import report",
        "import incidentpilot.evaluations_archive",
        "from . import evaluator",
        "TEXT = 'evaluation happens later'",
        "PATH = 'scenarios/observations/case.json'",
    ],
)
def test_lookalikes_are_not_flagged(source: str) -> None:
    assert problems(source, "incidentpilot.adapters.fixture_bundle") == []


def test_a_planted_import_fails_the_check(tmp_path: Path) -> None:
    shutil.copytree(SOURCE_ROOT / "incidentpilot", tmp_path / "incidentpilot")
    leak = tmp_path / "incidentpilot" / "adapters" / "leak.py"
    leak.write_text("from ..evaluation.oracle import Oracle\n")

    assert violations(tmp_path) == [
        "incidentpilot/adapters/leak.py: imports incidentpilot.evaluation.oracle",
        "incidentpilot/adapters/leak.py: imports incidentpilot.evaluation.oracle.Oracle",
    ]


def test_evaluation_itself_may_import_anything(tmp_path: Path) -> None:
    shutil.copytree(SOURCE_ROOT / "incidentpilot", tmp_path / "incidentpilot")
    grader = tmp_path / "incidentpilot" / "evaluation" / "grader.py"
    grader.write_text("from incidentpilot.evaluation.oracle import Oracle\n")

    assert violations(tmp_path) == []
