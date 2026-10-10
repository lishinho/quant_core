"""零依赖的轻量 GBDT 回归（numpy 向量化分箱分裂），作为 LightGBM 的兜底引擎。

实现平方损失、分位数分箱、行/列采样、深度受限回归树。
模型可序列化为纯 JSON（分箱边界 + 嵌套树）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

Tree = Any  # ("leaf", value) 或 ("node", f, t, left, right)


class GBDTRegressor:
    def __init__(self, n_estimators: int = 40, max_depth: int = 3, lr: float = 0.05,
                 min_child_samples: int = 30, bins: int = 12, subsample: float = 0.8,
                 seed: int = 42):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.lr = lr
        self.min_child = min_child_samples
        self.bins = bins
        self.subsample = subsample
        self.seed = seed
        self.edges: List[np.ndarray] = []
        self.bin_ids: Optional[np.ndarray] = None
        self.base: float = 0.0
        self.trees: List[Tree] = []

    def _binarize(self, X: np.ndarray, fit: bool) -> np.ndarray:
        n, fnum = X.shape
        if fit:
            self.edges = []
            for j in range(fnum):
                col = X[:, j]
                valid = col[~np.isnan(col)]
                if len(valid) <= self.bins:
                    edges = np.unique(valid)[:-1] if len(valid) > 1 else np.array([])
                else:
                    qs = np.linspace(0, 1, self.bins + 1)[1:-1]
                    edges = np.unique(np.quantile(valid, qs))
                self.edges.append(edges)
        out = np.zeros((n, fnum), dtype=np.int32)
        for j in range(fnum):
            col = X[:, j]
            ids = np.searchsorted(self.edges[j], np.nan_to_num(col, nan=np.inf), side="right")
            out[:, j] = ids
        return out

    def _build_tree(self, idx: np.ndarray, residual: np.ndarray, depth: int,
                    rng: np.random.Generator) -> Tree:
        if depth >= self.max_depth or len(idx) < max(2 * self.min_child, 4):
            return ("leaf", float(np.mean(residual[idx])) if len(idx) else 0.0)
        n_rows = len(idx)
        fnum = self.bin_ids.shape[1]
        max_bin = self.bin_ids.max() + 1
        gain_g = np.zeros((fnum, max_bin))
        gain_n = np.zeros((fnum, max_bin))
        sub_bins = self.bin_ids[idx]
        sub_res = residual[idx]
        for j in range(fnum):
            g = np.bincount(sub_bins[:, j], weights=sub_res, minlength=max_bin)
            n_ = np.bincount(sub_bins[:, j], minlength=max_bin)
            gain_g[j] = g
            gain_n[j] = n_
        cum_g = np.cumsum(gain_g, axis=1)
        cum_n = np.cumsum(gain_n, axis=1)
        total_g = cum_g[:, -1:]
        total_n = cum_n[:, -1:]
        with np.errstate(divide="ignore", invalid="ignore"):
            score = (cum_g ** 2 / np.maximum(cum_n, 1)
                     + (total_g - cum_g) ** 2 / np.maximum(total_n - cum_n, 1))
        valid = (cum_n >= self.min_child) & (total_n - cum_n >= self.min_child)
        score = np.where(valid, score, -np.inf)
        if not np.isfinite(score).any():
            return ("leaf", float(np.mean(sub_res)))
        best = int(np.argmax(score))
        f, t = divmod(best, max_bin)
        left_mask = sub_bins[:, f] <= t
        left = idx[left_mask]
        right = idx[~left_mask]
        if len(left) == 0 or len(right) == 0:
            return ("leaf", float(np.mean(sub_res)))
        return ("node", int(f), int(t),
                self._build_tree(left, residual, depth + 1, rng),
                self._build_tree(right, residual, depth + 1, rng))

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        rng = np.random.default_rng(self.seed)
        self.bin_ids = self._binarize(X, fit=True)
        self.base = float(np.mean(y))
        residual = y - self.base
        n = len(y)
        for _ in range(self.n_estimators):
            size = max(200, int(n * self.subsample))
            sample = rng.choice(n, size=min(n, size), replace=False)
            tree = self._build_tree(sample, residual, 0, rng)
            self.trees.append(tree)
            pred_delta = self._predict_trees(X, [tree])
            residual -= self.lr * pred_delta

    def _predict_tree(self, ids: np.ndarray, tree: Tree) -> np.ndarray:
        kind = tree[0]
        if kind == "leaf":
            return np.full(len(ids), tree[1])
        _, f, t, left, right = tree
        out = np.zeros(len(ids))
        mask = ids[:, f] <= t
        out[mask] = self._predict_tree(ids[mask], left)
        out[~mask] = self._predict_tree(ids[~mask], right)
        return out

    def _predict_trees(self, X: np.ndarray, trees: List[Tree]) -> np.ndarray:
        ids = self._binarize(X, fit=False)
        return np.sum([self._predict_tree(ids, t) for t in trees], axis=0)

    def predict(self, X: np.ndarray) -> np.ndarray:
        if not self.trees:
            return np.full(len(X), self.base)
        return self.base + self.lr * self._predict_trees(X, self.trees)

    # ------------------------------------------------------------- 序列化
    def to_dict(self) -> Dict[str, Any]:
        return {"base": self.base, "lr": self.lr,
                "edges": [e.tolist() for e in self.edges],
                "trees": self.trees}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GBDTRegressor":
        m = cls()
        m.base = d["base"]
        m.lr = d["lr"]
        m.edges = [np.array(e) for e in d["edges"]]
        m.trees = d["trees"]
        return m
