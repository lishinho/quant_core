"""东方财富/腾讯 HTTP 直连数据层（参考 TradingAgents-CN 的成熟做法）。

背景：本网络下 akshare 封装的东财接口（stock_zh_a_hist 等）会被反爬断连，
而携带浏览器 UA + Referer 的直连请求稳定可用。本模块：

- push2his /api/qt/stock/kline/get  日线 K 线（前/后复权）
- push2   /api/qt/stock/get         单票实时行情、PE(TTM)/PB/市值
- push2   /api/qt/clist/get         全市场 A 股批量快照（分页 + 节流）
- qt.gtimg.cn                      腾讯实时行情兜底

解析函数与 HTTP 分离，可离线单测；请求层带节流、指数退避重试。
仅在已安装 requests（akshare 依赖）时可用。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ..common.log import get_logger
from ..common.models import Bar

log = get_logger("quant_core.emhttp")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://quote.eastmoney.com/",
    "Connection": "keep-alive",
}
_PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_PUSH2 = "https://push2.eastmoney.com/api/qt/stock/get"
_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
_TENCENT = "https://qt.gtimg.cn/q={market}{code}"

# 东财对同主机高频请求会直接断连，参考 TradingAgents-CN 做最小间隔节流
_THROTTLE = 0.35
_CLIST_THROTTLE = 0.5
_LAST_CALL = [0.0]
_SESSION = None


def _session():
    """带连接级重试的 requests.Session（应对东财翻页时的主动断连）。"""
    global _SESSION
    if _SESSION is None:
        import requests
        from requests.adapters import HTTPAdapter
        try:
            from urllib3.util.retry import Retry
            retry = Retry(connect=2, read=1, backoff_factor=0.8,
                          status_forcelist=(429, 502, 503, 504))
        except Exception:  # noqa: BLE001
            retry = 2
        sess = requests.Session()
        sess.mount("https://", HTTPAdapter(max_retries=retry))
        sess.mount("http://", HTTPAdapter(max_retries=retry))
        _SESSION = sess
    return _SESSION

# clist 全市场 A 股（沪深主板/创业板/科创板，不含北交所）
_CLIST_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_CLIST_FIELDS = "f2,f3,f5,f6,f9,f12,f14,f20,f21,f23,f115"
# f2 最新价 f3 涨跌幅% f5 成交量(手) f6 成交额
# f9 动态PE f23 PB f20 总市值 f21 流通市值 f115 PE(TTM)

_ADJUST_MAP = {"qfq": "1", "hfq": "2", "": "0", None: "0"}


def _secid(code: str) -> str:
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def _throttle(gap: float = _THROTTLE) -> None:
    wait = gap - (time.time() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[0] = time.time()


def _get_json(url: str, params: Dict[str, Any], attempts: int = 3,
              timeout: float = 10.0, gap: float = _THROTTLE) -> Optional[Dict[str, Any]]:
    last_err: Optional[Exception] = None
    for i in range(attempts):
        _throttle(gap)
        try:
            resp = _session().get(url, params=params, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as err:  # noqa: BLE001 - 断连/限流均重试
            last_err = err
            if i < attempts - 1:
                time.sleep(0.5 * (2 ** i))
    log.warning("东财直连失败 %s params=%s: %s", url, params.get("secid", ""),
                type(last_err).__name__ if last_err else "")
    return None


# =============================================================== 纯解析
def parse_kline(payload: Dict[str, Any]) -> List[Bar]:
    """解析 push2his klines 文本行：
    date,open,close,high,low,vol(手),amount,振幅%,涨跌幅%,涨跌额,换手率%
    """
    data = (payload or {}).get("data") or {}
    bars: List[Bar] = []
    prev_close: Optional[float] = None
    for line in data.get("klines", []) or []:
        p = line.split(",")
        if len(p) < 9:
            continue
        close = float(p[2])
        bars.append(Bar(
            date=p[0], open=float(p[1]), high=float(p[3]), low=float(p[4]),
            close=close, volume=float(p[5]) * 100.0,  # 手 -> 股
            amount=float(p[6]), pct_chg=float(p[8]) / 100.0,
        ))
    bars.sort(key=lambda b: b.date)
    return bars


def parse_quote(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析 push2 stock/get。价格类字段 ×100，市值为元。"""
    d = (payload or {}).get("data")
    if not d or not d.get("f57"):
        return None

    def scaled(key: str) -> Optional[float]:
        v = d.get(key)
        return float(v) / 100.0 if v not in (None, "-", 0) else None

    price = scaled("f43")
    total_mv = float(d.get("f117") or 0) or None
    circ_mv = float(d.get("f85") or 0) or None
    return {
        "code": str(d["f57"]), "name": d.get("f58"), "price": price,
        "pe_ttm": None, "pe_dynamic": scaled("f162"),
        "pb": scaled("f167"),
        "total_mv": total_mv, "circ_mv": circ_mv,
        "suspended": price in (None, 0.0),
        "source": "eastmoney_push2",
    }


