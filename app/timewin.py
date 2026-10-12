"""Time-window helpers: 'day' | 'week' | 'month' | 'quarter' | 'year'."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WINDOWS = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "quarter": timedelta(days=91),
    "year": timedelta(days=365),
}


@dataclass(frozen=True)
class TimeWindow:
    name: str
    label: str
    start: datetime
    end: datetime
    timedelta: timedelta

    def contains(self, when: datetime) -> bool:
        return self.start <= when < self.end


def resolve_window(name: str, now: datetime | None = None) -> TimeWindow:
    now = now or datetime.now(timezone.utc)
    key = (name or "month").lower().strip()
    if key in ("week",):
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return TimeWindow("week", "this week", start, start + timedelta(weeks=1), WINDOWS["week"])
    if key in ("day", "today"):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return TimeWindow("day", "today", start, start + timedelta(days=1), WINDOWS["day"])
    if key in ("month",):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last = calendar.monthrange(start.year, start.month)[1]
        end = start.replace(day=last) + timedelta(days=1)
        return TimeWindow("month", "this month", start, end, WINDOWS["month"])
    if key in ("quarter",):
        q = (now.month - 1) // 3
        start = now.replace(month=q * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end_month = q * 3 + 4
        if end_month > 12:
            end_month -= 12
        end = start.replace(year=start.year + (1 if end_month < start.month else 0), month=end_month)
        return TimeWindow("quarter", "this quarter", start, end, WINDOWS["quarter"])
    if key in ("year",):
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return TimeWindow("year", "this year", start, start.replace(year=start.year + 1), WINDOWS["year"])
    # unknown window -> treat as a rolling span of that many days if numeric
    td = WINDOWS.get(key, WINDOWS["month"])
    return TimeWindow(key, f"last {td.days} days", now - td, now + timedelta(days=1), td)


def window_bounds(name: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    w = resolve_window(name, now)
    return w.start, w.end