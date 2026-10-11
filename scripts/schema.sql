-- quant_core 数据库建表脚本（v2 规范化结构）
-- 用法: mysql -u root < scripts/schema.sql
--
-- 规范说明：
--   1. 业务唯一性由复合唯一键表达：(lane, date, code) / (lane, date) / plan_key，
--      不额外冗余拼接字符串键；
--   2. 金额/价格统一 DECIMAL（避免浮点误差），评分为 DOUBLE；
--   3. 业务键列 NOT NULL，表/列均有中文注释，索引命名 uk_/idx_ 前缀；
--   4. 除常用检索/统计列外，其余可变字段统一存 fields JSON。

CREATE DATABASE IF NOT EXISTS quant_core
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE quant_core;

-- ---------------------------------------------------------------------------
-- 当日候选股：每条 lane 每个交易日每只股票至多一行
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidates (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  lane        VARCHAR(32)  NOT NULL COMMENT '策略 lane 名，如 dividend_v7',
  `date`      DATE         NOT NULL COMMENT '信号交易日(T日)',
  code        VARCHAR(16)  NOT NULL COMMENT '证券代码',
  name        VARCHAR(64)           DEFAULT NULL COMMENT '证券名称',
  score       DOUBLE                DEFAULT NULL COMMENT '综合评分',
  fields      JSON                  DEFAULT NULL COMMENT '其余可变字段（维度评分/理由等）',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_lane_date_code (lane, `date`, code),
  KEY idx_date_code (`date`, code),
  KEY idx_code (code),
  KEY idx_score (score)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='策略候选股日快照';

-- ---------------------------------------------------------------------------
-- 交易计划：盘后生成、次日开盘执行；plan_key 由账本生成，全局幂等
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plans (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  plan_key    VARCHAR(128) NOT NULL COMMENT '计划业务键（账本生成，全局唯一）',
  lane        VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  code        VARCHAR(16)  NOT NULL COMMENT '证券代码',
  name        VARCHAR(64)           DEFAULT NULL COMMENT '证券名称',
  side        VARCHAR(8)            DEFAULT NULL COMMENT '买卖方向 BUY/SELL',
  trade_date  DATE                  DEFAULT NULL COMMENT '计划成交日(T+1)',
  gen_date    DATE                  DEFAULT NULL COMMENT '计划生成日(T日)',
  qty         INT                   DEFAULT NULL COMMENT '计划数量(股)',
  price       DECIMAL(12,4)         DEFAULT NULL COMMENT '计划/限价价格',
  amount      DECIMAL(16,2)         DEFAULT NULL COMMENT '计划金额',
  weight      DECIMAL(10,4)         DEFAULT NULL COMMENT '目标仓位权重',
  reason      VARCHAR(255)          DEFAULT NULL COMMENT '计划原因',
  tag         VARCHAR(32)           DEFAULT NULL COMMENT '计划标签，如 TP_HALF',
  status      VARCHAR(16)  NOT NULL DEFAULT 'PENDING' COMMENT '计划状态 PENDING/FILLED/CANCELLED',
  fields      JSON                  DEFAULT NULL COMMENT '其余可变字段',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_plan_key (plan_key),
  KEY idx_lane_gen_date (lane, gen_date),
  KEY idx_status_trade (status, trade_date),
  KEY idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='T+1 交易计划（账本生命周期镜像）';

-- ---------------------------------------------------------------------------
-- 成交记录：一笔计划至多一笔成交（plan_key 唯一），经 plan_key 关联 plans
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fills (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  plan_key    VARCHAR(128) NOT NULL COMMENT '对应计划业务键',
  lane        VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  code        VARCHAR(16)  NOT NULL COMMENT '证券代码',
  side        VARCHAR(8)            DEFAULT NULL COMMENT '买卖方向 BUY/SELL',
  trade_date  DATE                  DEFAULT NULL COMMENT '成交日期',
  qty         INT                   DEFAULT NULL COMMENT '成交数量(股)',
  price       DECIMAL(12,4)         DEFAULT NULL COMMENT '成交价格',
  amount      DECIMAL(16,2)         DEFAULT NULL COMMENT '成交金额',
  fee         DECIMAL(12,2)         DEFAULT NULL COMMENT '手续费',
  fields      JSON                  DEFAULT NULL COMMENT '其余可变字段',
  created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_plan_key (plan_key),
  KEY idx_lane_date (lane, trade_date),
  KEY idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='模拟成交记录';

-- ---------------------------------------------------------------------------
-- 盘后持仓快照：每条 lane 每个交易日每只持仓股票一行
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  lane          VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  `date`        DATE         NOT NULL COMMENT '持仓快照日期',
  code          VARCHAR(16)  NOT NULL COMMENT '证券代码',
  name          VARCHAR(64)           DEFAULT NULL COMMENT '证券名称',
  qty           INT                   DEFAULT NULL COMMENT '持仓数量(股)',
  avg_cost      DECIMAL(12,4)         DEFAULT NULL COMMENT '持仓均价',
  last_price    DECIMAL(12,4)         DEFAULT NULL COMMENT '最新价',
  market_value  DECIMAL(16,2)         DEFAULT NULL COMMENT '最新市值',
  pnl           DECIMAL(16,2)         DEFAULT NULL COMMENT '浮动盈亏金额',
  pnl_pct       DECIMAL(10,4)         DEFAULT NULL COMMENT '浮动盈亏比例',
  fields        JSON                  DEFAULT NULL COMMENT '其余可变字段',
  created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_lane_date_code (lane, `date`, code),
  KEY idx_date_code (`date`, code),
  KEY idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='盘后持仓快照';

-- ---------------------------------------------------------------------------
-- lane 日度汇总：每条 lane 每个交易日一行
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS summary (
  id               BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  lane             VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  `date`           DATE         NOT NULL COMMENT '汇总日期',
  status           VARCHAR(32)           DEFAULT NULL COMMENT '运行状态 ok/error/critical_abort',
  strategy         VARCHAR(32)           DEFAULT NULL COMMENT '策略标识',
  strategy_name    VARCHAR(64)           DEFAULT NULL COMMENT '策略名称',
  equity           DECIMAL(16,2)         DEFAULT NULL COMMENT '总权益',
  cash             DECIMAL(16,2)         DEFAULT NULL COMMENT '可用现金',
  equity_peak      DECIMAL(16,2)         DEFAULT NULL COMMENT '权益峰值',
  drawdown         DECIMAL(10,4)         DEFAULT NULL COMMENT '回撤比例',
  position_count   INT                   DEFAULT NULL COMMENT '持仓数量',
  candidate_count  INT                   DEFAULT NULL COMMENT '候选股数量',
  new_plan_count   INT                   DEFAULT NULL COMMENT '新计划数量(含退出)',
  fill_count       INT                   DEFAULT NULL COMMENT '成交数量',
  fields           JSON                  DEFAULT NULL COMMENT '其余可变字段（run_meta/anomalies 等）',
  created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_lane_date (lane, `date`),
  KEY idx_date (`date`),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='lane 日度运行汇总';
