"""V5：布林带上穿（弱势区域向上修复）。"""
from __future__ import annotations

from typing import List, Tuple

from .base import BaseStrategy, RunContext
from . import div_common as dc
from ..common.models import Candidate, TradePlan


class V5Strategy(BaseStrategy):
    strategy = "V5"

    def exits(self, ctx: RunContext) -> List[TradePlan]:
        return dc.standard_exits(ctx, ctx.cfg["trade"])

    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        codes = ctx.hub.universe(cfg["pool"]["sources"], ctx.date)
        market, panel, metrics, scored, dropped = dc.evaluate_universe(
            ctx, codes, cfg["pool"], cfg["scoring"]["weights"])
        for code, why in dropped[:10]:
            ctx.anomalies.append(f"V5 剔除 {code}: {why}")

        def rule(m: dc.StockMetrics) -> bool:
            if not all([m.boll_lower, m.boll_mid, m.boll_mid_prev, m.boll_lower_prev]):
                return False
            # 此前位于相对低位：近 10 日内曾收盘跌破布林下轨
            recent = m.bars[-11:-1]
            closes = [b.close for b in recent]
            if len(closes) < 6:
                return False
            lower_series = dc.ind.bollinger(dc.ind.closes(m.bars), 20, 2.0)[2]
            was_low = any(c < (lower_series[i] or 1e12)
                          for i, c in zip(range(len(m.bars) - 11, len(m.bars) - 1), closes))
            if not was_low:
                return False
            # 当日上穿布林中轨
            if not (m.bars[-2].close <= m.boll_mid_prev < m.close or
                    (m.bars[-2].close <= (m.boll_mid or 0) and m.close > (m.boll_mid or 0))):
                return False
            cc = cfg["candidate"]
            if cc.get("require_volume_ok") and m.vol_ratio < 0.8:
                return False
            if cc.get("require_trend_ok"):
                hist_up = (m.macd_hist is not None and m.macd_hist_prev is not None
                           and m.macd_hist >= m.macd_hist_prev)
                if not (hist_up or (m.ma60 and m.close > m.ma60)):
                    return False
            return True

        candidates = dc.filter_buy_candidates(ctx, cfg, scored, rule)
        plans: List[TradePlan] = []
        slots = dc.open_slots(ctx, cfg["trade"]["max_positions"])
        for cand in candidates:
            if slots <= 0:
                break
            ok, why = dc.buy_allowed(ctx, cfg, cand.code, cand.score)
            if not ok:
                continue
            m = metrics[cand.code]
            cand.reasons.append("自布林下轨区域修复并上穿中轨")
            plan = dc.create_buy_plan(ctx, m, cand, "V5 布林上穿修复", tag="V5")
            if dc.add_plan_once(ctx, plan, plans):
                slots -= 1
        return candidates[:10], plans, "ok"
