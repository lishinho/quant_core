"""技术指标：全部返回与输入等长、按位置对齐的序列，数据不足处为 None。"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

from ..common.models import Bar

Numeric = Optional[float]


def closes(bars: Sequence[Bar]) -> List[float]:
    return [b.close for b in bars]


def volumes(bars: Sequence[Bar]) -> List[float]:
    return [b.volume for b in bars]


def amounts(bars: Sequence[Bar]) -> List[float]:
    return [b.amount for b in bars]


def sma(values: Sequence[float], n: int) -> List[Numeric]:
    out: List[Numeric] = []
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def ema(values: Sequence[float], n: int) -> List[Numeric]:
    out: List[Numeric] = []
    alpha = 2.0 / (n + 1.0)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if i == 0:
            prev = v
            out.append(None if n > 1 else v)
            continue
        prev = alpha * v + (1 - alpha) * prev
        out.append(prev if i >= n - 1 else None)
    return out


def rolling_std(values: Sequence[float], n: int) -> List[Numeric]:
    out: List[Numeric] = []
    for i in range(len(values)):
        if i < n - 1:
            out.append(None)
            continue
        window = values[i - n + 1: i + 1]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        out.append(math.sqrt(var))
    return out


def bollinger(values: Sequence[float], n: int = 20, k: float = 2.0):
    """返回 (mid, upper, lower) 三条对齐序列。"""
    mid = sma(values, n)
    std = rolling_std(values, n)
    upper: List[Numeric] = []
    lower: List[Numeric] = []
    for m, s in zip(mid, std):
        if m is None or s is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(m + k * s)
            lower.append(m - k * s)
    return mid, upper, lower


def rsi(values: Sequence[float], n: int = 14) -> List[Numeric]:
    """Wilder RSI。"""
    out: List[Numeric] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = sum(max(values[i] - values[i - 1], 0.0) for i in range(1, n + 1))
    losses = sum(max(values[i - 1] - values[i], 0.0) for i in range(1, n + 1))
    avg_gain = gains / n
    avg_loss = losses / n
    out[n] = 100 - 100 / (1 + (avg_gain / avg_loss if avg_loss > 0 else 1e9))
    for i in range(n + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(change, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-change, 0.0)) / n
        out[i] = 100 - 100 / (1 + (avg_gain / avg_loss if avg_loss > 0 else 1e9))
    return out


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist)。"""
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    dif: List[float] = []
    for f, s in zip(ema_fast, ema_slow):
        dif.append(None if f is None or s is None else f - s)
    valid_start = next((i for i, v in enumerate(dif) if v is not None), len(dif))
    dea: List[Numeric] = [None] * len(values)
    hist: List[Numeric] = [None] * len(values)
    if valid_start < len(values):
        alpha = 2.0 / (signal + 1.0)
        prev = dif[valid_start]
        dea[valid_start] = prev
        hist[valid_start] = (dif[valid_start] - prev) * 2
        for i in range(valid_start + 1, len(values)):
            prev = alpha * dif[i] + (1 - alpha) * prev
            dea[i] = prev
            hist[i] = (dif[i] - prev) * 2
    return dif, dea, hist


def rolling_max(values: Sequence[float], n: int) -> List[Numeric]:
    out: List[Numeric] = []
    for i in range(len(values)):
        out.append(max(values[max(0, i - n + 1): i + 1]) if i >= 0 else None)
    return out


def rolling_min(values: Sequence[float], n: int) -> List[Numeric]:
    out: List[Numeric] = []
    for i in range(len(values)):
        out.append(min(values[max(0, i - n + 1): i + 1]) if i >= 0 else None)
    return out


def pct_rank(series: Sequence[float], value: float) -> float:
    """value 在历史序列中的百分位 0~1（含等号计数一半）。"""
    if not series:
        return 0.5
    below = sum(1 for x in series if x < value)
    equal = sum(1 for x in series if x == value)
    return (below + 0.5 * equal) / len(series)


def ret(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) <= n or values[-1 - n] == 0:
        return None
    return values[-1] / values[-1 - n] - 1.0


def max_drawdown(values: Sequence[float]) -> float:
    peak = values[0] if values else 1.0
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def cross_above(values_a: Sequence[Numeric], values_b: Sequence[Numeric]) -> bool:
    """最后一个位置 A 是否自下而上穿越 B。"""
    if len(values_a) < 2:
        return False
    a0, a1 = values_a[-2], values_a[-1]
    b0, b1 = values_b[-2], values_b[-1]
    if None in (a0, a1, b0, b1):
        return False
    return a0 <= b0 and a1 > b1


def cross_below(values_a: Sequence[Numeric], values_b: Sequence[Numeric]) -> bool:
    if len(values_a) < 2:
        return False
    a0, a1 = values_a[-2], values_a[-1]
    b0, b1 = values_b[-2], values_b[-1]
    if None in (a0, a1, b0, b1):
        return False
    return a0 >= b0 and a1 < b1
