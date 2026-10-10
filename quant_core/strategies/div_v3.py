"""V3：缩量回踩。"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .base import BaseStrategy, RunContext
from . import div_common as dc
from ..common.models import Candidate, TradePlan


class V3Strategy(BaseStrategy):
    strategy = "V3"

    def exits(self, ctx: RunContext) -> List[TradePlan]:
        return dc.standard_exits(ctx, ctx.cfg["trade"])

    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        codes = ctx.hub.universe(cfg["pool"]["sources"], ctx.date)
        market, panel, metrics, scored, dropped = dc.evaluate_universe(
            ctx, codes, cfg["pool"], cfg["scoring"]["weights"])
        for code, why in dropped[:10]:
            ctx.anomalies.append(f"V3 剔除 {code}: {why}")

        rule_cfg = cfg["candidate"]

        def rule(m: dc.StockMetrics) -> bool:
            if m.vol_ratio > rule_cfg["volume_shrink_ratio_max"]:
                return False
            if m.ma20 is None:
                return False
            if abs(m.close / m.ma20 - 1) > rule_cfg["price_near_ma_band"]:
                return False
            if m.ma5 is None or abs(m.ma5 / m.ma20 - 1) >= rule_cfg["ma5_ma20_dev_max"]:
                return False
            return True

        candidates = dc.filter_buy_candidates(ctx, cfg, scored, rule)
        plans: List[TradePlan] = []
        slots = cfg["trade"]["max_positions"] - ctx.ledger.position_count()
        for cand in candidates:
            if slots <= 0:
                break
            ok, why = dc.buy_allowed(ctx, cfg, cand.code, cand.score)
            if not ok:
                continue
            m = metrics[cand.code]
            cand.reasons.append(
                f"缩量(5日/20日量比{m.vol_ratio:.2f})，距MA20 {(m.close/m.ma20-1)*100:+.1f}%")
            plan = dc.create_buy_plan(ctx, m, cand, "V3 缩量回踩", tag="V3")
            if dc.add_plan_once(ctx, plan, plans):
                slots -= 1
        return candidates[:10], plans, "ok"
