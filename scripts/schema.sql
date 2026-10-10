-- quant_core 数据库建表脚本
-- 用法: mysql -u root < scripts/schema.sql

CREATE DATABASE IF NOT EXISTS quant_core
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE quant_core;

CREATE TABLE IF NOT EXISTS candidates (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  `key` VARCHAR(128) NOT NULL UNIQUE,
  lane VARCHAR(32) NOT NULL,
  date DATE NOT NULL,
  code VARCHAR(16) NOT NULL,
  name VARCHAR(64),
  score DOUBLE,
  dim_scores JSON,
  reasons JSON,
  fields JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_lane_date (lane, date),
  INDEX idx_code (code)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS plans (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  plan_key VARCHAR(128) NOT NULL UNIQUE,
  lane VARCHAR(32) NOT NULL,
  code VARCHAR(16) NOT NULL,
  name VARCHAR(64),
  side VARCHAR(8),
  trade_date DATE,
  gen_date DATE,
  qty INT,
  price DOUBLE,
  amount DOUBLE,
  weight DOUBLE,
  reason VARCHAR(255),
  fields JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_lane (lane),
  INDEX idx_code (code),
  INDEX idx_trade_date (trade_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS fills (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  plan_key VARCHAR(128) NOT NULL,
  lane VARCHAR(32) NOT NULL,
  code VARCHAR(16) NOT NULL,
  side VARCHAR(8),
  trade_date DATE,
  qty INT,
  price DOUBLE,
  amount DOUBLE,
  fee DOUBLE,
  fields JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_plan_key (plan_key),
  INDEX idx_lane_date (lane, trade_date),
  INDEX idx_code (code)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS positions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  `key` VARCHAR(128) NOT NULL UNIQUE,
  lane VARCHAR(32) NOT NULL,
  date DATE NOT NULL,
  code VARCHAR(16) NOT NULL,
  name VARCHAR(64),
  qty INT,
  avg_cost DOUBLE,
  last_price DOUBLE,
  market_value DOUBLE,
  pnl DOUBLE,
  pnl_pct DOUBLE,
  fields JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_lane_date (lane, date),
  INDEX idx_code (code)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS summary (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  `key` VARCHAR(128) NOT NULL UNIQUE,
  lane VARCHAR(32) NOT NULL,
  date DATE NOT NULL,
  status VARCHAR(32),
  equity DOUBLE,
  cash DOUBLE,
  position_count INT,
  candidate_count INT,
  new_plan_count INT,
  fill_count INT,
  fields JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_lane_date (lane, date)
) ENGINE=InnoDB;
