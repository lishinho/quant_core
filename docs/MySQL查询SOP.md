# quant_core MySQL 查询 SOP

> 目标：看完这份文档，你能用 **5 条以内 SQL** 回答"昨天跑了什么、明天买什么、成交了没、持仓多少、为什么没信号"。
>
> 适用环境：本机 MySQL（brew 安装），库名 `quant_core`，共 **5 张表**。

---

## 0. 一分钟连接

### 命令行

```bash
# 默认 root 无密码；进入交互式客户端后提示符变为 mysql>
mysql -u root quant_core

# 不进交互、直接执行一条查询（脚本里推荐 -e）
mysql -u root quant_core -e "SELECT COUNT(*) FROM summary;"

# 输出带表格边框，更适合截图
mysql -u root quant_core --table -e "SELECT * FROM plans WHERE status='PENDING';"
```

连接参数（与日跑程序共用同一套环境变量，默认值如下）：

| 参数 | 值 |
|---|---|
| Host | 127.0.0.1 |
| Port | 3306 |
| User | root |
| Password | 空（本机开发） |
| Database | quant_core |

### GUI 工具（任选其一）

TablePlus（推荐，macOS 体验好）/ DBeaver（免费）/ Navicat，新建 MySQL 连接，填上表参数即可。

> ⚠️ 日常查询只用**只读**方式，不要在业务库里执行 `UPDATE/DELETE/DDL`。库表是日跑程序按账本镜像维护的，手改会在下一次日跑被覆盖，或造成对账不一致。

---

## 1. 五张表速览（先记住这张图）

```
T 日盘后日跑
   │
   ├── candidates   当日入选的候选股（即使没买也记录，含评分）
   ├── plans        T+1 交易计划（状态机：PENDING → FILLED / CANCELLED）
   │       │
   │       └── T+1 开盘撮合
   │              └── fills    成交流水（一笔计划至多一笔成交）
   │
   ├── positions    盘后持仓快照（每天一行/股，历史可回看）
   └── summary      每个 lane 每天一行的汇总（先看它！）
```

| 表 | 一句话 | 唯一键 | 数据特点 |
|---|---|---|---|
| `summary` | **每日体检表，排障入口** | `(lane, date)` | 每天每 lane 一行 |
| `candidates` | 当日候选股与评分 | `(lane, date, code)` | 只追加，不随成交变化 |
| `plans` | 买卖计划及状态 | `plan_key` | **生命周期表**，状态会更新 |
| `fills` | 实际成交 | `plan_key` | 只追加流水 |
| `positions` | 每日盘后持仓快照 | `(lane, date, code)` | 历史快照，可画仓位变化 |

### lane 名称对照

| lane | 策略 |
|---|---|
| dividend_v3 | 红利低波 V3（缩量回踩） |
| dividend_v5 | 红利低波 V5（布林修复） |
| dividend_v7 | 红利低波 V7（十标的网格） |
| dividend_v8_1 / v8_2 | 银行股超跌 V8.1 / 质量优先 V8.2 |
| dividend_v9 | ML 增强（带大盘门控） |
| lhb_v3x | 龙头涨停 |

### 高频列含义

- `plans.status`：`PENDING` 待成交 / `FILLED` 已成交 / `CANCELLED` 已撤销
- `plans.gen_date` 计划生成日（T 日）；`plans.trade_date` 计划执行日（T+1）
- `summary.status`：`ok` 正常 / `market_gate_closed` V9 大盘门控关闭（非故障）/ `critical_abort` 关键数据缺失 / `panic_no_new` LHB 恐慌不开新仓
- `summary.equity` 总权益（现金+持仓市值）、`drawdown` 回撤（负数，如 -0.0588 = -5.88%）
- 每张表都有 `fields` JSON 列：扩展字段都放里面，不用改表结构

---

## 2. 日常 SOP

### SOP-1 昨天/某天跑得怎么样？（每天早上看这一条）

```sql
SELECT lane,
       status,
       candidate_count  AS 候选数,
       new_plan_count   AS 新增计划,
       fill_count       AS 当日成交,
       position_count   AS 持仓数,
       equity           AS 总权益,
       drawdown         AS 回撤
FROM summary
WHERE date = '2026-10-09'
ORDER BY lane;
```

