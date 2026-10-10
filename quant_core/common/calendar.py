"""交易日历工具。

默认按工作日 + 固定节假日表近似 A 股交易日；数据 Provider 可给出权威日历。
"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

# 简化的固定休市日（春节/国庆等），真实环境由行情源日历覆盖
_HOLIDAYS = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-04-06", "2026-05-01", "2026-06-19", "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    "2026-10-08",
}


def to_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    return dt.datetime.strptime(str(value), "%Y-%m-%d").date()


def dstr(value) -> str:
    return to_date(value).strftime("%Y-%m-%d")


def is_trading_day(value) -> bool:
    day = to_date(value)
    return day.weekday() < 5 and dstr(day) not in _HOLIDAYS


def next_trading_day(value, n: int = 1) -> dt.date:
    day = to_date(value)
    step = 1 if n >= 0 else -1
    remain = abs(n)
    while remain > 0:
        day += dt.timedelta(days=step)
        if is_trading_day(day):
            remain -= 1
    return day


def prev_trading_day(value, n: int = 1) -> dt.date:
    return next_trading_day(value, -n)


def latest_trading_day_on_or_before(value) -> dt.date:
    day = to_date(value)
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


def trading_days(end, n: int) -> List[dt.date]:
    """返回截至 end（含）的最近 n 个交易日，按时间升序。"""
    day = latest_trading_day_on_or_before(end)
    days: List[dt.date] = []
    while len(days) < n:
        days.append(day)
        day = prev_trading_day(day)
    return list(reversed(days))


def trading_day_index(days: List[str], target: str) -> Optional[int]:
    try:
        return days.index(dstr(target))
    except ValueError:
        return None
