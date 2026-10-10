"""稳定唯一键：重复执行日跑不会重复成交、重复写入。"""
from __future__ import annotations

import hashlib


def stable_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def plan_key(lane: str, code: str, gen_date: str, side: str, tag: str = "") -> str:
    """交易计划稳定键：同 lane/标的/生成日/方向/标签 只允许一份计划。"""
    return stable_key(lane, code, gen_date, side, tag)


def fill_key(plan_id: str) -> str:
    return stable_key("fill", plan_id)
