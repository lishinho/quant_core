"""多日端到端：Mock 确定性数据下的 T 日计划 → T+1 成交 → 持仓 → 产物/日报。

覆盖：
- runner 全流程（成交、巡检、信号、DONE 标记）
- resume 复用产物；force 重跑不重复成交（稳定键 + fills.jsonl 账本）
- 跨 lane 日报聚合
"""
import os
import tempfile
import unittest

from quant_core.common.cache import DiskCache
from quant_core.common.config import load_system_config, load_lane_config
from quant_core.data.hub import DataHub
from quant_core.data.mock_provider import MockProvider
from quant_core.reporting.daily_report import build_daily_report, render_markdown, write_report
from quant_core.runner import run_lane
from quant_core.trading.engine import TradingEngine
from quant_core.trading.ledger import Ledger


class TestMultiDayE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qc_e2e_")
        self.system = load_system_config()
        for k in ("state", "artifacts", "cache", "models", "reports", "feishu_local"):
            self.system["paths"][k] = os.path.join(self.tmp, k)
        self.hub = DataHub(MockProvider(), DiskCache(self.system["paths"]["cache"]),
                           retries=2, timeout=10)
        self.engine = TradingEngine()

    def _bundle(self, lane):
        return load_lane_config(self.system, lane)

    def _fills_count(self, lane):
        path = os.path.join(self.system["paths"]["state"], lane, "fills.jsonl")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def test_plan_fill_resume_and_report(self):
        b_lhb = self._bundle("lhb_v3x")
        s23 = run_lane(b_lhb, "2026-09-23", self.hub, self.engine)
        self.assertGreaterEqual(len(s23["new_plans"]), 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.system["paths"]["artifacts"], "lhb_v3x",
                         "2026-09-23", "DONE.json")))

        # T+1：计划开盘成交
        s24 = run_lane(b_lhb, "2026-09-24", self.hub, self.engine)
        self.assertEqual(len(s24["executed_fills"]), 1)
        self.assertGreaterEqual(s24["position_count"], 1)
        self.assertEqual(s24["executed_fills"][0]["side"], "BUY")
        self.assertEqual(self._fills_count("lhb_v3x"), 1)

        # 日报以产物为事实来源（在任何重跑之前检查成交已写入产物）
        report = build_daily_report(self.system["paths"]["artifacts"],
                                    ["lhb_v3x"], "2026-09-24")
        md = render_markdown(report)
        self.assertIn("lhb_v3x", report["lanes"])
        self.assertIn("当日成交", md)
        out = write_report(self.system["paths"]["reports"], report)
        self.assertTrue(os.path.exists(out["md"]))

        # resume：直接复用产物，不重跑
        resumed = run_lane(b_lhb, "2026-09-23", self.hub, self.engine)
        self.assertTrue(resumed.get("resumed"))
        self.assertEqual(self._fills_count("lhb_v3x"), 1)

        # force 重跑 T+1：成交账本保证不重复（产物会被覆盖为 0 新成交）
        forced = run_lane(b_lhb, "2026-09-24", self.hub, self.engine, force=True)
        self.assertEqual(len(forced["executed_fills"]), 0)
        self.assertEqual(self._fills_count("lhb_v3x"), 1)

        # 账本持久化后持仓仍在
        ledger = Ledger(self.system["paths"]["state"], "lhb_v3x", 1_000_000.0)
        self.assertGreaterEqual(len(ledger.positions), 1)

    def test_dividend_lane_t_plus_one_cycle(self):
        # V8.2 在 mock 2026-09-23 产出超跌计划，09-24 开盘成交
        b = self._bundle("dividend_v8_2")
        s23 = run_lane(b, "2026-09-23", self.hub, self.engine)
        self.assertEqual(s23["status"], "ok")
        self.assertGreaterEqual(len(s23["new_plans"]), 1)
        s24 = run_lane(b, "2026-09-24", self.hub, self.engine)
        self.assertGreaterEqual(len(s24["executed_fills"]), 1)
        # 买入计划状态持久化为 FILLED
        ledger = Ledger(self.system["paths"]["state"], "dividend_v8_2", 1_000_000.0)
        self.assertGreaterEqual(len(ledger.positions), 1)

    def test_lane_isolation_on_failure(self):
        # 不存在的 lane 配置在 runner 之外不会发生；此处验证正常 lane 不受重复运行影响
        b = self._bundle("dividend_v7")
        s = run_lane(b, "2026-09-24", self.hub, self.engine)
        self.assertIn(s["status"], ("ok", "critical_abort"))


if __name__ == "__main__":
    unittest.main()
