"""飞书多维表格写入：真实 OpenAPI 幂等 upsert，无凭证时自动使用本地 JSONL sink。

幂等策略：以业务稳定键（plan_key / candidate 键等）作为主字段，
写入前按键检索记录：存在则 update，不存在则 create。
重复执行日跑不会产生重复行。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..common.log import get_logger

log = get_logger("quant_core.feishu")
BASE_URL = "https://open.feishu.cn/open-apis"


class BaseSink(ABC):
    @abstractmethod
    def upsert(self, table: str, key_field: str, key: str, fields: Dict[str, Any]) -> bool: ...


class LocalJsonlSink(BaseSink):
    """离线/无凭证环境：按 表+键 幂等写入本地 JSONL，供人工导入或审计。"""

    def __init__(self, root: str, route: str):
        self.dir = os.path.join(root, route)
        os.makedirs(self.dir, exist_ok=True)
        self.route = route

    def _path(self, table: str) -> str:
        return os.path.join(self.dir, f"{table}.jsonl")

    def upsert(self, table: str, key_field: str, key: str, fields: Dict[str, Any]) -> bool:
        path = self._path(table)
        records = []
        found = False
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get(key_field) == key:
                        rec = {key_field: key, **fields}
                        found = True
                    records.append(rec)
        if not found:
            records.append({key_field: key, **fields})
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, path)
        return True


class FeishuBaseSink(BaseSink):
    """飞书 OpenAPI 实现（urllib，无需 requests 依赖）。"""

    def __init__(self, app_id: str, app_secret: str, app_token: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self._token: Optional[str] = None
        self._token_expire_at = 0.0

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                 retry: int = 2) -> Dict[str, Any]:
        url = BASE_URL + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if not path.endswith("tenant_access_token/internal"):
            headers["Authorization"] = f"Bearer {self._get_token()}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "ignore")
            if err.code in (429, 500, 502, 503) and retry > 0:
                time.sleep(1.0)
                return self._request(method, path, body, retry - 1)
            raise RuntimeError(f"飞书 API HTTP {err.code}: {text[:200]}")
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"飞书 API 错误 code={payload.get('code')}: {payload.get('msg')}")
        return payload.get("data", {})

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        data = self._request("POST", "/auth/v3/tenant_access_token/internal", {
            "app_id": self.app_id, "app_secret": self.app_secret,
        })
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + data.get("expire", 7200)
        return self._token

    def _find_record(self, table: str, key_field: str, key: str) -> Optional[str]:
        path = (f"/bitable/v1/apps/{self.app_token}/tables/{table}"
                f"/records/search?page_size=1")
        data = self._request("POST", path, {
            "filter": {
                "conjunction": "and",
                "conditions": [{"field_name": key_field, "operator": "is", "value": [str(key)]}],
            }
        })
        items = data.get("items", [])
        return items[0]["record_id"] if items else None

    def upsert(self, table: str, key_field: str, key: str, fields: Dict[str, Any]) -> bool:
        record_id = self._find_record(table, key_field, key)
        if record_id:
            self._request(
                "PUT",
                f"/bitable/v1/apps/{self.app_token}/tables/{table}/records/{record_id}",
                {"fields": fields},
            )
        else:
            self._request(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table}/records",
                {"fields": {key_field: str(key), **fields}},
            )
        return True


def build_sink(route_cfg: Dict[str, Any], local_root: str, route: str) -> BaseSink:
    """按 route 配置决定写入后端。

    type=mysql  -> MySQLSink（环境变量 MYSQL_HOST/PORT/USER/PASSWORD/DB）
    type=feishu -> FeishuBaseSink（需配置飞书凭证环境变量）
    type=local  -> LocalJsonlSink
    未指定 type 时：有 MySQL 环境变量优先 MySQL，其次飞书，最后本地。
    """
    sink_type = route_cfg.get("type", "")
    if sink_type == "mysql":
        from .mysql_sink import MysqlSink
        sink = MysqlSink.from_env()
        if sink is not None:
            return sink
        log.warning("MySQL sink 不可用，退回本地 jsonl")
        return LocalJsonlSink(local_root, route)
    if sink_type == "local":
        return LocalJsonlSink(local_root, route)

    # 自动探测：优先 MySQL（若已配置），其次飞书，最后本地
    mysql_db = os.environ.get("MYSQL_DB")
    if mysql_db:
        from .mysql_sink import MysqlSink
        sink = MysqlSink.from_env()
        if sink is not None:
            log.info("检测到 MySQL 配置，使用 MySQL sink")
            return sink

    app_id = os.environ.get(route_cfg.get("app_id_env", ""), "")
    app_secret = os.environ.get(route_cfg.get("app_secret_env", ""), "")
    app_token = os.environ.get(route_cfg.get("app_token_env", ""), "")
    if app_id and app_secret and app_token:
        try:
            return FeishuBaseSink(app_id, app_secret, app_token)
        except Exception as err:  # noqa: BLE001
            log.warning("飞书 sink 初始化失败，退回本地: %s", err)
    return LocalJsonlSink(local_root, route)
