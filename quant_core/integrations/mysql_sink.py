"""MySQL 写入 sink：pymysql 实现幂等 upsert。

连接参数全部来自环境变量：
  MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB

幂等策略（v2 规范化表结构）：
  - candidates / positions：复合唯一键 (lane, date, code)
  - summary：复合唯一键 (lane, date)
  - plans / fills：业务键 plan_key（fills 经其关联 plans）
  统一用 INSERT ... ON DUPLICATE KEY UPDATE，重跑不产生重复行。
候选/持仓/汇总三表不再写入冗余拼接键，业务键列直接来自 fields；
plans/fills 仍由调用方传入 plan_key。

金额/价格列在库中为 DECIMAL，常用检索字段抽成独立列，
其余可变字段统一存 JSON 列 `fields`。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from ..common.log import get_logger
from .feishu_base import BaseSink

log = get_logger("quant_core.mysql")

# 仅 plans/fills 需要把调用方传入的业务键写入独立列；
# candidates/positions/summary 的唯一性由 (lane,date,code)/(lane,date) 复合键承担
_KEY_COLUMN = {
    "plans": "plan_key",
    "fills": "plan_key",
}

# 表名 -> 抽成独立列的字段名（其余进 fields JSON）
_COLUMN_FIELDS = {
    "candidates": {"lane", "date", "code", "name", "score"},
    "plans": {"lane", "code", "name", "side", "trade_date", "gen_date",
              "qty", "price", "amount", "weight", "reason", "tag", "status"},
    "fills": {"lane", "code", "side", "trade_date", "qty", "price",
              "amount", "fee"},
    "positions": {"lane", "date", "code", "name", "qty", "avg_cost",
                  "last_price", "market_value", "pnl", "pnl_pct"},
    "summary": {"lane", "date", "status", "strategy", "strategy_name",
                "equity", "cash", "equity_peak", "drawdown", "position_count",
                "candidate_count", "new_plan_count", "fill_count"},
}

# 表名 -> 模型字段名到列名的别名（模型命名与读模型列名不一致时映射）
_FIELD_ALIASES = {
    "plans": {"price_hint": "price"},  # TradePlan.price_hint -> plans.price
    "fills": {"date": "trade_date"},   # Fill.date -> fills.trade_date
}

# DATE 列：空字符串归一化为 NULL，避免严格模式下写入 zero date
_DATE_COLUMNS = {"date", "trade_date", "gen_date"}

_NUMERIC = {"score", "qty", "price", "amount", "weight", "fee", "avg_cost",
            "last_price", "market_value", "pnl", "pnl_pct", "equity", "cash",
            "equity_peak", "drawdown",
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
        aliases = _FIELD_ALIASES.get(table, {})
        col_vals: Dict[str, Any] = {}
        rest: Dict[str, Any] = {}
        for k, v in fields.items():
            col = aliases.get(k, k)
            if col in cols:
                if v is None or (col in _DATE_COLUMNS and v == ""):
                    col_vals[col] = None
                elif col in _INT:
                    try:
                        col_vals[col] = int(v)
                    except (TypeError, ValueError):
                        col_vals[col] = None
                elif col in _NUMERIC:
                    try:
                        col_vals[col] = float(v)
                    except (TypeError, ValueError):
                        col_vals[col] = None
                else:
                    col_vals[col] = str(v)[:1024]
            else:
                rest[k] = self._embed_json(v)
        col_vals["fields"] = json.dumps(rest, ensure_ascii=False, default=str) if rest else None
        return col_vals

    @staticmethod
    def _embed_json(v: Any) -> Any:
        """runner 会把 dict/list 预序列化成 JSON 字符串；落 JSON 列时还原为
        原生对象，避免 fields 内出现双重编码、无法直接 JSON_EXTRACT。"""
        if isinstance(v, str):
            s = v.strip()
            if s[:1] in ("{", "["):
                try:
                    return json.loads(s)
                except ValueError:
                    return v
        return v

    def upsert(self, table: str, key_field: str, key: str, fields: Dict[str, Any]) -> bool:
        row = self._split(table, fields)
        key_col = _KEY_COLUMN.get(table)
        if key_col:
            row[key_col] = key
        columns = list(row.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        update_clause = ", ".join(f"{c}=VALUES({c})" for c in columns
                                  if c != key_col)
        sql = (f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) "
               f"VALUES ({placeholders}) "
               f"ON DUPLICATE KEY UPDATE {update_clause}")
        values = [row[c] for c in columns]
        try:
            # 长连接可能被 wait_timeout 断开，写入前自动重连
            self.conn.ping(reconnect=True)
            with self.conn.cursor() as cur:
                cur.execute(sql, values)
            return True
        except Exception as err:  # noqa: BLE001 - 单行失败隔离，不影响同 lane 其他行
            log.warning("MySQL upsert %s 失败: %s", table, err)
            return False

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