判断要点：
- 7 行齐全 = 7 个 lane 都跑了；缺行 = 那个 lane 没跑或崩了（看日跑日志）
- `status = ok` 即正常；`market_gate_closed` 是 V9 的**设计行为**（指数低于 MA60 时主动空仓）
- 候选/计划为 0 但 `status=ok` → 有数据、没信号，正常

### SOP-2 明天开盘要买/卖什么？（最常用 ★）

```sql
SELECT lane,
       code,
       name,
       side,                       -- BUY 买入 / SELL 卖出
       trade_date,                 -- 执行日
       qty,
       price,                      -- 计划参考价（开盘按真实开盘价成交）
       tag,
       JSON_EXTRACT(fields, '$.score') AS score,
       reason
FROM plans
WHERE status = 'PENDING'
  AND trade_date = '2026-10-12'    -- 换成下一交易日
ORDER BY lane, side, score DESC;
```

看每个 lane 各有几张待执行计划：

```sql
SELECT lane, side, COUNT(*) AS n
FROM plans
WHERE status = 'PENDING' AND trade_date = '2026-10-12'
GROUP BY lane, side;
```

### SOP-3 今天实际成交了什么？

```sql
SELECT lane,
       trade_date,
       code,
       side,
       qty,
       price,
       amount,
       fee
FROM fills
WHERE trade_date = '2026-10-12'
ORDER BY trade_date, lane;
```

### SOP-4 当前持有哪些股票？

positions 是每日快照，取**最新一天**即可：

```sql
SELECT p.lane, p.code, p.name, p.qty,
       p.avg_cost     AS 成本价,
       p.last_price   AS 最新价,
       p.market_value AS 市值,
       p.pnl          AS 浮动盈亏,
       p.pnl_pct      AS 盈亏比例
FROM positions p
JOIN (SELECT lane, MAX(date) AS d FROM positions GROUP BY lane) m
  ON p.lane = m.lane AND p.date = m.d
ORDER BY p.lane, p.market_value DESC;
```

### SOP-5 候选股与评分明细（为什么选它？）

```sql
SELECT code, name, score,
       JSON_EXTRACT(fields, '$.dim_scores')     AS 六维分,
       JSON_EXTRACT(fields, '$.reasons')        AS 入选理由,
       JSON_EXTRACT(fields, '$.extra')          AS 附加信息
FROM candidates
WHERE lane = 'dividend_v3' AND date = '2026-10-09'
ORDER BY score DESC
LIMIT 20;
```

LHB 候选的题材/连板数在 `extra` 里：

```sql
SELECT code, name, score,
       JSON_EXTRACT(fields, '$.extra.streak')  AS 连板数,
       JSON_EXTRACT(fields, '$.extra.themes')  AS 题材
FROM candidates
WHERE lane = 'lhb_v3x'
ORDER BY date DESC, score DESC;
```

### SOP-6 一条计划的完整生命周期

```sql
-- 计划 → 成交 串联（plan_key 关联，不用 JOIN 也能各自查）
SELECT plan_key, code, name, side, gen_date, trade_date,
       status, qty, price, reason
FROM plans
WHERE code = '600825'
ORDER BY gen_date DESC;

SELECT * FROM fills WHERE plan_key = '128d7ab66879f838';
```

状态解读：
- 只有 `plans` 一行 PENDING、没有 fills → 等次日成交
- `plans.status = FILLED` 且有 fills → 已成交
- `plans.status = CANCELLED` → 看 `reason`（资金不足 / 盘前龙虎榜偏空撤销等）

### SOP-7 权益曲线 / 回撤

```sql
SELECT date, lane, equity, drawdown, position_count
FROM summary
WHERE lane = 'lhb_v3x'
ORDER BY date;
```

导出到 Excel/GUI 工具里直接画线即可（`date` 是真正的 DATE 类型，可直接做时间轴）。

### SOP-8 没信号 / 怀疑数据有问题时怎么查

**第一步：看 lane 状态与异常字段**

```sql
SELECT lane, status,
       JSON_UNQUOTE(JSON_EXTRACT(fields, '$.run_meta.provider'))           AS 数据源,
       JSON_LENGTH(JSON_EXTRACT(fields, '$.run_meta.data_stats.errors'))   AS 错误数,
       JSON_LENGTH(JSON_EXTRACT(fields, '$.run_meta.data_stats.degraded')) AS 降级接口数,
       JSON_EXTRACT(fields, '$.anomalies')                                 AS 异常信息
FROM summary
WHERE date = '2026-10-09'
ORDER BY lane;
```

