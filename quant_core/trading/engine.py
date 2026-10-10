"""模拟撮合引擎：T 日计划、T+1 开盘成交、费用、持仓与 T+1 可用。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..common.keys import plan_key
from ..common.models import Bar, Fill, Layer, Position, Side, TradePlan
from .ledger import Ledger


class TradingEngine:
    def __init__(self, fee_rate: float = 0.0003, min_fee: float = 5.0,
                 lot_size: int = 100):
        self.fee_rate = fee_rate
        self.min_fee = min_fee
        self.lot = lot_size

    def _fee(self, amount: float) -> float:
        return max(amount * self.fee_rate, self.min_fee)

    # ------------------------------------------------------------- 计划构造
    def make_buy_plan(self, lane: str, code: str, name: str, gen_date: str,
                      trade_date: str, reason: str, weight: float,
                      hint_price: float, equity: float,
                      score: Optional[float] = None, layer: Optional[int] = None,
                      tag: str = "") -> TradePlan:
        notional = equity * weight
        raw_qty = int(notional / hint_price)
        qty = max(self.lot, (raw_qty // self.lot) * self.lot)
        return TradePlan(
            plan_key=plan_key(lane, code, gen_date, Side.BUY.value, tag or str(layer or "")),
            lane=lane, code=code, name=name, side=Side.BUY.value,
            gen_date=gen_date, trade_date=trade_date, reason=reason,
            qty=qty, price_hint=hint_price, score=score, layer=layer, tag=tag,
        )

    def make_sell_plan(self, lane: str, code: str, name: str, gen_date: str,
                       trade_date: str, reason: str, qty: int,
                       last_price: float, tag: str = "",
                       layer: Optional[int] = None) -> TradePlan:
        return TradePlan(
            plan_key=plan_key(lane, code, gen_date, Side.SELL.value, tag),
            lane=lane, code=code, name=name, side=Side.SELL.value,
            gen_date=gen_date, trade_date=trade_date, reason=reason,
            qty=qty, price_hint=last_price, layer=layer, tag=tag,
        )

    def round_lot(self, qty: int) -> int:
        return (qty // self.lot) * self.lot

    # --------------------------------------------------------------- 撮合
    def execute_pending(self, ledger: Ledger, date: str,
                        quotes: Dict[str, Bar]) -> List[Fill]:
        """在 date 开盘执行到期计划。quotes: code -> 当日 Bar。

        幂等：已成交计划/已存在成交记录直接跳过。
        """
        fills: List[Fill] = []
        for plan in list(ledger.pending_plans(date)):
            if plan.trade_date != date:
                continue  # 正常按日执行；历史遗留由运维显式处理
            if ledger.fill_exists(plan.plan_key):
                plan.status = "FILLED"
                continue
            bar = quotes.get(plan.code)
            if bar is None:
                continue  # 停牌或无行情：保留 PENDING，次日再试
            price = bar.open
            if price <= 0:
                continue
            if plan.side == Side.BUY.value:
                fill = self._fill_buy(ledger, plan, date, price)
            else:
                fill = self._fill_sell(ledger, plan, date, price)
            if fill:
                plan.status = "FILLED"
                plan.fill = fill.to_dict()
                ledger.append_fill(fill.to_dict())
                fills.append(fill)
        return fills

    def _fill_buy(self, ledger: Ledger, plan: TradePlan, date: str,
                  price: float) -> Optional[Fill]:
        qty = plan.qty
        amount = qty * price
        fee = self._fee(amount)
        # 资金不足：减仓到可负担的最大整手
        while qty >= self.lot and amount + fee > ledger.cash:
            qty -= self.lot
            amount = qty * price
            fee = self._fee(amount)
        if qty < self.lot:
            plan.status = "CANCELLED"
            plan.reason += " | 资金不足取消"
            return None
        pos = ledger.positions.get(plan.code)
        if pos is None:
            pos = Position(
                code=plan.code, name=plan.name, qty=0, available_qty=0,
                avg_cost=0.0, open_date=date, last_price=price, high_since_buy=price,
            )
            ledger.positions[plan.code] = pos
        total_cost = pos.avg_cost * pos.qty + amount
        pos.qty += qty
        pos.avg_cost = total_cost / pos.qty
        pos.last_price = price
        pos.high_since_buy = max(pos.high_since_buy, price)
        if plan.layer is not None:
            pos.layers.append(Layer(layer=plan.layer, buy_date=date,
                                    price=price, qty=qty).to_dict())
            pos.meta["grid"] = True
        ledger.cash -= amount + fee
        return Fill(plan.plan_key, date, plan.code, Side.BUY.value, price, qty, amount, fee)

    def _fill_sell(self, ledger: Ledger, plan: TradePlan, date: str,
                   price: float) -> Optional[Fill]:
        pos = ledger.positions.get(plan.code)
        if pos is None or pos.qty <= 0:
            plan.status = "CANCELLED"
            plan.reason += " | 无持仓取消"
            return None
        qty = min(plan.qty, pos.qty)
        qty = self.round_lot(qty) if qty >= self.lot else qty
        if qty <= 0:
            plan.status = "CANCELLED"
            plan.reason += " | 数量不足一手取消"
            return None
        amount = qty * price
        fee = self._fee(amount)
        pos.qty -= qty
        pos.available_qty = min(pos.available_qty, pos.qty)
        ledger.cash += amount - fee
        if pos.qty <= 0:
            ledger.positions.pop(plan.code, None)
        return Fill(plan.plan_key, date, plan.code, Side.SELL.value, price, qty, amount, fee)

    # ------------------------------------------------------------- 日终更新
    def mark_to_market(self, ledger: Ledger, date: str,
                       bars_map: Dict[str, Bar]) -> List[Dict[str, Any]]:
        """用当日收盘更新持仓行情、最高价并解除 T+1 冻结。"""
        updates = []
        for code, pos in list(ledger.positions.items()):
            bar = bars_map.get(code)
            if bar is None:
                updates.append({"code": code, "status": "no_quote"})
                continue
            pos.last_price = bar.close
            pos.high_since_buy = max(pos.high_since_buy, bar.high)
            pos.available_qty = pos.qty  # 开盘买入经盘后已可次日卖出
            pos.meta["last_update"] = date
            updates.append({
                "code": code, "close": bar.close,
                "return_ratio": round(pos.return_ratio, 5),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
            })
        ledger.equity_peak = max(ledger.equity_peak, ledger.equity())
        return updates
