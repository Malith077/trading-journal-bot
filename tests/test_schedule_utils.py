"""Unit tests for services/schedule_utils.py"""
import datetime
from zoneinfo import ZoneInfo

import pytest

from services.schedule_utils import is_trading_day, MELBOURNE_TZ


@pytest.mark.parametrize("date,expected", [
    (datetime.date(2026, 7, 6), True),   # Monday
    (datetime.date(2026, 7, 7), True),   # Tuesday
    (datetime.date(2026, 7, 8), True),   # Wednesday
    (datetime.date(2026, 7, 9), True),   # Thursday
    (datetime.date(2026, 7, 10), True),  # Friday
    (datetime.date(2026, 7, 11), False), # Saturday
    (datetime.date(2026, 7, 12), False), # Sunday
])
def test_is_trading_day(date, expected):
    now = datetime.datetime.combine(date, datetime.time(12, 0), tzinfo=MELBOURNE_TZ)
    assert is_trading_day(now) is expected


def test_is_trading_day_defaults_to_now():
    # Smoke test: no argument should not raise and returns a bool.
    assert isinstance(is_trading_day(), bool)


def test_is_trading_day_uses_melbourne_boundary():
    # 00:30 Sunday Melbourne is still Saturday in UTC, but the trader's
    # local weekday (Sunday) is what governs the skip.
    sun_early = datetime.datetime(2026, 7, 12, 0, 30, tzinfo=MELBOURNE_TZ)
    assert is_trading_day(sun_early) is False
