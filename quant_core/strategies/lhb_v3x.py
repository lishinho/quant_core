"""龙头涨停策略 LHB_V3X。"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from ..common import calendar
from ..common.keys import stable_key
from ..common.log import get_logger
from ..common.models import Candidate, TradePlan
from ..data.base import LimitUpInfo
from .base import BaseStrategy, RunContext
from . import div_common as dc

log = get_logger("quant_core.lhb")
ABNORMAL_PREFIXES = ("4", "8", "9", "43")  # 北交所等异常市场


def is_abnormal_market(code: str) -> bool:
    return code.startswith(ABNORMAL_PREFIXES) or code.endswith("退")


class LhbV3xStrategy(BaseStrategy):
    strategy = "LHB_V3X"

    # ------------------------------------------------------------- 盘后巡检
    def exits(self, ctx: RunContext) -> List[TradePlan]:
        cfg = ctx.cfg["trade"]
        zt_codes = {x.code for x in ctx.hub.limit_up_pool(ctx.date)}
        start = (calendar.to_date(ctx.date) - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        plans: List[TradePlan] = []
        for code, pos in list(ctx.ledger.positions.items()):
            bars = ctx.hub.bars(code, start, ctx.date)
            if not bars or bars[-1].date != ctx.date:
                continue
            close = bars[-1].close
            ratio = close / pos.avg_cost - 1.0
            reason = tag = None
            if ratio <= cfg["stop_loss"]:
                reason, tag = f"龙头止损 {ratio*100:.1f}%", "LHB_STOP"
            elif ratio >= cfg["take_profit"] and code not in zt_codes:
                reason, tag = f"离开涨停池止盈 +{ratio*100:.1f}%", "LHB_TP"
            # 已盈利但仍在候选池：继续持有；其他情况继续观察
            if reason:
                plan = ctx.engine.make_sell_plan(
                    lane=ctx.lane, code=code, name=pos.name, gen_date=ctx.date,
                    trade_date=ctx.next_date, reason=reason,
                    qty=pos.available_qty or pos.qty, last_price=close, tag=tag)
                dc.add_plan_once(ctx, plan, plans)
        return plans

    # ------------------------------------------------------------- 盘后选股
    def signals(self, ctx: RunContext) -> Tuple[List[Candidate], List[TradePlan], str]:
        cfg = ctx.cfg
        zt = ctx.hub.limit_up_pool(ctx.date)
        zbgc = ctx.hub.broken_board_pool(ctx.date)
        dtgc = ctx.hub.limit_down_pool(ctx.date)
        broken_map = {x.code: x for x in zbgc}

        # 市场情绪
        zt_count, broken_count, ld_count = len(zt), len(zbgc), len(dtgc)
        broken_rate = broken_count / max(1, zt_count + broken_count)
        sent = cfg["sentiment"]
        if (ld_count >= sent["panic_limit_down_count"]
                or (zt_count <= sent["weak_limit_up_count"]
                    and broken_rate >= sent["weak_broken_rate"])):
            env, buy_count = "PANIC", 0
        elif (zt_count <= sent["weak_limit_up_count"]
              or broken_rate >= sent["weak_broken_rate"]):
            env, buy_count = "WEAK", sent["weak_buy_count"]
        else:
            env, buy_count = "NORMAL", sent["normal_buy_count"]
        ctx_anomaly = ctx.anomalies
        ctx_anomaly.append(f"情绪: {env}（涨停{zt_count}/炸板{broken_count}"
                           f"@{broken_rate:.0%}/跌停{ld_count}），计划买入数 {buy_count}")

        # 题材统计：强度 + 梯队
        theme_members: Dict[str, List[LimitUpInfo]] = {}
        for info in zt:
            for theme in info.themes or ["未分类"]:
                theme_members.setdefault(theme, []).append(info)

        def theme_strength(themes: List[str]) -> Tuple[float, float, str]:
            best_name, best_size, ladder = "未分类", 0, 0.0
            for tname in themes or ["未分类"]:
                members = theme_members.get(tname, [])
                size = len(members)
                levels = {min(3, x.streak_days) for x in members}
                ladder_score = len({1, 2, 3} & levels) / 3.0
                if size >= best_size:
                    best_name, best_size, ladder = tname, size, ladder_score
            strength = min(1.0, best_size / 4.0)
            return strength, ladder, best_name

        # 评分
        scored: List[Tuple[float, LimitUpInfo, Dict[str, float], List[str]]] = []
        w = cfg["scoring"]["weights"]
        for info in zt:
            if "ST" in info.name.upper() or "ST" in info.name:
                continue
            if is_abnormal_market(info.code):
                continue
            first_min = info.first_seal_minutes
            seal_time_s = max(0.0, min(1.0, (14 * 60 - first_min) / (14 * 60 - 9 * 60 - 25))) * 100
            streak_s = min(100.0, 30.0 + max(1, info.streak_days) * 14.0)
            strength, ladder, theme_name = theme_strength(info.themes)
            theme_s = 60 * strength + 40 * ladder
            members = theme_members.get(theme_name, [])
            top_streak = max((x.streak_days for x in members), default=1)
            leader_s = (0.6 * min(1.0, info.streak_days / max(1, top_streak))
                        + 0.4 * seal_time_s / 100) * 100
            seal_ratio = info.seal_amount / info.amount if info.amount > 0 else 0
            quality_s = min(1.0, seal_ratio / 0.3) * 60 + min(1.0, info.amount / 1.0e9) * 40
            safety_s = min(1.0, info.circ_mv / 3.0e10) * 70 + 30
            lhb_s = 50.0  # 盘前龙虎榜补丁前给中性分
            dims = {
                "seal_time": round(seal_time_s, 1),
                "streak": round(streak_s, 1),
                "theme": round(theme_s, 1),
                "leader": round(leader_s, 1),
                "quality": round(quality_s, 1),
                "lhb_fund": lhb_s,
                "safety": round(safety_s, 1),
            }
            score = sum(dims[k] * w.get(k, 0) for k in dims)
            reasons = [f"{info.streak_days}连板", theme_name,
                       f"首封 {info.first_seal_time or '-'}"]
            if info.broken_times > 0:
                score -= cfg["scoring"]["broken_board_penalty"]
                reasons.append(f"炸板{info.broken_times}次")
            if first_min >= 13 * 60:
                score -= cfg["scoring"]["weak_seal_penalty"]
                reasons.append("尾盘弱封板")
            if info.code in broken_map:
                score -= cfg["scoring"]["broken_board_penalty"]
            scored.append((round(score, 2), info, dims, reasons))
        scored.sort(key=lambda x: x[0], reverse=True)

        candidates: List[Candidate] = []
        plans: List[TradePlan] = []
        # 重跑防护：账本中次日待成交买单已占用当日名额，避免 force 重跑
        # 把名额顺延给排名靠后的票而不断累积新计划
        used = sum(1 for p in ctx.ledger.plans
                   if p.status == "PENDING" and p.side == "BUY"
                   and p.trade_date == ctx.next_date)
        pending_codes = {p.code for p in ctx.ledger.plans
                         if p.status == "PENDING" and p.side == "BUY"
                         and p.trade_date == ctx.next_date}
        for score, info, dims, reasons in scored:
            if len(plans) + used >= buy_count:
                break
            if score < cfg["trade"]["buy_score_threshold"]:
                continue
            if info.code in ctx.ledger.positions or info.code in pending_codes:
                continue
            cand = Candidate(
                code=info.code, name=info.name, date=ctx.date, score=score,
                dim_scores=dims, reasons=reasons,
                extra={"streak": info.streak_days, "themes": info.themes,
                       "first_seal": info.first_seal_time,
                       "broken_times": info.broken_times,
                       "seal_amount": info.seal_amount, "env": env},
            )
            plan = ctx.engine.make_buy_plan(
                lane=ctx.lane, code=info.code, name=info.name, gen_date=ctx.date,
                trade_date=ctx.next_date,
                reason=f"LHB龙头 {info.streak_days}连板/{'/'.join(info.themes)}",
                weight=cfg["trade"]["per_position_weight"], hint_price=info.close,
                equity=ctx.ledger.equity(), score=score, tag="LHB")
            if dc.add_plan_once(ctx, plan, plans):
                candidates.append(cand)
        return candidates, plans, "ok" if env != "PANIC" else "panic_no_new"


# ============================================================ 盘前龙虎榜补丁
def apply_premarket_patch(ctx: RunContext) -> Dict[str, Any]:
    """次日盘前根据 T 日龙虎榜调整买入计划。

    - 看多：加分；偏空：降分（过低则取消高风险计划）
    - 未上龙虎榜：仅正常处理，绝不清空计划
    """
    cfg = ctx.cfg["scoring"]
    pending = [p for p in ctx.ledger.pending_plans(ctx.next_date)
               if p.side == "BUY" and p.trade_date == ctx.next_date]
    lhb_records = ctx.hub.lhb(ctx.date, ctx.date)
    lhb_map = {r.code: r for r in lhb_records if r.date == ctx.date}

    adjustments: List[Dict[str, Any]] = []
    for plan in pending:
        record: Optional[Any] = lhb_map.get(plan.code)
        old_score = plan.score or 0.0
        if record is None:
            # 未上龙虎榜：不取消，不调整
            adjustments.append({"code": plan.code, "action": "not_on_lhb",
                                "score": old_score})
            continue
        if record.sentiment == "BULL":
            plan.score = round(old_score + cfg["premarket_bull_bonus"], 2)
            plan.reason += " | 龙虎榜看多"
            action = "bull_upgrade"
        else:
            plan.score = round(old_score - cfg["premarket_bear_penalty"], 2)
            plan.reason += " | 龙虎榜偏空"
            action = "bear_downgrade"
            if plan.score < cfg["high_risk_cancel_threshold"]:
                ctx.ledger.cancel_plan(plan.plan_key, f"龙虎榜偏空后分数 {plan.score:.1f} 过低")
                action = "bear_cancelled"
        adjustments.append({"code": plan.code, "action": action,
                            "old_score": old_score, "score": plan.score,
                            "net_buy": record.net_buy})

    # 重新排序（按调整后分数降序写入顺序）
    pending_live = [p for p in pending if p.status == "PENDING"]
    pending_live.sort(key=lambda p: p.score or 0, reverse=True)
    for p in pending_live:
        ctx.ledger.plans.remove(p)
    ctx.ledger.plans.extend(pending_live)

    result = {
        "date": ctx.date, "patch_date": ctx.next_date,
        "lhb_count": len(lhb_records), "plans_total": len(pending),
        "adjustments": adjustments,
    }
    ctx.artifacts.write("premarket_patch", result)
    return result
