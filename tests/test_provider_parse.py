"""Akshare Provider 纯解析逻辑单测（不发起网络请求）。"""
import datetime as dt
import unittest

from quant_core.data.akshare_provider import _dstr, _f, compact, market_prefix


class NaTShim(dt.datetime):
    """模拟 pandas NaT：isinstance 是 datetime 子类但 strftime 抛 ValueError。"""

    def strftime(self, fmt):
        raise ValueError("NaTType does not support strftime")


class TestParseHelpers(unittest.TestCase):
    def test_market_prefix(self):
        self.assertEqual(market_prefix("600900"), "sh")
        self.assertEqual(market_prefix("688001"), "sh")
        self.assertEqual(market_prefix("000001"), "sz")
        self.assertEqual(market_prefix("300750"), "sz")
        self.assertEqual(market_prefix("430047"), "bj")

    def test_compact(self):
        self.assertEqual(compact("2026-10-09"), "20261009")

    def test_dstr_various_types(self):
        self.assertEqual(_dstr(dt.date(2026, 10, 9)), "2026-10-09")
        self.assertEqual(_dstr("20261009"), "2026-10-09")
        self.assertEqual(_dstr("2026-10-09 15:00:00"), "2026-10-09")
        self.assertEqual(_dstr(NaTShim(2026, 1, 1)), "")  # NaT 不再导致整条数据失败

    def test_float_guard_nan_like(self):
        self.assertEqual(_f("", 1.0), 1.0)
        self.assertEqual(_f(None, 2.0), 2.0)
        self.assertEqual(_f("12.5"), 12.5)
        self.assertEqual(_f("abc", 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