- `错误数 = 0`、`降级接口` 为空 → 数据完整，0 信号是策略条件没触发（市场没给机会）
- `errors` 非空或 `status = critical_abort` → 数据层问题，看 `anomalies` 与日跑日志

**第二步：LHB 情绪环境（涨停/炸板/跌停）**

```sql
SELECT JSON_EXTRACT(fields, '$.anomalies') AS 情绪
FROM summary
WHERE lane = 'lhb_v3x' AND date = '2026-10-09';
-- 例：["情绪: NORMAL（涨停69/炸板14@17%/跌停8），计划买入数 3"]
```

**第三步：V9 门控是否关闭**

```sql
SELECT lane, status FROM summary
WHERE lane = 'dividend_v9' AND date = '2026-10-09';
-- market_gate_closed = 沪深300 在 MA60 下方，设计上空仓，不是故障
```

---

## 3. `fields` JSON 查询速查

`fields` 是真正的 MySQL JSON 列，可用 `->` / `->>` 直接取值：

| 写法 | 含义 |
|---|---|
| `JSON_EXTRACT(fields, '$.score')` | 取字段，带引号（值是 JSON） |
| `fields->>'$.score'` | 取字段并去引号（等价 `JSON_UNQUOTE(JSON_EXTRACT(...))`） |
| `fields->'$.extra.streak'` | 取嵌套字段 |
| `JSON_EXTRACT(fields, '$.reasons[0]')` | 取数组第一个元素 |
| `JSON_KEYS(fields)` | 看这一行 fields 里有哪些键 |

不知道一行里有什么时，先 `SELECT JSON_KEYS(fields) FROM ... LIMIT 1;`。

各表 fields 常见键：

| 表 | fields 中的典型键 |
|---|---|
| candidates | `dim_scores`（各维度分）、`reasons`（入选理由）、`extra`（策略附加信息） |
| plans | `score`、`layer`、`fill`（成交回填）等非固定列 |
| positions | `available_qty`、`high_since_buy`、`layers`、`meta` |
| summary | `run_meta`（数据源/调用统计）、`anomalies`（异常说明） |

---

## 4. 标准排障决策树

```
日跑结束 / 想确认结果
        │
        ▼
 SOP-1 查 summary（7 行齐不齐？status？）
   ├── 缺行 ───────────────► 日跑进程报错，看控制台日志/日报
   ├── critical_abort ─────► SOP-8 查 errors/anomalies，数据层故障
   ├── market_gate_closed ─► V9 设计行为（大盘低于 MA60），无需处理
   └── ok 但没计划
          ├── 想知道明天操作 ──► SOP-2 查 PENDING（有单即正常，等成交）
          ├── 想知道为何没信号 ► SOP-8 查 errors=0？再查 candidates
          │                       candidates 有行=分数不够没成交
          │                       candidates 无行=技术形态未触发
          └── 成交日核对 ───────► SOP-3 fills + SOP-6 计划状态
```

**对账原则**：本地账本（`data/state/<lane>/`）是事实源，MySQL 是镜像。
重跑、换库、历史漏写都会自动补齐（upsert 幂等，重复执行不产生重复行）。
发现 MySQL 与账本不一致，重跑一次对应 lane 即可：

```bash
python -m quant_core.cli --lanes lhb_v3x --force
```

---

## 5. 注意事项

1. **只读查询**：业务库不要手工 `UPDATE/DELETE/INSERT`，日跑会以账本为准覆盖。
2. **日期格式**：`'2026-10-09'` 字符串（DATE 列直接比较，无需转换函数）。
3. **金额精度**：`DECIMAL` 列直接看即可，程序侧已避免浮点误差；自己计算时用 `CAST` 或在 Excel 里处理。
4. **plans/fills 查全量要记得过滤**：它们是生命周期表，会持续累积，查询尽量带 `trade_date` 或 `lane`。
5. **唯一键冲突不是错误**：程序刻意用 `INSERT ... ON DUPLICATE KEY UPDATE` 做幂等，同键重写为更新。
6. **建表/迁移脚本**：`scripts/schema.sql`（全新 v3）、`scripts/migration_v2.sql`、`scripts/migration_v3.sql`（均可重复执行）。
