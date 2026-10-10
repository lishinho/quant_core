"""V9：机器学习（LightGBM/内置 GBDT）+ 规则的混合排序。"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple

from ..analysis import indicators as ind
from ..common import calendar
from ..common.models import Candidate, TradePlan
from ..ml.features import latest_vector
from ..ml.model import ModelManager
from .base import BaseStrategy, RunContext
from . import div_common as dc


class V9Strategy(BaseStrategy):
    strategy = "V9"

    def exits(self, ctx: RunContext) -> List[TradePlan]:
        return dc.standard_exits(ctx, ctx.cfg["trade"])

    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        codes = ctx.hub.universe(cfg["pool"]["sources"], ctx.date)

        # 大盘趋势闸门
        market = dc.build_market_info(ctx, cfg["market_gate"]["index"])
        if not market.above_ma60:
            ctx.anomalies.append(
                f"大盘({cfg['market_gate']['index']}) 低于 MA60，V9 暂停新增仓位")
            return [], [], "market_gate_closed"

        # 模型（过期/特征不兼容自动重训）
        manager = ModelManager(ctx.bundle.paths["models"], ctx.hub)
        model = manager.get_or_train(
            codes, ctx.date, cfg["model"], ctx.bundle.lane_hash)

        _, _, metrics, scored, dropped = dc.evaluate_universe(
            ctx, codes, cfg["pool"], cfg["scoring"]["weights"], market=market)
        for code, why in dropped[:8]:
            ctx.anomalies.append(f"V9 剔除 {code}: {why}")

        feature_names = cfg["model"]["features"]
        horizon = cfg["model"]["horizon_days"]
        window = cfg["model"]["train_window_days"]
        start = (calendar.to_date(ctx.date) - dt.timedelta(days=int(window * 1.55))).strftime("%Y-%m-%d")

        # 横截面模型预测
        preds: Dict[str, float] = {}
        for code in metrics:
            bars = ctx.hub.bars(code, start, ctx.date)
            if len(bars) < 90:
                continue
            vec = latest_vector(bars, feature_names)
            preds[code] = model.predict(vec)
        ranked_pred = sorted(preds.values())

        def pct_rank_of(v: float) -> float:
            return ind.pct_rank(ranked_pred, v)

        weights = cfg["scoring"]["weights"]
        blend = cfg["blend"]
        scored_final: List[Tuple[float, dc.StockMetrics, Dict[str, float], float, float]] = []
        for m, dims, reasons in scored:
            if m.code not in preds:
                continue
            model_pct = pct_rank_of(preds[m.code]) * 100.0
            rule_score = dc.total_score(dims, weights)
            final = blend["model_weight"] * model_pct + blend["rule_weight"] * rule_score
            scored_final.append((final, m, dims, model_pct, rule_score))
        scored_final.sort(key=lambda x: x[0], reverse=True)

        sel = cfg["selection"]
        top_cut = max(1, int(len(scored_final) * sel["top_pct"]))
        candidates: List[Candidate] = []
        plans: List[TradePlan] = []
        daily_picks = 0
        slots = cfg["trade"]["max_positions"] - ctx.ledger.position_count()
        for rank, (final, m, dims, model_pct, rule_score) in enumerate(scored_final):
            if rank >= top_cut or final < sel["final_score_min"]:
                continue
            if daily_picks >= sel["max_daily_picks"] or slots <= 0:
                break
            if m.code in ctx.ledger.positions:
                continue
            cand = Candidate(
                code=m.code, name=m.name, date=ctx.date, score=round(final, 2),
                dim_scores={**dims, "model_pct": round(model_pct, 1),
                            "rule_score": round(rule_score, 1)},
                reasons=[f"模型分位 {model_pct:.0f} + 规则 {rule_score:.0f}，"
                         f"5日相对收益预测 {preds[m.code]*100:+.2f}%"],
                extra={"model_pct": round(model_pct, 2),
                       "rule_score": round(rule_score, 2),
                       "pred_5d": round(preds[m.code], 5),
                       "model_engine": model.artifact["engine"],
                       "model_train_date": model.artifact["train_date"]},
            )
            plan = dc.create_buy_plan(ctx, m, cand, "V9 ML+规则混合排序", tag="V9")
            if dc.add_plan_once(ctx, plan, plans):
                candidates.append(cand)
                daily_picks += 1
                slots -= 1
        return candidates, plans, "ok"
