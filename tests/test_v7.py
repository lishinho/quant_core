"""V7 布林网格单测：
- 新开仓：前日收下轨下方、当日站上
- 组合回撤暂停新开仓（但允许加仓与退出）
- 分层加仓 L2/L3、最多三层
- +5% 半仓止盈（只一次）、+10% 全退、盈利 MACD 死叉、单日大跌、灾难亏损、90 日到期
"""
import unittest

from quant_core.common.models import Position
from quant_core.strategies.div_v7 import V7Strategy
from tests.support import TempEnv, make_bars

DATE = "2026-10-09"


def flat(n: int, price: float = 10.0):
    return [price] * n


def open_position(env: TempEnv, code: str = "600900", qty: int = 1000,
                  cost: float = 10.0, open_date: str = "2026-10-08",
                  layers=None, meta=None) -> Position:
    pos = Position(
        code=code, name=code, qty=qty, available_qty=qty, avg_cost=cost,
        open_date=open_date, last_price=cost, high_since_buy=cost,
        layers=layers or [], meta=meta or {})
    env.ledger.positions[code] = pos
    return pos


class TestV7EntryAndGrid(unittest.TestCase):
    def test_reentry_below_then_above_lower(self):
        env = TempEnv("dividend_v7")
        env.hub.set_bars("600900", make_bars(DATE, flat(30) + [8.9, 9.8]))
        cands, plans, status = V7Strategy().signals(env.ctx)
        self.assertEqual(status, "ok")
        self.assertEqual(len(cands), 1)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].tag, "L1")
        self.assertEqual(plans[0].layer, 1)
        self.assertEqual(plans[0].trade_date, "2026-10-12")

    def test_no_entry_without_reentry(self):
        env = TempEnv("dividend_v7")
        env.hub.set_bars("600900", make_bars(DATE, flat(32)))
        _, plans, _ = V7Strategy().signals(env.ctx)
        self.assertEqual(plans, [])

    def test_drawdown_pauses_new_but_allows_add(self):
        env = TempEnv("dividend_v7")
        env.ledger.equity_peak = 1_200_000.0  # 当前现金 100 万 → 回撤 16.7%
        # 新票触发回轨：不应开新仓
        env.hub.set_bars("601088", make_bars(DATE, flat(30) + [8.9, 9.8]))
        _, plans, _ = V7Strategy().signals(env.ctx)
        self.assertEqual(plans, [])
        self.assertTrue(any("暂停 V7 新开仓" in a for a in env.ctx.anomalies))
        # 已持仓票跌至第二层：回撤期间仍允许加仓
        env.hub.set_bars("600900", make_bars(DATE, flat(38) + [9.65, 9.65]))
        open_position(env, layers=[{"layer": 1, "buy_date": "2026-10-08",
                                    "price": 10.0, "qty": 1000}])
        exits = V7Strategy().exits(env.ctx)
        tags = [p.tag for p in exits]
        self.assertIn("L2", tags)

    def test_layer_progression_and_cap(self):
        env = TempEnv("dividend_v7")
        env.hub.set_bars("600900", make_bars(DATE, flat(38) + [9.65, 9.65]))
        open_position(env, layers=[{"layer": 1, "buy_date": "2026-09-30",
                                    "price": 10.0, "qty": 1000}])
        exits = V7Strategy().exits(env.ctx)
        self.assertEqual([p.tag for p in exits], ["L2"])
        # 同一日重复巡检：稳定键去重，不重复加仓
        exits2 = V7Strategy().exits(env.ctx)
        self.assertEqual(exits2, [])

        env3 = TempEnv("dividend_v7")
        env3.hub.set_bars("600900", make_bars(DATE, flat(38) + [9.45, 9.45]))
        open_position(env3, cost=9.825,
                      layers=[{"layer": 1, "buy_date": "2026-09-25", "price": 10.0, "qty": 500},
                              {"layer": 2, "buy_date": "2026-09-30", "price": 9.65, "qty": 500}])
        exits = V7Strategy().exits(env3.ctx)
        self.assertEqual([p.tag for p in exits], ["L3"])

        env4 = TempEnv("dividend_v7")
        env4.hub.set_bars("600900", make_bars(DATE, flat(38) + [9.3, 9.3]))
        open_position(env4, cost=9.6,
                      layers=[{"layer": 1, "buy_date": "2026-09-20", "price": 10.0, "qty": 400},
                              {"layer": 2, "buy_date": "2026-09-25", "price": 9.65, "qty": 400},
                              {"layer": 3, "buy_date": "2026-09-30", "price": 9.45, "qty": 400}])
        self.assertEqual(V7Strategy().exits(env4.ctx), [])  # 已满三层


class TestV7Exits(unittest.TestCase):
    def _run_exit(self, closes, **pos_kw):
        env = TempEnv("dividend_v7")
        env.hub.set_bars("600900", make_bars(DATE, closes))
        open_position(env, **pos_kw)
        return V7Strategy().exits(env.ctx), env

    def test_take_profit_half_once(self):
        exits, env = self._run_exit(flat(39) + [10.55])
        self.assertEqual([p.tag for p in exits], ["TP_HALF"])
        self.assertEqual(exits[0].qty, 500)
        # 成交后 runner 置 tp_half_done：同日 +5.x% 不再重复半仓止盈
        env.ledger.positions["600900"].meta["tp_half_done"] = True
        env.hub.set_bars("600900", make_bars(DATE, flat(39) + [10.52]))
        self.assertEqual(V7Strategy().exits(env.ctx), [])

    def test_take_profit_all(self):
        exits, _ = self._run_exit(flat(39) + [11.02])
        self.assertEqual([p.tag for p in exits], ["TP_ALL"])
        self.assertEqual(exits[0].qty, 1000)

    def test_macd_death_cross_in_profit(self):
        closes = flat(26) + [10 + 2 * (i + 1) / 6 for i in range(6)] \
            + [12 - 1.5 * (i + 1) / 5 for i in range(5)]
        exits, _ = self._run_exit(closes)
        self.assertEqual([p.tag for p in exits], ["MACD_ALL"])

    def test_single_day_drop(self):
        exits, _ = self._run_exit(flat(38) + [10.14, 9.62])
        self.assertEqual([p.tag for p in exits], ["DROP_ALL"])

    def test_catastrophic_loss(self):
        exits, _ = self._run_exit(flat(39) + [9.15])
        self.assertEqual([p.tag for p in exits], ["STOP_ALL"])

    def test_expire_90_trading_days(self):
        exits, _ = self._run_exit(flat(40), qty=1000, cost=10.0,
                                  open_date="2026-05-06")
        self.assertEqual([p.tag for p in exits], ["EXPIRE_ALL"])

    def test_no_exit_in_normal_holding(self):
        exits, _ = self._run_exit(flat(39) + [10.2])
        self.assertEqual(exits, [])


if __name__ == "__main__":
    unittest.main()
