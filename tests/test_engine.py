"""交易引擎单测：整手/费用、T+1、资金不足减仓、幂等成交、网格分层。"""
import os
import tempfile
import unittest

from quant_core.common.models import Bar, Side
from quant_core.trading.engine import TradingEngine
from quant_core.trading.ledger import Ledger


def bar(date: str = "2026-10-12", open_: float = 10.0, close: float = 10.0,
        pct: float = 0.0) -> Bar:
    return Bar(date=date, open=open_, high=max(open_, close),
               low=min(open_, close), close=close, volume=1, pct_chg=pct)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qc_eng_")
        self.ledger = Ledger(self.dir, "test_lane", 1_000_000.0)
        self.engine = TradingEngine(fee_rate=0.0003, min_fee=5.0, lot_size=100)

    def _buy(self, qty: int = 10000, price_hint: float = 10.0,
             tag: str = "", layer=None, date="2026-10-09"):
        return self.engine.make_buy_plan(
            lane="test_lane", code="600000", name="测试", gen_date=date,
            trade_date="2026-10-12", reason="单测", weight=0.0,
            hint_price=price_hint, equity=1_000_000.0, tag=tag, layer=layer)

    def test_plan_round_lot_and_fee(self):
        plan = self._buy()
        self.assertEqual(plan.qty % 100, 0)
        plan.qty = 1000
        self.ledger.add_plan(plan)
        fills = self.engine.execute_pending(self.ledger, "2026-10-12",
                                            {"600000": bar(open_=10.0)})
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].amount, 10000.0)
        self.assertAlmostEqual(fills[0].fee, 5.0)  # 最低佣金
        self.assertAlmostEqual(self.ledger.cash, 1_000_000 - 10005)

    def test_idempotent_execute(self):
        plan = self._buy()
        plan.qty = 1000
        self.ledger.add_plan(plan)
        q = {"600000": bar(open_=10.0)}
        first = self.engine.execute_pending(self.ledger, "2026-10-12", q)
        second = self.engine.execute_pending(self.ledger, "2026-10-12", q)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])  # 计划已 FILLED
        # fills.jsonl 只追加一次
        with open(self.ledger.fills_path, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)
        # 模拟崩溃恢复：新 Ledger 读到 PENDING 但 fills 已存在，不能重复成交
        plan.status = "PENDING"
        self.ledger.save()
        ledger2 = Ledger(self.dir, "test_lane", 1_000_000.0)
        fills = self.engine.execute_pending(ledger2, "2026-10-12", q)
        self.assertEqual(fills, [])
        self.assertEqual(ledger2.get_plan(plan.plan_key).status, "FILLED")

    def test_insufficient_cash_shrinks_then_cancels(self):
        plan = self._buy()
        plan.qty = 100000  # 100 万市值，现金不够
        self.ledger.cash = 500_000.0
        self.ledger.add_plan(plan)
        fills = self.engine.execute_pending(self.ledger, "2026-10-12",
                                            {"600000": bar(open_=10.0)})
        self.assertEqual(len(fills), 1)
        self.assertLessEqual(fills[0].qty, 49900)
        self.assertEqual(fills[0].qty % 100, 0)
        # 完全负担不起：取消
        p2 = self.engine.make_buy_plan(
            lane="test_lane", code="600001", name="测试2", gen_date="2026-10-09",
            trade_date="2026-10-12", reason="单测", weight=0.0,
            hint_price=10.0, equity=1_000_000.0, tag="X")
        p2.qty = 100
        self.ledger.cash = 100.0
        self.ledger.add_plan(p2)
        fills = self.engine.execute_pending(self.ledger, "2026-10-12",
                                            {"600001": bar(open_=10.0)})
        self.assertEqual(fills, [])
        self.assertEqual(p2.status, "CANCELLED")

    def test_t_plus_one_frozen_qty(self):
        plan = self._buy(tag="L1", layer=1)
        plan.qty = 1000
        self.ledger.add_plan(plan)
        self.engine.execute_pending(self.ledger, "2026-10-12",
                                    {"600000": bar(open_=10.0, close=10.2)})
        pos = self.ledger.positions["600000"]
        self.assertEqual(pos.available_qty, 0)  # 买入当日不可卖
        self.engine.mark_to_market(
            self.ledger, "2026-10-12",
            {"600000": bar(open_=10.0, close=10.2)})
        self.assertEqual(pos.available_qty, 1000)  # 盘后解冻
        self.assertEqual(len(pos.layers), 1)
        self.assertEqual(pos.layers[0]["layer"], 1)

    def test_sell_requires_position(self):
        sell = self.engine.make_sell_plan(
            lane="test_lane", code="600000", name="测试", gen_date="2026-10-09",
            trade_date="2026-10-12", reason="无仓卖出", qty=1000, last_price=10.0)
        self.ledger.add_plan(sell)
        fills = self.engine.execute_pending(self.ledger, "2026-10-12",
                                            {"600000": bar(open_=10.0)})
        self.assertEqual(fills, [])
        self.assertEqual(sell.status, "CANCELLED")

    def test_pending_plan_waits_when_suspended(self):
        plan = self._buy()
        plan.qty = 100
        self.ledger.add_plan(plan)
        fills = self.engine.execute_pending(self.ledger, "2026-10-12", {})
        self.assertEqual(fills, [])
        self.assertEqual(plan.status, "PENDING")  # 停牌：次日再试


if __name__ == "__main__":
    unittest.main()
