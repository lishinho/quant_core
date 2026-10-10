"""V8.2 银行质量优先均值回归单测：
- 关键数据缺失（行情覆盖率不足 / 分红大面积缺失）→ CriticalDataMissing
- 先超跌触发、再以板块质量中位数为门槛、四维排序取前一半
"""
import unittest

from quant_core.common.retry import CriticalDataMissing
from quant_core.data.base import DividendEvent
from quant_core.strategies.div_v8_2 import V82Strategy
from tests.support import TempEnv, make_bars

DATE = "2026-10-09"
BANKS = {"600036": "招商银行", "600015": "华夏银行",
         "601398": "工商银行", "601328": "交通银行"}


def below_bars(volume=1_000_000.0):
    return make_bars(DATE, [10.0] * 68 + [8.9, 9.0], volumes=[volume] * 70)


def above_bars(volume=1_000_000.0):
    return make_bars(DATE, [10.0] * 70, volumes=[volume] * 70)


def rich_divs(code, ex_recent="2026-09-25"):
    """多年连续、TTM 股息率约 6.7% 的分红记录。"""
    return [
        DividendEvent(code=code, announce_date="2025-09-20",
                      ex_date="2025-09-25", cash_per_share=0.6, progress="实施"),
        DividendEvent(code=code, announce_date="2026-09-20",
                      ex_date=ex_recent, cash_per_share=0.6, progress="实施"),
    ]


def tiny_divs(code):
    return [DividendEvent(code=code, announce_date="2026-06-20",
                          ex_date="2026-06-30", cash_per_share=0.02, progress="实施")]


class TestV82CriticalData(unittest.TestCase):
    def test_abort_when_dividends_missing(self):
        env = TempEnv("dividend_v8_2")
        env.hub.set_names(BANKS)
        for code in BANKS:
            env.hub.set_bars(code, above_bars())  # 全部无分红数据
        with self.assertRaises(CriticalDataMissing):
            V82Strategy().signals(env.ctx)

    def test_abort_when_coverage_low(self):
        env = TempEnv("dividend_v8_2")
        names = dict(BANKS)
        names.update({f"601{idx:03d}": f"银行{idx}" for idx in range(6)})
        env.hub.set_names(names)
        for code in BANKS:  # 仅 4/10 有行情 → 覆盖率 40%
            env.hub.set_bars(code, above_bars())
            env.hub.set_dividends(code, rich_divs(code))
        with self.assertRaises(CriticalDataMissing):
            V82Strategy().signals(env.ctx)


class TestV82Selection(unittest.TestCase):
    def test_only_quality_oversold_passes(self):
        env = TempEnv("dividend_v8_2")
        env.hub.set_names(BANKS)
        # 600036：连续高分红 + 成交活跃 + 跌破下轨
        env.hub.set_bars("600036", below_bars(volume=2_000_000.0))
        env.hub.set_dividends("600036", rich_divs("600036"))
        # 600015：分红微薄（低质量）+ 同样跌破下轨
        env.hub.set_bars("600015", below_bars())
        env.hub.set_dividends("600015", tiny_divs("600015"))
        # 其余两只未破位
        env.hub.set_bars("601398", above_bars())
        env.hub.set_dividends("601398", rich_divs("601398"))
        env.hub.set_bars("601328", above_bars())
        env.hub.set_dividends("601328", tiny_divs("601328"))

        cands, plans, status = V82Strategy().signals(env.ctx)
        self.assertEqual(status, "ok")
        self.assertEqual([c.code for c in cands], ["600036"])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].code, "600036")
        self.assertGreaterEqual(cands[0].score, 60.0)  # 达到买入门槛

    def test_ex_dividend_day_excluded(self):
        env = TempEnv("dividend_v8_2")
        env.hub.set_names(BANKS)
        env.hub.set_bars("600036", below_bars(volume=2_000_000.0))
        env.hub.set_dividends("600036",
                              [DividendEvent(code="600036", announce_date="2026-09-30",
                                             ex_date=DATE, cash_per_share=0.6,
                                             progress="实施")])
        env.hub.set_bars("600015", below_bars())
        env.hub.set_dividends("600015", tiny_divs("600015"))
        env.hub.set_bars("601398", above_bars())
        env.hub.set_dividends("601398", rich_divs("601398"))
        env.hub.set_bars("601328", above_bars())
        env.hub.set_dividends("601328", tiny_divs("601328"))
        cands, plans, _ = V82Strategy().signals(env.ctx)
        self.assertEqual(cands, [])  # 除权日超跌不建新仓
        self.assertTrue(any("除权日" in a for a in env.ctx.anomalies))


if __name__ == "__main__":
    unittest.main()
