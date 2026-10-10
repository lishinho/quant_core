"""本地产物管理：lane 隔离、按日归档，是恢复与审计的第一优先级。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


class ArtifactStore:
    """ artifacts/<lane>/<date>/ 下的 JSON 产物。 """

    def __init__(self, root: str, lane: str, date: str):
        self.lane = lane
        self.date = date
        self.dir = os.path.join(root, lane, date)
        os.makedirs(self.dir, exist_ok=True)

    def path(self, name: str) -> str:
        if not name.endswith(".json"):
            name += ".json"
        return os.path.join(self.dir, name)

    def write(self, name: str, obj: Any) -> str:
        path = self.path(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
        return path

    def exists(self, name: str) -> bool:
        return os.path.exists(self.path(name))

    def read(self, name: str) -> Optional[Any]:
        path = self.path(name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def files(self) -> List[str]:
        if not os.path.isdir(self.dir):
            return []
        return sorted(f for f in os.listdir(self.dir) if f.endswith(".json"))

    def mark_done(self) -> None:
        self.write("DONE", {"lane": self.lane, "date": self.date})

    @property
    def done(self) -> bool:
        return self.exists("DONE")


def list_run_dates(root: str, lane: str) -> List[str]:
    lane_dir = os.path.join(root, lane)
    if not os.path.isdir(lane_dir):
        return []
    return sorted(d for d in os.listdir(lane_dir) if os.path.isdir(os.path.join(lane_dir, d)))


def load_latest_summary(root: str, lane: str, on_or_before: Optional[str] = None) -> Optional[Dict[str, Any]]:
    dates = list_run_dates(root, lane)
    if on_or_before:
        dates = [d for d in dates if d <= on_or_before]
    if not dates:
        return None
    for date in reversed(dates):
        store = ArtifactStore(root, lane, date)
        summary = store.read("summary")
        if summary is not None:
            return summary
    return None
