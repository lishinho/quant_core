"""东财/腾讯直连接口的离线解析单测（不发起网络请求）。"""
import unittest

from quant_core.data import eastmoney_http as emh


class TestEastMoneyHttpParse(unittest.TestCase):
    def test_parse_kline_units_and_order(self):
        payload = {"data": {"klines": [
            "2026-10-08,28.50,28.90,28.97,28.50,1134669,3270960934.00,1.65,1.26,0.36,0.46",
            "2026-10-09,28.98,28.99,29.40,28.91,1331033,3879076274.00,1.70,0.31,0.09,0.54",
        ]}}
        bars = emh.parse_kline(payload)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].date, "2026-10-08")
        self.assertEqual(bars[0].open, 28.50)
        self.assertEqual(bars[0].volume, 113_466_900.0)  # 手 -> 股
        self.assertAlmostEqual(bars[0].pct_chg, 0.0126, places=6)
        self.assertEqual(bars[-1].close, 28.99)

    def test_parse_kline_empty_and_garbage(self):
        self.assertEqual(emh.parse_kline({}), [])
        self.assertEqual(emh.parse_kline({"data": {"klines": ["x"]}}), [])

    def test_parse_tencent_kline(self):
        payload = {"data": {"sh601288": {"qfqday": [
            ["2026-10-08", "6.96", "7.02", "7.06", "6.94", "2997118"],
            ["2026-10-09", "7.04", "6.98", "7.09", "6.98", "3417518", {"qfqday": "info"}],
        ]}}}
        bars = emh.parse_tencent_kline(payload, "601288", symbol="sh601288")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].open, 6.96)
        self.assertEqual(bars[0].volume, 299_711_800.0)  # 手 -> 股
        self.assertAlmostEqual(bars[1].pct_chg, 6.98 / 7.02 - 1, places=8)
        # 默认按代码推 symbol
        bars2 = emh.parse_tencent_kline(payload, "601288")
        self.assertEqual(len(bars2), 2)

    def test_parse_tencent_kline_day_fallback(self):
        # 指数节点只有 day
        payload = {"data": {"sh000300": {"day": [["2026-10-09", "4300", "4317", "4320", "4290", "100"]]}}}
        bars = emh.parse_tencent_kline(payload, "000300", symbol="sh000300")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 4317.0)
        self.assertEqual(bars[0].pct_chg, 0.0)  # 首根无前收盘

    def test_parse_quote_scaled_fields(self):
        payload = {"data": {
            "f43": 2899, "f57": "600900", "f58": "长江电力",
            "f85": 24468217716.0, "f117": 709333631586.0,
            "f162": 2404, "f167": 328,
        }}
        q = emh.parse_quote(payload)
        self.assertEqual(q["code"], "600900")
        self.assertEqual(q["name"], "长江电力")
        self.assertEqual(q["price"], 28.99)
        self.assertEqual(q["pe_dynamic"], 24.04)
        self.assertEqual(q["pb"], 3.28)
        self.assertAlmostEqual(q["total_mv"], 709_333_631_586.0)
        self.assertFalse(q["suspended"])

    def test_parse_quote_none_data(self):
        self.assertIsNone(emh.parse_quote({"data": None}))
        self.assertIsNone(emh.parse_quote({}))

    def test_parse_clist_row(self):
        row = {"f2": 36.94, "f3": 1.25, "f5": 12345, "f6": 4.5e8,
               "f9": 13.48, "f12": "689009", "f14": "九号公司",
               "f20": 2.7e10, "f21": 2.0e10, "f23": 3.7, "f115": 14.1}
        q = emh.parse_clist_row(row)
        self.assertEqual(q["code"], "689009")
        self.assertEqual(q["price"], 36.94)
        self.assertAlmostEqual(q["pct_chg"], 0.0125)
        self.assertEqual(q["volume"], 1_234_500.0)
        self.assertEqual(q["amount"], 4.5e8)
        self.assertEqual(q["pe_dynamic"], 13.48)
        self.assertEqual(q["pe_ttm"], 14.1)
        self.assertEqual(q["pb"], 3.7)
        self.assertFalse(q["suspended"])

    def test_parse_clist_suspended(self):
        q = emh.parse_clist_row({"f2": "-", "f12": "1", "f14": "停牌股"})
        self.assertEqual(q["code"], "000001")
        self.assertIsNone(q["price"])
        self.assertTrue(q["suspended"])

    def test_parse_tencent(self):
        fields = ["0"] * 54
        fields[0], fields[1], fields[2], fields[3] = "1", "长江电力", "600900", "28.99"
        fields[39] = "20.5"
        fields[44] = "2446.8"
        fields[45] = "7093.3"
        fields[46] = "3.28"
        fields[52] = "24.0"
        text = 'v_sh600900="' + "~".join(fields) + '";'
        q = emh.parse_tencent(text, "600900")
        self.assertEqual(q["name"], "长江电力")
        self.assertEqual(q["pe_ttm"], 20.5)
        self.assertAlmostEqual(q["total_mv"], 7093.3 * 1e8)
        self.assertEqual(q["pb"], 3.28)
        self.assertEqual(q["source"], "tencent_qt")

    def test_parse_tencent_batch(self):
        def seg(prefix, name, code, price, pb):
            fields = ["0"] * 54
            fields[1], fields[2], fields[3] = name, code, price
            fields[46] = pb
            return f'v_{prefix}="{ "~".join(fields) }"'

        text = (seg("sh600900", "长江电力", "600900", "28.99", "3.28") + ";"
                + seg("sz000001", "平安银行", "000001", "11.2", "0.65") + ";")
        out = emh.parse_tencent_batch(text)
        self.assertEqual(set(out.keys()), {"600900", "000001"})
        self.assertEqual(out["600900"]["price"], 28.99)
        self.assertEqual(out["000001"]["pb"], 0.65)

    def test_tencent_symbol_prefix(self):
        self.assertEqual(emh._tencent_symbol("600900"), "sh600900")
        self.assertEqual(emh._tencent_symbol("000001"), "sz000001")
        self.assertEqual(emh._tencent_symbol("300750"), "sz300750")
        self.assertEqual(emh._tencent_symbol("430047"), "bj430047")

    def test_secid_prefix(self):
        self.assertEqual(emh._secid("600900"), "1.600900")
        self.assertEqual(emh._secid("000001"), "0.000001")
        self.assertEqual(emh._secid("300750"), "0.300750")
        self.assertEqual(emh._secid("688981"), "1.688981")


if __name__ == "__main__":
    unittest.main()
