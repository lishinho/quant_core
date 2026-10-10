"""V8.1：银行高分股超跌均值回归（先质量取前20%，再找跌破布林下轨）。"""
from __future__ import annotations

from typing import List, Tuple

from .base import BaseStrategy, RunContext
from . import div_common as dc
from ..data.universe import SW_BANK_CODE
from ..common.models import Candidate, TradePlan


class V81Strategy(BaseStrategy):
    strategy = "V8_1"

    def exits(self, ctx: RunContext) -> List[TradePlan]:
        return dc.standard_exits(ctx, ctx.cfg["trade"])

    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        pool_cfg = dict(cfg["pool"])
        pool_cfg["min_history_days"] = cfg["candidate"]["min_history_days"]
        codes = ctx.hub.universe([f"sw:{SW_BANK_CODE}"], ctx.date)
        market, panel, metrics, scored, dropped = dc.evaluate_universe(
            ctx, codes, pool_cfg, cfg["scoring"]["weights"])
        for code, why in dropped[:8]:
            ctx.anomalies.append(f"V8.1 剔除 {code}: {why}")

        # 综合评分前 20%
        scored_sorted = sorted(scored, key=lambda x: dc.total_score(x[1], cfg["scoring"]["weights"]),
                               reverse=True)
        top_n = max(1, int(len(scored_sorted) * cfg["candidate"]["quality_top_pct"]))
        top_codes = {m.code for m, _, _ in scored_sorted[:top_n]}

        candidates: List[Candidate] = []
        plans: List[TradePlan] = []
        slots = cfg["trade"]["max_positions"] - ctx.ledger.position_count()
        for m, dims, reasons in scored_sorted:
            if m.code not in top_codes:
                continue
            # 跌破布林下轨
            if not m.boll_lower or m.close >= m.boll_lower:
                continue
            if cfg["candidate"].get("exclude_ex_dividend_day") and m.ex_date_today:
                ctx.anomalies.append(f"V8.1 {m.code} 除权日不建新仓")
                continue
            cand = dc.make_candidate(m, dims, cfg["scoring"]["weights"],
                                     reasons + [f"跌破布林下轨(现价{m.close:.2f}<{m.boll_lower:.2f})"],
                                     ctx.date)
            if cand.score < cfg["trade"]["buy_score_threshold"]:
                continue
            ok, _ = dc.buy_allowed(ctx, cfg, m.code, cand.score)
            if not ok or slots <= 0:
                continue
            candidates.append(cand)
            plan = dc.create_buy_plan(ctx, m, cand, "V8.1 银行高分股超跌", tag="V8_1")
            if dc.add_plan_once(ctx, plan, plans):
                slots -= 1
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:10], plans, "ok"
