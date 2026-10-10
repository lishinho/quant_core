"""轻量日志：统一格式，按 lane 标注，便于日跑排查。"""
from __future__ import annotations

import logging
import sys

_CONFIGURED = set()


def get_logger(name: str = "quant_core") -> logging.Logger:
    logger = logging.getLogger(name)
    if name not in _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _CONFIGURED.add(name)
    return logger
