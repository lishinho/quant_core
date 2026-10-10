"""V8.2：银行质量优先均值回归（先找超跌触发，再以质量中位数为门槛排序）。

关键行情/质量评分/除权数据不完整时不生成新信号（critical_abort）。
评分只使用基本面、估值动量、资金、事件四个维度。
"""
from __future__ import annotations

import statistics
from typing import Dict, List, Tuple

from .base import BaseStrategy, RunContext
from . import div_common as dc
from ..data.universe import SW_BANK_CODE
from ..common.models import Candidate, TradePlan
from ..common.retry import CriticalDataMissing


class V82Strategy(BaseStrategy):
    strategy = "V8_2"

    def exits(self, ctx: RunContext) -> List[TradePlan]:
        return dc.standard_exits(ctx, ctx.cfg["trade"])

    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        pool_cfg = dict(cfg["pool"])
        pool_cfg["min_history_days"] = cfg["candidate"]["min_history_days"]
        codes = ctx.hub.universe([f"sw:{SW_BANK_CODE}"], ctx.date)
        market, panel, metrics, scored, dropped = dc.evaluate_universe(
            ctx, codes, pool_cfg, cfg["scoring"]["weights"])

        # 关键数据完整性检查
        coverage = len(metrics) / max(1, len(codes))
        missing_ex = sum(1 for m in metrics.values()
                         if not ctx.hub.dividends(m.code, ctx.date))
        if cfg["candidate"].get("critical_data_required"):
            if coverage < 0.85:
                raise CriticalDataMissing(
                    f"V8.2 银行行情覆盖率 {coverage:.0%}（{len(metrics)}/{len(codes)}）")
            if missing_ex / max(1, len(metrics)) > 0.3:
                raise CriticalDataMissing("V8.2 除权/分红数据大面积缺失")
        for code, why in dropped[:8]:
            ctx.anomalies.append(f"V8.2 剔除 {code}: {why}")

        # 全板块基本面质量分中位数
        fund_by_code: Dict[str, float] = {
            m.code: dims["fundamental"] for m, dims, _ in scored
        }
        median_fund = statistics.median(fund_by_code.values()) if fund_by_code else 0.0

        weights = cfg["scoring"]["weights"]
        # 第一步：超跌触发池
        trigger: List[Tuple[dc.StockMetrics, Dict[str, float], List[str]]] = []
        for m, dims, reasons in scored:
            if not m.boll_lower or m.close >= m.boll_lower:
                continue
            if cfg["candidate"].get("exclude_ex_dividend_day") and m.ex_date_today:
                ctx.anomalies.append(f"V8.2 {m.code} 除权日不建新仓")
                continue
            if dims["fundamental"] < median_fund:
                continue
            trigger.append((m, dims, reasons))

        # 第二步：四维质量排序，取前一半
        trigger.sort(key=lambda x: dc.total_score(x[1], weights), reverse=True)
        keep = max(1, -(-len(trigger) // 2)) if trigger else 0  # ceil 一半
        trigger = trigger[:keep]

        candidates: List[Candidate] = []
        plans: List[TradePlan] = []
        slots = cfg["trade"]["max_positions"] - ctx.ledger.position_count()
        for m, dims, reasons in trigger:
            cand = dc.make_candidate(
                m, dims, weights,
                reasons + [f"超跌({m.close:.2f}<下轨{m.boll_lower:.2f})，"
                           f"基本面{dims['fundamental']:.0f}≥板块中位数{median_fund:.0f}"],
                ctx.date)
            if cand.score < cfg["trade"]["buy_score_threshold"]:
                continue
            ok, _ = dc.buy_allowed(ctx, cfg, m.code, cand.score)
            if not ok or slots <= 0:
                continue
            candidates.append(cand)
            plan = dc.create_buy_plan(ctx, m, cand, "V8.2 质量优先超跌", tag="V8_2")
            if dc.add_plan_once(ctx, plan, plans):
                slots -= 1
        return candidates[:10], plans, "ok"
