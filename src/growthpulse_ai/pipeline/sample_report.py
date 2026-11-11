"""Run the local flow: synthetic connector, validation, KPIs, anomalies, report.

Usage: uv run python -m growthpulse_ai.pipeline.sample_report
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

from growthpulse_ai.analytics.report import build_report, dumps_report
from growthpulse_ai.config import PROJECT_ROOT
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.connectors.synthetic_data import DEFAULT_DAYS, DEFAULT_START_DATE

SAMPLE_REPORT_PATH = PROJECT_ROOT / "docs" / "samples" / "synthetic_analytics_report.json"


def build_sample_report() -> dict[str, Any]:
    end_date = DEFAULT_START_DATE + timedelta(days=DEFAULT_DAYS - 1)
    result = SyntheticConnector().fetch(DEFAULT_START_DATE, end_date)
    return build_report(result.records, data_quality=result.summary)


def write_sample_report(path: Path = SAMPLE_REPORT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_report(build_sample_report()), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(f"Wrote {write_sample_report().relative_to(PROJECT_ROOT)}")
