"""Fixed, storable error codes for every kind of pipeline run.

Codes never carry a message, host, user name or data value. Account syncs use them to
decide whether to retry (see growthpulse_ai.accounts.scheduling).
"""

from growthpulse_ai.accounts.models import AccountMismatchError
from growthpulse_ai.analytics.errors import AnalyticsError
from growthpulse_ai.connectors.base import ConnectorError
from growthpulse_ai.persistence.errors import classify_database_error


class NoValidRecordsError(Exception):
    """Every row was quarantined, or no stored record covers the analysis window."""


class QualityEvidenceIncompleteError(Exception):
    """Successful syncs do not cover every day of the analysis window for every account."""


class IncompleteResponseError(Exception):
    """The connector could not confirm it returned every row for the window."""


def classify_error(error: BaseException) -> str:
    """Map any exception to a fixed code that is safe to store."""
    database_code = classify_database_error(error)
    if database_code is not None:
        return database_code
    if isinstance(error, AccountMismatchError):
        return "account_mismatch"
    if isinstance(error, ConnectorError):
        return "connector_unavailable"
    if isinstance(error, NoValidRecordsError):
        return "no_valid_records"
    if isinstance(error, QualityEvidenceIncompleteError):
        return "quality_evidence_incomplete"
    if isinstance(error, IncompleteResponseError):
        return "incomplete_response"
    if isinstance(error, AnalyticsError):
        return "analytics_error"
    return "unexpected_error"
