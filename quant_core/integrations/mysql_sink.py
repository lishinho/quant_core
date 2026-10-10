"""MySQL 写入 sink：用 pymysql 实现幂等 upsert。

连接参数全部来自环境变量：
  MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB

幂等策略：每张表有唯一键（candidates/positions/summary 用 `key`，
plans/fills 用 `plan_key`），用 INSERT ... ON DUPLICATE KEY UPDATE。
可变字段统一存 JSON 列 `fields`，常用检索字段（lane/date/code/score 等）
抽成独立列便于查询。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from ..common.log import get_logger
from .feishu_base import BaseSink

log = get_logger("quant_core.mysql")

# 表名 -> 唯一键列名
_KEY_COLUMN = {
    "candidates": "key",
    "plans": "plan_key",
    "fills": "plan_key",
    "positions": "key",
    "summary": "key",
}

# 表名 -> 抽成独立列的字段名（其余进 fields JSON）
_COLUMN_FIELDS = {
    "candidates": {"lane", "date", "code", "name", "score"},
    "plans": {"lane", "code", "name", "side", "trade_date", "gen_date",
              "qty", "price", "amount", "weight", "reason"},
    "fills": {"lane", "code", "side", "trade_date", "qty", "price", "amount", "fee"},
    "positions": {"lane", "date", "code", "name", "qty", "avg_cost",
                  "last_price", "market_value", "pnl", "pnl_pct"},
    "summary": {"lane", "date", "status", "equity", "cash", "position_count",
                "candidate_count", "new_plan_count", "fill_count"},
}

_NUMERIC = {"score", "qty", "price", "amount", "weight", "fee", "avg_cost",
            "last_price", "market_value", "pnl", "pnl_pct", "equity", "cash",
            "position_count", "candidate_count", "new_plan_count", "fill_count"}
_INT = {"qty", "position_count", "candidate_count", "new_plan_count", "fill_count"}


class MysqlSink(BaseSink):
    def __init__(self, host: str, port: int, user: str, password: str, db: str):
        import pymysql
        self._pymysql = pymysql
        self.conn = pymysql.connect(
            host=host, port=port, user=user, password=password, db=db,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
        self.db = db
        log.info("MySQL sink 已连接 %s@%s:%s/%s", user, host, port, db)

    @staticmethod
    def from_env() -> Optional["MysqlSink"]:
        host = os.environ.get("MYSQL_HOST", "127.0.0.1")
        port = int(os.environ.get("MYSQL_PORT", "3306"))
        user = os.environ.get("MYSQL_USER", "root")
        password = os.environ.get("MYSQL_PASSWORD", "")
        db = os.environ.get("MYSQL_DB", "quant_core")
        try:
            return MysqlSink(host, port, user, password, db)
        except Exception as err:  # noqa: BLE001
            log.warning("MySQL 连接失败，退回本地 sink: %s", err)
            return None

    def _split(self, table: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        cols = _COLUMN_FIELDS.get(table, set())
        col_vals: Dict[str, Any] = {}
        rest: Dict[str, Any] = {}
        for k, v in fields.items():
            if k in cols:
                if v is None:
                    col_vals[k] = None
                elif k in _INT:
                    try:
                        col_vals[k] = int(v)
                    except (TypeError, ValueError):
                        col_vals[k] = None
                elif k in _NUMERIC:
                    try:
                        col_vals[k] = float(v)
                    except (TypeError, ValueError):
                        col_vals[k] = None
                else:
                    col_vals[k] = str(v)[:1024]
            else:
                rest[k] = v
        col_vals["fields"] = json.dumps(rest, ensure_ascii=False, default=str) if rest else None
        return col_vals

    def upsert(self, table: str, key_field: str, key: str, fields: Dict[str, Any]) -> bool:
        key_col = _KEY_COLUMN.get(table, key_field)
        row = self._split(table, fields)
        row[key_col] = key
        columns = list(row.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        update_clause = ", ".join(f"{c}=VALUES({c})" for c in columns if c != key_col)
        sql = (f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) "
               f"VALUES ({placeholders}) "
               f"ON DUPLICATE KEY UPDATE {update_clause}")
        values = [row[c] for c in columns]
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, values)
            return True
        except Exception as err:  # noqa: BLE001
            log.warning("MySQL upsert %s 失败: %s", table, err)
            return False

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
