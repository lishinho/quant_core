# quant_core

面向 A 股模拟交易与策略验证的量化系统。基于 lane 隔离 + 本地产物 + 飞书 Base 的设计：

- 每套策略独立计算候选、交易信号和持仓
- 运行先生成本地 JSON，作为恢复与审计依据
- 结果再幂等写入飞书 Base，供长期追踪
- 日报汇总各策略的信号、持仓、收益与异常

## 1. 功能范围

复刻主板 7 套正式策略；ETF 与宏观评分板不在本仓库范围。

| Lane | 策略 | 定位 | 说明 |
|---|---|---|---|
| `dividend_v3` | 缩量回踩 | 正式 | 红利低波+沪深300+中证500 合并池，缩量回踩 20 日均线后介入 |
| `dividend_v5` | 布林带上穿 | 正式 | 布林下轨弱势区向上修复，结合基本面/估值/资金/事件评分 |
| `dividend_v7` | 布林网格 | 正式 | 10 只优质标的，跌破下轨三层分批建仓，分段止盈 |
| `dividend_v8_1` | 银行高分超跌 | 正式 | 银行板块质量前 20% → 跌破布林下轨，最多 8 仓 |
| `dividend_v8_2` | 银行质量优先 | 正式 | 先超跌再卡质量中位数，四维排序取前一半 |
| `dividend_v9` | ML 混合排序 | 正式 | LightGBM（缺失时内置 GBDT 兜底）预测 5 日相对收益，模型 60% + 规则 40% |
| `lhb_v3x` | 龙头涨停 | 正式 | 涨停池选股 + 次日盘前龙虎榜补丁 + 盘后巡检止盈止损 |

红利低波策略共享六维评分（基本面/行业/估值动量/资金/事件/宏观）。

## 2. 快速开始

### 环境

- Python 3.9+（核心仅依赖标准库 + `numpy`）
- 可选：`akshare`（真实 A 股行情）、`lightgbm`（V9 可选，未装自动用内置 GBDT）、`requests`（飞书调用，默认走标准库 urllib）

```bash
pip install -r requirements.txt
```

### 运行

```bash
# 全部 lane 跑最近交易日（auto 数据源：akshare 不可用自动退回 mock）
python -m quant_core.cli

# 指定日期与 lane
python -m quant_core.cli --date 2026-10-09 --lanes dividend_v7,lhb_v3x

# 离线 mock 数据强制重跑
python -m quant_core.cli --provider mock --force

# 次日盘前执行 LHB 龙虎榜补丁
python -m quant_core.cli --premarket --date 2026-10-09 --lanes lhb_v3x

# 只重新生成日报（复用已有产物）
python -m quant_core.cli --report-only --date 2026-10-09
```

`auto` 模式下系统会先做 akshare 健康检查，失败则自动退回确定性 Mock 数据。

## 3. 数据层（多源容错）

数据 Provider 抽象在 `quant_core/data/base.py`，真实实现 `AkshareProvider` 对每个接口做多源切换，非关键数据失败由 `DataHub` 退回历史缓存或中性分。

| 数据 | 主源 | 兜底源 |
|---|---|---|
| 个股日线（前复权） | 新浪 stock_zh_a_daily | 东财 push2his 直连 → 腾讯 web.ifzq.gtimg.cn → akshare 东财 |
| 指数日线 | 新浪 stock_zh_index_daily | 腾讯 ifzq 指数日线 |
| 当日 PB / PE_TTM / 市值 | 腾讯 qt.gtimg.cn 批量（80 只/请求，按池预热按日缓存） | 百度 stock_zh_valuation_baidu（逐股） |
| PE 历史分位 | 百度 stock_zh_valuation_baidu | 中性分 |
| 分红 | 新浪 stock_history_dividend_detail（每 10 股口径） | — |
| 指数成分 | 中证 index_stock_cons | — |
| 银行成分 | 申万 index_component_sw | 内置银行名单 |
| 涨停/炸板/跌停池 | 东财 push2ex | — |
| 龙虎榜 | 东财 stock_lhb_detail_em | — |
| 全市场快照 | 东财 clist 分页直连 | 上一交易日快照 / 池内腾讯批量 |

直连模块在 [eastmoney_http.py](quant_core/data/eastmoney_http.py)：浏览器 UA + Referer、同主机请求节流、Session 连接级重试、断连冷却续拉。`clist` 全市场分页做完整性校验，不完整快照绝不返回。

## 4. 交易与持仓模型

统一的模拟交易周期：

```
T 日盘后生成买入/卖出计划 → 下一交易日开盘模拟成交 → 当日盘后更新持仓和浮动收益
```

- 每笔成交带稳定唯一键，重复执行不重复成交
- T+1 冻结、整百股取整、佣金 0.03%（最低 5 元）
- 停牌顺延；资金不足时等比例减仓
- 账本 `data/state/<lane>/ledger.json` 持久化持仓与成交

## 5. 目录结构