def parse_clist_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    code = str(row.get("f12") or "").zfill(6)
    if not code:
        return None
    price = row.get("f2")
    price = float(price) if isinstance(price, (int, float)) else None

    def num(key: str) -> Optional[float]:
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    return {
        "code": code, "name": row.get("f14"), "price": price,
        "pct_chg": (num("f3") or 0.0) / 100.0,
        "volume": (num("f5") or 0.0) * 100.0,  # 手 -> 股
        "amount": num("f6") or 0.0,
        "pe_dynamic": num("f9"), "pb": num("f23"),
        "total_mv": num("f20"), "circ_mv": num("f21"),
        "pe_ttm": num("f115"),
        "suspended": price is None,
        "source": "eastmoney_clist",
    }


def parse_tencent(text: str, code: str) -> Optional[Dict[str, Any]]:
    """解析 qt.gtimg.cn 返回（~ 分隔）。市值单位亿元。"""
    try:
        body = text.split("=", 1)[1].strip('";\n')
        p = body.split("~")
        if len(p) < 53:
            return None

        def f(idx: int) -> Optional[float]:
            try:
                return float(p[idx]) if p[idx] else None
            except (ValueError, IndexError):
                return None

        return {
            "code": code, "name": p[1], "price": f(3),
            "pe_ttm": f(39), "pe_dynamic": f(52), "pb": f(46),
            "total_mv": (f(45) or 0) * 1e8 or None,
            "circ_mv": (f(44) or 0) * 1e8 or None,
            "suspended": f(3) in (None, 0.0),
            "source": "tencent_qt",
        }
    except (IndexError, ValueError):
        return None


def parse_tencent_kline(payload: Dict[str, Any], code: str,
                        symbol: Optional[str] = None) -> List[Bar]:
    """解析 web.ifzq.gtimg.cn fqkline 返回：
    node 下 qfqday/hfqday/day 列表，每行 [date,open,close,high,low,vol(手), ...]
    symbol 可显式指定（指数代码如 sh000300 与个股前缀规则不同）。
    """
    node = ((payload or {}).get("data") or {}).get(symbol or _tencent_symbol(code)) or {}
    rows = node.get("qfqday") or node.get("hfqday") or node.get("day") or []
    bars: List[Bar] = []
    prev_close: Optional[float] = None
    for row in sorted(rows, key=lambda r: r[0]):
        if len(row) < 6:
            continue
        close = float(row[2])
        pct = (close / prev_close - 1.0) if prev_close else 0.0
        bars.append(Bar(date=row[0], open=float(row[1]), high=float(row[3]),
                        low=float(row[4]), close=close,
                        volume=float(row[5]) * 100.0,  # 手 -> 股
                        amount=0.0, pct_chg=pct))
        prev_close = close if close > 0 else prev_close
    return bars


# =============================================================== HTTP 调用
def fetch_kline_tencent(code: str, start: str, end: str, adjust: str = "qfq",
                        symbol: Optional[str] = None) -> List[Bar]:
    """腾讯 web.ifzq.gtimg.cn 日线（独立主机，东财封 IP 时的兜底行情源）。"""
    sym = symbol or _tencent_symbol(code)
    fq = adjust if adjust in ("qfq", "hfq") else ""
    limit = 640
    tail = f",{fq}" if fq else ""
    payload = _get_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        {"param": f"{sym},day,{start},{end},{limit}{tail}"},
        attempts=3,
    )
    return parse_tencent_kline(payload or {}, code, sym) if payload else []


