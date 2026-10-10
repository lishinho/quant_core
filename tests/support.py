"""测试共享支撑：合成 K 线、内存 FakeHub、临时配置上下文。"""
from __future__ import annotations

import os
import tempfile
import datetime as dt
from typing import Dict, List, Optional, Sequence, Tuple

from quant_core.common import calendar
from quant_core.common.artifacts import ArtifactStore
from quant_core.common.config import ConfigBundle, load_system_config, load_lane_config
from quant_core.common.models import Bar
from quant_core.data.base import DividendEvent, LimitUpInfo, LhbRecord
from quant_core.trading.engine import TradingEngine
from quant_core.trading.ledger import Ledger
from quant_core.strategies.base import RunContext


def make_bars(end_date: str, closes: Sequence[float],
              volumes: Optional[Sequence[float]] = None,
              opens: Optional[Sequence[float]] = None,
              amounts: Optional[Sequence[float]] = None) -> List[Bar]:
    """以 end_date 为最后一个交易日，按 closes 构造日 K，pct_chg 自动计算。"""
    days = calendar.trading_days(end_date, len(closes))
    vols = list(volumes) if volumes is not None else [1_000_000.0] * len(closes)
    opns = list(opens) if opens is not None else list(closes)
    amts = list(amounts) if amounts is not None else [v * c for v, c in zip(vols, closes)]
    bars: List[Bar] = []
    prev: Optional[float] = None
    for d, c, o, h_vol, amt in zip(days, closes, opns, vols, amts):
        pct = 0.0 if prev is None else c / prev - 1.0
        bars.append(Bar(date=d.strftime("%Y-%m-%d"), open=o, high=max(o, c) * 1.001,
                        low=min(o, c) * 0.999, close=c, volume=h_vol,
                        amount=amt, pct_chg=pct))
        prev = c
    return bars


class FakeHub:
    """仅实现策略调用到的 DataHub 接口的内存桩。"""

    def __init__(self):
        self.name = "fake"
        self.provider = type("P", (), {"name": "fake"})()
        self.stats = {"calls": 0, "cache_hits": 0, "degraded": [], "errors": []}
        self._bars: Dict[str, List[Bar]] = {}
        self._divs: Dict[str, List[DividendEvent]] = {}
        self._names: Dict[str, str] = {}
        self.zt: List[LimitUpInfo] = []
        self.zb: List[LimitUpInfo] = []
        self.ld: List[LimitUpInfo] = []
        self.lhb_records: List[LhbRecord] = []

    def set_bars(self, code: str, bars: List[Bar]) -> None:
        self._bars[code] = bars

    def set_dividends(self, code: str, events: List[DividendEvent]) -> None:
        self._divs[code] = events

    def set_names(self, names: Dict[str, str]) -> None:
        self._names = dict(names)

    def bars(self, code: str, start: str, end: str, adjust: str = "qfq") -> List[Bar]:
        return [b for b in self._bars.get(code, []) if start <= b.date <= end]

    def index_bars(self, code: str, start: str, end: str) -> List[Bar]:
        return self._bars.get(code, [])

    def dividends(self, code: str, date_tag: str) -> List[DividendEvent]:
        return self._divs.get(code, [])

    def valuation(self, code: str, indicator: str, date_tag: str):
        return []

    def universe(self, sources, date_tag, fixed=None):
        out = dict(self._names)
        for code in fixed or []:
            out.setdefault(code, self._names.get(code, code))
        return out

    def limit_up_pool(self, date: str) -> List[LimitUpInfo]:
        return self.zt

    def broken_board_pool(self, date: str) -> List[LimitUpInfo]:
        return self.zb

    def limit_down_pool(self, date: str) -> List[LimitUpInfo]:
        return self.ld

    def lhb(self, start: str, end: str) -> List[LhbRecord]:
        return self.lhb_records

    def market_snapshot(self, date_tag: str):
        return {}

    def prefetch_snapshot(self, codes, date_tag: str):
        return {}


def limit_up(code: str, name: str, *, streak: int = 1, seal: str = "0930",
             amount: float = 5e8, seal_amount: float = 1e8, circ_mv: float = 8e9,
             broken: int = 0, themes: Sequence[str] = ("AI",),
             close: float = 10.0, date: str = "2026-10-09") -> LimitUpInfo:
    return LimitUpInfo(
        code=code, name=name, date=date, close=close, pct_chg=0.1,
        amount=amount, turnover_rate=5.0, circ_mv=circ_mv, seal_amount=seal_amount,
        first_seal_time=seal, last_seal_time=seal, broken_times=broken,
        streak_days=streak, themes=list(themes))


def lhb_record(code: str, net_buy: float, date: str = "2026-10-09") -> LhbRecord:
    return LhbRecord(code=code, name=code, date=date, net_buy=net_buy,
                     buy_amount=max(net_buy, 0.0) + 1e7,
                     sell_amount=max(-net_buy, 0.0) + 1e7,
                     interpretation="机构买入" if net_buy > 0 else "机构卖出")


class TempEnv:
    """创建使用临时 state/artifacts/models 目录的 lane 运行上下文。"""

    def __init__(self, lane: str, date: str = "2026-10-09",
                 next_date: str = "2026-10-12", cash: float = 1_000_000.0):
        self.tmp = tempfile.mkdtemp(prefix=f"qc_test_{lane}_")
        self.system = load_system_config()
        for k in ("state", "artifacts", "models", "reports", "feishu_local"):
            self.system["paths"][k] = os.path.join(self.tmp, k)
        os.makedirs(self.system["paths"]["state"], exist_ok=True)
        self.bundle: ConfigBundle = load_lane_config(self.system, lane)
        self.hub = FakeHub()
        self.engine = TradingEngine()
        self.ledger = Ledger(self.bundle.paths["state"], lane, cash)
        self.artifacts = ArtifactStore(self.bundle.paths["artifacts"], lane, date)
        self.ctx = RunContext(bundle=self.bundle, date=date, next_date=next_date,
                              hub=self.hub, engine=self.engine, ledger=self.ledger,
                              artifacts=self.artifacts)

    def reload_ledger(self, cash: float = 1_000_000.0) -> Ledger:
        return Ledger(self.bundle.paths["state"], self.bundle.lane_name, cash)
