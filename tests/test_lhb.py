"""LHB_V3X 单测：
- 情绪分级 PANIC/WEAK/NORMAL 与买入数量
- ST/北交所剔除、龙头排序
- 巡检：-5% 止损、+5% 离开涨停池止盈、仍在池内继续持有
- 盘前龙虎榜补丁：看多 +8、未上榜不清空、偏空 -15 且低于 55 取消
"""
import unittest

from quant_core.common.models import Position
from quant_core.strategies.lhb_v3x import LhbV3xStrategy, apply_premarket_patch, is_abnormal_market
from tests.support import TempEnv, make_bars, limit_up, lhb_record

DATE = "2026-10-09"


def ai_leader_pool():
    """3 个梯队完整的 AI 题材：3板龙头 + 2板 + 两个1板。"""
    return [
        limit_up("600111", "AI龙头", streak=3, seal="0930", seal_amount=2e8,
                 amount=5e8, circ_mv=8e9),
        limit_up("600112", "AI二板", streak=2, seal="1000", seal_amount=1.5e8,
                 amount=4e8, circ_mv=6e9),
        limit_up("600113", "AI三板A", streak=1, seal="1030", seal_amount=1e8,
                 amount=3e8, circ_mv=5e9),
        limit_up("600114", "AI三板B", streak=1, seal="1100", seal_amount=0.8e8,
                 amount=2e8, circ_mv=4e9),
    ]


class TestLHBSelection(unittest.TestCase):
    def test_normal_env_buy_three_leader_first(self):
        env = TempEnv("lhb_v3x")
        pool = ai_leader_pool()
        # 补足到 30 只涨停（>20 阈值，情绪 NORMAL）；其余为尾盘弱封板低分票
        pool += [limit_up(f"6003{idx:02d}", f"冷门{idx}", streak=1, seal="1330",
                          seal_amount=0.1e8, amount=1e8, circ_mv=2e9,
                          themes=[f"题材{idx}"])
                 for idx in range(26)]
        env.hub.zt = pool
        cands, plans, status = LhbV3xStrategy().signals(env.ctx)
        self.assertEqual(status, "ok")
        self.assertEqual(len(plans), 3)  # NORMAL 买 3
        self.assertEqual(plans[0].code, "600111")
        self.assertTrue(all(p.trade_date == "2026-10-12" for p in plans))

    def test_weak_env_buy_one(self):
        env = TempEnv("lhb_v3x")
        pool = ai_leader_pool()
        # 只保留 10 只涨停（<=20 阈值），炸板率 0 → WEAK
        env.hub.zt = pool + [limit_up(f"6002{idx:02d}", f"弱势{idx}", streak=1,
                                      seal="1330", seal_amount=0.2e8, amount=1e8)
                             for idx in range(6)]
        cands, plans, status = LhbV3xStrategy().signals(env.ctx)
        self.assertEqual(status, "ok")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].code, "600111")

    def test_panic_env_no_new_positions(self):
        env = TempEnv("lhb_v3x")
        env.hub.zt = ai_leader_pool()
        env.hub.ld = [limit_up(f"0003{idx:02d}", f"跌停{idx}") for idx in range(50)]
        _, plans, status = LhbV3xStrategy().signals(env.ctx)
        self.assertEqual(status, "panic_no_new")
        self.assertEqual(plans, [])

    def test_exclude_st_and_abnormal_market(self):
        env = TempEnv("lhb_v3x")
        env.hub.zt = ai_leader_pool() + [
            limit_up("600115", "ST测试", streak=4, seal="0925",
                     seal_amount=3e8, amount=6e8),
            limit_up("430001", "北交龙头", streak=5, seal="0925",
                     seal_amount=3e8, amount=6e8),
        ]
        cands, plans, _ = LhbV3xStrategy().signals(env.ctx)
        codes = {p.code for p in plans}
        self.assertNotIn("600115", codes)
        self.assertNotIn("430001", codes)
        self.assertTrue(is_abnormal_market("430001"))
        self.assertTrue(is_abnormal_market("000003退"))

    def test_force_rerun_no_quota_accumulation(self):
        # 同一账本上重跑信号：次日待成交买单已占名额，不得把名额顺延给后面的票
        env = TempEnv("lhb_v3x")
        pool = ai_leader_pool()
        pool += [limit_up(f"6003{idx:02d}", f"冷门{idx}", streak=1, seal="1330",
                          seal_amount=0.1e8, amount=1e8, circ_mv=2e9,
                          themes=[f"题材{idx}"])
                 for idx in range(26)]
        env.hub.zt = pool
        _, plans1, _ = LhbV3xStrategy().signals(env.ctx)
        self.assertEqual(len(plans1), 3)
        env.ledger.save()
        # 模拟 --force：基于已含 3 个 PENDING 计划的账本再跑一次
        _, plans2, _ = LhbV3xStrategy().signals(env.ctx)
        self.assertEqual(plans2, [])  # 无新增计划
        self.assertEqual(len(env.ledger.plans), 3)  # 不累积
        self.assertTrue(all(p.status == "PENDING" for p in env.ledger.plans))


