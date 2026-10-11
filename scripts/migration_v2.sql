-- quant_core v1 -> v2 在线迁移脚本（可重复执行）
-- 用法: mysql -u root quant_core < scripts/migration_v2.sql
--   或: mysql -u root < scripts/migration_v2.sql
--
-- 变更内容：
--   1. candidates/positions/summary 删除冗余拼接列 `key`，
--      唯一约束改为复合唯一键 (lane,date,code) / (lane,date)；
--   2. 价格/金额 DOUBLE -> DECIMAL，业务键列补 NOT NULL 与注释；
--   3. plans 新增 tag 列、索引 lane+gen_date；删除被覆盖的冗余索引；
--   4. summary 新增 strategy/strategy_name/equity_peak/drawdown 独立列；
--   5. candidates 旧 dim_scores/reasons 独立列历史上从未写入（数据在 fields JSON），
--      直接删除。
--
-- 注意：本脚本含存储过程与 DELIMITER 语法，请使用 mysql 命令行客户端执行。

USE quant_core;

-- 通用幂等工具 -----------------------------------------------------------------

DROP PROCEDURE IF EXISTS qc_drop_index_if_exists;
DROP PROCEDURE IF EXISTS qc_drop_column_if_exists;
DROP PROCEDURE IF EXISTS qc_add_index_if_exists;
DROP PROCEDURE IF EXISTS qc_add_column_if_exists;

DELIMITER //

CREATE PROCEDURE qc_drop_index_if_exists(IN p_table VARCHAR(64), IN p_index VARCHAR(64))
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.statistics
             WHERE table_schema = DATABASE()
               AND table_name = p_table AND index_name = p_index) THEN
    SET @sql = CONCAT('ALTER TABLE `', p_table, '` DROP INDEX `', p_index, '`');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
  END IF;
END//

CREATE PROCEDURE qc_drop_column_if_exists(IN p_table VARCHAR(64), IN p_column VARCHAR(64))
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = DATABASE()
               AND table_name = p_table AND column_name = p_column) THEN
    SET @sql = CONCAT('ALTER TABLE `', p_table, '` DROP COLUMN `', p_column, '`');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
  END IF;
END//

CREATE PROCEDURE qc_add_index_if_exists(IN p_table VARCHAR(64), IN p_index VARCHAR(64),
                                        IN p_unique TINYINT, IN p_cols VARCHAR(255))
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.statistics
                 WHERE table_schema = DATABASE()
                   AND table_name = p_table AND index_name = p_index) THEN
    SET @sql = CONCAT('ALTER TABLE `', p_table, '` ADD ',
                      IF(p_unique = 1, 'UNIQUE KEY ', 'KEY '),
                      '`', p_index, '` (', p_cols, ')');
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
  END IF;
END//

CREATE PROCEDURE qc_add_column_if_exists(IN p_table VARCHAR(64), IN p_column VARCHAR(64),
                                         IN p_ddl VARCHAR(255))
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = DATABASE()
                   AND table_name = p_table AND column_name = p_column) THEN
    SET @sql = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN ', p_ddl);
    PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
  END IF;
END//

DELIMITER ;

-- candidates -------------------------------------------------------------------

-- 加唯一键前去重保险：同一 (lane,date,code) 保留最早一行
DELETE c1 FROM candidates c1
JOIN candidates c2
  ON c1.lane = c2.lane AND c1.`date` = c2.`date`
 AND c1.code = c2.code AND c1.id > c2.id;

CALL qc_drop_index_if_exists('candidates', 'key');
CALL qc_drop_column_if_exists('candidates', 'key');
CALL qc_drop_index_if_exists('candidates', 'idx_lane_date');
CALL qc_drop_column_if_exists('candidates', 'dim_scores');
CALL qc_drop_column_if_exists('candidates', 'reasons');
CALL qc_add_index_if_exists('candidates', 'uk_lane_date_code', 1, 'lane, `date`, code');
CALL qc_add_index_if_exists('candidates', 'idx_date_code', 0, '`date`, code');
CALL qc_add_index_if_exists('candidates', 'idx_code', 0, 'code');
CALL qc_add_index_if_exists('candidates', 'idx_score', 0, 'score');

