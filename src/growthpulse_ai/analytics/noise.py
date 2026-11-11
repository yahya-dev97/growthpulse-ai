"""A heuristic guard against changes that small conversion counts could easily produce.

A change clears the guard when::

    |ln(current / baseline)| >= width * sqrt(1 / B + 1 / C)

where B and C are the platform-attributed conversion counts in the two periods.

The band treats the counts as if they were independent Poisson counts, for which the
approximate variance of a log ratio is 1/B + 1/C. Other inputs, such as spend and clicks,
are treated as fixed, and the variability of conversion value is ignored.

This is not a statistical significance test or a confidence interval, and no p-value or
coverage is implied. Platform-attributed conversions are not verified customers or bookings.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from enum import StrEnum

_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_ONE = Decimal(1)


class NoiseGuardOutcome(StrEnum):
    PASSED = "passed"
    DOWNGRADED = "downgraded"
    SUPPRESSED = "suppressed"
    NOT_EVALUATED = "not_evaluated"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class NoiseGuardResult:
    outcome: NoiseGuardOutcome
    log_ratio_magnitude: Decimal | None = None
    band: Decimal | None = None
    high_severity_band: Decimal | None = None
    baseline_conversions: Decimal | None = None
    current_conversions: Decimal | None = None


NOT_APPLICABLE = NoiseGuardResult(NoiseGuardOutcome.NOT_APPLICABLE)


def evaluate_noise_guard(
    baseline_value: Decimal | int | None,
    current_value: Decimal | int | None,
    baseline_conversions: Decimal,
    current_conversions: Decimal,
    width: Decimal,
    high_severity_width: Decimal,
    is_high_severity: bool,
) -> NoiseGuardResult:
    """Assess a change. A zero value or count leaves the log ratio undefined, so it is skipped."""
    if (
        baseline_value is None
        or current_value is None
        or baseline_value <= 0
        or current_value <= 0
        or baseline_conversions <= 0
        or current_conversions <= 0
    ):
        return NoiseGuardResult(
            NoiseGuardOutcome.NOT_EVALUATED,
            baseline_conversions=baseline_conversions,
            current_conversions=current_conversions,
        )

    ratio = _CONTEXT.divide(Decimal(current_value), Decimal(baseline_value))
    magnitude = abs(_CONTEXT.ln(ratio))
    spread = _CONTEXT.sqrt(
        _CONTEXT.add(
            _CONTEXT.divide(_ONE, baseline_conversions),
            _CONTEXT.divide(_ONE, current_conversions),
        )
    )
    band = _CONTEXT.multiply(width, spread)
    high_band = _CONTEXT.multiply(high_severity_width, spread)

    if magnitude < band:
        outcome = NoiseGuardOutcome.SUPPRESSED
    elif is_high_severity and magnitude < high_band:
        outcome = NoiseGuardOutcome.DOWNGRADED
    else:
        outcome = NoiseGuardOutcome.PASSED
    return NoiseGuardResult(
        outcome, magnitude, band, high_band, baseline_conversions, current_conversions
    )
