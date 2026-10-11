"""外部 sink 同步与 MySQL 字段映射单测：
- runner 按账本全量镜像 plans/fills（历史存量计划也会补写）
- MysqlSink._split 字段别名与类型（price_hint→price、date→trade_date、status）
"""
import os
import tempfile
import unittest
from typing import Any, Dict, List

from quant_core.common.cache import DiskCache
from quant_core.common.config import load_system_config, load_lane_config
from quant_core.data.hub import DataHub
from quant_core.data.mock_provider import MockProvider
from quant_core.integrations.feishu_base import BaseSink
from quant_core.integrations.mysql_sink import MysqlSink
from quant_core.runner import run_lane
from quant_core.trading.engine import TradingEngine
from quant_core.trading.ledger import Ledger


class RecordingSink(BaseSink):
    def __init__(self):
        self.rows: List[Dict[str, Any]] = []

    def upsert(self, table: str, key_field: str, key: str,
               fields: Dict[str, Any]) -> bool:
        self.rows.append({"table": table, "key_field": key_field,
                          "key": key, "fields": fields})
        return True

    def keys(self, table: str) -> set:
        return {r["key"] for r in self.rows if r["table"] == table}


class TestSinkMirror(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qc_mirror_")
        self.system = load_system_config()
        for k in ("state", "artifacts", "cache", "models", "reports", "feishu_local"):
            self.system["paths"][k] = os.path.join(self.tmp, k)
        self.hub = DataHub(MockProvider(), DiskCache(self.system["paths"]["cache"]),
                           retries=2, timeout=10)
        self.engine = TradingEngine()

    def test_ledger_plans_and_fills_fully_mirrored(self):
        # 预置一条"历史遗留" PENDING 计划与一笔历史成交（模拟早于建库产生的数据）
        ledger = Ledger(self.system["paths"]["state"], "lhb_v3x", 1_000_000.0)
        old_plan = self.engine.make_buy_plan(
            lane="lhb_v3x", code="600999", name="招商证券",
            gen_date="2026-09-22", trade_date="2026-09-23",
            reason="历史计划", weight=0.1, hint_price=12.5,
            equity=1_000_000.0, score=80.0, tag="LHB")
        ledger.add_plan(old_plan)
        ledger.save()
        ledger.append_fill({
            "plan_key": "fill-hist-001", "date": "2026-09-22", "code": "600999",
            "side": "SELL", "price": 13.2, "qty": 1000,
            "amount": 13200.0, "fee": 5.0,
        })

        sink = RecordingSink()
        bundle = load_lane_config(self.system, "lhb_v3x")
        run_lane(bundle, "2026-09-23", self.hub, self.engine,
                 force=True, sinks={"main": sink})

        # 历史 PENDING 计划即使本次不是新计划，也被补写到 plans
        self.assertIn(old_plan.plan_key, sink.keys("plans"))
        plan_row = next(r for r in sink.rows
                        if r["table"] == "plans" and r["key"] == old_plan.plan_key)
        self.assertEqual(plan_row["fields"]["status"], "PENDING")
        # 历史成交补写到 fills
        self.assertIn("fill-hist-001", sink.keys("fills"))
        fill_row = next(r for r in sink.rows if r["key"] == "fill-hist-001")
        self.assertEqual(fill_row["fields"]["lane"], "lhb_v3x")
        # summary 也同步
        self.assertEqual(len(sink.keys("summary")), 1)


class TestMysqlSplit(unittest.TestCase):
    def setUp(self):
        # 不建立真实连接，仅测试纯字段拆分逻辑
        self.sink = MysqlSink.__new__(MysqlSink)

    def test_plan_alias_and_status(self):
        row = self.sink._split("plans", {
            "lane": "lhb_v3x", "code": "600825", "name": "新华传媒",
            "side": "BUY", "trade_date": "2026-10-12", "gen_date": "2026-10-09",
            "qty": "14300", "price_hint": 10.25, "tag": "LHB",
            "status": "PENDING", "score": 88.11,
        })
        self.assertEqual(row["price"], 10.25)      # price_hint -> price
        self.assertEqual(row["qty"], 14300)
        self.assertEqual(row["status"], "PENDING")
        self.assertNotIn("price_hint", row)
        import json
        self.assertIn("score", json.loads(row["fields"]))  # 非列字段进 JSON

    def test_fill_date_alias(self):
        row = self.sink._split("fills", {
            "lane": "lhb_v3x", "plan_key": "abc", "date": "2026-10-12",
            "code": "600825", "side": "BUY", "qty": 1000,
            "price": 10.5, "amount": 10500.0, "fee": 5.0,
        })
        self.assertEqual(row["trade_date"], "2026-10-12")
        self.assertNotIn("date", row)

    def test_empty_date_becomes_null(self):
        row = self.sink._split("fills", {"lane": "x", "date": "", "code": "1"})
        self.assertIsNone(row["trade_date"])

    def test_json_string_embedded_native(self):
        # runner 预序列化的 JSON 字符串落 fields 时应还原为原生 JSON
        row = self.sink._split("summary", {
            "lane": "lhb_v3x", "date": "2026-10-09",
            "run_meta": '{"provider": "akshare", "calls": 12}',
        })
        import json
        self.assertEqual(json.loads(row["fields"])["run_meta"],
                         {"provider": "akshare", "calls": 12})


if __name__ == "__main__":
    unittest.main()
