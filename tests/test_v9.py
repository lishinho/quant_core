"""V9 机器学习混合排序端到端单测（使用确定性 Mock 数据 + 内置 GBDT 训练）。"""
import os
import unittest

from quant_core.common.cache import DiskCache
from quant_core.data.hub import DataHub
from quant_core.data.mock_provider import MockProvider
from quant_core.data.universe import DIVIDEND_LOW_VOL
from quant_core.strategies.div_v9 import V9Strategy
from tests.support import TempEnv


class TestV9MLPipeline(unittest.TestCase):
    def test_gate_blend_and_model_artifact(self):
        env = TempEnv("dividend_v9", date="2026-07-09", next_date="2026-07-10")
        hub = DataHub(MockProvider(),
                      DiskCache(os.path.join(env.tmp, "cache")), retries=2, timeout=10)
        subset = dict(DIVIDEND_LOW_VOL[:16])
        full_universe = hub.universe
        hub.universe = lambda sources, date_tag, fixed=None: dict(subset)
        env.ctx.hub = hub

        cands, plans, status = V9Strategy().signals(env.ctx)
        self.assertEqual(status, "ok")  # 2026-07-09 大盘在 MA60 上方，闸门开启
        self.assertEqual(len(cands), 1)
        self.assertEqual(len(plans), 1)
        cand = cands[0]
        self.assertGreaterEqual(cand.score, 85.0)  # 最终得分门槛
        self.assertIn("model_pct", cand.dim_scores)
        self.assertEqual(cand.extra["model_engine"], "builtin_gbdt")
        # 模型产物落盘并记录训练日期/特征结构
        model_path = os.path.join(env.bundle.paths["models"], "dividend_v9.json")
        self.assertTrue(os.path.exists(model_path))

    def test_market_gate_closed(self):
        env = TempEnv("dividend_v9", date="2026-09-28", next_date="2026-09-29")
        hub = DataHub(MockProvider(),
                      DiskCache(os.path.join(env.tmp, "cache")), retries=2, timeout=10)
        subset = dict(DIVIDEND_LOW_VOL[:16])
        hub.universe = lambda sources, date_tag, fixed=None: dict(subset)
        env.ctx.hub = hub
        cands, plans, status = V9Strategy().signals(env.ctx)
        self.assertEqual(status, "market_gate_closed")
        self.assertEqual(cands, [])
        self.assertEqual(plans, [])
        self.assertTrue(any("MA60" in a for a in env.ctx.anomalies))


if __name__ == "__main__":
    unittest.main()
