"""配置管理：加载 JSON 配置，记录内容哈希，保证历史结果可追溯。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict


def canonical_json(obj: Any) -> str:
    """规范化序列化，使相同配置得到相同哈希。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def config_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:16]


@dataclass
class ConfigBundle:
    """一次运行使用的完整配置（系统配置 + lane 配置）。"""

    system: Dict[str, Any]
    lane: Dict[str, Any]
    lane_name: str
    system_hash: str
    lane_hash: str

    @property
    def runtime(self) -> Dict[str, Any]:
        return self.system["runtime"]

    @property
    def paths(self) -> Dict[str, Any]:
        return self.system["paths"]


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_system_config(path: str = "configs/system.json") -> Dict[str, Any]:
    return _read_json(path)


def load_lane_config(system: Dict[str, Any], lane_name: str) -> ConfigBundle:
    if lane_name not in system["lanes"]:
        raise KeyError(f"未知 lane: {lane_name}")
    lane_meta = system["lanes"][lane_name]
    lane_cfg_path = lane_meta["config"]
    if not os.path.exists(lane_cfg_path):
        raise FileNotFoundError(f"lane 配置不存在: {lane_cfg_path}")
    lane_cfg = _read_json(lane_cfg_path)
    return ConfigBundle(
        system=system,
        lane=lane_cfg,
        lane_name=lane_name,
        system_hash=config_hash(system),
        lane_hash=config_hash(lane_cfg),
    )


def config_summary(bundle: ConfigBundle) -> Dict[str, Any]:
    """写入 run_meta 的配置摘要。"""
    return {
        "lane": bundle.lane_name,
        "strategy": bundle.lane.get("strategy"),
        "system_hash": bundle.system_hash,
        "lane_hash": bundle.lane_hash,
        "system_config": bundle.system.get("lanes", {}).get(bundle.lane_name, {}),
        "lane_config": bundle.lane,
    }
