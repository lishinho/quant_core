"""数据 Provider 抽象接口与扩展数据结构。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from ..common.models import Bar


@dataclass
class DividendEvent:
    code: str
    announce_date: str
    ex_date: str            # 除权除息日
    cash_per_share: float   # 每股派息（税前，元）
    progress: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LimitUpInfo:
    """涨停池单只股票（东财涨停池口径）。"""

    code: str
    name: str
    date: str
    close: float
    pct_chg: float
    amount: float
    turnover_rate: float
    circ_mv: float
    seal_amount: float          # 封板资金
    first_seal_time: str        # 首次封板时间 HHMM
    last_seal_time: str         # 最后封板时间
    broken_times: int           # 炸板次数
    streak_days: int            # 连板数
    limit_up_count: int = 1     # N 天 M 板中的板数
    themes: List[str] = field(default_factory=list)

    @property
    def weak_seal(self) -> bool:
        return self.broken_times > 0

    @property
    def first_seal_minutes(self) -> int:
        try:
            t = self.first_seal_time.zfill(4)
            return int(t[:2]) * 60 + int(t[2:])
        except (ValueError, TypeError):
            return 240

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LhbRecord:
    code: str
    name: str
    date: str
    net_buy: float          # 龙虎榜净买额（元）
    buy_amount: float
    sell_amount: float
    interpretation: str

    @property
    def sentiment(self) -> str:
        if self.net_buy > 0:
            return "BULL"
        if self.net_buy < 0:
            return "BEAR"
        return "NEUTRAL"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Quote:
    code: str
    name: str
    open: float
    high: float
    low: float
    close: float
    pct_chg: float
    volume: float
    amount: float
    suspended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DataProvider(ABC):
    """所有数据提供方实现的统一接口。

    实现方允许返回空列表/空字典表示“无数据”，由 DataHub 按关键/非关键分级处理。
    """

    name: str = "base"

    @abstractmethod
    def get_bars(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> List[Bar]: ...

    @abstractmethod
    def get_index_bars(self, code: str, start_date: str, end_date: str) -> List[Bar]: ...

    @abstractmethod
    def get_spot(self) -> Dict[str, Quote]: ...

    @abstractmethod
    def get_index_constituents(self, index_code: str) -> List[Tuple[str, str]]: ...

    @abstractmethod
    def get_industry_constituents(self, sw_code: str) -> List[Tuple[str, str]]: ...

    @abstractmethod
    def get_dividends(self, code: str) -> List[DividendEvent]: ...

    @abstractmethod
    def get_valuation(self, code: str, indicator: str = "pe_ttm") -> List[Tuple[str, float]]: ...

    @abstractmethod
    def get_limit_up_pool(self, date: str) -> List[LimitUpInfo]: ...

    @abstractmethod
    def get_broken_board_pool(self, date: str) -> List[LimitUpInfo]: ...

    @abstractmethod
    def get_limit_down_pool(self, date: str) -> List[LimitUpInfo]: ...

    @abstractmethod
    def get_lhb(self, start_date: str, end_date: str) -> List[LhbRecord]: ...

    def get_market_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """全市场当日快照 {code: {price, pe_ttm, pb, total_mv, circ_mv, ...}}。

        非关键增强数据：默认返回空字典，由上层退回逐股估值接口/中性分。
        """
        return {}

    def get_quotes(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """按股票池批量获取当日快照（非关键增强数据，默认空字典）。"""
        return {}

    def health_check(self) -> bool:
        return True
