"""The committed portfolio screenshots exist and are exactly 1920 x 1080 PNGs."""

import struct

import pytest

from growthpulse_ai.config import PROJECT_ROOT

SCREENSHOTS = PROJECT_ROOT / "portfolio" / "screenshots"
FILES = (
    "growthpulse_performance_intelligence_1920x1080.png",
    "growthpulse_analytical_findings_experiments_1920x1080.png",
)


@pytest.mark.parametrize("name", FILES)
def test_screenshot_is_a_1920_by_1080_png(name: str):
    data = (SCREENSHOTS / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1920, 1080)
