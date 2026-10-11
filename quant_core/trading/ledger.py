"""Lane 级状态账本：现金、持仓、计划状态 + 只追加成交账（JSONL）。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..common.models import Position, TradePlan


class Ledger:
    def __init__(self, state_dir: str, lane: str, initial_capital: float):
        self.lane = lane
        self.dir = os.path.join(state_dir, lane)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.fills_path = os.path.join(self.dir, "fills.jsonl")
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.cash: float = data["cash"]
            self.positions: Dict[str, Position] = {
                k: Position.from_dict(v) for k, v in data.get("positions", {}).items()
            }
            self.plans: List[TradePlan] = [TradePlan.from_dict(p) for p in data.get("plans", [])]
            self.equity_peak: float = data.get("equity_peak", initial_capital)
        else:
            self.cash = initial_capital
            self.positions = {}
            self.plans = []
            self.equity_peak = initial_capital

    # ------------------------------------------------------------- 持久化
    def save(self) -> None:
        payload = {
            "lane": self.lane,
            "cash": round(self.cash, 4),
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "plans": [p.to_dict() for p in self.plans],
            "equity_peak": round(self.equity_peak, 4),
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self.state_path)

    def append_fill(self, fill: Dict[str, Any]) -> None:
        with open(self.fills_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(fill, ensure_ascii=False, default=str) + "\n")

    def fill_exists(self, plan_key: str) -> bool:
        if not os.path.exists(self.fills_path):
            return False
        with open(self.fills_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if f'"{plan_key}"' in line:
                    return True
        return False

    def iter_fills(self) -> List[Dict[str, Any]]:
        """读取全部历史成交（fills.jsonl 只追加），供外部存储全量对账。"""
        if not os.path.exists(self.fills_path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.fills_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    # --------------------------------------------------------------- 计划
    def add_plan(self, plan: TradePlan) -> bool:
        """幂等加入：相同 plan_key 已存在则忽略。返回是否新增。"""
        if any(p.plan_key == plan.plan_key for p in self.plans):
            return False
        self.plans.append(plan)
        return True

    def pending_plans(self, trade_date: Optional[str] = None) -> List[TradePlan]:
        return [
            p for p in self.plans
            if p.status == "PENDING" and (trade_date is None or p.trade_date <= trade_date)
        ]

    def get_plan(self, key: str) -> Optional[TradePlan]:
        return next((p for p in self.plans if p.plan_key == key), None)

    def cancel_plan(self, key: str, reason: str) -> bool:
        plan = self.get_plan(key)
        if plan and plan.status == "PENDING":
            plan.status = "CANCELLED"
            plan.reason = f"{plan.reason} | 撤销: {reason}"
            return True
        return False

    # --------------------------------------------------------------- 持仓
    def equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def position_count(self) -> int:
        return len(self.positions)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity(), 2),
            "equity_peak": round(self.equity_peak, 2),
            "position_count": self.position_count(),
            "positions": [p.to_dict() for p in self.positions.values()],
            "pending_plans": [p.to_dict() for p in self.pending_plans()],
        }
