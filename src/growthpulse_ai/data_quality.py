"""Validation and data quality rules, shared by every connector.

Errors are hard failures: the row is quarantined and never reaches analytics.
Warnings keep the record but flag something a reader should know about.

Issue messages and logs never contain field values, so a misplaced secret cannot leak.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from growthpulse_ai.schema import AdPerformanceRecord, DataSource


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    # Hard failures
    INVALID_SCHEMA = "invalid_schema"
    INVALID_FIELD = "invalid_field"
    NEGATIVE_VALUE = "negative_value"
    CLICKS_EXCEED_IMPRESSIONS = "clicks_exceed_impressions"
    MISSING_CAMPAIGN_IDENTIFIER = "missing_campaign_identifier"
    INVALID_DATE = "invalid_date"
    INVALID_CURRENCY = "invalid_currency"
    INVALID_TIMEZONE = "invalid_timezone"
    MISSING_CONVERSION_DEFINITION = "missing_conversion_definition"
    INVALID_ATTRIBUTION = "invalid_attribution"
    INCONSISTENT_DIMENSIONS = "inconsistent_dimensions"
    DUPLICATE_RECORD = "duplicate_record"
    CONFLICTING_DUPLICATE = "conflicting_duplicate"
    DATA_SOURCE_MISMATCH = "data_source_mismatch"
    # Warnings
    MISSING_ATTRIBUTION = "missing_attribution"
    ZERO_CONVERSIONS_WITH_SPEND = "zero_conversions_with_spend"
    SPEND_WITHOUT_IMPRESSIONS = "spend_without_impressions"
    CLICKS_WITHOUT_SPEND = "clicks_without_spend"
    CONVERSIONS_EXCEED_CLICKS = "conversions_exceed_clicks"
    VALUE_WITHOUT_CONVERSIONS = "value_without_conversions"
    CAMPAIGN_RENAMED = "campaign_renamed"
    ATTRIBUTION_CHANGED = "attribution_changed"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: IssueCode
    severity: Severity
    message: str
    field: str | None = None
    row_index: int | None = None
    record_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    row_index: int
    issues: tuple[QualityIssue, ...]
    raw: Any = field(repr=False)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    records: tuple[AdPerformanceRecord, ...]
    quarantined: tuple[QuarantinedRow, ...]
    warnings: tuple[QualityIssue, ...]

    @property
    def summary(self) -> dict[str, Any]:
        errors = Counter(issue.code.value for row in self.quarantined for issue in row.issues)
        return {
            "total_rows": len(self.records) + len(self.quarantined),
            "valid_records": len(self.records),
            "quarantined_rows": len(self.quarantined),
            "warnings": len(self.warnings),
            "errors_by_code": dict(errors),
            "warnings_by_code": dict(Counter(issue.code.value for issue in self.warnings)),
        }


# Row index and parsed record, for rows that passed schema validation.
_Candidate = tuple[int, AdPerformanceRecord]
_Rejections = defaultdict[int, list[QualityIssue]]

_FIELD_CODES = {
    "campaign_id": IssueCode.MISSING_CAMPAIGN_IDENTIFIER,
    "campaign_name": IssueCode.MISSING_CAMPAIGN_IDENTIFIER,
    "reporting_date": IssueCode.INVALID_DATE,
    "ingested_at": IssueCode.INVALID_DATE,
    "currency": IssueCode.INVALID_CURRENCY,
    "account_timezone": IssueCode.INVALID_TIMEZONE,
    "conversion_definition": IssueCode.MISSING_CONVERSION_DEFINITION,
    "attribution": IssueCode.INVALID_ATTRIBUTION,
}


def _code_for(error: ErrorDetails) -> IssueCode:
    if error["type"] in IssueCode:
        return IssueCode(error["type"])
    if not error["loc"]:
        return IssueCode.INVALID_SCHEMA
    top_level_field = str(error["loc"][0])
    if top_level_field in _FIELD_CODES:
        return _FIELD_CODES[top_level_field]
    if error["type"] == "greater_than_equal":
        return IssueCode.NEGATIVE_VALUE
    return IssueCode.INVALID_FIELD


def _schema_issues(error: ValidationError, row_index: int) -> list[QualityIssue]:
    return [
        QualityIssue(
            code=_code_for(detail),
            severity=Severity.ERROR,
            message=detail["msg"],
            field=".".join(str(part) for part in detail["loc"]) or None,
            row_index=row_index,
        )
        for detail in error.errors(include_input=False, include_url=False, include_context=False)
    ]


def _reject(
    rejections: _Rejections,
    candidate: _Candidate,
    code: IssueCode,
    message: str,
    field_name: str | None = None,
) -> None:
    row_index, record = candidate
    rejections[row_index].append(
        QualityIssue(code, Severity.ERROR, message, field_name, row_index, record.record_id)
    )


def _group(
    candidates: list[_Candidate], key: Callable[[AdPerformanceRecord], Hashable]
) -> dict[Hashable, list[_Candidate]]:
    groups: defaultdict[Hashable, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[key(candidate[1])].append(candidate)
    return groups


def _comparable_content(record: AdPerformanceRecord) -> dict[str, Any]:
    return record.model_dump(exclude={"ingested_at"})


def _check_duplicates(candidates: list[_Candidate], rejections: _Rejections) -> None:
    """Keep the first of exact repeats. Reject every row when repeats disagree."""
    for group in _group(candidates, lambda record: record.record_id).values():
        if len(group) == 1:
            continue
        first_content = _comparable_content(group[0][1])
        if all(_comparable_content(record) == first_content for _, record in group):
            for candidate in group[1:]:
                _reject(
                    rejections,
                    candidate,
                    IssueCode.DUPLICATE_RECORD,
                    "Exact repeat of an earlier row with the same record identity",
                )
        else:
            for candidate in group:
                _reject(
                    rejections,
                    candidate,
                    IssueCode.CONFLICTING_DUPLICATE,
                    "Rows share a record identity but report different values",
                )


# Each rule rejects a whole group whose rows disagree, since no single row can be trusted.
_CONSISTENCY_RULES: tuple[tuple[Callable, Callable, str, str], ...] = (
    (
        lambda r: (r.platform, r.account_id),
        lambda r: r.currency,
        "currency",
        "Account reports more than one currency",
    ),
    (
        lambda r: (r.platform, r.account_id),
        lambda r: r.account_timezone,
        "account_timezone",
        "Account reports more than one timezone",
    ),
    (
        lambda r: (r.platform, r.account_id, r.campaign_id),
        lambda r: r.campaign_type,
        "campaign_type",
        "Campaign reports more than one campaign type",
    ),
    (
        lambda r: (r.platform, r.account_id, r.conversion_definition.event_id),
        lambda r: (r.conversion_definition.event_name, r.conversion_definition.stage),
        "conversion_definition",
        "One conversion event ID has more than one definition",
    ),
    (
        lambda r: r.delivery_key,
        lambda r: (r.impressions, r.clicks, r.spend),
        None,
        "Rows for the same campaign-day report different delivery metrics",
    ),
)


def _check_consistency(candidates: list[_Candidate], rejections: _Rejections) -> None:
    for group_key, value_of, field_name, message in _CONSISTENCY_RULES:
        for group in _group(candidates, group_key).values():
            if len({value_of(record) for _, record in group}) > 1:
                for candidate in group:
                    _reject(
                        rejections,
                        candidate,
                        IssueCode.INCONSISTENT_DIMENSIONS,
                        message,
                        field_name,
                    )


def _warn(
    code: IssueCode, message: str, candidate: _Candidate, field_name: str | None = None
) -> QualityIssue:
    row_index, record = candidate
    return QualityIssue(code, Severity.WARNING, message, field_name, row_index, record.record_id)


def _record_warnings(candidate: _Candidate) -> list[QualityIssue]:
    record = candidate[1]
    checks = (
        (
            record.attribution is None,
            IssueCode.MISSING_ATTRIBUTION,
            "Attribution metadata is unavailable, so conversions cannot be compared reliably",
        ),
        (
            record.spend > 0 and record.conversions == 0,
            IssueCode.ZERO_CONVERSIONS_WITH_SPEND,
            "Campaign spent money and reported no conversions",
        ),
        (
            record.spend > 0 and record.impressions == 0,
            IssueCode.SPEND_WITHOUT_IMPRESSIONS,
            "Campaign spent money and reported no impressions",
        ),
        (
            record.clicks > 0 and record.spend == 0,
            IssueCode.CLICKS_WITHOUT_SPEND,
            "Campaign reported clicks with no spend",
        ),
        (
            record.conversions > record.clicks,
            IssueCode.CONVERSIONS_EXCEED_CLICKS,
            "Conversions exceed clicks, which view-through attribution can explain",
        ),
        (
            record.conversions == 0 and record.conversion_value > 0,
            IssueCode.VALUE_WITHOUT_CONVERSIONS,
            "Conversion value reported with no conversions",
        ),
    )
    return [_warn(code, message, candidate) for failed, code, message in checks if failed]


def _dataset_warnings(candidates: list[_Candidate]) -> list[QualityIssue]:
    warnings = []
    for group in _group(candidates, lambda r: (r.platform, r.account_id, r.campaign_id)).values():
        if len({record.campaign_name for _, record in group}) > 1:
            warnings.append(
                _warn(
                    IssueCode.CAMPAIGN_RENAMED,
                    "Campaign name changed during the reporting period",
                    group[0],
                    "campaign_name",
                )
            )
    for group in _group(
        candidates,
        lambda r: (r.platform, r.account_id, r.campaign_id, r.conversion_definition.event_id),
    ).values():
        if len({record.attribution for _, record in group}) > 1:
            warnings.append(
                _warn(
                    IssueCode.ATTRIBUTION_CHANGED,
                    "Attribution settings changed, so the series is not comparable across the "
                    "change",
                    group[0],
                    "attribution",
                )
            )
    return warnings


def validate_rows(
    rows: Iterable[Any], expected_source: DataSource | None = None
) -> ValidationResult:
    """Validate raw rows, returning clean records, quarantined rows, and warnings."""
    raw_rows = list(rows)
    rejections: _Rejections = defaultdict(list)
    candidates: list[_Candidate] = []

    for row_index, raw in enumerate(raw_rows):
        try:
            record = AdPerformanceRecord.model_validate(raw)
        except ValidationError as error:
            rejections[row_index].extend(_schema_issues(error, row_index))
            continue
        candidate = (row_index, record)
        if expected_source is not None and record.data_source is not expected_source:
            _reject(
                rejections,
                candidate,
                IssueCode.DATA_SOURCE_MISMATCH,
                "Row data_source does not match this connector",
                "data_source",
            )
            continue
        candidates.append(candidate)

    _check_duplicates(candidates, rejections)
    candidates = [candidate for candidate in candidates if candidate[0] not in rejections]
    _check_consistency(candidates, rejections)
    clean = [candidate for candidate in candidates if candidate[0] not in rejections]

    warnings = [issue for candidate in clean for issue in _record_warnings(candidate)]
    warnings.extend(_dataset_warnings(clean))

    return ValidationResult(
        records=tuple(record for _, record in clean),
        quarantined=tuple(
            QuarantinedRow(row_index, tuple(issues), raw_rows[row_index])
            for row_index, issues in sorted(rejections.items())
        ),
        warnings=tuple(warnings),
    )
