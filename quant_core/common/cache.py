"""按日期的数据缓存，支持历史缓存降级。"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional


class DiskCache:
    """cache/<namespace>/<key>.json，key 通常含日期与标的。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, namespace: str, key: str) -> str:
        folder = os.path.join(self.root, namespace)
        os.makedirs(folder, exist_ok=True)
        safe = key.replace("/", "_").replace(":", "_")
        return os.path.join(folder, f"{safe}.json")

    def get(self, namespace: str, key: str) -> Optional[Any]:
        path = self._path(namespace, key)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def set(self, namespace: str, key: str, value: Any, date_tag: Optional[str] = None) -> None:
        payload = {"date_tag": date_tag, "value": value}
        path = self._path(namespace, key)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
        os.replace(tmp, path)

    def latest_before(self, namespace: str, prefix: str, date_tag: str) -> Optional[Any]:
        """在历史缓存中找 date_tag 严格早于指定日期的最新一份，用于降级。"""
        folder = os.path.join(self.root, namespace)
        if not os.path.isdir(folder):
            return None
        safe_prefix = prefix.replace("/", "_").replace(":", "_")
        candidates: List[str] = []
        for fname in os.listdir(folder):
            if not fname.endswith(".json") or not fname.startswith(safe_prefix):
                continue
            path = os.path.join(folder, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            tag = payload.get("date_tag")
            if tag and tag < date_tag:
                candidates.append((tag, path))
        if not candidates:
            return None
        candidates.sort()
        with open(candidates[-1][1], "r", encoding="utf-8") as fh:
            return json.load(fh).get("value")
