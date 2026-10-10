"""外部调用的超时、重试与异常分级。"""
from __future__ import annotations

import concurrent.futures
import time
from typing import Callable, TypeVar

from .log import get_logger

T = TypeVar("T")
log = get_logger("quant_core.data")


class DataSourceError(RuntimeError):
    """数据源异常（经重试后仍失败）。"""


class CriticalDataMissing(RuntimeError):
    """决定策略有效性的关键数据缺失：lane 必须停止生成新信号。"""


def call_with_timeout(fn: Callable[[], T], timeout: float, label: str = "") -> T:
    """在线程中执行阻塞调用，保证超时可控（akshare 内部不总是接受 timeout）。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise DataSourceError(f"调用超时({timeout}s): {label}")


def with_retry(
    fn: Callable[[], T],
    attempts: int = 3,
    timeout: float = 20.0,
    base_delay: float = 0.8,
    label: str = "",
) -> T:
    """指数退避重试。所有尝试失败后抛出最后一个异常。"""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            return call_with_timeout(fn, timeout, label)
        except Exception as err:  # noqa: BLE001 - 外部接口异常类型多样
            last_err = err
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                log.warning("数据调用失败 %s 第%d/%d次: %s；%.1fs 后重试",
                            label, i + 1, attempts, type(err).__name__, delay)
                time.sleep(delay)
    raise DataSourceError(f"重试{attempts}次仍失败: {label}: {last_err}")
