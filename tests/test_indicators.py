"""指标库单测：均线、布林、RSI(Wilder)、MACD、百分位、回撤、穿越。"""
import math
import unittest

from quant_core.analysis import indicators as ind


class TestIndicators(unittest.TestCase):
    def test_sma_rolling_and_none(self):
        out = ind.sma([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[-1], 4.0)
        self.assertAlmostEqual(out[2], 2.0)

    def test_bollinger_bands_and_width(self):
        vals = [10.0] * 19 + [8.9, 9.8]
        mid, upper, lower = ind.bollinger(vals, 20, 2.0)
        self.assertIsNone(lower[0])
        self.assertIsNotNone(lower[-1])
        # 滚动窗口为最后 20 个值：18 个 10 + 8.9 + 9.8
        self.assertAlmostEqual(mid[-1], 9.935, places=8)
        self.assertGreater(upper[-1], mid[-1])
        self.assertLess(lower[-1], mid[-1])
        # 8.9 在窗口内，末日 9.8 应处于下轨之上、8.9 处于其下轨之下
        self.assertLess(8.9, lower[-2])
        self.assertGreater(9.8, lower[-1])

    def test_rsi_wilder_all_gains_and_mixed(self):
        out = ind.rsi([1.0] + [2.0] * 20, 14)
        self.assertAlmostEqual(out[-1], 100.0, places=6)
        down = ind.rsi([10.0] + [9.0] * 20, 14)
        self.assertAlmostEqual(down[-1], 0.0, places=6)
        self.assertEqual(ind.rsi([1, 2, 3], 14), [None, None, None])

    def test_macd_centerline_and_history(self):
        dif, dea, hist = ind.macd([10.0] * 60)
        self.assertAlmostEqual(dif[-1], 0.0, places=8)
        self.assertAlmostEqual(dea[-1], 0.0, places=8)
        self.assertEqual(len(dif), 60)
        self.assertIsNone(dif[0])

    def test_macd_death_cross_series(self):
        # 26 日平盘 + 6 日拉升至 12 + 5 日回落至 10.5：末日死叉
        closes = [10.0] * 26
        closes += [10 + 2 * (i + 1) / 6 for i in range(6)]
        closes += [12 - 1.5 * (i + 1) / 5 for i in range(5)]
        dif, dea, _ = ind.macd(closes)
        self.assertGreaterEqual(dif[-2], dea[-2])
        self.assertLess(dif[-1], dea[-1])

    def test_pct_rank_and_ret_and_drawdown(self):
        self.assertAlmostEqual(ind.pct_rank([1, 2, 3, 4], 3.0), 0.625)
        self.assertEqual(ind.pct_rank([], 1.0), 0.5)
        self.assertAlmostEqual(ind.ret([1.0, 1.1, 1.2], 2), 0.2)
        self.assertAlmostEqual(ind.max_drawdown([10, 12, 9, 11]), -0.25)

    def test_cross_helpers(self):
        a = [1.0, 2.0]
        b = [1.5, 1.5]
        self.assertTrue(ind.cross_above(a, b))
        self.assertFalse(ind.cross_below(a, b))
        self.assertTrue(ind.cross_below([2.0, 1.0], [1.5, 1.5]))
        self.assertFalse(ind.cross_above([None, 2.0], [1.0, 1.0]))


if __name__ == "__main__":
    unittest.main()
