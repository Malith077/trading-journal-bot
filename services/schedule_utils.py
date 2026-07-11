"""Helpers for scheduling bot tasks around the trading week.

Centralises the "only run on weekdays" rule so every scheduled trigger
(morning prep, daily bias, and future Tradovate polling) stays consistent.
"""
import datetime
from zoneinfo import ZoneInfo

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")


def is_trading_day(now: datetime.datetime | None = None) -> bool:
    """Return True on Mon–Fri (Melbourne time), False on Sat/Sun.

    Used to gate scheduled bot triggers so they stay quiet on weekends.

    Note: Melbourne weekday is used as the reference for the trader's routine.
    CME futures technically reopen Sunday evening US time (Monday morning
    Melbourne), so Mon–Fri local already covers every active trading session.
    """
    if now is None:
        now = datetime.datetime.now(MELBOURNE_TZ)
    return now.weekday() < 5  # Mon=0 … Fri=4; Sat=5, Sun=6
