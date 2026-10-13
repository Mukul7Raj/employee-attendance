"""
Comprehensive Unit Tests for Business Rules (R1 - R8) and Application Logic
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from app.main import (
    IST,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    count_working_days,
    dt_to_epoch_ms,
    epoch_ms_to_utc_dt,
    get_attendance_date,
    round_half_up,
)


def test_round_half_up():
    assert round_half_up(2.345, 2) == 2.35
    assert round_half_up(2.344, 2) == 2.34
    assert round_half_up(9.12345, 4) == 9.1235
    assert round_half_up(None, 2) is None


def test_rule_r1_attendance_date_normal():
    # Normal shift 09:30 - 18:30 IST on 2026-07-06
    # 09:28 IST = 03:58 UTC
    punch_in_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = get_attendance_date(punch_in_utc, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_rule_r1_attendance_date_overnight():
    # Overnight shift 22:00 - 06:00 IST
    # Punch in at 2026-07-07 02:00:00 IST (which is < 06:00 IST)
    # Belongs to previous day (2026-07-06)
    # 02:00 IST on 2026-07-07 is 2026-07-06 20:30 UTC
    punch_in_utc = datetime(2026, 7, 6, 20, 30, 0, tzinfo=timezone.utc)
    att_date = get_attendance_date(punch_in_utc, "22:00", "06:00")
    assert att_date == "2026-07-06"

    # Punch in at 2026-07-06 22:05:00 IST (which is >= 06:00 IST)
    # Belongs to 2026-07-06
    punch_in_utc2 = datetime(2026, 7, 6, 16, 35, 0, tzinfo=timezone.utc)
    att_date2 = get_attendance_date(punch_in_utc2, "22:00", "06:00")
    assert att_date2 == "2026-07-06"


def test_rule_r2_late_minutes():
    # Shift 09:30 on 2026-07-06
    # 09:40:00 IST -> 0
    t_09_40_00 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(t_09_40_00, "09:30", "2026-07-06") == 0

    # 09:40:01 IST -> 10
    t_09_40_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(t_09_40_01, "09:30", "2026-07-06") == 10

    # 10:15:59 IST -> 45
    t_10_15_59 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(t_10_15_59, "09:30", "2026-07-06") == 45


def test_rule_r3_overtime():
    # Shift end 18:30 on 2026-07-06
    # Punch out at 18:35 (5 min) -> 0 (< 30 min)
    t_18_35 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_18_35, "09:30", "18:30", "2026-07-06") == 0

    # Punch out at 19:10 (40 min) -> 40 (>= 30 min)
    t_19_10 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(t_19_10, "09:30", "18:30", "2026-07-06") == 40


def test_rule_r4_work_hours():
    t_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    # (13:05 - 03:58) = 9 hours 7 minutes = 32820 seconds / 3600 = 9.11666... -> 9.12
    assert compute_work_hours(t_in, t_out) == 9.12


def test_rule_r5_half_day():
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(8.00) is False
    assert compute_half_day(None) is False


def test_rule_r7_working_days():
    # July 2026: 2026-07-01 to 2026-07-31
    # July 1, 2026 is Wednesday
    # Total Mon-Fri in July 2026 is 23 working days
    assert count_working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23

    # Employee joined mid-month: 2026-07-13 (Monday)
    # Count from 2026-07-13 to 2026-07-31: 15 working days
    assert count_working_days(date(2026, 7, 13), date(2026, 7, 31)) == 15
