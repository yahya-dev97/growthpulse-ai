"""Deterministic data quality for client analyses: evidence, deduplication and coverage."""

import itertools
from datetime import date
from uuid import uuid4

import pytest

from factories import make_row
from growthpulse_ai.accounts.legacy import LEGACY_META_ACCOUNT_ID, LEGACY_SYNTHETIC_CLIENT
from growthpulse_ai.accounts.scheduling import DateWindow, windows_cover
from growthpulse_ai.connectors.synthetic import SyntheticConnector
from growthpulse_ai.data_quality import (
    QUALITY_EVIDENCE_VERSION,
    merge_quarantine_evidence,
    record_warnings,
    validate_rows,
    window_quality_summary,
)
from growthpulse_ai.persistence.rows import quarantine_evidence, quarantine_rows
from growthpulse_ai.persistence.tenancy import SyncEvidence
from growthpulse_ai.pipeline.client_analysis import window_evidence
from growthpulse_ai.pipeline.failures import QualityEvidenceIncompleteError
from growthpulse_ai.schema import DataSource

ACCOUNT = uuid4()
OCTOBER = DateWindow(date(2026, 10, 1), date(2026, 10, 30))


def evidence_for(raw_rows: list[dict], account=ACCOUNT) -> list[dict]:
    quarantined = validate_rows(raw_rows).quarantined
    return quarantine_evidence(
        quarantined, data_source=DataSource.SYNTHETIC, connector="synthetic", ad_account_id=account
    )


BAD = [
    make_row(reporting_date="2026-10-25", spend="-1.00"),
    make_row(reporting_date="2026-10-26", clicks=500, impressions=100),
    make_row(reporting_date="not a date", campaign_id="x"),
]


# Evidence from one sync


def test_evidence_ids_are_the_stored_quarantine_ids():
    quarantined = validate_rows(BAD).quarantined
    stored = quarantine_rows(
        quarantined, data_source=DataSource.SYNTHETIC, connector="synthetic",
        run_id=uuid4(), ad_account_id=ACCOUNT,
    )  # fmt: skip
    assert [e["quarantine_id"] for e in evidence_for(BAD)] == sorted(
        row["quarantine_id"] for row in stored
    )


def test_evidence_counts_copies_codes_and_dates_without_values():
    items = evidence_for([*BAD, BAD[0]])  # the first bad row appears twice
    by_date = {item["reporting_date"]: item for item in items}
    assert set(by_date) == {"2026-10-25", "2026-10-26", None}
    assert by_date["2026-10-25"]["rows"] == 2
    assert by_date["2026-10-25"]["errors_by_code"] == {"negative_value": 2}
    assert "-1.00" not in repr(items)


# Overlapping syncs


def test_overlapping_refetches_count_each_bad_row_once():
    full = evidence_for(BAD)
    lookback = evidence_for(BAD[:2])
    merged = merge_quarantine_evidence([*full, *lookback, *full])
    assert merged == merge_quarantine_evidence(full)
    summary = window_quality_summary([], [*full, *lookback])
    assert summary["quarantined_rows"] == 3


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_merging_never_depends_on_sync_order(order):
    syncs = [evidence_for(BAD), evidence_for(BAD[:1] * 2), evidence_for(BAD[1:])]
    merged = merge_quarantine_evidence(item for i in order for item in syncs[i])
    assert merged == merge_quarantine_evidence(item for sync in syncs for item in sync)
    # Where syncs saw a different number of copies, the larger is kept.
    assert {m["reporting_date"]: m["rows"] for m in merged}["2026-10-25"] == 2


def test_window_quality_matches_validation_for_the_milestone_4_data():
    result = SyntheticConnector().fetch(OCTOBER.start, OCTOBER.end)
    assert window_quality_summary(result.records, []) == result.summary
    shuffled = list(reversed(result.records))
    assert window_quality_summary(shuffled, []) == result.summary


