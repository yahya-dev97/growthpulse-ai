"""The frozen Milestone 4 store used by upgrade tests must stay exactly as committed."""

import hashlib
from pathlib import Path

import pytest

FROZEN = Path(__file__).parent / "m4_frozen"


@pytest.mark.parametrize(
    ("name", "sha256"),
    [
        # Verbatim from commit 0eb3ca4 except the one import line naming m4_frozen.rows.
        ("store.py", "1f06cea53dea4805150e2fd8ef3f52c8b6dbd8c921955d89dfa53fbf215d9ae5"),
        # Verbatim from commit 0eb3ca4.
        ("rows.py", "95e37cd261fdbafce616b3bd1f8e90490305af49fc48e8c7e3fb6346cb7ca8c9"),
    ],
)
def test_frozen_milestone_4_files_are_unchanged(name: str, sha256: str):
    assert hashlib.sha256((FROZEN / name).read_bytes()).hexdigest() == sha256


def test_frozen_store_writes_no_scope_columns():
    text = (FROZEN / "store.py").read_text()
    assert "client_id" not in text and "ad_account_id" not in text
