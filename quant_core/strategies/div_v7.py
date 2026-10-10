"""V7：固定优质标的 + 三层布林网格。"""
from __future__ import annotations

import datetime as dt
from typing import List, Optional, Tuple

from ..common import calendar
from ..common.models import Candidate, TradePlan
from .base import BaseStrategy, RunContext
from . import div_common as dc


class V7Strategy(BaseStrategy):
    strategy = "V7"

    # ------------------------------------------------------------- 盘后巡检
    def exits(self, ctx: RunContext) -> List[TradePlan]:
        cfg = ctx.cfg
        grid = cfg["grid"]
        ex = cfg["exit"]
        risk = cfg["risk"]
        plans: List[TradePlan] = []
        start = (calendar.to_date(ctx.date) - dt.timedelta(days=200)).strftime("%Y-%m-%d")
        for code in cfg["pool"]["fixed_universe"]:
            pos = ctx.ledger.positions.get(code)
            bars = ctx.hub.bars(code, start, ctx.date)
            if pos is None or not bars or bars[-1].date != ctx.date:
                continue
            bar = bars[-1]
            ratio = bar.close / pos.avg_cost - 1.0
            hold = dc.hold_trading_days(pos.open_date, ctx.date)
            closes = dc.ind.closes(bars)
            dif, dea, hist = dc.ind.macd(closes)
            name = pos.name
            sell: Optional[Tuple[str, str, int]] = None  # (reason, tag, qty)

            # 1) 灾难性/单日大跌/到期：全部退出
            if ratio <= ex["catastrophic_loss"]:
                sell = (f"灾难性亏损 {ratio*100:.1f}%", "STOP_ALL", pos.qty)
            elif bar.pct_chg <= ex["single_day_drop"]:
                sell = (f"单日大跌 {bar.pct_chg*100:.1f}%", "DROP_ALL", pos.qty)
            elif hold >= ex["max_hold_days"]:
                sell = (f"持仓到期({hold}日)", "EXPIRE_ALL", pos.qty)
            # 2) +10% 全部止盈
            elif ratio >= ex["take_profit_all"]:
                sell = (f"全部止盈 +{ratio*100:.1f}%", "TP_ALL", pos.qty)
            # 3) 盈利中 MACD 死叉全部退出
            elif (ex.get("macd_death_cross_in_profit") and ratio > 0
                  and dif[-2] is not None and dea[-2] is not None
                  and dif[-2] >= dea[-2] and dif[-1] < dea[-1]):
                sell = ("盈利状态 MACD 死叉", "MACD_ALL", pos.qty)
            # 4) +5% 卖一半（只执行一次）
            elif ratio >= ex["take_profit_half"] and not pos.meta.get("tp_half_done"):
                half = ctx.engine.round_lot(pos.qty // 2)
                if half >= ctx.engine.lot:
                    sell = (f"半仓止盈 +{ratio*100:.1f}%", "TP_HALF", half)

            if sell:
                reason, tag, qty = sell
                plan = ctx.engine.make_sell_plan(
                    lane=ctx.lane, code=code, name=name, gen_date=ctx.date,
                    trade_date=ctx.next_date, reason=reason, qty=qty,
                    last_price=bar.close, tag=tag)
                if dc.add_plan_once(ctx, plan, plans) and tag == "TP_HALF":
                    pos.meta["tp_half_pending"] = True

            # 5) 网格加仓（回撤期间仍允许）
            self._maybe_add_layer(ctx, code, name, bars, grid, plans)
        return plans

    def _maybe_add_layer(self, ctx, code, name, bars, grid, plans) -> None:
        pos = ctx.ledger.positions.get(code)
        if pos is None or not pos.layers:
            return
        layers = sorted(l["layer"] for l in pos.layers)
        current_layer = max(layers)
        if current_layer >= grid["max_layers"]:
            return
        pending_tags = {p.tag for p in ctx.ledger.pending_plans(ctx.date) if p.code == code}
        first_price = pos.layers[0]["price"]
        close = bars[-1].close
        drop = close / first_price - 1.0
        if current_layer == 1 and drop <= grid["layer2_drop"] and "L2" not in pending_tags:
            self._add_layer_plan(ctx, code, name, close, 2, drop, plans)
        elif current_layer == 2 and drop <= grid["layer3_drop"] and "L3" not in pending_tags:
            self._add_layer_plan(ctx, code, name, close, 3, drop, plans)

    def _add_layer_plan(self, ctx, code, name, price, layer, drop, plans) -> None:
        plan = ctx.engine.make_buy_plan(
            lane=ctx.lane, code=code, name=name, gen_date=ctx.date,
            trade_date=ctx.next_date, reason=f"V7 网格第{layer}层(较首买{drop*100:.1f}%)",
            weight=ctx.cfg["grid"]["per_layer_weight"], hint_price=price,
            equity=ctx.ledger.equity(), layer=layer, tag=f"L{layer}")
        dc.add_plan_once(ctx, plan, plans)

    # ------------------------------------------------------------- 新开仓
    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        plans: List[TradePlan] = []
        candidates: List[Candidate] = []
        peak = max(ctx.ledger.equity_peak, 1.0)
        drawdown = ctx.ledger.equity() / peak - 1.0
        pause_new = drawdown <= -cfg["risk"]["portfolio_drawdown_pause"]
        if pause_new:
            ctx.anomalies.append(f"组合回撤 {drawdown*100:.1f}%，暂停 V7 新开仓（仍允许加仓/退出）")
        start = (calendar.to_date(ctx.date) - dt.timedelta(days=200)).strftime("%Y-%m-%d")
        for code in cfg["pool"]["fixed_universe"]:
            name = ctx.hub.universe([], ctx.date, fixed=[code]).get(code, code)
            if code in ctx.ledger.positions:
                continue
            bars = ctx.hub.bars(code, start, ctx.date)
            if len(bars) < cfg["candidate"]["boll_window"] + 2:
                continue
            closes = dc.ind.closes(bars)
            _, _, lower = dc.ind.bollinger(
                closes, cfg["candidate"]["boll_window"], cfg["candidate"]["boll_k"])
            prev_close, today_close = closes[-2], closes[-1]
            if lower[-2] is None or lower[-1] is None:
                continue
            if pause_new:
                continue
            # 前一日收于下轨之下，当日重新站上
            if prev_close < lower[-2] and today_close > lower[-1]:
                candidate = Candidate(
                    code=code, name=name, date=ctx.date, score=0.0,
                    dim_scores={}, reasons=["前日收于布林下轨下方，当日重新站上"],
                )
                candidates.append(candidate)
                plan = ctx.engine.make_buy_plan(
                    lane=ctx.lane, code=code, name=name, gen_date=ctx.date,
                    trade_date=ctx.next_date, reason="V7 网格第一层建仓",
                    weight=cfg["grid"]["per_layer_weight"], hint_price=today_close,
                    equity=ctx.ledger.equity(), layer=1, tag="L1")
                dc.add_plan_once(ctx, plan, plans)
        return candidates, plans, "ok"