def fetch_kline(code: str, start: str, end: str, adjust: str = "qfq") -> List[Bar]:
    payload = _get_json(_PUSH2HIS, {
        "secid": _secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": _ADJUST_MAP.get(adjust, "1"),
        "beg": start.replace("-", ""), "end": end.replace("-", ""),
    })
    if payload is None:
        return []
    return parse_kline(payload)


def fetch_quote(code: str) -> Optional[Dict[str, Any]]:
    payload = _get_json(_PUSH2, {
        "secid": _secid(code),
        "fields": "f43,f57,f58,f85,f117,f162,f167",
    })
    quote = parse_quote(payload)
    if quote is not None:
        return quote
    return fetch_quote_tencent(code)


def _tencent_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def parse_tencent_batch(text: str) -> Dict[str, Dict[str, Any]]:
    """解析 qt.gtimg.cn 批量返回：多个 v_xx="..."; 段。"""
    out: Dict[str, Dict[str, Any]] = {}
    for seg in text.split(";"):
        if "=" not in seg or "~" not in seg:
            continue
        head, _, body = seg.partition("=")
        symbol = head.strip().split("_")[-1]  # 如 v_sh600900 -> sh600900
        code = symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol
        parsed = parse_tencent(seg.strip(), code)
        if parsed:
            out[code] = parsed
    return out


def fetch_quotes_tencent(codes: List[str], chunk_size: int = 80) -> Dict[str, Dict[str, Any]]:
    """批量获取多只股票实时行情（PE/PB/市值）。

    腾讯接口单次支持上百只代码；850 只股票池约 11 次请求即可完成。
    """
    out: Dict[str, Dict[str, Any]] = {}
    uniq = list(dict.fromkeys(codes))
    for i in range(0, len(uniq), chunk_size):
        group = uniq[i:i + chunk_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(_tencent_symbol(c) for c in group)
        _throttle()
        text = None
        for attempt in range(3):
            try:
                resp = _session().get(url, headers={"User-Agent": _HEADERS["User-Agent"]},
                                      timeout=10)
                resp.encoding = "gbk"
                text = resp.text
                break
            except Exception:  # noqa: BLE001
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        if text is not None:
            out.update(parse_tencent_batch(text))
    log.info("腾讯批量快照: 请求 %d 组，取得 %d/%d 只",
             (len(uniq) + chunk_size - 1) // chunk_size, len(out), len(uniq))
    return out


def fetch_quote_tencent(code: str) -> Optional[Dict[str, Any]]:
    batch = fetch_quotes_tencent([code], chunk_size=1)
    return batch.get(code)


def fetch_all_quotes(page_size: int = 100, max_pages: int = 80,
                     market_filter: str = _CLIST_FS) -> Optional[Dict[str, Dict[str, Any]]]:
    """分页拉取全市场快照。单页服务端上限 100；自动节流与断连重试。

    任一页在多轮冷却后仍失败则整体放弃（返回 None），由上层走历史快照
    降级——绝不返回不完整快照导致部分股票被误判为无估值。
    """
    out: Dict[str, Dict[str, Any]] = {}
    total = 0
    for page in range(1, max_pages + 1):
        params = {
            "pn": page, "pz": page_size, "po": 1, "np": 1,
            "fltt": 2, "invt": 2, "fid": "f12", "fs": market_filter,
            "fields": _CLIST_FIELDS,
        }
        payload = None
        for cooldown in (0, 3):  # 页面级冷却续拉
            if cooldown:
                log.warning("clist 第 %d 页失败，冷却 %ds 后续拉", page, cooldown)
                time.sleep(cooldown)
            payload = _get_json(_CLIST, params, attempts=3, gap=_CLIST_THROTTLE)
            if payload is not None and (payload.get("data") or {}).get("diff") is not None:
                break
        if payload is None:
            log.error("clist 第 %d 页多轮重试仍失败，放弃本次全市场快照（已取 %d 只）",
                      page, len(out))
            return None
        data = payload.get("data") or {}
        rows = data.get("diff") or []
        total = int(data.get("total") or total)
        for row in rows:
            parsed = parse_clist_row(row)
            if parsed:
                out[parsed["code"]] = parsed
        if len(rows) < page_size or (total and len(out) >= total):
            break
    if total and len(out) < total * 0.99:
        log.error("clist 快照不完整: %d/%d，放弃", len(out), total)
        return None
    log.info("东财全市场快照: %d 只", len(out))
    return out
