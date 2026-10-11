"""红利低波策略公共组件：股票池、风险过滤、六维因子评分、通用退出。"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..analysis import indicators as ind
from ..common import calendar
from ..common.log import get_logger
from ..common.models import Bar, Candidate, TradePlan
from ..common.retry import CriticalDataMissing
from .base import RunContext

log = get_logger("quant_core.dividend")

# ------------------------------------------------------------------ 行业映射
INDUSTRIES: Dict[str, str] = {
    "600900": "公用事业", "601088": "煤炭", "600028": "石油石化", "601857": "石油石化",
    "600019": "钢铁", "600585": "建材", "601668": "建筑", "601390": "建筑",
    "601186": "建筑", "601006": "交通运输", "600519": "食品饮料", "000568": "食品饮料",
    "600809": "食品饮料", "600887": "食品饮料", "000895": "食品饮料", "000423": "医药生物",
    "000538": "医药生物", "000999": "医药生物", "002007": "医药生物", "600276": "医药生物",
    "600436": "医药生物", "300015": "医药生物", "600763": "医药生物", "603259": "医药生物",
    "300347": "医药生物", "000651": "家电", "000333": "家电", "600690": "家电",
    "600104": "汽车", "000625": "汽车", "002594": "汽车", "600036": "银行",
    "600016": "银行", "601328": "银行", "601939": "银行", "601988": "银行",
    "600919": "银行", "601288": "银行", "601398": "银行", "600000": "银行",
    "600015": "银行", "601009": "银行", "601229": "银行", "601601": "非银金融",
    "601318": "非银金融", "601628": "非银金融", "600030": "非银金融", "000776": "非银金融",
    "600031": "机械设备", "600048": "房地产", "600050": "通信", "600150": "国防军工",
    "600346": "基础化工", "600426": "基础化工", "600309": "基础化工",
    "600406": "电力设备", "601012": "电力设备", "300014": "电力设备",
    "600660": "汽车", "601669": "建筑", "601766": "机械设备", "601899": "有色金属",
    "000002": "房地产", "000063": "通信", "000725": "电子", "002241": "电子",
    "002475": "电子", "300124": "电力设备", "002027": "传媒", "002120": "交通运输",
    "002179": "国防军工", "002271": "建材", "002311": "农林牧渔", "002410": "计算机",
    "002714": "农林牧渔", "002142": "银行",
}
DEFENSIVE_SCORE = {"银行": 90, "公用事业": 85, "煤炭": 72, "石油石化": 66,
                   "交通运输": 60, "通信": 55, "食品饮料": 55}
IND_ROE_PROXY = {"银行": 11.0, "食品饮料": 18.0, "家电": 17.0, "医药生物": 13.0,
                 "煤炭": 14.0, "石油石化": 10.0, "公用事业": 9.0, "基础化工": 9.5,
                 "有色金属": 11.0, "非银金融": 8.0, "汽车": 7.0, "房地产": 5.0,
                 "电子": 8.5, "电力设备": 9.0, "建筑": 8.0, "钢铁": 6.0,
                 "建材": 8.0, "交通运输": 8.5, "通信": 7.0, "机械设备": 7.5,
                 "国防军工": 6.0, "计算机": 6.5, "传媒": 5.0, "农林牧渔": 6.0,
                 "综合": 7.0}


def industry_of(code: str) -> str:
    if code in INDUSTRIES:
        return INDUSTRIES[code]
    if code.startswith(("601", "600")) and code.endswith(("88",)):
        return "综合"
    return "综合"


def hold_trading_days(open_date: str, date: str) -> int:
    d0 = calendar.to_date(open_date)
    d1 = calendar.to_date(date)
    n = 0
    day = d0
    while day < d1:
        day += dt.timedelta(days=1)
        if calendar.is_trading_day(day):
            n += 1
    return n


# ------------------------------------------------------------------ 市场环境
@dataclass
class MarketInfo:
    index_code: str
    close: float
    ma60: Optional[float]
    above_ma60: bool
    percentile_1y: float
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index_code, "close": self.close, "ma60": self.ma60,
                "above_ma60": self.above_ma60, "percentile_1y": round(self.percentile_1y, 3),
                "macro_score": round(self.score, 1)}


def build_market_info(ctx: RunContext, index_code: str = "sh000300",
                      days: int = 420) -> MarketInfo:
    end = ctx.date
    start = (calendar.to_date(end) - dt.timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
    bars = ctx.hub.index_bars(index_code, start, end)
    if len(bars) < 60:
        return MarketInfo(index_code, 0.0, None, True, 0.5, 50.0)
    closes = ind.closes(bars)
    ma60 = ind.sma(closes, 60)[-1]
    close = closes[-1]
    window = closes[-250:]
    pct = ind.pct_rank(window, close)
    above = bool(ma60 and close > ma60)
    score = pct * 55 + (45 if above else 0)
    return MarketInfo(index_code, close, ma60, above, pct, score)


# ------------------------------------------------------------------ 个股指标
@dataclass
class StockMetrics:
    code: str
    name: str
    bars: List[Bar]
    close: float
    pct_chg: float
    ma5: Optional[float]
    ma10: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    boll_mid: Optional[float]
    boll_upper: Optional[float]
    boll_lower: Optional[float]
    boll_mid_prev: Optional[float]
    boll_lower_prev: Optional[float]
    rsi14: Optional[float]
    macd_dif: Optional[float]
    macd_dea: Optional[float]
    macd_hist: Optional[float]
    macd_hist_prev: Optional[float]
    vol5: float
    vol20: float
    vol_ratio: float
    amount_mean5: float
    ret20: Optional[float]
    ret5: Optional[float]
    vol20_daily: float
    high20: float
    low20: float
    pos20: float
    dividend_yield: float
    div_years: int
    div_cash_ttm: float
    div_yoy: float
    pe_pct: Optional[float]
    pb: Optional[float]
    ex_date_today: bool
    event_score_raw: float
    industry: str
    is_st: bool
    traded_today: bool

    def to_compact(self) -> Dict[str, Any]:
        keys = ["code", "name", "close", "pct_chg", "dividend_yield", "rsi14",
                "ret20", "vol_ratio", "pe_pct", "industry", "ex_date_today"]
        return {k: getattr(self, k) for k in keys}


def _dividend_stats(dividends, close: float, date: str) -> Tuple[float, int, float, float, bool, float]:
    d = calendar.to_date(date)
    year_start = dt.date(d.year, 1, 1)
    ttm_cut = d - dt.timedelta(days=365)
    cash_ttm = sum(e.cash_per_share for e in dividends if ttm_cut <= calendar.to_date(e.ex_date) <= d)
    years = {calendar.to_date(e.ex_date).year for e in dividends}
    prev_year_cash = sum(e.cash_per_share for e in dividends
                         if calendar.to_date(e.ex_date).year == d.year - 1)
    this_year_cash = sum(e.cash_per_share for e in dividends
                         if year_start <= calendar.to_date(e.ex_date) <= d)
    yoy = ((this_year_cash / prev_year_cash - 1) if prev_year_cash > 0 else 0.0)
    ex_today = any(e.ex_date == date for e in dividends)
    event_raw = 40.0
    for e in dividends:
        delta = (calendar.to_date(e.ex_date) - d).days
        if -5 <= delta <= 15:
            event_raw = max(event_raw, 85.0 if delta >= 0 else 70.0)
        elif -30 <= delta < -5:
            event_raw = max(event_raw, 60.0)
    return cash_ttm / close if close else 0.0, len(years), cash_ttm, yoy, ex_today, event_raw


def compute_metrics(ctx: RunContext, code: str, name: str, window_days: int = 420) -> Optional[StockMetrics]:
    start = (calendar.to_date(ctx.date) - dt.timedelta(days=int(window_days * 1.6))).strftime("%Y-%m-%d")
    bars = ctx.hub.bars(code, start, ctx.date)
    if len(bars) < 30:
        return None
    closes = ind.closes(bars)
    vols = ind.volumes(bars)
    ma5_, ma10_, ma20_, ma60_ = (ind.sma(closes, n)[-1] for n in (5, 10, 20, 60))
    mid, upper, lower = ind.bollinger(closes, 20, 2.0)
    rsi14 = ind.rsi(closes, 14)[-1]
    dif, dea, hist = ind.macd(closes)
    vol5 = sum(vols[-5:]) / 5
    vol20 = sum(vols[-20:]) / 20
    amts = ind.amounts(bars)
    last = bars[-1]
    dividends = ctx.hub.dividends(code, ctx.date)
    dy, years, cash_ttm, yoy, ex_today, event_raw = _dividend_stats(dividends, last.close, ctx.date)
    pe_series = ctx.hub.valuation(code, "pe_ttm", ctx.date)
    pe_pct = None
    if pe_series:
        pe_pct = ind.pct_rank([v for _, v in pe_series[:-1]], pe_series[-1][1])
    # PB 优先走全市场批量快照（单次分页拉取），缺失再退回逐股百度估值序列
    snap = ctx.hub.market_snapshot(ctx.date).get(code) or {}
    pb = snap.get("pb")
    if not pb:
        pb_series = ctx.hub.valuation(code, "pb", ctx.date)
        if pb_series:
            pb = pb_series[-1][1]
    hi20 = max(closes[-20:])
    lo20 = min(closes[-20:])
    pos20 = ind.pct_rank(closes[-20:], last.close)
    return StockMetrics(
        code=code, name=name, bars=bars, close=last.close, pct_chg=last.pct_chg,
        ma5=ma5_, ma10=ma10_, ma20=ma20_, ma60=ma60_,
        boll_mid=mid[-1], boll_upper=upper[-1], boll_lower=lower[-1],
        boll_mid_prev=mid[-2], boll_lower_prev=lower[-2],
        rsi14=rsi14, macd_dif=dif[-1], macd_dea=dea[-1], macd_hist=hist[-1],
        macd_hist_prev=hist[-2],
        vol5=vol5, vol20=vol20, vol_ratio=vol5 / vol20 if vol20 > 0 else 1.0,
        amount_mean5=sum(amts[-5:]) / 5,
        ret20=ind.ret(closes, 20), ret5=ind.ret(closes, 5),
        vol20_daily=(sum((closes[i] / closes[i - 1] - 1) ** 2
                         for i in range(len(closes) - 20, len(closes))) / 20) ** 0.5,
        high20=hi20, low20=lo20, pos20=pos20,
        dividend_yield=dy, div_years=years, div_cash_ttm=cash_ttm, div_yoy=yoy,
        pe_pct=pe_pct, pb=pb, ex_date_today=ex_today, event_score_raw=event_raw,
        industry=industry_of(code), is_st="ST" in name.upper() or "ST" in name,
        traded_today=last.date == ctx.date,
    )


# ------------------------------------------------------------------ 风险过滤
def risk_pass(ctx: RunContext, m: StockMetrics, pool_cfg: Dict[str, Any]) -> Tuple[bool, str]:
    if not m.traded_today:
        return False, "停牌或无当日行情"
    if pool_cfg.get("exclude_st", True) and m.is_st:
        return False, "ST/风险警示"
    if code_in_blacklist(m.code, pool_cfg.get("risk_blacklist", [])):
        return False, "风险黑名单"
    min_hist = pool_cfg.get("min_history_days", 60)
    if len(m.bars) < min_hist:
        return False, f"历史不足{min_hist}日"
    if pool_cfg.get("min_listing_days"):
        need = min(250, int(pool_cfg["min_listing_days"] * 0.7))
        if len(m.bars) < need:
            return False, "上市时间不足"
    if m.close <= 0 or m.vol20 <= 0:
        return False, "行情数据异常"
    if pool_cfg.get("exclude_risk_warning", True) and abs(m.pct_chg) > 0.205:
        return False, "单日涨跌幅异常"
    return True, ""


def code_in_blacklist(code: str, blacklist: List[str]) -> bool:
    return code in set(blacklist or [])


# ------------------------------------------------------------------ 六维评分
@dataclass
class Panel:
    metrics: Dict[str, StockMetrics] = field(default_factory=dict)
    amount_pct: Dict[str, float] = field(default_factory=dict)
    industry_ret20: Dict[str, float] = field(default_factory=dict)
    industry_score: Dict[str, float] = field(default_factory=dict)


def build_panel(metrics: Dict[str, StockMetrics]) -> Panel:
    panel = Panel(metrics=metrics)
    amts = sorted(((m.amount_mean5, code) for code, m in metrics.items()))
    for rank, (_, code) in enumerate(amts):
        panel.amount_pct[code] = rank / max(1, len(amts) - 1)
    groups: Dict[str, List[float]] = {}
    for m in metrics.values():
        if m.ret20 is not None:
            groups.setdefault(m.industry, []).append(m.ret20)
    panel.industry_ret20 = {k: sum(v) / len(v) for k, v in groups.items()}
    roes = IND_ROE_PROXY
    ordered = sorted(panel.industry_ret20, key=lambda k: roes.get(k, 7.0))
    for rank, ind_name in enumerate(ordered):
        panel.industry_score[ind_name] = rank / max(1, len(ordered) - 1)
    return panel


def _bell(x: float, ideal: float, width: float) -> float:
    return max(0.0, 100.0 * math.exp(-((x - ideal) / width) ** 2))


def score_stock(m: StockMetrics, panel: Panel, market: MarketInfo) -> Tuple[Dict[str, float], List[str]]:
    """返回六个维度得分（0-100）与解释文本。"""
    reasons: List[str] = []
    # 1) 基本面质地
    dy_s = min(1.0, m.dividend_yield / 0.06) * 100
    vol_s = (1 - min(1.0, m.vol20_daily / 0.04)) * 100
    pe_s = (1 - m.pe_pct) * 100 if m.pe_pct is not None else 50.0
    quality_s = min(1.0, m.div_years / 3) * 100
    fundamental = 0.35 * dy_s + 0.25 * vol_s + 0.25 * pe_s + 0.15 * quality_s
    if m.dividend_yield >= 0.04:
        reasons.append(f"股息率{m.dividend_yield*100:.1f}%")
    # 2) 行业
    defensive = DEFENSIVE_SCORE.get(m.industry, 45.0)
    rets = sorted(panel.industry_ret20.values())
    ind_mom = ind.pct_rank(rets, panel.industry_ret20.get(m.industry, 0.0)) * 100
    roe_rank = panel.industry_score.get(m.industry, 0.5) * 100
    industry = 0.4 * defensive + 0.3 * ind_mom + 0.3 * roe_rank
    # 3) 估值与动量
    pos_s = _bell(m.pos20, 0.35, 0.35)
    bias = (m.close / m.ma20 - 1) if m.ma20 else 0.0
    bias_s = _bell(bias, 0.0, 0.08)
    rsi_s = _bell(m.rsi14 or 50, 50, 22)
    yoy_s = 50 + max(-1, min(1, m.div_yoy)) * 40
    valuation_momentum = 0.30 * pos_s + 0.30 * bias_s + 0.25 * rsi_s + 0.15 * yoy_s
    # 4) 资金
    amount_s = panel.amount_pct.get(m.code, 0.5) * 100
    ratio_s = _bell(m.vol_ratio, 1.0, 0.8)
    capital = 0.5 * amount_s + 0.5 * ratio_s
    # 5) 事件
    event = m.event_score_raw
    if m.ex_date_today:
        reasons.append("今日除权除息")
    # 6) 宏观
    macro = market.score
    dims = {
        "fundamental": round(fundamental, 1),
        "industry": round(industry, 1),
        "valuation_momentum": round(valuation_momentum, 1),
        "capital": round(capital, 1),
        "event": round(event, 1),
        "macro": round(macro, 1),
    }
    if m.rsi14 is not None and m.rsi14 < 40:
        reasons.append(f"RSI {m.rsi14:.0f} 偏低")
    return dims, reasons


def total_score(dim_scores: Dict[str, float], weights: Dict[str, float]) -> float:
    total_w = sum(weights.get(k, 0) for k in dim_scores)
    if total_w <= 0:
        return 0.0
    return sum(dim_scores[k] * weights.get(k, 0) for k in dim_scores) / total_w


def make_candidate(m: StockMetrics, dims: Dict[str, float], weights: Dict[str, float],
                   extra_reasons: List[str], date: str,
                   extra: Optional[Dict[str, Any]] = None) -> Candidate:
    score = total_score(dims, weights)
    return Candidate(
        code=m.code, name=m.name, date=date, score=round(score, 2),
        dim_scores=dims, reasons=extra_reasons, extra=extra or {},
    )


# ------------------------------------------------------------------ 买入闸门
def pending_buy_count(ctx: RunContext, trade_date: Optional[str] = None) -> int:
    """账本中将于指定日（默认次日）执行的 PENDING 买单数。

    这些计划次日开盘即变成持仓，计算新开仓名额时必须计入，
    否则 --force 重跑会因只看到当前持仓而重复释放名额。
    """
    td = trade_date or ctx.next_date
    return sum(1 for p in ctx.ledger.plans
               if p.status == "PENDING" and p.side == "BUY" and p.trade_date == td)


def open_slots(ctx: RunContext, max_positions: int) -> int:
    """新开仓可用名额 = 上限 - 现持仓 - 次日待成交买单。"""
    return max(0, max_positions
               - ctx.ledger.position_count()
               - pending_buy_count(ctx))


def buy_allowed(ctx: RunContext, cfg: Dict[str, Any], code: str, score: float) -> Tuple[bool, str]:
    trade = cfg["trade"]
    if score < trade["buy_score_threshold"]:
        return False, f"分数{score:.1f}<{trade['buy_score_threshold']}"
    if code in ctx.ledger.positions:
        return False, "已持有"
    if open_slots(ctx, trade["max_positions"]) <= 0:
        return False, "持仓数达上限（含次日待成交买单）"
    return True, ""


def create_buy_plan(ctx: RunContext, m: StockMetrics, cand: Candidate,
                    reason: str, tag: str = "", layer: Optional[int] = None,
                    weight: Optional[float] = None) -> TradePlan:
    trade = ctx.cfg["trade"]
    w = weight if weight is not None else trade["per_position_weight"]
    return ctx.engine.make_buy_plan(
        lane=ctx.lane, code=m.code, name=m.name, gen_date=ctx.date,
        trade_date=ctx.next_date, reason=reason, weight=w,
        hint_price=m.close, equity=ctx.ledger.equity(),
        score=cand.score, layer=layer, tag=tag,
    )


# ------------------------------------------------------------------ 通用退出
def standard_exits(ctx: RunContext, trade_cfg: Dict[str, Any]) -> List[TradePlan]:
    """止损/止盈（可阴线确认）/修复失败/到期；每只标的每日最多一份卖出计划。"""
    plans: List[TradePlan] = []
    start = (calendar.to_date(ctx.date) - dt.timedelta(days=200)).strftime("%Y-%m-%d")
    for code, pos in list(ctx.ledger.positions.items()):
        bars = ctx.hub.bars(code, start, ctx.date)
        if not bars or bars[-1].date != ctx.date:
            continue
        bar = bars[-1]
        ratio = bar.close / pos.avg_cost - 1.0
        hold = hold_trading_days(pos.open_date, ctx.date)
        name = pos.name
        reason = None
        tag = ""
        if ratio <= trade_cfg.get("stop_loss", -1):
            reason, tag = f"止损 {ratio*100:.1f}%", "STOP"
        elif ratio >= trade_cfg.get("take_profit", 9):
            if trade_cfg.get("take_profit_confirm_bear_candle"):
                if bar.close < bar.open:
                    reason, tag = f"止盈阴线确认 +{ratio*100:.1f}%", "TP"
            else:
                reason, tag = f"止盈 +{ratio*100:.1f}%", "TP"
        if reason is None and trade_cfg.get("repair_fail_days") and hold >= trade_cfg["repair_fail_days"] and ratio <= 0:
            reason, tag = f"{hold}日修复失败 {ratio*100:.1f}%", "REPAIR"
        if reason is None and trade_cfg.get("max_hold_days") and hold >= trade_cfg["max_hold_days"]:
            reason, tag = f"到期退出({hold}日)", "EXPIRE"
        if reason:
            plans.append(ctx.engine.make_sell_plan(
                lane=ctx.lane, code=code, name=name, gen_date=ctx.date,
                trade_date=ctx.next_date, reason=reason, qty=pos.available_qty or pos.qty,
                last_price=bar.close, tag=tag,
            ))
    return plans


def add_plan_once(ctx: RunContext, plan: TradePlan, bucket: List[TradePlan]) -> bool:
    if ctx.ledger.add_plan(plan):
        bucket.append(plan)
        return True
    return False


def evaluate_universe(
    ctx: RunContext, codes: Dict[str, str], pool_cfg: Dict[str, Any],
    weights: Dict[str, float], market: Optional[MarketInfo] = None,
) -> Tuple[MarketInfo, Panel, Dict[str, StockMetrics], List[Tuple[StockMetrics, Dict[str, float], List[str]]], List[Tuple[str, str]]]:
    """计算整池指标并返回：市场环境、面板、指标表、(指标,维度分,理由) 列表、被剔除项。"""
    market = market or build_market_info(ctx)
    # 一次批量请求补齐整池 PB/PE 快照（腾讯 80 只/请求，非关键增强数据）
    ctx.hub.prefetch_snapshot(list(codes.keys()), ctx.date)
    metrics: Dict[str, StockMetrics] = {}
    dropped: List[Tuple[str, str]] = []
    for code, name in codes.items():
        m = compute_metrics(ctx, code, name)
        if m is None:
            dropped.append((code, "指标计算失败"))
            continue
        ok, why = risk_pass(ctx, m, pool_cfg)
        if not ok:
            dropped.append((code, why))
            continue
        metrics[code] = m
    panel = build_panel(metrics)
    scored: List[Tuple[StockMetrics, Dict[str, float], List[str]]] = []
    for code, m in metrics.items():
        dims, reasons = score_stock(m, panel, market)
        scored.append((m, dims, reasons))
    return market, panel, metrics, scored, dropped


def filter_buy_candidates(
    ctx: RunContext, cfg: Dict[str, Any],
    scored: List[Tuple[StockMetrics, Dict[str, float], List[str]]],
    rule: callable,
) -> List[Candidate]:
    """套用策略候选规则 + 分数门槛，返回按分数排序的候选（不考虑持仓/名额）。"""
    weights = cfg["scoring"]["weights"]
    threshold = cfg["trade"]["buy_score_threshold"]
    out: List[Candidate] = []
    for m, dims, reasons in scored:
        if not rule(m):
            continue
        cand = make_candidate(m, dims, weights, reasons, ctx.date)
        if cand.score < threshold:
            continue
        out.append(cand)
    out.sort(key=lambda c: c.score, reverse=True)
    return out
