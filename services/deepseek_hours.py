"""
DeepSeek peak-hour awareness.

DeepSeek bills double during fixed UTC windows on weekdays. Interactive use
(coaching a journal entry as you write it) should just run, but bulk work like
/rebuild_all can wait, and should — so it asks this module when it may proceed.

All functions take timezone-aware UTC datetimes. The windows are UTC and UTC
has no DST, so there is nothing to convert on the Pi's Melbourne clock.
"""
import asyncio
import datetime as dt
from typing import Awaitable, Callable

from config import DEEPSEEK_PEAK_HOURS_UTC

UTC = dt.timezone.utc


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def is_peak(moment: dt.datetime, windows=None) -> bool:
    """True if `moment` falls inside a weekday peak window."""
    windows = DEEPSEEK_PEAK_HOURS_UTC if windows is None else windows
    moment = moment.astimezone(UTC)
    if moment.weekday() >= 5:           # Saturday, Sunday
        return False
    return any(start <= moment.hour < end for start, end in windows)


def next_off_peak(moment: dt.datetime, windows=None) -> dt.datetime:
    """`moment` itself if it is off-peak, otherwise the end of the current window."""
    moment = moment.astimezone(UTC)
    if not is_peak(moment, windows):
        return moment
    t = moment.replace(minute=0, second=0, microsecond=0)
    # Windows are whole hours, so stepping an hour at a time is exact, and it
    # stays correct if the configured windows ever change or abut each other.
    while is_peak(t, windows):
        t += dt.timedelta(hours=1)
    return t


def discord_time(moment: dt.datetime, style: str = "F") -> str:
    """A Discord timestamp tag — renders in each viewer's own timezone."""
    return f"<t:{int(moment.timestamp())}:{style}>"


async def wait_for_off_peak(
    *,
    now: Callable[[], dt.datetime] = utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_pause: Callable[[dt.datetime], Awaitable[None]] | None = None,
    max_chunk: float = 300.0,
) -> bool:
    """
    Return as soon as it is off-peak. Returns True if it had to wait.

    Sleeps in chunks rather than one long sleep, so a suspended or
    clock-corrected machine re-checks instead of oversleeping. `on_pause` is
    called once per pause with the time work is expected to resume.
    """
    paused = False
    while True:
        current = now()
        if not is_peak(current):
            return paused
        resume_at = next_off_peak(current)
        if not paused:
            paused = True
            if on_pause:
                await on_pause(resume_at)
        remaining = (resume_at - current).total_seconds()
        await sleep(max(1.0, min(remaining + 1, max_chunk)))
