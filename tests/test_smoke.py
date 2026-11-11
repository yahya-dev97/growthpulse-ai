import importlib
import sys

import pytest

import growthpulse_ai


def test_runs_on_supported_python():
    assert sys.version_info >= (3, 12)


def test_package_exposes_version():
    assert growthpulse_ai.__version__


@pytest.mark.parametrize(
    "module",
    ["config", "connectors", "analytics", "ai_analysis", "persistence", "pipeline", "dashboard"],
)
def test_module_imports(module: str):
    importlib.import_module(f"growthpulse_ai.{module}")
