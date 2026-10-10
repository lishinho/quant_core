"""确定性 Mock 行情 Provider（离线/测试用）。

设计要点：
- 时间线锚定固定纪元（2024-01-01），同一交易日永远生成同一批数据，
  连续多日运行时历史数据严格一致，仅在尾部追加新 K 线。
- 每只代码按稳定哈希分配"行为画像"，使 V3/V5/V7/V8/LHB 信号都能自然出现：
  revert（均值回归，频繁刺穿布林下轨）、shrink（周期性缩量）、
  cross（弱势→修复周期，穿布林中轨）、hot（周期涨停）。
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Dict, List, Optional, Tuple

from ..common.models import Bar
from .base import DataProvider, DividendEvent, LimitUpInfo, LhbRecord, Quote
from .universe import (BANK_NAMES, DIVIDEND_LOW_VOL, MOCK_HOT_THEMES,
                       MOCK_HS300_EXTRA, MOCK_ZZ500_EXTRA, UNIVERSE_NAMES,
                       SW_BANK_CODE)

EPOCH = dt.date(2024, 1, 1)

# 仅用于涨停/龙虎榜场景的额外热股（Mock 专用）
HOT_EXTRA: List[Tuple[str, str]] = [
    ("605588", "模拟热股A"), ("605589", "模拟热股B"),
    ("605590", "模拟热股C"), ("605591", "模拟热股D"),
    ("605592", "模拟热股E"),
]
_ALL_NAMES = dict(UNIVERSE_NAMES)
_ALL_NAMES.update(dict(HOT_EXTRA))
_HOT_EXTRA_CODES = {c for c, _ in HOT_EXTRA}


def _seed(code: str) -> int:
    h = 2166136261
    for ch in code:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


class _Rng:
    """每只代码独立的确定性伪随机序列（LCG + Box-Muller）。"""

    def __init__(self, seed: int):
        self.state = seed or 1
        self._spare: Optional[float] = None

    def uniform(self) -> float:
        self.state = (self.state * 1664525 + 1013904223) & 0xFFFFFFFF
        return self.state / 4294967296.0

    def normal(self) -> float:
        if self._spare is not None:
            v, self._spare = self._spare, None
            return v
        u1 = max(self.uniform(), 1e-9)
        u2 = self.uniform()
        mag = math.sqrt(-2.0 * math.log(u1))
        self._spare = mag * math.sin(2.0 * math.pi * u2)
        return mag * math.cos(2.0 * math.pi * u2)


def _weekday_index(day: dt.date) -> int:
    """从 EPOCH（含）到 day（含）经过的工作日序号。"""
    days = (day - EPOCH).days
    total = 0
    # 按周批量计算
    full_weeks, rem = divmod(days + 1, 7)
    total += full_weeks * 5
    for k in range(rem):
        if (EPOCH + dt.timedelta(days=full_weeks * 7 + k)).weekday() < 5:
            total += 1
    return total - 1


def _date_by_index(t: int) -> dt.date:
    """第 t 个工作日对应的日期。"""
    full_weeks, rem = divmod(t, 5)
    day = EPOCH + dt.timedelta(days=full_weeks * 7)
    while day.weekday() != 0:
        day -= dt.timedelta(days=1)
    day += dt.timedelta(days=rem)
    return day


def _persona(code: str) -> str:
    s = _seed(code)
    if code.startswith("60559") or code in _HOT_EXTRA_CODES:
        return "hot"
    if code in BANK_NAMES:
        return "revert"
    bucket = s % 10
    if bucket <= 3:
        return "revert"
    if bucket <= 5:
        return "shrink"
    if bucket <= 7:
        return "cross"
    return "hot"


class MockProvider(DataProvider):
    name = "mock"

    def __init__(self, horizon_days: int = 900):
        self.horizon_days = horizon_days
        self._bars: Dict[str, List[Bar]] = {}
        self.today = dt.date.today()
        while self.today.weekday() >= 5:
            self.today -= dt.timedelta(days=1)

    # ------------------------------------------------------------- 序列生成
    def _hot_event(self, code: str, t: int) -> Optional[str]:
        if _persona(code) != "hot":
            return None
        s = _seed(code)
        period = 9 + s % 7
        if (t + s % period) % period != 0:
            return None
        return "broken" if (t + s) % 23 == 0 else "limit"

    def _generate(self, code: str) -> List[Bar]:
        if code in self._bars:
            return self._bars[code]
        s = _seed(code)
        rng = _Rng(s)
        persona = _persona(code)
        base_price = 6.0 + s % 380 / 10.0
        base_vol = 6.0e6 + s % 5.0e7
        lnp = math.log(base_price)
        center0 = lnp
        prev_close = base_price
        max_t = _weekday_index(self.today)
        bars: List[Bar] = []
        for t in range(max_t + 1):
            day = _date_by_index(t)
            z = rng.normal()
            drift = 0.00025
            if persona == "revert":
                center = center0 + 0.00015 * t
                lnp = lnp + 0.72 * (center - lnp) + 0.024 * z
            elif persona == "shrink":
                center = center0 + 0.0002 * t
                lnp = lnp + 0.68 * (center - lnp) + 0.016 * z
            elif persona == "cross":
                phase = t % 34
                d = -0.0045 if phase < 22 else 0.006
                lnp = lnp + d + 0.011 * z
            else:
                lnp = lnp + drift + 0.018 * z

            vol_mult = 1.0 + 0.25 * rng.normal()
            event = self._hot_event(code, t)
            close = math.exp(lnp)
            if event == "limit":
                limit_pct = 0.199 if code.startswith("30") else 0.0997
                close = prev_close * (1.0 + limit_pct)
                lnp = math.log(close)
                vol_mult = 4.0 + rng.uniform()
            elif event == "broken":
                close = prev_close * (1.0 + 0.02 + 0.01 * rng.uniform())
                lnp = math.log(close)
                vol_mult = 3.0
            if persona == "shrink" and (t % 14) in (7, 8, 9, 10, 11):
                vol_mult *= 0.52

            open_ = prev_close * (1.0 + 0.003 * rng.normal())
            if event == "limit":
                open_ = prev_close * 1.01
            hi = max(open_, close) * (1.0 + 0.006 * abs(rng.normal()))
            lo = min(open_, close) * (1.0 - 0.006 * abs(rng.normal()))
            if event == "limit":
                lo = max(lo, prev_close * 1.015)
            volume = max(1.0e5, base_vol * max(0.15, vol_mult))
            amount = volume * close
            pct = close / prev_close - 1.0 if prev_close else 0.0
            bars.append(Bar(
                date=day.strftime("%Y-%m-%d"), open=round(open_, 2),
                high=round(hi, 2), low=round(lo, 2), close=round(close, 2),
                volume=round(volume, 0), amount=round(amount, 0),
                pct_chg=round(pct, 5),
            ))
            prev_close = close
        self._bars[code] = bars
        return bars

    def get_bars(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> List[Bar]:
        if code not in _ALL_NAMES and code not in BANK_NAMES:
            return []
        return [b for b in self._generate(code) if start_date <= b.date <= end_date]

    def get_index_bars(self, code: str, start_date: str, end_date: str) -> List[Bar]:
        s = _seed("IDX" + code)
        rng = _Rng(s)
        lnp = math.log(3500.0)
        prev = 3500.0
        max_t = _weekday_index(self.today)
        bars: List[Bar] = []
        for t in range(max_t + 1):
            # 长周期牛熊：约 80% 时间在 MA60 上方
            wave = 0.0012 * math.sin(t / 55.0)
            lnp += 0.00035 + wave / 10.0 + 0.010 * rng.normal()
            close = math.exp(lnp)
            open_ = prev * (1.0 + 0.001 * rng.normal())
            bars.append(Bar(
                date=_date_by_index(t).strftime("%Y-%m-%d"),
                open=round(open_, 2), high=round(max(open_, close) * 1.003, 2),
                low=round(min(open_, close) * 0.997, 2), close=round(close, 2),
                volume=2.0e9, amount=2.0e9 * close,
                pct_chg=round(close / prev - 1.0, 5),
            ))
            prev = close
        return [b for b in bars if start_date <= b.date <= end_date]

    def get_spot(self) -> Dict[str, Quote]:
        quotes: Dict[str, Quote] = {}
        for code, name in _ALL_NAMES.items():
            bars = self._generate(code)
            b = bars[-1]
            quotes[code] = Quote(
                code=code, name=name, open=b.open, high=b.high, low=b.low,
                close=b.close, pct_chg=b.pct_chg, volume=b.volume, amount=b.amount,
                suspended=False,
            )
        return quotes

    def get_index_constituents(self, index_code: str) -> List[Tuple[str, str]]:
        if index_code == "000300":
            pairs = DIVIDEND_LOW_VOL + MOCK_HS300_EXTRA
        elif index_code == "000905":
            pairs = MOCK_ZZ500_EXTRA
        else:
            return []
        seen: Dict[str, str] = {}
        for code, name in pairs:
            seen.setdefault(code, name)
        return list(seen.items())

    def get_industry_constituents(self, sw_code: str) -> List[Tuple[str, str]]:
        if sw_code == SW_BANK_CODE:
            return list(BANK_NAMES.items())
        return []

    def get_dividends(self, code: str) -> List[DividendEvent]:
        s = _seed(code)
        # 每股年股息按“股价 × 3%~8% 股息率”构造，半年分两次实施
        bars = self._generate(code)
        if not bars:
            return []
        annual_yield = 0.03 + (s % 50) / 1000.0
        cash = round(bars[0].open * annual_yield / 2.0, 3)
        events: List[DividendEvent] = []
        for year in (2024, 2025, 2026):
            for month, day in ((6, 17), (12, 16)):
                d = dt.date(year, month, day)
                while d.weekday() >= 5:
                    d += dt.timedelta(days=1)
                if d <= self.today:
                    events.append(DividendEvent(
                        code=code, announce_date=d.strftime("%Y-%m-%d"),
                        ex_date=d.strftime("%Y-%m-%d"),
                        cash_per_share=round(cash, 3), progress="实施",
                    ))
        return events

    def get_valuation(self, code: str, indicator: str = "pe_ttm") -> List[Tuple[str, float]]:
        s = _seed(code)
        base = 6.0 + s % 9
        out: List[Tuple[str, float]] = []
        for k in range(250):
            d = self.today - dt.timedelta(days=k)
            if d.weekday() >= 5:
                continue
            t = _weekday_index(d)
            v = base + 2.5 * math.sin(t / 35.0 + s % 7)
            if indicator == "pb":
                v = round(v * 0.35, 3)
            elif indicator == "market_cap":
                v = round(v * 5.0e9, 0)
            else:
                v = round(v, 3)
            out.append((d.strftime("%Y-%m-%d"), v))
        return list(reversed(out))

    # --------------------------------------------------------- 涨停/龙虎榜
    def _zt_codes(self, date: str) -> List[str]:
        day = dt.datetime.strptime(date, "%Y-%m-%d").date()
        t = _weekday_index(day)
        codes = [c for c in _ALL_NAMES if self._hot_event(c, t) is not None]
        return codes

    def get_limit_up_pool(self, date: str) -> List[LimitUpInfo]:
        day = dt.datetime.strptime(date, "%Y-%m-%d").date()
        t = _weekday_index(day)
        out: List[LimitUpInfo] = []
        theme = MOCK_HOT_THEMES[t % len(MOCK_HOT_THEMES)]
        for code in self._zt_codes(date):
            if self._hot_event(code, t) != "limit":
                continue
            s = _seed(code)
            bars = self._generate(code)
            bar = bars[t]
            broken = 1 if (s % 6 == 0) else 0
            # 连板数：向前数连续 limit
            streak = 1
            for back in range(1, 6):
                if self._hot_event(code, t - back) == "limit":
                    streak += 1
                else:
                    break
            first_min = 9 * 60 + 25 + (s % (40 - min(streak - 1, 5) * 6))
            out.append(LimitUpInfo(
                code=code, name=_ALL_NAMES[code], date=date,
                close=bar.close, pct_chg=bar.pct_chg, amount=bar.amount,
                turnover_rate=2.0 + s % 12, circ_mv=bar.close * 9.0e8,
                seal_amount=5.0e7 + s % 30 * 1.0e7 * (streak / 2.0 + 0.5),
                first_seal_time=f"{first_min // 60:02d}{first_min % 60:02d}",
                last_seal_time=f"{first_min // 60:02d}{first_min % 60:02d}",
                broken_times=broken, streak_days=streak, limit_up_count=streak,
                themes=[theme] + ([MOCK_HOT_THEMES[(t + 3) % 7]] if s % 4 == 0 else []),
            ))
        out.sort(key=lambda x: (-x.streak_days, x.first_seal_time))
        return out

    def get_broken_board_pool(self, date: str) -> List[LimitUpInfo]:
        day = dt.datetime.strptime(date, "%Y-%m-%d").date()
        t = _weekday_index(day)
        out: List[LimitUpInfo] = []
        for code in self._zt_codes(date):
            if self._hot_event(code, t) != "broken":
                continue
            s = _seed(code)
            bar = self._generate(code)[t]
            out.append(LimitUpInfo(
                code=code, name=_ALL_NAMES[code], date=date,
                close=bar.close, pct_chg=bar.pct_chg, amount=bar.amount,
                turnover_rate=8.0, circ_mv=bar.close * 9.0e8, seal_amount=0.0,
                first_seal_time="1015", last_seal_time="", broken_times=2,
                streak_days=0, themes=[MOCK_HOT_THEMES[t % 7]],
            ))
        return out

    def get_limit_down_pool(self, date: str) -> List[LimitUpInfo]:
        t = _weekday_index(dt.datetime.strptime(date, "%Y-%m-%d").date())
        out: List[LimitUpInfo] = []
        for code in list(_ALL_NAMES)[:40]:
            if (_seed(code) + t) % 97 == 3:
                out.append(LimitUpInfo(
                    code=code, name=_ALL_NAMES[code], date=date,
                    close=5.0, pct_chg=-0.0997, amount=3.0e8,
                    turnover_rate=5.0, circ_mv=4.0e9, seal_amount=-8.0e7,
                    first_seal_time="", last_seal_time="1430", broken_times=0,
                ))
        return out

    def get_lhb(self, start_date: str, end_date: str) -> List[LhbRecord]:
        out: List[LhbRecord] = []
        d0 = dt.datetime.strptime(start_date, "%Y-%m-%d").date()
        d1 = dt.datetime.strptime(end_date, "%Y-%m-%d").date()
        day = d0
        while day <= d1:
            ds = day.strftime("%Y-%m-%d")
            if day.weekday() < 5:
                for info in self.get_limit_up_pool(ds):
                    s = _seed(info.code + ds)
                    if s % 3 == 2:
                        continue  # 未上龙虎榜：不得影响计划
                    sign = 1 if s % 2 == 0 else -1
                    net = sign * (3.0e7 + s % 60 * 1.0e6)
                    out.append(LhbRecord(
                        code=info.code, name=info.name, date=ds,
                        net_buy=net, buy_amount=abs(net) + 8.0e7,
                        sell_amount=8.0e7 if sign > 0 else abs(net) + 8.0e7,
                        interpretation="日涨幅偏离值达7%" if sign > 0 else "资金兑现明显",
                    ))
            day += dt.timedelta(days=1)
        return out