ALTER TABLE candidates
  MODIFY COLUMN id         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  MODIFY COLUMN lane       VARCHAR(32) NOT NULL COMMENT '策略 lane 名，如 dividend_v7',
  MODIFY COLUMN `date`     DATE        NOT NULL COMMENT '信号交易日(T日)',
  MODIFY COLUMN code       VARCHAR(16) NOT NULL COMMENT '证券代码',
  MODIFY COLUMN name       VARCHAR(64) DEFAULT NULL COMMENT '证券名称',
  MODIFY COLUMN score      DOUBLE      DEFAULT NULL COMMENT '综合评分',
  MODIFY COLUMN fields     JSON        DEFAULT NULL COMMENT '其余可变字段（维度评分/理由等）',
  MODIFY COLUMN created_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  COMMENT = '策略候选股日快照';

-- plans ------------------------------------------------------------------------

CALL qc_drop_index_if_exists('plans', 'idx_lane');
CALL qc_add_index_if_exists('plans', 'idx_lane_gen_date', 0, 'lane, gen_date');
CALL qc_add_index_if_exists('plans', 'idx_trade_date', 0, 'trade_date');
CALL qc_add_index_if_exists('plans', 'idx_code', 0, 'code');
CALL qc_add_column_if_exists('plans', 'tag',
  'tag VARCHAR(32) DEFAULT NULL COMMENT ''计划标签，如 TP_HALF'' AFTER reason');

ALTER TABLE plans
  MODIFY COLUMN id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  MODIFY COLUMN plan_key   VARCHAR(128) NOT NULL COMMENT '计划业务键（账本生成，全局唯一）',
  MODIFY COLUMN lane       VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  MODIFY COLUMN code       VARCHAR(16)  NOT NULL COMMENT '证券代码',
  MODIFY COLUMN name       VARCHAR(64)  DEFAULT NULL COMMENT '证券名称',
  MODIFY COLUMN side       VARCHAR(8)   DEFAULT NULL COMMENT '买卖方向 BUY/SELL',
  MODIFY COLUMN trade_date DATE         DEFAULT NULL COMMENT '计划成交日(T+1)',
  MODIFY COLUMN gen_date   DATE         DEFAULT NULL COMMENT '计划生成日(T日)',
  MODIFY COLUMN qty        INT          DEFAULT NULL COMMENT '计划数量(股)',
  MODIFY COLUMN price      DECIMAL(12,4) DEFAULT NULL COMMENT '计划/限价价格',
  MODIFY COLUMN amount     DECIMAL(16,2) DEFAULT NULL COMMENT '计划金额',
  MODIFY COLUMN weight     DECIMAL(10,4) DEFAULT NULL COMMENT '目标仓位权重',
  MODIFY COLUMN reason     VARCHAR(255) DEFAULT NULL COMMENT '计划原因',
  MODIFY COLUMN fields     JSON         DEFAULT NULL COMMENT '其余可变字段',
  MODIFY COLUMN created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  COMMENT = 'T+1 交易计划';

-- fills ------------------------------------------------------------------------

ALTER TABLE fills
  MODIFY COLUMN id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  MODIFY COLUMN plan_key   VARCHAR(128) NOT NULL COMMENT '对应计划业务键',
  MODIFY COLUMN lane       VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  MODIFY COLUMN code       VARCHAR(16)  NOT NULL COMMENT '证券代码',
  MODIFY COLUMN side       VARCHAR(8)   DEFAULT NULL COMMENT '买卖方向 BUY/SELL',
  MODIFY COLUMN trade_date DATE         DEFAULT NULL COMMENT '成交日期',
  MODIFY COLUMN qty        INT          DEFAULT NULL COMMENT '成交数量(股)',
  MODIFY COLUMN price      DECIMAL(12,4) DEFAULT NULL COMMENT '成交价格',
  MODIFY COLUMN amount     DECIMAL(16,2) DEFAULT NULL COMMENT '成交金额',
  MODIFY COLUMN fee        DECIMAL(12,2) DEFAULT NULL COMMENT '手续费',
  MODIFY COLUMN fields     JSON         DEFAULT NULL COMMENT '其余可变字段',
  MODIFY COLUMN created_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  COMMENT = '模拟成交记录';

-- positions --------------------------------------------------------------------

DELETE p1 FROM positions p1
JOIN positions p2
  ON p1.lane = p2.lane AND p1.`date` = p2.`date`
 AND p1.code = p2.code AND p1.id > p2.id;

CALL qc_drop_index_if_exists('positions', 'key');
CALL qc_drop_column_if_exists('positions', 'key');
CALL qc_drop_index_if_exists('positions', 'idx_lane_date');
CALL qc_add_index_if_exists('positions', 'uk_lane_date_code', 1, 'lane, `date`, code');
CALL qc_add_index_if_exists('positions', 'idx_date_code', 0, '`date`, code');
CALL qc_add_index_if_exists('positions', 'idx_code', 0, 'code');