class TestLHBExits(unittest.TestCase):
    def _position(self, env, code="600111", cost=10.0):
        env.ledger.positions[code] = Position(
            code=code, name=code, qty=1000, available_qty=1000,
            avg_cost=cost, open_date="2026-09-25", last_price=cost)

    def test_stop_loss(self):
        env = TempEnv("lhb_v3x")
        env.hub.set_bars("600111", make_bars(DATE, [10.0] * 29 + [9.4]))
        self._position(env)
        plans = LhbV3xStrategy().exits(env.ctx)
        self.assertEqual([p.tag for p in plans], ["LHB_STOP"])

    def test_take_profit_only_after_leaving_pool(self):
        # 仍在涨停池：+5% 继续持有
        env = TempEnv("lhb_v3x")
        env.hub.set_bars("600111", make_bars(DATE, [10.0] * 29 + [10.6]))
        env.hub.zt = [limit_up("600111", "AI龙头", streak=4)]
        self._position(env)
        self.assertEqual(LhbV3xStrategy().exits(env.ctx), [])
        # 离开涨停池：止盈
        env.hub.zt = []
        plans = LhbV3xStrategy().exits(env.ctx)
        self.assertEqual([p.tag for p in plans], ["LHB_TP"])

    def test_hold_when_no_signal(self):
        env = TempEnv("lhb_v3x")
        env.hub.set_bars("600111", make_bars(DATE, [10.0] * 29 + [10.1]))
        self._position(env)
        self.assertEqual(LhbV3xStrategy().exits(env.ctx), [])


class TestPremarketPatch(unittest.TestCase):
    def _plan(self, code: str, score: float):
        plan = env_holder["env"].engine.make_buy_plan(
            lane="lhb_v3x", code=code, name=code, gen_date=DATE,
            trade_date="2026-10-12", reason="龙头", weight=0.18,
            hint_price=10.0, equity=1_000_000.0, score=score, tag="LHB")
        env_holder["env"].ledger.add_plan(plan)
        return plan

    def test_bull_bear_neutral_paths(self):
        global env_holder
        env = TempEnv("lhb_v3x")
        env_holder = {"env": env}
        p_bull = self._plan("600111", 70.0)
        p_miss = self._plan("600112", 60.0)
        p_bear = self._plan("600113", 54.0)
        env.hub.lhb_records = [
            lhb_record("600111", 50_000_000),
            lhb_record("600113", -30_000_000),
        ]
        result = apply_premarket_patch(env.ctx)
        actions = {a["code"]: a["action"] for a in result["adjustments"]}
        self.assertEqual(actions["600111"], "bull_upgrade")
        self.assertEqual(actions["600112"], "not_on_lhb")  # 未上榜：不清空
        self.assertEqual(actions["600113"], "bear_cancelled")
        self.assertAlmostEqual(p_bull.score, 78.0)
        self.assertAlmostEqual(p_miss.score, 60.0)
        self.assertEqual(p_miss.status, "PENDING")
        self.assertEqual(p_bear.status, "CANCELLED")  # 54-15=39 < 55
        # 调整后按分数降序：78 的计划排最前
        live = [p for p in env.ledger.plans if p.status == "PENDING"]
        self.assertEqual(live[0].code, "600111")

    def test_bear_above_threshold_kept(self):
        global env_holder
        env = TempEnv("lhb_v3x")
        env_holder = {"env": env}
        p = self._plan("600111", 75.0)
        env.hub.lhb_records = [lhb_record("600111", -1.0)]
        result = apply_premarket_patch(env.ctx)
        action = result["adjustments"][0]["action"]
        self.assertEqual(action, "bear_downgrade")  # 75-15=60 仍高于 55
        self.assertAlmostEqual(p.score, 60.0)
        self.assertEqual(p.status, "PENDING")


env_holder = {}


if __name__ == "__main__":
    unittest.main()
