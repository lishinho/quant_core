"""DataHub：Provider 之上的缓存、重试、降级与关键数据分级。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from ..common.cache import DiskCache
from ..common.log import get_logger
from ..common.retry import CriticalDataMissing, DataSourceError, with_retry
from ..common.models import Bar
from .base import DataProvider, DividendEvent, LimitUpInfo, LhbRecord, Quote
from .universe import DIVIDEND_LOW_VOL, MOCK_HS300_EXTRA, MOCK_ZZ500_EXTRA, BANK_NAMES


class DataHub:
    def __init__(self, provider: DataProvider, cache: DiskCache,
                 retries: int = 3, timeout: float = 20.0):
        self.provider = provider
        self.cache = cache
        self.retries = retries
        self.timeout = timeout
        self.log = get_logger(f"quant_core.hub.{provider.name}")
        self.stats: Dict[str, Any] = {"calls": 0, "cache_hits": 0, "degraded": [], "errors": []}
        self._snapshot_mem: Optional[Dict[str, Dict[str, Any]]] = None
        self._snapshot_date: Optional[str] = None

    # ------------------------------------------------------------- 核心包装
    def _fetch(self, namespace: str, key: str, date_tag: str, prefix: str,
               call: Callable[[], Any], serialize: Callable[[Any], Any],
               deserialize: Callable[[Any], Any]) -> Any:
        self.stats["calls"] += 1
        cached = self.cache.get(namespace, key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return deserialize(cached["value"])
        try:
            value = with_retry(call, attempts=self.retries, timeout=self.timeout, label=key)
        except DataSourceError as err:
            stale = self.cache.latest_before(namespace, prefix, "9999-99-99")
            if stale is not None:
                self.log.warning("降级使用历史缓存: %s (%s)", key, type(err).__name__)
                self.stats["degraded"].append(key)
                return deserialize(stale)
            self.stats["errors"].append(f"{key}: {err}")
            self.log.error("数据获取失败且无缓存: %s: %s", key, err)
            return None
        self.cache.set(namespace, key, serialize(value), date_tag=date_tag)
        return value

    @staticmethod
    def require(value: Any, label: str) -> Any:
        """关键数据缺失时停止该 lane 的新信号生成。"""
        if value is None or (isinstance(value, (list, dict)) and len(value) == 0):
            raise CriticalDataMissing(f"关键数据缺失: {label}")
        return value

    # ----------------------------------------------------------------- 行情
    def bars(self, code: str, start: str, end: str, adjust: str = "qfq") -> List[Bar]:
        value = self._fetch(
            "bars", f"{code}:{start}:{end}:{adjust}", end, code,
            lambda: self.provider.get_bars(code, start, end, adjust),
            lambda v: [b.to_dict() for b in v],
            lambda v: [Bar.from_dict(b) for b in v],
        )
        return value or []

    def index_bars(self, code: str, start: str, end: str) -> List[Bar]:
        value = self._fetch(
            "idx_bars", f"{code}:{start}:{end}", end, code,
            lambda: self.provider.get_index_bars(code, start, end),
            lambda v: [b.to_dict() for b in v],
            lambda v: [Bar.from_dict(b) for b in v],
        )
        return value or []

    def spot(self, date_tag: str) -> Dict[str, Quote]:
        value = self._fetch(
            "spot", date_tag, date_tag, date_tag,
            lambda: self.provider.get_spot(),
            lambda v: {k: q.to_dict() for k, q in v.items()},
            lambda v: {k: Quote(**q) for k, q in v.items()},
        )
        return value or {}

    def _read_snapshot(self, date_tag: str) -> Dict[str, Dict[str, Any]]:
        """只读进程内/磁盘快照，不发起任何外部请求。"""
        if self._snapshot_mem is not None and self._snapshot_date == date_tag:
            return self._snapshot_mem
        cached = self.cache.get("snapshot", date_tag)
        if cached is not None:
            self._snapshot_mem, self._snapshot_date = cached["value"], date_tag
            return self._snapshot_mem
        return {}

    def market_snapshot(self, date_tag: str) -> Dict[str, Dict[str, Any]]:
        """全市场当日快照（PE/PB/市值/量额），进程内 + 磁盘按日缓存。

        冷缓存时做一次全市场拉取（clist，分页较多）；失败退回上一交易日缓存，
        仍无则返回空字典，由调用方退回逐股估值接口/中性分。
        """
        current = self._read_snapshot(date_tag)
        if current:
            return current
        try:
            value = with_retry(self.provider.get_market_snapshot,
                               attempts=self.retries, timeout=max(self.timeout, 90.0),
                               label="market_snapshot")
        except DataSourceError as err:
            self.log.warning("全市场快照失败: %s", err)
            value = {}
        if value:
            self.cache.set("snapshot", date_tag, value, date_tag=date_tag)
        else:
            # 空结果不覆写缓存，尝试最近一个交易日的快照降级
            stale = self.cache.latest_before("snapshot", "", "9999-99-99")
            if stale:
                value = stale
                self.stats["degraded"].append("market_snapshot")
                self.log.warning("使用历史全市场快照降级")
        self._snapshot_mem, self._snapshot_date = value or {}, date_tag
        return self._snapshot_mem

    def prefetch_snapshot(self, codes: List[str], date_tag: str) -> Dict[str, Dict[str, Any]]:
        """按股票池批量预热当日快照（腾讯接口 80 只/请求），与磁盘缓存合并。

        只读缓存 + 池内批量补点，不触发全市场分页拉取。
        非关键数据：失败静默，上层逐股估值接口/中性分照常兜底。
        """
        current = dict(self._read_snapshot(date_tag))
        missing = list(dict.fromkeys(c for c in codes if c not in current))
        if not missing:
            self._snapshot_mem, self._snapshot_date = current, date_tag
            return current
        try:
            fetched = with_retry(lambda: self.provider.get_quotes(missing),
                                 attempts=2, timeout=60.0, label="quotes_batch") or {}
        except DataSourceError as err:
            self.log.warning("批量快照预热失败: %s", err)
            fetched = {}
        if fetched:
            current.update(fetched)
            self.cache.set("snapshot", date_tag, current, date_tag=date_tag)
        self._snapshot_mem, self._snapshot_date = current, date_tag
        return current

    # ----------------------------------------------------------------- 成分
    def index_constituents(self, index_code: str, date_tag: str) -> List[Tuple[str, str]]:
        value = self._fetch(
            "cons", f"idx_{index_code}", date_tag, f"idx_{index_code}",
            lambda: self.provider.get_index_constituents(index_code),
            lambda v: [list(p) for p in v],
            lambda v: [tuple(p) for p in v],
        )
        return value or []

    def industry_constituents(self, sw_code: str, date_tag: str) -> List[Tuple[str, str]]:
        value = self._fetch(
            "cons", f"sw_{sw_code}", date_tag, f"sw_{sw_code}",
            lambda: self.provider.get_industry_constituents(sw_code),
            lambda v: [list(p) for p in v],
            lambda v: [tuple(p) for p in v],
        )
        # 真实源失败时使用静态兜底（银行名单）
        if not value and sw_code == "801780":
            value = list(BANK_NAMES.items())
            self.log.warning("行业成分接口失败，使用内置银行兜底名单（%d 只）", len(value))
        return value or []

    # ------------------------------------------------------------- 分红/估值
    def dividends(self, code: str, date_tag: str) -> List[DividendEvent]:
        value = self._fetch(
            "div", code, date_tag, code,
            lambda: self.provider.get_dividends(code),
            lambda v: [e.to_dict() for e in v],
            lambda v: [DividendEvent(**e) for e in v],
        )
        return value or []

    def valuation(self, code: str, indicator: str, date_tag: str) -> List[Tuple[str, float]]:
        value = self._fetch(
            "val", f"{code}:{indicator}", date_tag, f"{code}:{indicator}",
            lambda: self.provider.get_valuation(code, indicator),
            lambda v: [list(p) for p in v],
            lambda v: [tuple(p) for p in v],
        )
        return value or []

    # ----------------------------------------------------------- 涨停/龙虎榜
    def _zt_fetch(self, kind: str, date: str, call_name: str) -> List[LimitUpInfo]:
        value = self._fetch(
            f"zt_{kind}", date, date, date,
            lambda: getattr(self.provider, call_name)(date),
            lambda v: [i.to_dict() for i in v],
            lambda v: [LimitUpInfo(**i) for i in v],
        )
        return value or []

    def limit_up_pool(self, date: str) -> List[LimitUpInfo]:
        return self._zt_fetch("up", date, "get_limit_up_pool")

    def broken_board_pool(self, date: str) -> List[LimitUpInfo]:
        return self._zt_fetch("broken", date, "get_broken_board_pool")

    def limit_down_pool(self, date: str) -> List[LimitUpInfo]:
        return self._zt_fetch("down", date, "get_limit_down_pool")

    def lhb(self, start: str, end: str) -> List[LhbRecord]:
        value = self._fetch(
            "lhb", f"{start}_{end}", end, start,
            lambda: self.provider.get_lhb(start, end),
            lambda v: [r.to_dict() for r in v],
            lambda v: [LhbRecord(**r) for r in v],
        )
        return value or []

    # ----------------------------------------------------------------- 股票池
    def universe(self, sources: List[str], date_tag: str,
                 fixed: Optional[List[str]] = None) -> Dict[str, str]:
        """合并多个来源为 {code: name}。

        支持：hs300 / zz500 / dividend_low_vol / dividend_extended /
              sw:801780 / fixed_universe（传入 fixed）
        """
        result: Dict[str, str] = {}
        static_map = dict(DIVIDEND_LOW_VOL)
        static_map.update(dict(MOCK_HS300_EXTRA))
        static_map.update(dict(MOCK_ZZ500_EXTRA))
        for src in sources:
            if src in ("dividend_low_vol", "dividend_extended"):
                for code, name in DIVIDEND_LOW_VOL:
                    result.setdefault(code, name)
            if src == "dividend_extended":
                for code, name in MOCK_HS300_EXTRA + MOCK_ZZ500_EXTRA:
                    result.setdefault(code, name)
            elif src == "hs300":
                for code, name in self.index_constituents("000300", date_tag):
                    result.setdefault(code, name)
            elif src == "zz500":
                for code, name in self.index_constituents("000905", date_tag):
                    result.setdefault(code, name)
            elif src.startswith("sw:"):
                for code, name in self.industry_constituents(src[3:], date_tag):
                    result.setdefault(code, name)
        if fixed:
            for code in fixed:
                result.setdefault(code, static_map.get(code, code))
        return result


def create_provider(setting: str) -> DataProvider:
    """按配置创建 Provider；auto 模式下真实源不可用自动退回 mock。"""
    log = get_logger("quant_core.hub")
    if setting == "mock":
        return _build_mock()
    if setting == "akshare":
        from .akshare_provider import AkshareProvider
        return AkshareProvider()
    # auto
    try:
        from .akshare_provider import AkshareProvider
        from ..common.retry import call_with_timeout
        provider = AkshareProvider()
        ok = call_with_timeout(provider.health_check, 15.0, "akshare health")
        if ok:
            log.info("使用真实数据源 akshare")
            return provider
        log.warning("akshare 健康检查失败，退回 mock 数据源")
    except Exception as err:  # noqa: BLE001
        log.warning("akshare 不可用（%s），退回 mock 数据源", type(err).__name__)
    return _build_mock()


def _build_mock() -> DataProvider:
    from .mock_provider import MockProvider
    get_logger("quant_core.hub").info("使用 Mock 数据源（离线确定性数据）")
    return MockProvider()
