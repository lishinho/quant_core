"""DataHub 快照预热与降级的离线单测。"""
import os
import shutil
import tempfile
import unittest

from quant_core.common.cache import DiskCache
from quant_core.data.hub import DataHub


class _StubProvider:
    name = "stub"

    def __init__(self):
        self.batch_calls = 0
        self.full_calls = 0

    def get_quotes(self, codes):
        self.batch_calls += 1
        # 只认识 600000
        return {"600000": {"code": "600000", "pb": 1.2, "pe_ttm": 8.8,
                           "source": "tencent_qt"}} if "600000" in codes else {}

    def get_market_snapshot(self):
        self.full_calls += 1
        return {}


class TestHubSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qc_hub_snap_")
        self.cache = DiskCache(os.path.join(self.tmp, "cache"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefetch_merge_and_disk_cache(self):
        p = _StubProvider()
        hub = DataHub(p, self.cache, retries=1, timeout=5)
        snap = hub.prefetch_snapshot(["600000", "600001"], "2026-10-09")
        self.assertIn("600000", snap)
        self.assertNotIn("600001", snap)
        self.assertEqual(p.batch_calls, 1)
        self.assertEqual(p.full_calls, 0)  # 预热不触发全市场 clist
        # compute_metrics 的读取入口直接可见
        self.assertEqual(hub.market_snapshot("2026-10-09")["600000"]["pb"], 1.2)

        # 新 Hub 实例复用磁盘缓存：已覆盖代码不再请求
        hub2 = DataHub(_StubProvider(), self.cache, retries=1, timeout=5)
        snap2 = hub2.prefetch_snapshot(["600000"], "2026-10-09")
        self.assertEqual(snap2["600000"]["pe_ttm"], 8.8)
        self.assertEqual(hub2.provider.batch_calls, 0)

    def test_incremental_merge(self):
        p = _StubProvider()
        hub = DataHub(p, self.cache, retries=1, timeout=5)
        hub.prefetch_snapshot(["600000"], "2026-10-09")
        # 第二次预热只请求新增代码；旧数据保留
        snap = hub.prefetch_snapshot(["600000"], "2026-10-09")
        self.assertEqual(p.batch_calls, 1)
        self.assertIn("600000", snap)


if __name__ == "__main__":
    unittest.main()
