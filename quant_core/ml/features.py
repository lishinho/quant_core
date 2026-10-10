"""量价特征工程：指标序列一次性对齐计算，训练/推理共用同一套定义。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from ..analysis import indicators as ind
from ..common.models import Bar


@dataclass
class FeatureRow:
    code: str
    date: str
    x: List[Optional[float]]
    label: Optional[float] = None


def _roll_mean(values: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def _roll_std(values: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        out.append(float(np.std(values[max(0, i - n + 1): i + 1], ddof=0)) if i >= n - 1 else None)
    return out


def _ret_n(closes: List[float], n: int, i: int) -> Optional[float]:
    if i < n or closes[i - n] <= 0:
        return None
    return closes[i] / closes[i - n] - 1.0


def feature_matrix(bars: List[Bar], names: List[str]):
    """返回 {feature_name: 与 bars 等长对齐的序列}。"""
    closes = ind.closes(bars)
    vols = ind.volumes(bars)
    amts = ind.amounts(bars)
    rets = [0.0] + [closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0
                    for i in range(1, len(closes))]
    ma = {n: ind.sma(closes, n) for n in (5, 10, 20, 60)}
    _, bup, blo = ind.bollinger(closes, 20, 2.0)
    rsi = ind.rsi(closes, 14)
    dif, dea, hist = ind.macd(closes)
    volm5, volm20 = _roll_mean(vols, 5), _roll_mean(vols, 20)
    am5, am20, am60 = _roll_mean(amts, 5), _roll_mean(amts, 20), _roll_mean(amts, 60)
    volstd5, volstd20 = _roll_std(rets, 5), _roll_std(rets, 20)
    hi20 = ind.rolling_max(closes, 20)

    def at(i: int) -> Dict[str, Optional[float]]:
        c = closes[i]
        bias = lambda m: (c / m[i] - 1.0) if m[i] else None
        return {
            "ret_1d": _ret_n(closes, 1, i),
            "ret_3d": _ret_n(closes, 3, i),
            "ret_5d": _ret_n(closes, 5, i),
            "ret_10d": _ret_n(closes, 10, i),
            "ret_20d": _ret_n(closes, 20, i),
            "vol_5d": volstd5[i],
            "vol_20d": volstd20[i],
            "vol_ratio_5_20": (volm5[i] / volm20[i] if volm5[i] and volm20[i] else None),
            "turnover_5d": (am5[i] / am60[i] if am5[i] and am60[i] else None),
            "amount_ratio": (am5[i] / am20[i] if am5[i] and am20[i] else None),
            "ma5_bias": bias(ma[5]),
            "ma10_bias": bias(ma[10]),
            "ma20_bias": bias(ma[20]),
            "ma60_bias": bias(ma[60]),
            "dist_boll_lower": (c / blo[i] - 1.0) if blo[i] else None,
            "dist_boll_upper": (c / bup[i] - 1.0) if bup[i] else None,
            "boll_width": ((bup[i] - blo[i]) / ma[20][i])
                          if bup[i] and blo[i] and ma[20][i] else None,
            "rsi_14": (rsi[i] / 100.0) if rsi[i] is not None else None,
            "macd_dif": (dif[i] / c) if dif[i] is not None and c else None,
            "macd_dea": (dea[i] / c) if dea[i] is not None and c else None,
            "macd_hist": (hist[i] / c) if hist[i] is not None and c else None,
            "high_20d_pos": (c / hi20[i]) if hi20[i] else None,
            "volume_trend_5d": (volm5[i] / volm5[i - 5]
                                if volm5[i] and i >= 5 and volm5[i - 5] else None),
            "amount_log": math.log(am5[i]) if am5[i] else None,
        }

    columns: Dict[str, List[Optional[float]]] = {n: [] for n in names}
    for i in range(len(bars)):
        row = at(i)
        for n in names:
            columns[n].append(row.get(n))
    return columns


def latest_vector(bars: List[Bar], names: List[str]) -> List[Optional[float]]:
    columns = feature_matrix(bars, names)
    return [columns[n][-1] for n in names]


def build_rows(bars: List[Bar], names: List[str], horizon: int,
               min_index: int = 60, code: str = "") -> List[FeatureRow]:
    """训练样本：t 时刻特征，t+horizon 收益为标签。"""
    columns = feature_matrix(bars, names)
    closes = ind.closes(bars)
    rows: List[FeatureRow] = []
    for i in range(min_index, len(bars) - horizon):
        x = [columns[n][i] for n in names]
        label = closes[i + horizon] / closes[i] - 1.0 if closes[i] > 0 else None
        rows.append(FeatureRow(code=code, date=bars[i].date, x=x, label=label))
    return rows
