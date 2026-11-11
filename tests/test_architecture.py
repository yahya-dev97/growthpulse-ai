"""Enforce the package boundaries described in the README."""

import ast
from collections.abc import Iterator
from pathlib import Path

import growthpulse_ai

ROOT_PACKAGE = "growthpulse_ai"
PACKAGE_DIR = Path(growthpulse_ai.__file__).parent

LAYERS = {
    "connectors",
    "analytics",
    "ai_analysis",
    "persistence",
    "pipeline",
    "dashboard",
    "credentials",
}

FORBIDDEN_IMPORTS = {
    # Shared modules sit below every layer, so any layer can depend on them.
    "schema": LAYERS | {"data_quality", "accounts"},
    "data_quality": LAYERS | {"accounts"},
    "accounts": LAYERS,
    # Tokens: nothing above may reach analytics, AI or the dashboard through credentials,
    # and credentials depend on no layer.
    "credentials": LAYERS - {"credentials"},
    "connectors": {"analytics", "ai_analysis", "persistence", "pipeline", "dashboard"},
    "analytics": {"connectors", "ai_analysis", "persistence", "pipeline", "dashboard"}
    | {"credentials", "accounts"},
    "ai_analysis": {"connectors", "persistence", "pipeline", "dashboard", "credentials"},
    "persistence": {"connectors", "analytics", "ai_analysis", "pipeline", "dashboard"},
    "pipeline": {"dashboard"},
    "dashboard": {"connectors", "ai_analysis", "pipeline", "credentials"},
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
        single_module = package_dir / f"{layer}.py"
        paths = [single_module] if single_module.exists() else (package_dir / layer).rglob("*.py")
        for path in paths:
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


def test_checker_covers_shared_modules(tmp_path: Path):
    (tmp_path / "schema.py").write_text("from growthpulse_ai.connectors import base\n")

    assert find_violations(tmp_path)


def test_analytics_depends_only_on_the_schema():
    """Records are the only contract, so new data sources need no analytics changes."""
    allowed = {"analytics", "schema"}
    for path in (PACKAGE_DIR / "analytics").rglob("*.py"):
        for module in imported_modules(path, PACKAGE_DIR):
            parts = module.split(".")
            if parts[0] == ROOT_PACKAGE and len(parts) > 1:
                assert parts[1] in allowed, f"{path.name} imports {module}"
