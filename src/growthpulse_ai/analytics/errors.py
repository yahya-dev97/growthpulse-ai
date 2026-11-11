"""Errors raised when analytics would otherwise produce a misleading number."""


class AnalyticsError(Exception):
    """Base class for analytics failures."""


class InvalidPeriodError(AnalyticsError):
    """A reporting period or comparison is malformed."""


class IncompletePeriodError(AnalyticsError):
    """A reporting period has missing or still-open days."""


class MixedCurrencyError(AnalyticsError):
    """An aggregate would combine more than one currency."""


class MixedDataSourceError(AnalyticsError):
    """An aggregate would combine synthetic and live data."""


class ConflictingDeliveryError(AnalyticsError):
    """Rows for one delivery_key report different delivery metrics."""


class DuplicateRecordError(AnalyticsError):
    """The same record identity appears more than once."""
