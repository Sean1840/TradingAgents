"""A-share special / market-structure data, formatted for the analyst agents.

Covers the market-level signals the US-centric framework never had:
limit-up / limit-down / limit-break pools, the consecutive-limit-up ladder,
the hot-stock rank, and the dragon-tiger (龙虎榜) institutional/hot-money list.
These are the market-environment and flow signals A-share analysis lives on.

Every function returns a formatted text block and degrades to a
``DATA_UNAVAILABLE: ...`` sentinel instead of raising, so a broken endpoint
never crashes an agent turn.
"""

from __future__ import annotations

import logging

from .hithink_common import _date_to_ms, _request

logger = logging.getLogger(__name__)

_DAY_MS = 24 * 60 * 60 * 1000


def _fmt_money(v) -> str:
    """Format a money amount (seal/net value) into 万/亿."""
    if v is None:
        return "N/A"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:,.0f}"


def _fetch(path: str, params: dict) -> dict | list | None:
    try:
        return _request(path, params)
    except Exception as exc:  # noqa: BLE001 — sentinel, never raise to the agent
        logger.warning("hithink special data %s failed: %s", path, exc)
        return None


def _date_param(date: str | None) -> dict:
    if not date:
        return {}
    try:
        return {"date_ms": _date_to_ms(date)}
    except ValueError:
        return {}


def limit_up_pool(date: str | None = None, size: int = 10) -> str:
    """涨停池: total count + top names (连板数 / 涨停时间 / 原因 / 封单额)."""
    data = _fetch("/api/a-share/special-data/limit-up-pool", {
        **_date_param(date), "size": size,
        "sort_field": "continue_day_cnt", "sort_dir": "desc",
    })
    if not isinstance(data, dict):
        return "DATA_UNAVAILABLE: 涨停池数据不可用"
    items = data.get("item") or []
    total = (data.get("pagination") or {}).get("total", len(items))
    if not items:
        return f"涨停池（{date or '今日'}）：共 {total} 家涨停"
    lines = [f"涨停池（{date or '今日'}）：共 {total} 家，Top{len(items)}："]
    for it in items:
        lines.append(
            f"- {it.get('name')}({it.get('thscode')}) {it.get('continue_day_text') or ''} "
            f"涨停时间 {it.get('limit_up_time')} 封单 {_fmt_money(it.get('seal_money'))} "
            f"原因: {it.get('limit_up_reason') or '—'}"
        )
    return "\n".join(lines)


def limit_down_pool(date: str | None = None, size: int = 8) -> str:
    data = _fetch("/api/a-share/special-data/limit-down-pool", {
        **_date_param(date), "size": size,
        "sort_field": "last_limit_time", "sort_dir": "desc",
    })
    if not isinstance(data, dict):
        return "DATA_UNAVAILABLE: 跌停池数据不可用"
    items = data.get("item") or []
    if not items:
        return f"跌停池（{date or '今日'}）：无"
    lines = [f"跌停池（{date or '今日'}）：共 {len(items)} 家（Top{len(items)}）："]
    for it in items:
        lines.append(f"- {it.get('name')}({it.get('thscode')}) {it.get('price_change_ratio_pct')}%")
    return "\n".join(lines)


def limit_break_pool(date: str | None = None, size: int = 8) -> str:
    data = _fetch("/api/a-share/special-data/limit-break-pool", {
        **_date_param(date), "size": size,
        "sort_field": "price_change_ratio_pct", "sort_dir": "desc",
    })
    if not isinstance(data, dict):
        return "DATA_UNAVAILABLE: 炸板池数据不可用"
    items = data.get("item") or []
    if not items:
        return f"炸板池（{date or '今日'}）：无"
    lines = [f"炸板池（{date or '今日'}）：{len(items)} 家（Top{len(items)}）："]
    for it in items:
        lines.append(
            f"- {it.get('name')}({it.get('thscode')}) {it.get('price_change_ratio_pct')}% "
            f"开板 {it.get('open_times')} 次"
        )
    return "\n".join(lines)


