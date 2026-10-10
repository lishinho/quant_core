"""单 lane 日跑编排：执行到期计划 → 盘后更新 → 退出巡检 → 新信号 → 产物/Base。

恢复策略：优先复用本地产物。已完成 lane 直接读取 summary；
force 时全量重跑（成交账本与稳定键保证不会重复成交）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .common.artifacts import ArtifactStore
from .common.config import ConfigBundle, config_summary
from .common import calendar
from .common.keys import stable_key
from .common.log import get_logger
from .common.models import Bar
from .common.retry import CriticalDataMissing
from .data.hub import DataHub
from .integrations.feishu_base import BaseSink, build_sink
from .trading.engine import TradingEngine
from .trading.ledger import Ledger
from .strategies.base import RunContext
from .strategies.div_v3 import V3Strategy
from .strategies.div_v5 import V5Strategy
from .strategies.div_v7 import V7Strategy
from .strategies.div_v8_1 import V81Strategy
from .strategies.div_v8_2 import V82Strategy
from .strategies.div_v9 import V9Strategy
from .strategies.lhb_v3x import LhbV3xStrategy

log = get_logger("quant_core.runner")

STRATEGIES = {
    "V3": V3Strategy, "V5": V5Strategy, "V7": V7Strategy,
    "V8_1": V81Strategy, "V8_2": V82Strategy, "V9": V9Strategy,
    "LHB_V3X": LhbV3xStrategy,
}


def _day_bars(hub: DataHub, codes: List[str], date: str) -> Dict[str, Bar]:
    out: Dict[str, Bar] = {}
    for code in set(codes):
        bars = hub.bars(code, date, date)
        if bars and bars[-1].date == date:
            out[code] = bars[-1]
    return out


def _sanitize(fields: Dict[str, Any]) -> Dict[str, Any]:
    def conv(v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return json_s(v)
        if isinstance(v, dict):
            return json_s(v)
        if v is None:
            return ""
        return v
    return {k: conv(v) for k, v in fields.items()}


def json_s(v: Any) -> str:
    import json
    return json.dumps(v, ensure_ascii=False, default=str)


def run_lane(bundle: ConfigBundle, date: str, hub: DataHub,
             engine: TradingEngine, force: bool = False,
             sinks: Optional[Dict[str, BaseSink]] = None) -> Dict[str, Any]:
    lane = bundle.lane_name
    cfg = bundle.lane
    paths = bundle.paths
    next_date = calendar.next_trading_day(date).strftime("%Y-%m-%d")
    artifacts = ArtifactStore(paths["artifacts"], lane, date)

    if artifacts.done and not force:
        summary = artifacts.read("summary")
        log.info("[%s] %s 已完成，复用本地产物（resume）", lane, date)
        return {**summary, "resumed": True}

    ledger = Ledger(paths["state"], lane, bundle.runtime["initial_capital"])
    strategy = STRATEGIES[cfg["strategy"]]()
    ctx = RunContext(bundle=bundle, date=date, next_date=next_date, hub=hub,
                     engine=engine, ledger=ledger, artifacts=artifacts)
    anomalies: List[str] = []
    status = "ok"

    # 1) 执行到期计划（T+1 开盘模拟成交）
    pending = ledger.pending_plans(date)
    day_bars = _day_bars(hub, [p.code for p in pending], date)
    fills = engine.execute_pending(ledger, date, day_bars)
    for f in fills:
        plan = ledger.get_plan(f.plan_key)
        if plan and plan.tag == "TP_HALF" and f.code in ledger.positions:
            ledger.positions[f.code].meta["tp_half_done"] = True
    artifacts.write("executed_fills", [f.to_dict() for f in fills])
    log.info("[%s] 成交 %d 笔", lane, len(fills))

    # 2) 盘后行情更新
    mtm = engine.mark_to_market(
        ledger, date,
        {**day_bars, **_day_bars(hub, list(ledger.positions.keys()), date)})

    # 3) 退出巡检（生成次日卖出计划）
    try:
        exit_plans = strategy.exits(ctx)
        if exit_plans:
            log.info("[%s] 退出计划 %d 份", lane, len(exit_plans))
    except CriticalDataMissing as err:
        exit_plans = []
        anomalies.append(f"退出阶段关键数据缺失: {err}")
        log.error("[%s] %s", lane, err)

    # 4) 新信号（关键数据缺失时只停止新信号，不影响退出）
    candidates, new_plans = [], []
    try:
        candidates, new_plans, status = strategy.signals(ctx)
    except CriticalDataMissing as err:
        status = "critical_abort"
        anomalies.append(str(err))
        log.error("[%s] 停止生成新信号: %s", lane, err)
    except Exception as err:  # noqa: BLE001 - 单策略异常不应拖垮其他 lane
        status = "error"
        anomalies.append(f"信号生成异常: {type(err).__name__}: {err}")
        log.exception("[%s] 信号生成异常", lane)
    anomalies.extend(ctx.anomalies)

    ledger.save()

    # 5) 汇总与本地产物
    equity = ledger.equity()
    drawdown = equity / max(ledger.equity_peak, 1.0) - 1.0
    new_plan_dicts = [p.to_dict() for p in new_plans]
    cand_dicts = [c.to_dict() for c in candidates]
    summary = {
        "date": date,
        "lane": lane,
        "strategy": cfg.get("strategy"),
        "strategy_name": cfg.get("strategy_name"),
        "status": status,
        "run_meta": {
            **config_summary(bundle),
            "provider": hub.provider.name,
            "data_stats": hub.stats,
            "trade_date_next": next_date,
        },
        "candidates": cand_dicts,
        "new_plans": new_plan_dicts,
        "exit_plans": [p.to_dict() for p in exit_plans],
        "executed_fills": [f.to_dict() for f in fills],
        "positions": [p.to_dict() for p in ledger.positions.values()],
        "cash": round(ledger.cash, 2),
        "equity": round(equity, 2),
        "equity_peak": round(ledger.equity_peak, 2),
        "drawdown": round(drawdown, 4),
        "position_count": ledger.position_count(),
        "mtm": mtm,
        "anomalies": anomalies,
    }
    artifacts.write("candidates", cand_dicts)
    artifacts.write("new_plans", new_plan_dicts)
    artifacts.write("positions", summary["positions"])
    artifacts.write("summary", summary)
    artifacts.mark_done()

    # 6) 幂等写入飞书 Base（失败不影响本地产物）
    _sync_feishu(bundle, summary, sinks)
    return summary


def _sync_feishu(bundle: ConfigBundle, summary: Dict[str, Any],
                 sinks: Optional[Dict[str, BaseSink]]) -> None:
    if sinks is None:
        return
    lane = bundle.lane_name
    route_name = bundle.system["lanes"][lane].get("base_route", "main")
    try:
        sink = sinks.get(route_name)
        if sink is None:
            route_cfg = bundle.system["feishu"][route_name]
            sink = build_sink(route_cfg, bundle.paths["feishu_local"], route_name)
            sinks[route_name] = sink
        tables = bundle.system["feishu"][route_name]["tables"]
        date = summary["date"]
        for c in summary["candidates"]:
            sink.upsert(tables["candidates"], "key",
                        stable_key(lane, c["code"], date),
                        _sanitize({"lane": lane, "date": date, **c}))
        for p in summary["new_plans"] + summary.get("exit_plans", []):
            sink.upsert(tables["plans"], "plan_key", p["plan_key"],
                        _sanitize({"lane": lane, **p}))
        for f in summary["executed_fills"]:
            sink.upsert(tables["fills"], "plan_key", f["plan_key"],
                        _sanitize({"lane": lane, **f}))
        for pos in summary["positions"]:
            sink.upsert(tables["positions"], "key",
                        stable_key(lane, pos["code"], date),
                        _sanitize({"lane": lane, "date": date, **pos}))
        summary_fields = {k: v for k, v in summary.items()
                          if k not in ("candidates", "new_plans", "exit_plans",
                                       "executed_fills", "positions", "mtm")}
        summary_fields["candidate_count"] = len(summary.get("candidates", []))
        summary_fields["new_plan_count"] = (len(summary.get("new_plans", []))
                                            + len(summary.get("exit_plans", [])))
        summary_fields["fill_count"] = len(summary.get("executed_fills", []))
        sink.upsert(tables["summary"], "key", stable_key(lane, date),
                    _sanitize(summary_fields))
    except Exception as err:  # noqa: BLE001
        log.warning("[%s] sink 写入失败（本地产物已完整）: %s", lane, err)
