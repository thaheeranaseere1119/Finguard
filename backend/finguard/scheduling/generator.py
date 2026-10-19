"""Deterministic recurring-payment schedule generation from an approved contract version.

Rules (documented in docs/scheduling.md):
  * Service periods are anchored at the contract start date: P_k = [A_k, A_{k+1} − 1 day], where A_k is computed
    from the ORIGINAL anchor (start + k·step), never by repeatedly adding to the previous date, so month-end dates
    do not drift (Jan 31 → Feb 28/29 → Mar 31). "Monthly" is calendar months, never 30 days.
  * The last period is truncated at the contract end date; with proration DAILY its amount is
    amount × actual_days / full_period_days (Decimal, ROUND_HALF_UP to minor units); with NONE the full amount applies.
  * Nominal payment date: ADVANCE → first rule date on/after period start; ARREARS → first rule date after period
    end. Rule date = due day-of-month (clamped to month length; 31 = last day) or due weekday; without a rule,
    ADVANCE pays on the period start and ARREARS on the period end. An explicit first_payment_date overrides the
    first occurrence's nominal date.
  * Business-day adjustment (weekends + organization holidays): NONE | FOLLOWING | PRECEDING | MODIFIED_FOLLOWING.
  * The execution instant is due_date at the organization's execution hour in the CONTRACT's time zone, converted
    to UTC (zoneinfo handles DST; a non-existent local time is shifted forward by fold handling).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from finguard.core.money import quantize_money

GENERATOR_VERSION = "sched-gen-1.0"
MAX_OCCURRENCES = 520  # safety bound (10 years weekly)


@dataclass(frozen=True)
class Terms:
    payment_amount: Decimal
    currency: str
    frequency: str            # WEEKLY | MONTHLY | QUARTERLY | ANNUALLY | CUSTOM
    start_date: date
    end_date: date | None
    interval_count: int = 1
    custom_unit: str | None = None  # DAYS | WEEKS | MONTHS (CUSTOM only)
    due_day_of_month: int | None = None
    due_weekday: int | None = None
    first_payment_date: date | None = None
    payment_timing: str = "ARREARS"
    business_day_rule: str = "FOLLOWING"
    proration_rule: str = "NONE"
    timezone: str = "Asia/Kolkata"


@dataclass(frozen=True)
class Occurrence:
    sequence_no: int
    period_start: date
    period_end: date
    nominal_date: date
    due_date: date
    due_at: datetime
    amount: Decimal
    is_prorated: bool


@dataclass(frozen=True)
class Calendar:
    holidays: frozenset[date] = frozenset()
    weekend: frozenset[int] = frozenset({5, 6})
    execution_hour: int = 9

    def is_business_day(self, d: date) -> bool:
        return d.weekday() not in self.weekend and d not in self.holidays


class ScheduleError(ValueError):
    pass


def _step(terms: Terms) -> tuple[str, int]:
    f, k = terms.frequency, terms.interval_count
    if f == "WEEKLY":
        return "weeks", k
    if f == "MONTHLY":
        return "months", k
    if f == "QUARTERLY":
        return "months", 3 * k
    if f == "ANNUALLY":
        return "months", 12 * k
    if f == "CUSTOM":
        unit = (terms.custom_unit or "").upper()
        if unit not in ("DAYS", "WEEKS", "MONTHS"):
            raise ScheduleError("CUSTOM frequency requires custom_unit DAYS, WEEKS or MONTHS")
        return unit.lower(), k
    raise ScheduleError(f"unsupported frequency {f}")


def _anchor(start: date, unit: str, n: int) -> date:
    if unit == "months":
        return start + relativedelta(months=n)  # relativedelta clamps to month end
    if unit == "weeks":
        return start + timedelta(weeks=n)
    return start + timedelta(days=n)


def _clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _rule_date_on_or_after(d: date, terms: Terms, unit: str) -> date:
    if terms.due_day_of_month and unit == "months":
        cand = _clamp_day(d.year, d.month, terms.due_day_of_month)
        if cand < d:
            nxt = d + relativedelta(months=1)
            cand = _clamp_day(nxt.year, nxt.month, terms.due_day_of_month)
        return cand
    if terms.due_weekday is not None and unit in ("weeks", "days"):
        return d + timedelta(days=(terms.due_weekday - d.weekday()) % 7)
    return d


def adjust_business_day(d: date, rule: str, cal: Calendar) -> date:
    if rule == "NONE" or cal.is_business_day(d):
        return d
    if rule == "PRECEDING":
        x = d
        while not cal.is_business_day(x):
            x -= timedelta(days=1)
        return x
    x = d
    while not cal.is_business_day(x):
        x += timedelta(days=1)
    if rule == "MODIFIED_FOLLOWING" and x.month != d.month:
        return adjust_business_day(d, "PRECEDING", cal)
    return x


def execution_instant(d: date, tz_name: str, hour: int) -> datetime:
    tz = ZoneInfo(tz_name)
    local = datetime.combine(d, time(hour=hour), tzinfo=tz)
    # Round-trip through UTC normalizes non-existent wall times (DST gap) deterministically.
    return local.astimezone(timezone.utc)


def generate(terms: Terms, cal: Calendar | None = None, *, from_date: date | None = None) -> list[Occurrence]:
    """Generate all occurrences. With from_date, only occurrences whose period starts on/after it are returned
    (used to regenerate future occurrences after an amendment); sequence numbers stay globally consistent."""
    cal = cal or Calendar()
    if terms.payment_amount <= 0:
        raise ScheduleError("payment amount must be positive")
    if terms.end_date is None:
        raise ScheduleError("an end date is required to generate a finite schedule")
    if terms.end_date < terms.start_date:
        raise ScheduleError("end date precedes start date")
    unit, n = _step(terms)
    out: list[Occurrence] = []
    k = 0
    while True:
        p_start = _anchor(terms.start_date, unit, k * n)
        if p_start > terms.end_date:
            break
        full_next = _anchor(terms.start_date, unit, (k + 1) * n)
        full_end = full_next - timedelta(days=1)
        p_end = min(full_end, terms.end_date)
        amount = terms.payment_amount
        prorated = False
        if p_end < full_end and terms.proration_rule == "DAILY":
            actual = (p_end - p_start).days + 1
            full = (full_end - p_start).days + 1
            amount = quantize_money((terms.payment_amount * Decimal(actual) / Decimal(full)), terms.currency)
            prorated = True
        if terms.payment_timing == "ADVANCE":
            nominal = _rule_date_on_or_after(p_start, terms, unit)
        else:
            nominal = (_rule_date_on_or_after(p_end + timedelta(days=1), terms, unit)
                       if (terms.due_day_of_month or terms.due_weekday is not None) else p_end)
        if k == 0 and terms.first_payment_date:
            nominal = terms.first_payment_date
        due = adjust_business_day(nominal, terms.business_day_rule, cal)
        if from_date is None or p_start >= from_date:
            out.append(Occurrence(k + 1, p_start, p_end, nominal, due, execution_instant(due, terms.timezone,
                                                                                          cal.execution_hour),
                                  amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), prorated))
        k += 1
        if k > MAX_OCCURRENCES:
            raise ScheduleError("schedule exceeds the maximum number of occurrences")
    return out


def expected_amount_for_period(terms: Terms, period_start: date, cal: Calendar | None = None) -> Decimal | None:
    """Re-derive the authorized amount for a period from approved terms (used by the payment hard rule)."""
    for occ in generate(terms, cal):
        if occ.period_start == period_start:
            return occ.amount
    return None


def schedule_total(terms: Terms, cal: Calendar | None = None) -> Decimal:
    return sum((o.amount for o in generate(terms, cal)), Decimal(0))
