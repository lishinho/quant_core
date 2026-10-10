# quant_core 数据层与存储改造方案

> 状态说明：该方案对应代码已实现并通过验证（上一阶段直接落地，未先出方案）。
> 本文档为事后整理的正式方案，供评审确认。**如有设计点不认可，将按确认后的调整方案修改或回滚对应代码。**

## 1. 背景与目标

系统范围：主板 lane 共 7 条 —— 红利低波 V3 / V5 / V7 / V8.1 / V8.2 / V9 + 龙头涨停 LHB_V3X（不含 ETF 与宏观评分板）。

本次改造解决两个问题：

1. **行情获取稳定性**：akshare 高层封装易被数据源反爬（实测东财曾对整站限流断连本机 IP）。参考 TradingAgents-CN 的做法，改为浏览器 UA + Referer 直连数据源 HTTP 接口，并建立多源容错链。
2. **结果存储**：飞书 Base 依赖外部凭证与网络、写入慢。改为本地 MySQL 幂等写入，重跑不产生重复行。

## 2. 数据层方案：多源容错

### 2.1 个股日 K（Provider.get_bars）

四级容错链，逐级降级：

| 优先级 | 来源 | 方式 | 说明 |
|---|---|---|---|
| 1 | 新浪 | akshare `stock_zh_a_daily` | 稳定但字段少 |
| 2 | 东财 push2his | HTTP 直连 | 浏览器头 + 0.35s 同主机节流 + Session 重试 |
| 3 | 腾讯 ifzq | HTTP 直连 | 跨厂商兜底；东财封 IP 期间实证可用，数据与新浪一致 |
| 4 | 东财 | akshare 封装 | 最后兜底 |

### 2.2 指数日 K

东财直连 → 腾讯指数接口兜底（需传复权参数位，数据在 `day` 键下；末日 4317.25 与已知值吻合）。

### 2.3 实时快照与 PB

- **池内预热**：腾讯 `qt.gtimg.cn` 批量 80 只/请求。720 只股票池约 2.9 秒完成（替代原逐股百度 PB 请求，约 15 分钟）。
- **全市场快照**：东财 clist 分页 best-effort；分页完整性 <99% 时返回 None，降级到历史磁盘快照。
- PB / 总市值等字段优先取快照，不再逐股请求。

### 2.4 反爬与节流

- 浏览器 UA + Referer 请求头（TradingAgents-CN 同款做法）
- 同主机请求节流：push2his 0.35s、clist 0.5s
- requests.Session 连接级重试

### 2.5 模块结构

- `quant_core/data/eastmoney_http.py`：东财/腾讯 HTTP 层，HTTP 与解析分离，解析函数全部可离线单测
- `quant_core/data/akshare_provider.py`：Provider 多源切换
- `quant_core/data/hub.py`：磁盘缓存、快照预热、降级

## 3. 存储方案：MySQL 替代飞书 Base

### 3.1 表结构（scripts/schema.sql，可重复执行）

| 表 | 唯一业务键 | 内容 |
|---|---|---|
| candidates | key（lane+code+date） | 当日候选股 |
| plans | plan_key | 交易计划（买入/退出） |
| fills | plan_key | 成交记录 |
| positions | key（lane+code+date） | 盘后持仓快照 |
| summary | key（lane+date） | lane 日度汇总（含 candidate/plan/fill 计数） |

除业务键与常用统计列外，其余字段统一放 `fields` JSON 列。

### 3.2 写入（quant_core/integrations/mysql_sink.py）

- pymysql，`INSERT ... ON DUPLICATE KEY UPDATE` 幂等 upsert
- 连接参数全部来自环境变量：`MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB`（默认 127.0.0.1:3306，库 quant_core）

### 3.3 路由（build_sink）

- `type=mysql`：MySQL，连接失败退回本地 JSONL
- `type=feishu`：飞书 OpenAPI（保留，可随时切回）
- `type=local`：本地 JSONL
- 未指定 type 时自动探测：有 `MYSQL_DB` 优先 MySQL → 飞书凭证齐全用飞书 → 否则本地

`configs/system.json` 当前 `feishu.main.type = "mysql"`。

### 3.4 本地先行原则不变

每条 lane 仍先生成本地产物（data/artifacts），再幂等写入 MySQL；写入失败不影响本地产物与后续重跑。

## 4. 文件变更清单

新增：

- `quant_core/data/eastmoney_http.py` —— 东财/腾讯 HTTP 层
- `quant_core/integrations/mysql_sink.py` —— MySQL 幂等 sink
- `scripts/schema.sql` —— 建表脚本（5 张表）
- `tests/test_emhttp.py`（12 个离线解析用例）、`tests/test_hub_snapshot.py`、`tests/test_v9.py`
- `README.md`

修改：

- `quant_core/data/akshare_provider.py`、`quant_core/data/hub.py`、`quant_core/data/base.py`
- `quant_core/integrations/feishu_base.py`（build_sink 路由）
- `quant_core/runner.py`（summary 补 candidate/plan/fill 计数字段）
- `quant_core/strategies/div_common.py`（快照预热 + PB 接线）
- `configs/system.json`（默认 type=mysql）

## 5. 验证结果

- 全量 63 个单元测试通过，诊断零问题
- 东财封 IP 期间：腾讯兜底取个股/指数日线成功，数值与新浪一致（四级容错有效性实证）
- MySQL 端到端：LHB_V3X + V8.2 实跑写入 candidates/plans/summary；`--force` 重跑无重复行
- 720 只池快照预热 2.9 秒

## 6. 待确认项

1. MySQL 作为默认写入后端是否确认？（飞书 Base 路由代码保留，改配置即可切回）
2. 是否还需要 Excel 导出作为人工查看/备份渠道？（目前未实现）
3. 数据源优先级（新浪 → 东财直连 → 腾讯 → akshare）是否认可？
4. clist 全市场快照按 best-effort + 历史快照降级处理是否可接受？
