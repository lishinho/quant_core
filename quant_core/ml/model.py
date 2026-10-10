"""V9 模型管理：训练、持久化、过期/特征不兼容自动重训。

模型产物记录训练日期、特征结构、配置哈希与评估指标，保证可追溯。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..common import calendar
from ..common.log import get_logger
from ..common.keys import stable_key
from ..data.hub import DataHub
from .features import FeatureRow, build_rows, latest_vector
from .gbdt import GBDTRegressor

log = get_logger("quant_core.ml")
MODEL_FILE = "dividend_v9.json"


def _pearson_ic(pred: np.ndarray, y: np.ndarray) -> float:
    if len(pred) < 5 or np.std(pred) == 0 or np.std(y) == 0:
        return 0.0
    return float(np.corrcoef(pred, y)[0, 1])


def _matrix(rows: List[FeatureRow], feature_names: List[str]
            ) -> Tuple[np.ndarray, np.ndarray, List[float]]:
    X = np.array([[np.nan if v is None else v for v in r.x] for r in rows], dtype=float)
    med = np.nanmedian(X, axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    inds = np.where(np.isnan(X))
    X[inds] = np.take(med, inds[1])
    y = np.array([r.label if r.label is not None else np.nan for r in rows], dtype=float)
    return X, y, med.tolist()


def gather_dataset(hub: DataHub, codes: Dict[str, str], date: str,
                   window_days: int, feature_names: List[str],
                   horizon: int, max_rows: int = 8000) -> List[FeatureRow]:
    start = (calendar.to_date(date) - dt.timedelta(days=int(window_days * 1.55))).strftime("%Y-%m-%d")
    all_rows: List[FeatureRow] = []
    for code in codes:
        bars = hub.bars(code, start, date)
        if len(bars) < 120:
            continue
        rows = build_rows(bars, feature_names, horizon, min_index=60, code=code)
        # 时间均匀抽样，避免单票权重过大
        if len(rows) > 180:
            step = len(rows) / 180
            rows = [rows[int(k * step)] for k in range(180)]
        all_rows.extend(rows)
    all_rows = [r for r in all_rows if r.label is not None and np.isfinite(r.label)]
    # 横截面去均值（相对收益标签）
    by_date: Dict[str, List[FeatureRow]] = {}
    for r in all_rows:
        by_date.setdefault(r.date, []).append(r)
    for group in by_date.values():
        if len(group) < 2:
            continue
        mean_v = float(np.mean([r.label for r in group]))
        for r in group:
            r.label -= mean_v
    if len(all_rows) > max_rows:
        rng = np.random.default_rng(42)
        pick = rng.choice(len(all_rows), max_rows, replace=False)
        all_rows = [all_rows[i] for i in sorted(pick)]
    return all_rows


def _train_lightgbm(X: np.ndarray, y: np.ndarray, params: Dict[str, Any]):
    import lightgbm as lgb  # type: ignore
    rounds = int(params.pop("n_estimators", 200))
    dtrain = lgb.Dataset(X, label=y)
    booster = lgb.train(params, dtrain, num_boost_round=rounds)
    return "lightgbm", booster.model_to_string(), lambda X_, b=booster: b.predict(X_)


def _train_builtin(X: np.ndarray, y: np.ndarray, params: Dict[str, Any]):
    model = GBDTRegressor(
        n_estimators=int(params.get("n_estimators", 40)),
        max_depth=int(params.get("max_depth", 3)),
        lr=float(params.get("learning_rate", 0.05)),
        min_child_samples=int(params.get("min_child_samples", 30)),
        bins=int(params.get("bins", 12)),
        subsample=float(params.get("subsample", 0.8)),
        seed=int(params.get("random_state", 42)),
    )
    model.fit(X, y)
    return "builtin_gbdt", model.to_dict(), lambda X_, m=model: m.predict(X_)


def train_artifact(rows: List[FeatureRow], feature_names: List[str],
                   model_cfg: Dict[str, Any], config_hash: str, date: str) -> Dict[str, Any]:
    X, y_raw, medians = _matrix(rows, feature_names)
    valid = np.isfinite(y_raw)
    X, y = X[valid], y_raw[valid]
    if len(X) < 200:
        raise RuntimeError(f"训练样本不足: {len(X)}")
    # 时间序切分最后 10% 做评估
    cut = int(len(X) * 0.9)
    X_tr, y_tr = X[:cut], y[:cut]
    X_va, y_va = X[cut:], y[cut:]
    params = dict(model_cfg["params"])
    engine_name = model_cfg.get("engine", "lightgbm")

    if engine_name == "lightgbm":
        try:
            import lightgbm as lgb  # type: ignore
            engine_name, model_payload, _ = _train_lightgbm(X_tr, y_tr, dict(params))
            pred_va = lgb.Booster(model_string=model_payload).predict(X_va)
        except ImportError:
            log.warning("未安装 lightgbm，使用内置 GBDT 兜底训练")
            engine_name, model_payload, predict = _train_builtin(X_tr, y_tr, dict(params))
            pred_va = predict(X_va)
    else:
        engine_name, model_payload, predict = _train_builtin(X_tr, y_tr, dict(params))
        pred_va = predict(X_va)

    ic = _pearson_ic(np.asarray(pred_va), y_va)
    return {
        "version": 1,
        "engine": engine_name,
        "train_date": date,
        "features": feature_names,
        "config_hash": config_hash,
        "params": params,
        "medians": medians,
        "model": model_payload,
        "metrics": {"ic_5d": round(ic, 4), "samples": int(len(X)),
                    "eval_samples": int(len(y_va))},
    }


class MLModel:
    def __init__(self, artifact: Dict[str, Any]):
        self.artifact = artifact
        self.features = artifact["features"]
        self.medians = np.array(artifact["medians"], dtype=float)
        if artifact["engine"] == "lightgbm":
            import lightgbm as lgb  # type: ignore
            self._booster = lgb.Booster(model_string=artifact["model"])
            self._gbdt = None
        else:
            self._gbdt = GBDTRegressor.from_dict(artifact["model"])
            self._booster = None

    def predict_vectors(self, X: np.ndarray) -> np.ndarray:
        if self._booster is not None:
            return self._booster.predict(X)
        return self._gbdt.predict(X)

    def predict(self, x: List[Optional[float]]) -> float:
        arr = np.array([np.nan if v is None else v for v in x], dtype=float)
        mask = np.isnan(arr)
        arr[mask] = self.medians[mask]
        return float(self.predict_vectors(arr.reshape(1, -1))[0])


class ModelManager:
    def __init__(self, models_dir: str, hub: DataHub):
        self.dir = models_dir
        self.hub = hub
        os.makedirs(models_dir, exist_ok=True)
        self.path = os.path.join(models_dir, MODEL_FILE)

    def _load(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, artifact: Dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, ensure_ascii=False, default=str)
        os.replace(tmp, self.path)

    def is_fresh(self, artifact: Dict[str, Any], feature_names: List[str],
                 config_hash: str, date: str, max_age_days: int) -> bool:
        if artifact.get("features") != feature_names:
            log.info("模型特征结构不兼容，触发重训")
            return False
        if artifact.get("config_hash") != config_hash:
            log.info("模型配置版本变化（%s → %s），触发重训",
                     artifact.get("config_hash"), config_hash)
            return False
        age = (calendar.to_date(date) - calendar.to_date(artifact["train_date"])).days
        if age > max_age_days:
            log.info("模型已过期（%d 天），触发重训", age)
            return False
        return True

    def get_or_train(self, codes: Dict[str, str], date: str, model_cfg: Dict[str, Any],
                     config_hash: str, force: bool = False) -> MLModel:
        feature_names = model_cfg["features"]
        artifact = None if force else self._load()
        if artifact is None or not self.is_fresh(
                artifact, feature_names, config_hash, date, model_cfg["max_age_days"]):
            log.info("开始训练 V9 模型（%d 只标的，窗口 %d 日）",
                     len(codes), model_cfg["train_window_days"])
            rows = gather_dataset(
                self.hub, codes, date, model_cfg["train_window_days"],
                feature_names, model_cfg["horizon_days"],
                max_rows=model_cfg.get("max_train_samples", 8000))
            log.info("训练样本数: %d", len(rows))
            artifact = train_artifact(rows, feature_names, model_cfg, config_hash, date)
            self._save(artifact)
            log.info("模型训练完成 engine=%s IC=%s",
                     artifact["engine"], artifact["metrics"]["ic_5d"])
        return MLModel(artifact)

    def latest_vector(self, bars, feature_names: List[str]):
        return latest_vector(bars, feature_names)
