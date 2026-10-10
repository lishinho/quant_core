"""quant_core 命令行入口。

示例：
  python -m quant_core.cli                          # 全部 lane 跑最近交易日（auto 数据源）
  python -m quant_core.cli --date 2026-10-09 --lanes dividend_v7,lhb_v3x
  python -m quant_core.cli --provider mock --force # 离线模拟数据强制重跑
  python -m quant_core.cli --premarket --date 2026-10-09  # 次日盘前龙虎榜补丁
  python -m quant_core.cli --report-only --date 2026-10-09
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Dict, List

from .common import calendar
from .common.artifacts import ArtifactStore
from .common.cache import DiskCache
from .common.config import load_system_config, load_lane_config, ConfigBundle
from .common.log import get_logger
from .data.hub import DataHub, create_provider
from .integrations.feishu_base import BaseSink, build_sink
from .reporting.daily_report import build_daily_report, write_report
from .runner import run_lane
from .trading.engine import TradingEngine
from .trading.ledger import Ledger
from .strategies.base import RunContext
from .strategies.lhb_v3x import apply_premarket_patch

log = get_logger("quant_core.cli")


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="quant_core 日跑")
    p.add_argument("--date", default=None, help="T 日 YYYY-MM-DD，默认最近交易日")
    p.add_argument("--lanes", default="", help="逗号分隔，默认全部 enabled lane")
    p.add_argument("--provider", default=None, choices=["auto", "akshare", "mock"])
    p.add_argument("--config", default="configs/system.json")
    p.add_argument("--force", action="store_true", help="忽略已有产物强制重跑")
    p.add_argument("--premarket", action="store_true", help="执行盘前龙虎榜补丁")
    p.add_argument("--report-only", action="store_true", help="只重新生成日报")
    return p.parse_args(argv)


def resolve_lanes(system: dict, only: str) -> List[str]:
    if only:
        names = [x.strip() for x in only.split(",") if x.strip()]
        unknown = [n for n in names if n not in system["lanes"]]
        if unknown:
            raise SystemExit(f"未知 lane: {unknown}")
        return names
    return [n for n, meta in system["lanes"].items() if meta.get("enabled", True)]


def main(argv: List[str] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    system = load_system_config(args.config)
    if args.provider:
        system["runtime"]["data_provider"] = args.provider
    date = args.date or calendar.latest_trading_day_on_or_before(dt.date.today()).strftime("%Y-%m-%d")
    lanes = resolve_lanes(system, args.lanes)
    paths = system["paths"]

    if args.report_only:
        report = build_daily_report(paths["artifacts"], lanes, date)
        out = write_report(paths["reports"], report)
        log.info("日报已生成: %s", out["md"])
        return 0

    cache = DiskCache(paths["cache"])
    provider = create_provider(system["runtime"]["data_provider"])
    hub = DataHub(provider, cache,
                  retries=system["runtime"]["provider_retries"],
                  timeout=system["runtime"]["provider_timeout"])
    engine = TradingEngine(
        fee_rate=system["runtime"]["fee_rate"],
        min_fee=system["runtime"]["min_fee"],
        lot_size=system["runtime"]["lot_size"])
    sinks: Dict[str, BaseSink] = {}

    code = 0
    if args.premarket:
        for lane_name in lanes:
            if lane_name != "lhb_v3x":
                continue
            bundle = load_lane_config(system, lane_name)
            next_date = calendar.next_trading_day(date).strftime("%Y-%m-%d")
            artifacts = ArtifactStore(paths["artifacts"], lane_name, date)
            ledger = Ledger(paths["state"], lane_name, system["runtime"]["initial_capital"])
            ctx = RunContext(bundle=bundle, date=date, next_date=next_date,
                             hub=hub, engine=engine, ledger=ledger, artifacts=artifacts)
            result = apply_premarket_patch(ctx)
            ledger.save()
            log.info("龙虎榜补丁完成: %s", result["adjustments"])
        return 0

    for lane_name in lanes:
        bundle: ConfigBundle = load_lane_config(system, lane_name)
        try:
            summary = run_lane(bundle, date, hub, engine, force=args.force, sinks=sinks)
            log.info("[%s] %s 状态=%s 候选=%d 新计划=%d 持仓=%d 权益=%s",
                     lane_name, date, summary.get("status"),
                     len(summary.get("candidates", [])),
                     len(summary.get("new_plans", [])),
                     summary.get("position_count", 0), summary.get("equity"))
        except Exception as err:  # noqa: BLE001 - lane 隔离：单 lane 失败不影响其他
            log.exception("[%s] 运行失败: %s", lane_name, err)
            code = 1

    report = build_daily_report(paths["artifacts"], lanes, date)
    out = write_report(paths["reports"], report)
    log.info("日报已生成: %s", out["md"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
