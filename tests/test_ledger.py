"""账本单测：计划幂等键、状态持久化、权益与撤销。"""
import tempfile
import unittest

from quant_core.common.keys import plan_key
from quant_core.common.models import Position, Side
from quant_core.trading.engine import TradingEngine
from quant_core.trading.ledger import Ledger


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qc_led_")
        self.ledger = Ledger(self.dir, "lane_x", 500_000.0)
        self.engine = TradingEngine()

    def test_plan_dedup_by_stable_key(self):
        def make(tag):
            return self.engine.make_buy_plan(
                lane="lane_x", code="600000", name="测试", gen_date="2026-10-09",
                trade_date="2026-10-12", reason="r", weight=0.1,
                hint_price=10.0, equity=500_000, tag=tag)
        self.assertTrue(self.ledger.add_plan(make("L1")))
        self.assertFalse(self.ledger.add_plan(make("L1")))  # 同键忽略
        self.assertTrue(self.ledger.add_plan(make("L2")))   # 网格第二层不同键
        self.assertEqual(len(self.ledger.plans), 2)
        self.assertNotEqual(plan_key("lane_x", "600000", "2026-10-09", "BUY", "L1"),
                            plan_key("lane_x", "600000", "2026-10-09", "BUY", "L2"))

    def test_cancel_and_persistence_roundtrip(self):
        plan = self.engine.make_buy_plan(
            lane="lane_x", code="600001", name="测试2", gen_date="2026-10-09",
            trade_date="2026-10-12", reason="r", weight=0.1,
            hint_price=10.0, equity=500_000)
        self.ledger.add_plan(plan)
        self.assertTrue(self.ledger.cancel_plan(plan.plan_key, "测试撤销"))
        self.assertFalse(self.ledger.cancel_plan(plan.plan_key, "不能重复撤销"))
        self.ledger.positions["600002"] = Position(
            code="600002", name="持仓", qty=1000, available_qty=1000,
            avg_cost=8.0, open_date="2026-09-01", last_price=9.0)
        self.ledger.save()

        restored = Ledger(self.dir, "lane_x", 500_000.0)
        self.assertEqual(restored.plans[0].status, "CANCELLED")
        self.assertIn("600002", restored.positions)
        self.assertAlmostEqual(restored.equity(),
                               restored.cash + 9000)

    def test_pending_filter_by_date(self):
        early = self.engine.make_buy_plan(
            lane="lane_x", code="600003", name="a", gen_date="2026-10-08",
            trade_date="2026-10-09", reason="r", weight=0.1,
            hint_price=10.0, equity=500_000)
        later = self.engine.make_buy_plan(
            lane="lane_x", code="600004", name="b", gen_date="2026-10-09",
            trade_date="2026-10-12", reason="r", weight=0.1,
            hint_price=10.0, equity=500_000)
        self.ledger.add_plan(early)
        self.ledger.add_plan(later)
        pending_09 = self.ledger.pending_plans("2026-10-09")
        self.assertEqual([p.code for p in pending_09], ["600003"])
        pending_12 = self.ledger.pending_plans("2026-10-12")
        self.assertEqual(len(pending_12), 2)  # 遗留未成交计划顺延可见


if __name__ == "__main__":
    unittest.main()
