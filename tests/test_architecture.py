"""Enforce the package boundaries described in the README."""

import ast
from collections.abc import Iterator
from pathlib import Path

import growthpulse_ai

ROOT_PACKAGE = "growthpulse_ai"
PACKAGE_DIR = Path(growthpulse_ai.__file__).parent

FORBIDDEN_IMPORTS = {
    "connectors": {"analytics", "ai_analysis", "persistence", "pipeline", "dashboard"},
    "analytics": {"connectors", "ai_analysis", "persistence", "pipeline", "dashboard"},
    "ai_analysis": {"connectors", "persistence", "pipeline", "dashboard"},
    "persistence": {"connectors", "analytics", "ai_analysis", "pipeline", "dashboard"},
    "pipeline": {"dashboard"},
    "dashboard": {"connectors", "ai_analysis", "pipeline"},
}


def imported_modules(path: Path, package_dir: Path) -> Iterator[str]:
    """Yield absolute dotted names for every import in a file, resolving relative ones."""
    relative = path.relative_to(package_dir).with_suffix("")
    current_package = [ROOT_PACKAGE, *relative.parts[:-1]]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = current_package[: len(current_package) - node.level + 1] if node.level else []
            prefix = ".".join([*base, *([node.module] if node.module else [])])
            yield prefix
            yield from (f"{prefix}.{alias.name}" for alias in node.names)


def find_violations(package_dir: Path) -> list[str]:
    violations = []
    for layer, forbidden in FORBIDDEN_IMPORTS.items():
        for path in (package_dir / layer).rglob("*.py"):
            for module in imported_modules(path, package_dir):
                parts = module.split(".")
                if len(parts) > 1 and parts[0] == ROOT_PACKAGE and parts[1] in forbidden:
                    violations.append(f"{path.name} in {layer} imports {module}")
    return violations


def test_package_boundaries_hold():
    assert find_violations(PACKAGE_DIR) == []


def test_checker_detects_absolute_and_relative_violations(tmp_path: Path):
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "absolute.py").write_text("from growthpulse_ai.pipeline import run\n")
    (dashboard / "relative.py").write_text("from ..connectors import meta\n")
    (dashboard / "parent.py").write_text("from .. import ai_analysis\n")
    (dashboard / "allowed.py").write_text("from growthpulse_ai.persistence import read\n")

    offending_files = {violation.split()[0] for violation in find_violations(tmp_path)}

    assert offending_files == {"absolute.py", "relative.py", "parent.py"}
