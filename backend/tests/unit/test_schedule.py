"""Deterministic schedule generation: calendar months, month ends, leap years, business days, DST, proration."""

from datetime import date
from decimal import Decimal as D

import pytest

from finguard.scheduling.generator import (Calendar, ScheduleError, Terms, adjust_business_day, execution_instant,
                                           expected_amount_for_period, generate)


def T(**kw):
    base = dict(payment_amount=D("20000"), currency="INR", frequency="MONTHLY", start_date=date(2026, 1, 1),
                end_date=date(2026, 12, 31), due_day_of_month=5)
    base.update(kw)
    return Terms(**base)


def test_monthly_on_fifth_in_arrears_12_occurrences():
    occ = generate(T(business_day_rule="NONE"))
    assert len(occ) == 12
    assert [o.nominal_date for o in occ[:3]] == [date(2026, 2, 5), date(2026, 3, 5), date(2026, 4, 5)]
    assert occ[0].period_start == date(2026, 1, 1) and occ[0].period_end == date(2026, 1, 31)
    assert sum(o.amount for o in occ) == D("240000.00")


def test_advance_pays_at_period_start():
    occ = generate(T(payment_timing="ADVANCE", business_day_rule="NONE"))
    assert occ[0].nominal_date == date(2026, 1, 5)


def test_month_end_does_not_drift():
    occ = generate(T(start_date=date(2024, 1, 31), end_date=date(2024, 12, 30), due_day_of_month=31,
                     payment_timing="ADVANCE", business_day_rule="NONE"))
    starts = [o.period_start for o in occ[:4]]
    assert starts == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)]  # leap year Feb 29
    assert occ[1].nominal_date == date(2024, 2, 29)
    assert occ[2].nominal_date == date(2024, 3, 31)


def test_non_leap_february_clamps_to_28():
    occ = generate(T(start_date=date(2025, 1, 31), end_date=date(2025, 4, 30), due_day_of_month=31,
                     payment_timing="ADVANCE", business_day_rule="NONE"))
    assert occ[1].nominal_date == date(2025, 2, 28)


def test_monthly_is_not_thirty_days():
    occ = generate(T(business_day_rule="NONE"))
    gaps = {(b.nominal_date - a.nominal_date).days for a, b in zip(occ, occ[1:])}
    assert gaps != {30} and 28 in gaps and 31 in gaps


def test_business_day_rules():
    cal = Calendar(holidays=frozenset({date(2026, 4, 6)}))
    sunday = date(2026, 4, 5)
    assert adjust_business_day(sunday, "FOLLOWING", cal) == date(2026, 4, 7)  # Monday is a holiday
    assert adjust_business_day(sunday, "PRECEDING", cal) == date(2026, 4, 3)
    assert adjust_business_day(sunday, "NONE", cal) == sunday
    month_end = date(2026, 5, 31)  # Sunday; following would cross into June
    assert adjust_business_day(month_end, "MODIFIED_FOLLOWING", Calendar()) == date(2026, 5, 29)


def test_weekly_every_friday():
    occ = generate(T(frequency="WEEKLY", due_day_of_month=None, due_weekday=4, start_date=date(2026, 1, 5),
                     end_date=date(2026, 2, 1), payment_timing="ADVANCE", business_day_rule="NONE"))
    assert all(o.nominal_date.weekday() == 4 for o in occ)
    assert len(occ) == 4


def test_quarterly_and_custom():
    assert len(generate(T(frequency="QUARTERLY"))) == 4
    assert len(generate(T(frequency="CUSTOM", custom_unit="WEEKS", interval_count=2, due_day_of_month=None,
                          end_date=date(2026, 3, 31)))) == 7
    with pytest.raises(ScheduleError):
        generate(T(frequency="CUSTOM", custom_unit=None))


def test_daily_proration_of_partial_last_period():
    occ = generate(T(end_date=date(2026, 3, 15), proration_rule="DAILY", business_day_rule="NONE"))
    assert occ[-1].is_prorated
    assert occ[-1].amount == (D("20000") * 15 / 31).quantize(D("0.01"))
    occ_none = generate(T(end_date=date(2026, 3, 15), proration_rule="NONE"))
    assert occ_none[-1].amount == D("20000.00")


def test_dst_execution_instant():
    # 09:00 in New York is 14:00 UTC in winter and 13:00 UTC in summer.
    assert execution_instant(date(2026, 1, 15), "America/New_York", 9).hour == 14
    assert execution_instant(date(2026, 7, 15), "America/New_York", 9).hour == 13
    assert execution_instant(date(2026, 3, 8), "America/New_York", 2).hour == 7  # non-existent local time → normalized
    assert execution_instant(date(2026, 1, 15), "Asia/Kolkata", 9).strftime("%H:%M") == "03:30"


def test_expected_amount_rederivation_and_regeneration():
    t = T()
    assert expected_amount_for_period(t, date(2026, 6, 1)) == D("20000.00")
    assert expected_amount_for_period(t, date(2026, 6, 2)) is None
    tail = generate(t, from_date=date(2026, 7, 1))
    assert tail[0].period_start == date(2026, 7, 1) and tail[0].sequence_no == 7


def test_invalid_terms():
    with pytest.raises(ScheduleError):
        generate(T(end_date=None))
    with pytest.raises(ScheduleError):
        generate(T(end_date=date(2025, 1, 1)))
