"""基于 akshare + 多源 HTTP 直连的真实行情 Provider。

经实测（akshare 1.18.x，2026-10，参考 TradingAgents-CN 的浏览器直连做法）：
- 个股历史日线四级容错：新浪 stock_zh_a_daily（前复权）
  → 东财 push2his 直连（浏览器 UA+Referer）
  → 腾讯 web.ifzq.gtimg.cn 直连（跨厂商独立主机，东财整站限流时兜底）
  → akshare 东财 stock_zh_a_hist。
- 指数日线：新浪 stock_zh_index_daily，失败后腾讯指数日线兜底。
- 指数成分：中证 index_stock_cons（000300/000905）。
- 行业成分：申万 index_component_sw（银行 801780）。
- 分红：新浪 stock_history_dividend_detail（派息为每10股口径）。
- 估值：PE 历史分位走百度 stock_zh_valuation_baidu；当日 PB/PE_TTM/市值
  改走腾讯 qt.gtimg.cn 批量快照（80 只/请求，由 DataHub 按池预热、按日缓存）。
- 全市场快照：东财 clist 分页直连（best-effort，含限流冷却与完整性校验）。
- 涨停/炸板/跌停池、龙虎榜：东财 push2ex 接口。

单接口异常由 DataHub 的重试/缓存降级统一处理；本类只负责多源切换与字段归一化。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from ..common.log import get_logger
from . import eastmoney_http as emh
from .base import DataProvider, DividendEvent, LimitUpInfo, LhbRecord, Quote
from ..common.models import Bar

log = get_logger("quant_core.akshare")

# 估值指标名 -> 百度接口 indicator 字符串
_VALUATION_INDICATORS = {
    "pe_ttm": "市盈率(TTM)",
    "pb": "市净率",
    "market_cap": "总市值",
}


def market_prefix(code: str) -> str:
    if code.startswith(("60", "68", "11", "13", "50", "51", "56", "58")):
        return "sh"
    if code.startswith(("00", "30", "12", "15", "16", "18")):
        return "sz"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return "sh"


def compact(value: str) -> str:
    return str(value).replace("-", "")


def _dstr(value: Any) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        try:
            return value.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            # pandas NaT 等空日期类型会在 strftime 时抛错
            return ""
    s = str(value)
    if len(s) >= 10 and "-" in s:
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "" or value != value:  # NaN 判定
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    return int(_f(value, default))


def _cell(row: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


class AkshareProvider(DataProvider):
    name = "akshare"

    def __init__(self) -> None:
        try:
            import akshare as ak  # noqa: F401
        except ImportError as err:
            raise RuntimeError("未安装 akshare，无法使用 AkshareProvider（pip install akshare）") from err
        import akshare as ak
        self.ak = ak

    # ------------------------------------------------------------------ 行情
    def _bars_from_sina(self, code: str, start: str, end: str, adjust: str) -> List[Bar]:
        symbol = market_prefix(code) + code
        df = self.ak.stock_zh_a_daily(
            symbol=symbol, start_date=compact(start), end_date=compact(end), adjust=adjust
        )
        return self._bars_from_records(df.to_dict("records"), source="sina")

    def _bars_from_eastmoney(self, code: str, start: str, end: str, adjust: str) -> List[Bar]:
        df = self.ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=compact(start), end_date=compact(end), adjust=adjust,
        )
        records = []
        for r in df.to_dict("records"):
            records.append({
                "date": _dstr(r["日期"]), "open": _f(r["开盘"]), "high": _f(r["最高"]),
                "low": _f(r["最低"]), "close": _f(r["收盘"]), "volume": _f(r["成交量"]),
                "amount": _f(r["成交额"]), "pct_chg": _f(r.get("涨跌幅")) / 100.0,
            })
        return self._finalize_bars(records)

    @staticmethod
    def _bars_from_records(records: List[Dict[str, Any]], source: str) -> List[Bar]:
        out = []
        for r in records:
            out.append({
                "date": _dstr(r["date"]), "open": _f(r["open"]), "high": _f(r["high"]),
                "low": r["low"], "close": _f(r["close"]), "volume": _f(r["volume"]),
                "amount": _f(r.get("amount")), "pct_chg": 0.0,
            })
        return AkshareProvider._finalize_bars(out)

    @staticmethod
    def _finalize_bars(rows: List[Dict[str, Any]]) -> List[Bar]:
        rows.sort(key=lambda x: x["date"])
        prev_close: Optional[float] = None
        bars: List[Bar] = []
        for row in rows:
            close = _f(row["close"])
            if prev_close and not row.get("pct_chg"):
                row["pct_chg"] = close / prev_close - 1.0
            prev_close = close if close > 0 else prev_close
            bars.append(Bar(**row))
        return bars

    def _bars_from_em_direct(self, code: str, start: str, end: str, adjust: str) -> List[Bar]:
        return emh.fetch_kline(code, start, end, adjust)

    def _bars_from_tx_direct(self, code: str, start: str, end: str, adjust: str) -> List[Bar]:
        return emh.fetch_kline_tencent(code, start, end, adjust)

    def get_bars(self, code: str, start_date: str, end_date: str, adjust: str = "qfq") -> List[Bar]:
        sources = (
            ("sina", lambda: self._bars_from_sina(code, start_date, end_date, adjust)),
            # akshare 封装的东财接口在部分网络被反爬断连；带浏览器头的直连稳定可用
            ("eastmoney_direct",
             lambda: self._bars_from_em_direct(code, start_date, end_date, adjust)),
            # 腾讯独立主机：东财整站封 IP 时的跨厂兜底
            ("tencent_direct",
             lambda: self._bars_from_tx_direct(code, start_date, end_date, adjust)),
            ("eastmoney", lambda: self._bars_from_eastmoney(code, start_date, end_date, adjust)),
        )
        last_err: Optional[Exception] = None
        for source_name, call in sources:
            try:
                bars = call()
                if bars:
                    return bars
                log.warning("行情源 %s 返回空: %s", source_name, code)
            except Exception as err:  # noqa: BLE001
                last_err = err
                log.warning("行情源 %s 获取 %s 失败: %s", source_name, code, type(err).__name__)
        if last_err:
            raise last_err
        return []

    def get_index_bars(self, code: str, start_date: str, end_date: str) -> List[Bar]:
        symbol = code if code.startswith(("sh", "sz", "bj")) else market_prefix(code) + code
        try:
            df = self.ak.stock_zh_index_daily(symbol=symbol)
            rows = []
            for r in df.to_dict("records"):
                date = _dstr(r["date"])
                if date < start_date or date > end_date:
                    continue
                rows.append({
                    "date": date, "open": _f(r["open"]), "high": _f(r["high"]),
                    "low": _f(r["low"]), "close": _f(r["close"]), "volume": _f(r["volume"]),
                    "amount": 0.0, "pct_chg": 0.0,
                })
            bars = self._finalize_bars(rows)
            if bars:
                return bars
            log.warning("指数行情源 sina 返回空: %s", symbol)
        except Exception as err:  # noqa: BLE001
            log.warning("指数行情源 sina 获取 %s 失败: %s", symbol, type(err).__name__)
        # 腾讯指数兜底（指数接口要求带复权参数位，数据在 day 键下，无实际复权差异）
        return emh.fetch_kline_tencent(symbol[2:], start_date, end_date, "qfq", symbol=symbol)

    # ------------------------------------------------------------------ 快照
    def get_market_snapshot(self) -> Dict[str, Dict[str, Any]]:
        """东财 clist 全市场批量快照（约 56 页分页 + 节流，按日缓存于 Hub）。"""
        try:
            return emh.fetch_all_quotes() or {}
        except Exception as err:  # noqa: BLE001
            log.warning("全市场快照获取失败: %s", type(err).__name__)
            return {}

    def get_quotes(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """腾讯 qt.gtimg 批量快照（80 只/请求），覆盖 PE_TTM/PB/市值。"""
        if not codes:
            return {}
        try:
            return emh.fetch_quotes_tencent(list(codes))
        except Exception as err:  # noqa: BLE001
            log.warning("腾讯批量快照获取失败: %s", type(err).__name__)
            return {}

    def get_spot(self) -> Dict[str, Quote]:
        # 优先直连 clist（秒级全市场）；失败再退回新浪全量快照
        snap = self.get_market_snapshot()
        if snap:
            quotes: Dict[str, Quote] = {}
            for code, q in snap.items():
                close = q.get("price") or 0.0
                volume = q.get("volume") or 0.0
                quotes[code] = Quote(
                    code=code, name=q.get("name") or "",
                    open=0.0, high=0.0, low=0.0, close=close,
                    pct_chg=q.get("pct_chg") or 0.0, volume=volume,
                    amount=q.get("amount") or 0.0,
                    suspended=bool(q.get("suspended")) or (close <= 0 and volume <= 0),
                )
            return quotes
        df = self.ak.stock_zh_a_spot()
        quotes = {}
        for r in df.to_dict("records"):
            raw_code = str(_cell(r, "代码", default=""))
            code = raw_code[2:] if raw_code[:2] in ("sh", "sz") else raw_code
            close = _f(r.get("最新价"))
            volume = _f(r.get("成交量"))
            quotes[code] = Quote(
                code=code, name=str(_cell(r, "名称", default="")),
                open=_f(r.get("今开")), high=_f(r.get("最高")), low=_f(r.get("最低")),
                close=close, pct_chg=_f(r.get("涨跌幅")) / 100.0,
                volume=volume, amount=_f(r.get("成交额")),
                suspended=(close <= 0 or volume <= 0),
            )
        return quotes

    # ------------------------------------------------------------------ 成分
    def get_index_constituents(self, index_code: str) -> List[Tuple[str, str]]:
        df = self.ak.index_stock_cons(symbol=index_code)
        rows = df.to_dict("records")
        code_col = "品种代码" if "品种代码" in rows[0] else "成分券代码"
        name_col = "品种名称" if "品种名称" in rows[0] else "成分券名称"
        return [(str(r[code_col]).zfill(6), str(r[name_col])) for r in rows]

    def get_industry_constituents(self, sw_code: str) -> List[Tuple[str, str]]:
        df = self.ak.index_component_sw(symbol=sw_code)
        return [
            (str(r["证券代码"]).zfill(6), str(r["证券名称"]))
            for r in df.to_dict("records")
        ]

    # ------------------------------------------------------------------ 分红
    def get_dividends(self, code: str) -> List[DividendEvent]:
        df = self.ak.stock_history_dividend_detail(symbol=code, indicator="分红")
        events: List[DividendEvent] = []
        for r in df.to_dict("records"):
            ex_date = _dstr(_cell(r, "除权除息日", default=""))
            cash = _f(_cell(r, "派息", default=0)) / 10.0  # 每10股 -> 每股
            if not ex_date or cash <= 0:
                continue
            events.append(DividendEvent(
                code=code,
                announce_date=_dstr(_cell(r, "公告日期", default=ex_date)),
                ex_date=ex_date,
                cash_per_share=cash,
                progress=str(_cell(r, "进度", default="")),
            ))
        events.sort(key=lambda e: e.ex_date)
        return events

    # ------------------------------------------------------------------ 估值
    def get_valuation(self, code: str, indicator: str = "pe_ttm") -> List[Tuple[str, float]]:
        name = _VALUATION_INDICATORS.get(indicator, indicator)
        df = self.ak.stock_zh_valuation_baidu(symbol=code, indicator=name, period="近一年")
        return [(_dstr(r["date"]), _f(r["value"])) for r in df.to_dict("records")]

    # ------------------------------------------------------------ 涨停/龙虎榜
    def get_limit_up_pool(self, date: str) -> List[LimitUpInfo]:
        df = self.ak.stock_zt_pool_em(date=compact(date))
        out: List[LimitUpInfo] = []
        for r in df.to_dict("records"):
            code = str(r["代码"]).zfill(6)
            stat = str(_cell(r, "涨停统计", default=""))
            count = int(stat.split("/")[1]) if "/" in stat else 1
            themes_raw = str(_cell(r, "所属行业", default=""))
            out.append(LimitUpInfo(
                code=code, name=str(r["名称"]), date=date,
                close=_f(r["最新价"]), pct_chg=_f(r["涨跌幅"]) / 100.0,
                amount=_f(r["成交额"]), turnover_rate=_f(r["换手率"]),
                circ_mv=_f(r["流通市值"]), seal_amount=_f(r["封板资金"]),
                first_seal_time=str(_cell(r, "首次封板时间", default="")).zfill(6)[:4],
                last_seal_time=str(_cell(r, "最后封板时间", default="")).zfill(6)[:4],
                broken_times=_i(r["炸板次数"]), streak_days=_i(r["连板数"]),
                limit_up_count=count, themes=[themes_raw] if themes_raw else [],
            ))
        return out

    def get_broken_board_pool(self, date: str) -> List[LimitUpInfo]:
        df = self.ak.stock_zt_pool_zbgc_em(date=compact(date))
        out: List[LimitUpInfo] = []
        for r in df.to_dict("records"):
            out.append(LimitUpInfo(
                code=str(r["代码"]).zfill(6), name=str(r["名称"]), date=date,
                close=_f(r["最新价"]), pct_chg=_f(r["涨跌幅"]) / 100.0,
                amount=_f(r["成交额"]), turnover_rate=_f(r["换手率"]),
                circ_mv=_f(r["流通市值"]), seal_amount=0.0,
                first_seal_time=str(_cell(r, "首次封板时间", default="")).zfill(6)[:4],
                last_seal_time="", broken_times=max(1, _i(r["炸板次数"])),
                streak_days=0, limit_up_count=0,
                themes=[str(_cell(r, "所属行业", default=""))],
            ))
        return out

    def get_limit_down_pool(self, date: str) -> List[LimitUpInfo]:
        df = self.ak.stock_zt_pool_dtgc_em(date=compact(date))
        out: List[LimitUpInfo] = []
        for r in df.to_dict("records"):
            out.append(LimitUpInfo(
                code=str(r["代码"]).zfill(6), name=str(r["名称"]), date=date,
                close=_f(r["最新价"]), pct_chg=_f(r["涨跌幅"]) / 100.0,
                amount=_f(r["成交额"]), turnover_rate=_f(r["换手率"]),
                circ_mv=_f(r["流通市值"]), seal_amount=-_f(r["封单资金"]),
                first_seal_time="", last_seal_time=str(_cell(r, "最后封板时间", default="")),
                broken_times=_i(r["开板次数"]), streak_days=0,
                themes=[str(_cell(r, "所属行业", default=""))],
            ))
        return out

    def get_lhb(self, start_date: str, end_date: str) -> List[LhbRecord]:
        df = self.ak.stock_lhb_detail_em(start_date=compact(start_date), end_date=compact(end_date))
        out: List[LhbRecord] = []
        for r in df.to_dict("records"):
            out.append(LhbRecord(
                code=str(r["代码"]).zfill(6), name=str(r["名称"]),
                date=_dstr(_cell(r, "上榜日", default=start_date)),
                net_buy=_f(_cell(r, "龙虎榜净买额", "净买额")),
                buy_amount=_f(_cell(r, "龙虎榜买入额", "买入额")),
                sell_amount=_f(_cell(r, "龙虎榜卖出额", "卖出额")),
                interpretation=str(_cell(r, "解读", default="")),
            ))
        return out

    def health_check(self) -> bool:
        try:
            bars = self.get_index_bars("sh000300",
                                       (dt.date.today() - dt.timedelta(days=15)).strftime("%Y-%m-%d"),
                                       dt.date.today().strftime("%Y-%m-%d"))
            return len(bars) > 0
        except Exception:  # noqa: BLE001
            return False