```
quant_core/
├── common/         # 日志、配置、交易日历、稳定键、模型、缓存、重试
├── data/           # Provider 抽象、akshare 多源实现、Mock、东财/腾讯直连、股票池、Hub
├── analysis/       # 指标库（均线/布林/RSI/MACD/分位/回撤）
├── trading/        # 计划、引擎、账本
├── integrations/   # 飞书 Base（OpenAPI + 本地 jsonl 兜底）
├── reporting/      # 日报聚合与渲染
├── strategies/     # 红利低波公共 + V3/V5/V7/V8.1/V8.2/V9 + LHB_V3X
├── ml/             # V9 特征工程、LightGBM/内置 GBDT、模型缓存
├── runner.py       # lane 编排（执行成交→持仓→退出→信号→产物→飞书）
└── cli.py          # 命令行入口
configs/            # system.json + 7 个 lane 配置
tests/              # 单元 + 端到端测试
data/               # state/artifacts/cache/reports/models（已 gitignore）
```

## 6. 运行产物

```
data/
├── artifacts/<lane>/<date>/   # candidates/new_plans/positions/summary/executed_fills/DONE.json
├── state/<lane>/ledger.json   # 持仓与成交账本
├── cache/                     # 行情/成分/分红/估值/快照按日缓存
├── models/                    # V9 模型（含训练日期、特征结构、配置哈希）
└── reports/daily_<date>.md    # 日报
```

恢复优先级：本地产物 → 状态账本 → 外部 sink（MySQL/飞书） → 历史缓存。`--force` 强制重跑时仍靠成交账本防重复成交。

## 7. 数据写入

默认写入 **MySQL**（`configs/system.json` 的 `feishu.main.type = "mysql"`）。

### MySQL

```bash
# macOS 安装并启动
brew install mysql
brew services start mysql

# 创建库表（全新部署，当前 v3）
mysql -u root < scripts/schema.sql
# 已部署旧表结构的环境：按顺序在线迁移（脚本均可重复执行）
mysql -u root quant_core < scripts/migration_v2.sql
mysql -u root quant_core < scripts/migration_v3.sql
```

连接参数通过环境变量传入（默认连本地 3306、root 无密码、库 `quant_core`）：

```bash
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_USER=root
export MYSQL_PASSWORD=
export MYSQL_DB=quant_core
```

库表结构 v3（仅 5 张表；幂等写入，复合业务唯一键，`INSERT ... ON DUPLICATE KEY UPDATE`；
金额/价格为 DECIMAL，可变字段统一进 `fields` JSON，便于扩展）：

| 表 | 唯一键 | 粒度 | 主要列 |
|---|---|---|---|
| `candidates` | `(lane, date, code)` | 每日候选 | lane, date, code, name, score, fields(JSON) |
| `plans` | `plan_key` | 计划生命周期 | lane, code, name, side, trade_date, gen_date, qty, price, weight, reason, tag, **status**, fields(JSON) |
| `fills` | `plan_key` | 成交流水 | lane, code, side, trade_date, qty, price, amount, fee, fields(JSON) |
| `positions` | `(lane, date, code)` | 每日持仓快照 | lane, date, code, name, qty, avg_cost, last_price, market_value, pnl, pnl_pct, fields(JSON) |
| `summary` | `(lane, date)` | lane 日汇总 | lane, date, status, strategy, equity, cash, drawdown, position_count, candidate_count, new_plan_count, fill_count, fields(JSON) |

同步语义：`candidates`/`positions`/`summary` 按日切片只写当日；
`plans`/`fills` 是生命周期表，日跑时按**账本全量镜像**（含历史 PENDING/FILLED/CANCELLED），
漏写、换库、重跑都能补齐且不产生重复行。

> 日常怎么查库（连接、看图、排障步骤与常用 SQL）见 [docs/MySQL查询SOP.md](docs/MySQL查询SOP.md)。

### 其他写入后端

`build_sink` 按 `feishu.<route>.type` 分发：

- `mysql`：MySQLSink（需 `pymysql`）
- `feishu`：飞书多维表格 OpenAPI（需配置 `FEISHU_APP_ID/SECRET/MAIN_APP_TOKEN`）
- `local`：本地 JSONL（`data/feishu/<route>/<table>.jsonl`）
- 未指定：有 `MYSQL_DB` 环境变量优先 MySQL，其次飞书，最后本地

安装依赖：

```bash
pip install pymysql        # MySQL sink
# akshare / lightgbm 为可选，见 requirements.txt
```

## 8. 测试

```bash
python -m unittest discover -s tests -v
```

覆盖指标库、引擎幂等/T+1/资金不足减仓、V7 三层网格与五种退出、LHB 情绪分级与龙虎榜补丁三路径、V8.2 关键数据熔断与除权日排除、V9 训练-闸门-混合排序、多日 T→T+1 端到端、resume/force 不重复成交、东财/腾讯 HTTP 解析与 Hub 快照预热。

## 9. 故障恢复

- 单个外部数据源失败不影响其他子系统运行
- sink 写入失败不阻断日跑（本地产物已完整落盘）
- 非关键增强因子缺失退中性分；决定策略有效性的关键数据缺失则停止该 lane 新信号
- 恢复时优先复用已完成 lane 的产物，只重跑失败模块，避免重复成交
