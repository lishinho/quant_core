-- quant_core v2 -> v3 在线迁移脚本（可重复执行）
-- 用法: mysql -u root quant_core < scripts/migration_v3.sql
--
-- 变更内容：
--   plans 表新增 status 独立列（PENDING/FILLED/CANCELLED），
--   便于直接阅读与筛选"次日待执行计划"；
--   idx_trade_date 升级为 (status, trade_date) 复合索引；
--   历史行的 status 从 fields JSON 中回填，缺失则置 PENDING。
--
-- 注意：含存储过程与 DELIMITER 语法，请使用 mysql 命令行客户端执行。

USE quant_core;

DROP PROCEDURE IF EXISTS qc_drop_index_if_exists;
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

-- plans.status -----------------------------------------------------------------

CALL qc_add_column_if_exists('plans', 'status',
  "status VARCHAR(16) NOT NULL DEFAULT 'PENDING' COMMENT '计划状态 PENDING/FILLED/CANCELLED' AFTER tag");

-- 历史行从 fields JSON 回填真实状态（v2 时 status 存在 fields 里）
UPDATE plans
SET status = JSON_UNQUOTE(JSON_EXTRACT(fields, '$.status'))
WHERE fields IS NOT NULL
  AND JSON_EXTRACT(fields, '$.status') IS NOT NULL
  AND status = 'PENDING';

CALL qc_drop_index_if_exists('plans', 'idx_trade_date');
CALL qc_add_index_if_exists('plans', 'idx_status_trade', 0, 'status, trade_date');

ALTER TABLE plans
  COMMENT = 'T+1 交易计划（账本生命周期镜像）';

-- 清理工具 ---------------------------------------------------------------------

DROP PROCEDURE IF EXISTS qc_drop_index_if_exists;
DROP PROCEDURE IF EXISTS qc_add_index_if_exists;
DROP PROCEDURE IF EXISTS qc_add_column_if_exists;
