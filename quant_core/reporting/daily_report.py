"""跨 lane 日报：以本地产物为唯一事实来源，输出 JSON + Markdown。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ..common.artifacts import ArtifactStore


def build_daily_report(artifacts_root: str, lanes: List[str], date: str) -> Dict[str, Any]:
    report: Dict[str, Any] = {"date": date, "lanes": {}, "anomalies": []}
    for lane in lanes:
        store = ArtifactStore(artifacts_root, lane, date)
        summary = store.read("summary")
        if summary is None:
            report["lanes"][lane] = {"status": "missing_artifact"}
            report["anomalies"].append(f"{lane}: 当日产物缺失")
            continue
        report["lanes"][lane] = summary
        for note in summary.get("anomalies", []):
            report["anomalies"].append(f"{lane}: {note}")
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    date = report["date"]
    lines: List[str] = [f"# quant_core 日报 {date}", ""]
    total_pnl = 0.0
    for lane, s in report["lanes"].items():
        lines.append(f"## {s.get('strategy_name', lane)} ({lane})")
        status = s.get("status", "?")
        lines.append(f"- 状态: **{status}**  权益: {s.get('equity', '-')}  "
                     f"持仓数: {s.get('position_count', 0)}")
        if status == "missing_artifact":
            lines.append("- ⚠️ 当日产物缺失，可能未运行或失败")
            lines.append("")
            continue
        cands = s.get("candidates", [])
        if cands:
            lines.append("")
            lines.append("### 候选信号（Top）")
            lines.append("| 代码 | 名称 | 总分 | 关键理由 |")
            lines.append("|---|---|---|---|")
            for c in cands[:5]:
                reason = "；".join(c.get("reasons", [])[:2])
                lines.append(f"| {c['code']} | {c['name']} | {c['score']:.1f} | {reason} |")
        new_plans = s.get("new_plans", [])
        if new_plans:
            lines.append("")
            lines.append("### 新交易计划（次日开盘）")
            for p in new_plans:
                lines.append(f"- [{p['side']}] {p['code']} {p['name']} "
                             f"{p.get('qty', 0)}股 — {p['reason']}")
        fills = s.get("executed_fills", [])
        if fills:
            lines.append("")
            lines.append("### 当日成交")
            for f in fills:
                lines.append(f"- [{f['side']}] {f['code']} {f['qty']}股 "
                             f"@{f['price']:.2f}，费用 {f['fee']:.2f}")
        positions = s.get("positions", [])
        if positions:
            lines.append("")
            lines.append("### 持仓")
            lines.append("| 代码 | 名称 | 数量 | 成本 | 现价 | 浮动收益 |")
            lines.append("|---|---|---|---|---|---|")
            for p in positions:
                ratio = (p['last_price'] / p['avg_cost'] - 1) if p['avg_cost'] else 0
                pnl = (p['last_price'] - p['avg_cost']) * p['qty']
                total_pnl += pnl
                lines.append(
                    f"| {p['code']} | {p['name']} | {p['qty']} | "
                    f"{p['avg_cost']:.2f} | {p['last_price']:.2f} | "
                    f"{ratio * 100:+.2f}% ({pnl:+.0f}) |"
                )
        anomalies = s.get("anomalies", [])
        if anomalies:
            lines.append("")
            lines.append("### 异常与降级")
            for a in anomalies:
                lines.append(f"- ⚠️ {a}")
        lines.append("")
    lines.append(f"## 全局异常 ({len(report['anomalies'])})")
    for a in report["anomalies"]:
        lines.append(f"- {a}")
    lines.append("")
    lines.append(f"持仓浮动盈亏合计: **{total_pnl:,.0f}** 元")
    return "\n".join(lines)


def write_report(report_root: str, report: Dict[str, Any]) -> Dict[str, str]:
    os.makedirs(report_root, exist_ok=True)
    date = report["date"]
    json_path = os.path.join(report_root, f"daily_{date}.json")
    md_path = os.path.join(report_root, f"daily_{date}.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(report))
    return {"json": json_path, "md": md_path}