ALTER TABLE positions
  MODIFY COLUMN id           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  MODIFY COLUMN lane         VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  MODIFY COLUMN `date`       DATE         NOT NULL COMMENT '持仓快照日期',
  MODIFY COLUMN code         VARCHAR(16)  NOT NULL COMMENT '证券代码',
  MODIFY COLUMN name         VARCHAR(64)  DEFAULT NULL COMMENT '证券名称',
  MODIFY COLUMN qty          INT          DEFAULT NULL COMMENT '持仓数量(股)',
  MODIFY COLUMN avg_cost     DECIMAL(12,4) DEFAULT NULL COMMENT '持仓均价',
  MODIFY COLUMN last_price   DECIMAL(12,4) DEFAULT NULL COMMENT '最新价',
  MODIFY COLUMN market_value DECIMAL(16,2) DEFAULT NULL COMMENT '最新市值',
  MODIFY COLUMN pnl          DECIMAL(16,2) DEFAULT NULL COMMENT '浮动盈亏金额',
  MODIFY COLUMN pnl_pct      DECIMAL(10,4) DEFAULT NULL COMMENT '浮动盈亏比例',
  MODIFY COLUMN fields       JSON         DEFAULT NULL COMMENT '其余可变字段',
  MODIFY COLUMN created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  COMMENT = '盘后持仓快照';

-- summary ----------------------------------------------------------------------

DELETE s1 FROM summary s1
JOIN summary s2
  ON s1.lane = s2.lane AND s1.`date` = s2.`date` AND s1.id > s2.id;

CALL qc_drop_index_if_exists('summary', 'key');
CALL qc_drop_column_if_exists('summary', 'key');
CALL qc_drop_index_if_exists('summary', 'idx_lane_date');
CALL qc_add_index_if_exists('summary', 'uk_lane_date', 1, 'lane, `date`');
CALL qc_add_index_if_exists('summary', 'idx_date', 0, '`date`');
CALL qc_add_index_if_exists('summary', 'idx_status', 0, 'status');
CALL qc_add_column_if_exists('summary', 'strategy',
  'strategy VARCHAR(32) DEFAULT NULL COMMENT ''策略标识'' AFTER status');
CALL qc_add_column_if_exists('summary', 'strategy_name',
  'strategy_name VARCHAR(64) DEFAULT NULL COMMENT ''策略名称'' AFTER strategy');
CALL qc_add_column_if_exists('summary', 'equity_peak',
  'equity_peak DECIMAL(16,2) DEFAULT NULL COMMENT ''权益峰值'' AFTER cash');
CALL qc_add_column_if_exists('summary', 'drawdown',
  'drawdown DECIMAL(10,4) DEFAULT NULL COMMENT ''回撤比例'' AFTER equity_peak');

ALTER TABLE summary
  MODIFY COLUMN id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  MODIFY COLUMN lane            VARCHAR(32)  NOT NULL COMMENT '策略 lane 名',
  MODIFY COLUMN `date`          DATE         NOT NULL COMMENT '汇总日期',
  MODIFY COLUMN status          VARCHAR(32)  DEFAULT NULL COMMENT '运行状态 ok/error/critical_abort',
  MODIFY COLUMN equity          DECIMAL(16,2) DEFAULT NULL COMMENT '总权益',
  MODIFY COLUMN cash            DECIMAL(16,2) DEFAULT NULL COMMENT '可用现金',
  MODIFY COLUMN position_count  INT          DEFAULT NULL COMMENT '持仓数量',
  MODIFY COLUMN candidate_count INT          DEFAULT NULL COMMENT '候选股数量',
  MODIFY COLUMN new_plan_count  INT          DEFAULT NULL COMMENT '新计划数量(含退出)',
  MODIFY COLUMN fill_count      INT          DEFAULT NULL COMMENT '成交数量',
  MODIFY COLUMN fields          JSON         DEFAULT NULL COMMENT '其余可变字段（run_meta/anomalies 等）',
  MODIFY COLUMN created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN updated_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  COMMENT = 'lane 日度运行汇总';

-- 清理工具 ---------------------------------------------------------------------

DROP PROCEDURE IF EXISTS qc_drop_index_if_exists;
DROP PROCEDURE IF EXISTS qc_drop_column_if_exists;
DROP PROCEDURE IF EXISTS qc_add_index_if_exists;
DROP PROCEDURE IF EXISTS qc_add_column_if_exists;