def test_recomputed_warnings_include_dataset_warnings():
    rows = [make_row(reporting_date="2026-10-01"),
            make_row(reporting_date="2026-10-02", campaign_name="[SYNTHETIC] Renamed")]  # fmt: skip
    records = validate_rows(rows).records
    codes = sorted(issue.code.value for issue in record_warnings(records))
    assert codes == sorted(issue.code.value for issue in validate_rows(rows).warnings)
    assert "campaign_renamed" in codes


# Coverage and selection for an analysis window


@pytest.mark.parametrize(
    ("windows", "covered"),
    [
        ([OCTOBER], True),
        ([DateWindow(date(2026, 10, 1), date(2026, 10, 15)),
          DateWindow(date(2026, 10, 16), date(2026, 10, 30))], True),
        ([DateWindow(date(2026, 10, 10), date(2026, 10, 30)),
          DateWindow(date(2026, 9, 1), date(2026, 10, 12))], True),
        ([DateWindow(date(2026, 10, 1), date(2026, 10, 14)),
          DateWindow(date(2026, 10, 16), date(2026, 10, 30))], False),
        ([DateWindow(date(2026, 10, 2), date(2026, 10, 30))], False),
        ([], False),
    ],
)  # fmt: skip
def test_windows_cover(windows, covered):
    assert windows_cover(windows, OCTOBER) is covered


def sync(account, window, items, version=QUALITY_EVIDENCE_VERSION):
    return SyncEvidence(account, window, {"evidence_version": version, "quarantine": items})


GOOGLE, META = (a.ad_account_id for a in LEGACY_SYNTHETIC_CLIENT.accounts)


def test_selection_counts_dated_items_inside_the_window_and_undated_ones():
    meta_items = evidence_for(BAD, account=LEGACY_META_ACCOUNT_ID)
    syncs = [sync(GOOGLE, OCTOBER, []), sync(META, OCTOBER, meta_items)]
    early = DateWindow(date(2026, 10, 1), date(2026, 10, 24))
    selected = window_evidence(LEGACY_SYNTHETIC_CLIENT, syncs, early)
    assert [item["reporting_date"] for item in selected] == [None]
    assert len(window_evidence(LEGACY_SYNTHETIC_CLIENT, syncs, OCTOBER)) == 3


def test_selection_refuses_a_window_an_account_did_not_sync():
    partial = DateWindow(date(2026, 10, 10), date(2026, 10, 30))
    with pytest.raises(QualityEvidenceIncompleteError):
        window_evidence(LEGACY_SYNTHETIC_CLIENT, [sync(GOOGLE, OCTOBER, []),
                                                 sync(META, partial, [])], OCTOBER)  # fmt: skip
    with pytest.raises(QualityEvidenceIncompleteError):
        window_evidence(LEGACY_SYNTHETIC_CLIENT, [sync(GOOGLE, OCTOBER, [])], OCTOBER)


def test_selection_refuses_evidence_it_cannot_read():
    syncs = [sync(GOOGLE, OCTOBER, [], version=None), sync(META, OCTOBER, [])]
    with pytest.raises(QualityEvidenceIncompleteError):
        window_evidence(LEGACY_SYNTHETIC_CLIENT, syncs, OCTOBER)


# Complete responses (decision 3)


class UnconfirmedConnector(SyntheticConnector):
    def _response_complete(self) -> bool:
        return False


class BrokenConnector(SyntheticConnector):
    def _fetch_raw(self, start_date, end_date):
        raise RuntimeError("page 3 failed")


def test_only_confirmed_responses_are_complete():
    connector = SyntheticConnector()
    connector.fetch(OCTOBER.start, OCTOBER.end)
    assert connector.last_fetch_complete is True
    unconfirmed = UnconfirmedConnector(rows=[])
    assert unconfirmed.fetch(OCTOBER.start, OCTOBER.end).records == ()
    assert unconfirmed.last_fetch_complete is False  # empty and unconfirmed: not trusted
    broken = BrokenConnector()
    broken.last_fetch_complete = True
    with pytest.raises(Exception, match="Could not read source"):
        broken.fetch(OCTOBER.start, OCTOBER.end)
    assert broken.last_fetch_complete is False
