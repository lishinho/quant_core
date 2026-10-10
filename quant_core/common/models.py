"""核心领域模型：K线、候选、交易计划、成交、持仓、快照。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PlanStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class MarketRegime(str, Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    WEAK_BEAR = "WEAK_BEAR"
    STRONG_BEAR = "STRONG_BEAR"


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float          # 股/份
    amount: float = 0.0    # 成交额（元）
    pct_chg: float = 0.0   # 当日涨跌幅（小数）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Bar":
        return Bar(**d)


@dataclass
class Candidate:
    """评分后的候选标的。"""

    code: str
    name: str
    date: str
    score: float
    dim_scores: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Candidate":
        return Candidate(**d)


@dataclass
class Fill:
    plan_key: str
    date: str
    code: str
    side: str
    price: float
    qty: int
    amount: float
    fee: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Fill":
        return Fill(**d)


@dataclass
class TradePlan:
    """T 日盘后生成、下一交易日开盘模拟成交的计划。"""

    plan_key: str
    lane: str
    code: str
    name: str
    side: str
    gen_date: str
    trade_date: str
    reason: str
    qty: int = 0
    price_hint: float = 0.0
    score: Optional[float] = None
    layer: Optional[int] = None
    tag: str = ""
    status: str = PlanStatus.PENDING.value
    fill: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TradePlan":
        return TradePlan(**d)


@dataclass
class Layer:
    """V7 网格分层持仓记录。"""

    layer: int
    buy_date: str
    price: float
    qty: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Layer":
        return Layer(**d)


@dataclass
class Position:
    code: str
    name: str
    qty: int
    available_qty: int
    avg_cost: float
    open_date: str
    last_price: float = 0.0
    high_since_buy: float = 0.0
    layers: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def cost_amount(self) -> float:
        return self.avg_cost * self.qty

    @property
    def market_value(self) -> float:
        return self.last_price * self.qty

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_cost) * self.qty

    @property
    def return_ratio(self) -> float:
        if self.avg_cost <= 0:
            return 0.0
        return self.last_price / self.avg_cost - 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Position":
        return Position(**d)
