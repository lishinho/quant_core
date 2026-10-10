"""策略运行上下文与基类。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..common.artifacts import ArtifactStore
from ..common.config import ConfigBundle
from ..common.models import Candidate, TradePlan
from ..data.hub import DataHub
from ..trading.engine import TradingEngine
from ..trading.ledger import Ledger


@dataclass
class RunContext:
    bundle: ConfigBundle
    date: str                       # T 日（盘后）
    next_date: str                  # T+1
    hub: DataHub
    engine: TradingEngine
    ledger: Ledger
    artifacts: ArtifactStore
    anomalies: List[str] = field(default_factory=list)

    @property
    def lane(self) -> str:
        return self.bundle.lane_name

    @property
    def cfg(self) -> Dict[str, Any]:
        return self.bundle.lane


class BaseStrategy:
    strategy: str = "BASE"

    def exits(self, ctx: RunContext) -> List[TradePlan]:
        """盘后巡检退出（先生成卖出计划），默认无。"""
        return []

    def signals(self, ctx: RunContext) -> tuple[List[Candidate], List[TradePlan], str]:
        """盘后候选与买入计划。

        返回 (候选, 新计划, 状态)；关键数据不足时状态置为 critical_abort。
        """
        return [], [], "ok"