def limit_up_ladder(date: str | None = None) -> str:
    """连板天梯: board_caps (2/3/4/5/6/7+板家数) + today's highest-ladder names."""
    data = _fetch("/api/a-share/special-data/limit-up-ladder", {})
    if not isinstance(data, dict):
        return "DATA_UNAVAILABLE: 连板天梯数据不可用"
    window = data.get("window") or {}
    caps = window.get("board_caps") or {}
    items = data.get("item") or []
    target = date or ((items[0].get("date")) if items else None)
    cap_lines = [
        f"2板 {caps.get('two_board', 0)} 家｜3板 {caps.get('three_board', 0)} 家｜"
        f"4板 {caps.get('four_board', 0)} 家｜5板 {caps.get('five_board', 0)} 家｜"
        f"6板 {caps.get('six_board', 0)} 家｜7+板 {caps.get('seven_over', 0)} 家"
    ]
    out = [f"连板天梯（近30交易日，当前数据截至 {items[0].get('date') if items else '—'}）",
           f"连板梯队：{' '.join(cap_lines)}"]
    for it in items:
        if target and it.get("date") != target:
            continue
        boards = it.get("boards") or {}
        for tier, label in (("seven_over", "7+板"), ("six_board", "6板"), ("five_board", "5板"),
                            ("four_board", "4板"), ("three_board", "3板")):
            names = boards.get(tier) or []
            if names:
                out.append(f"{label}：{'、'.join(n.get('name') for n in names[:5])}")
    return "\n".join(out)


def hot_stocks(period: str = "day", size: int = 15) -> str:
    data = _fetch("/api/a-share/special-data/hot-stock-list", {"period": period})
    if not isinstance(data, dict):
        return "DATA_UNAVAILABLE: 热股榜数据不可用"
    items = (data.get("item") or [])[:size]
    if not items:
        return f"热股榜（{'hour' if period == 'hour' else 'day'}）：无数据"
    lines = [f"热股榜 Top{len(items)}（{'小时榜' if period == 'hour' else '日榜'}）："]
    for it in items:
        trend = {"up": "↑", "down": "↓", "flat": "→"}.get(it.get("rank_trend"), "")
        lines.append(f"- #{it.get('rank')} {it.get('name')}({it.get('thscode')}) 热度 {it.get('heat')} {trend}")
    return "\n".join(lines)


def dragon_tiger(date: str | None = None, thscode: str | None = None, size: int = 10) -> str:
    """龙虎榜 (all board); optionally filtered to one thscode."""
    data = _fetch("/api/a-share/special-data/dragon-tiger-list", {"board_type": "all"})
    if not isinstance(data, dict):
        return "DATA_UNAVAILABLE: 龙虎榜数据不可用"
    trade_date = data.get("trade_date") or date or "最近交易日"
    items = data.get("stock_items") or []
    if thscode:
        code = thscode.split(".")[0]
        items = [it for it in items if it.get("ticker") == code or it.get("thscode") == thscode]
    items = items[:size]
    if not items:
        return f"龙虎榜（{trade_date}）：无上榜记录"
    lines = [f"龙虎榜（{trade_date}）：{len(items)} 条"]
    for it in items:
        lines.append(
            f"- {it.get('name')}({it.get('thscode')}) 涨跌 {it.get('change', 0) * 100:.2f}% "
            f"净买额 {_fmt_money(it.get('net_value'))} "
            f"机构净买 {_fmt_money(it.get('org_net_value'))} 游资净买 {_fmt_money(it.get('hot_money_net_value'))}"
            + (f" 题材: {','.join(c.get('name', '') for c in it.get('concept_list') or [])[:40]}" if it.get("concept_list") else "")
        )
    return "\n".join(lines)


def trading_days() -> list[str]:
    """A-share trading-day list (Asia/Shanghai, last year)."""
    data = _fetch("/api/a-share/calendar/trading-days", {})
    if isinstance(data, list):
        return [str(d) for d in data]
    if isinstance(data, dict):
        items = data.get("item")
        if isinstance(items, list):
            return [str(it.get("date") or it) for it in items]
    return []


def is_trading_day(date: str) -> str:
    days = trading_days()
    if not days:
        return "DATA_UNAVAILABLE: 交易日历不可用"
    return f"{date} 是否为 A 股交易日：{'是' if date in days else '否（休市，注意节假日）'}"
