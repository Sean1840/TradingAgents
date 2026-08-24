"""Xueqiu (雪球) retail-investor discussion data for A-share sentiment analysis.

Xueqiu is the largest Chinese retail-investor community; its per-stock
discussion stream is a high-quality A-share sentiment signal that the overseas
sources (StockTwits / Reddit) cannot cover. This module fetches the public
discussion feed for a thscode and formats it for the sentiment analyst.

Access model: the xueqiu public API requires an ``xq_a_token`` cookie. The
token is read from ``XUEQIU_A_TOKEN`` (or the full cookie string in
``XUEQIU_COOKIE``). Without a token — or when the request fails — the module
returns a ``DATA_UNAVAILABLE: ...`` sentinel instead of raising, so the
analyst turn never crashes and the report clearly marks the gap.

Only titles/summaries/heat are captured (cached for analysis, not re-published),
and only items published on/before ``curr_date`` are kept (look-ahead safe).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

XUEQIU_API = "https://xueqiu.com/query/v1/symbol/search/status"
XUEQIU_HOME = "https://xueqiu.com/"
REQUEST_TIMEOUT = 20

# Simple tone tags for titles; not a substitute for real NLP, just a coarse
# retail-bias hint that the LLM should treat with care.
_BULLISH = re.compile(r"涨|利好|加仓|突破|新高|买入|机会|起飞|涨停")
_BEARISH = re.compile(r"跌|利空|减仓|破位|新低|卖出|风险|暴雷|跌停|套牢")


def _resolve_code(thscode: str) -> str:
    """6-digit A-share code from a thscode / bare code / xueqiu symbol."""
    m = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", (thscode or "").strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"not an A-share thscode: {thscode}")
    return m.group(1)


def _token() -> str:
    """xq_a_token from XUEQIU_A_TOKEN or the full cookie string."""
    token = os.environ.get("XUEQIU_A_TOKEN", "").strip()
    if token:
        return token
    cookie = os.environ.get("XUEQIU_COOKIE", "").strip()
    m = re.search(r"(?:^|;\s*)xq_a_token=([^;]+)", cookie)
    return m.group(1).strip() if m else ""


def _fmt_ts(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def get_xueqiu_sentiment(thscode: str, curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    """Xueqiu discussion stream for ``thscode`` in the window ending at ``curr_date``.

    Returns a formatted list of recent posts (title, tone hint, heat, time) or
    a ``DATA_UNAVAILABLE: ...`` sentinel when no token / request failure.
    """
    token = _token()
    if not token:
        return (
            "DATA_UNAVAILABLE: 雪球数据未配置（缺 XUEQIU_A_TOKEN / XUEQIU_COOKIE）。"
            "雪球讨论是 A 股散户情绪的补充代理；无数据时请以热股榜为准，勿臆造讨论内容。"
        )
    try:
        code = _resolve_code(thscode)
    except ValueError as exc:
        return f"DATA_UNAVAILABLE: {exc}"

    cutoff = datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=int(look_back_days))
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Cookie": f"xq_a_token={token}",
        "Referer": f"https://xueqiu.com/S/{code}",
    })
    try:
        # Seed the session (xueqiu sets a device cookie) then fetch the feed.
        session.get(XUEQIU_HOME, timeout=REQUEST_TIMEOUT)
        resp = session.get(
            XUEQIU_API,
            params={
                "count": str(max(limit, 20)),
                "comment": "0",
                "symbol": code,
                "hl": "0",
                "source": "all",
                "sort": "time",
                "page": "1",
                "q": code,
                "type": "11",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — sentinel, never raise into the analyst
        logger.warning("xueqiu fetch failed for %s: %s", thscode, exc)
        return f"DATA_UNAVAILABLE: 雪球数据获取失败（{exc}）。请以热股榜为准。"

    items = ((payload.get("data") or {}).get("list")) or payload.get("list") or []
    rows = []
    seen = set()
    for it in items:
        title = (it.get("title") or it.get("description") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        published = _fmt_ts(it.get("created_at") or it.get("time"))
        if published:
            try:
                if datetime.strptime(published, "%Y-%m-%d %H:%M") > datetime.strptime(curr_date, "%Y-%m-%d"):
                    continue  # look-ahead safety
                if datetime.strptime(published, "%Y-%m-%d %H:%M") < cutoff:
                    continue
            except ValueError:
                pass
        tone = "偏多?" if _BULLISH.search(title) else ("偏空?" if _BEARISH.search(title) else "")
        heat = it.get("like_count") or it.get("reply_count") or ""
        rows.append(
            f"- {title}（{tone}）{(' 热度 ' + str(heat)) if heat else ''}{(' @' + published) if published else ''}"
        )
        if len(rows) >= int(limit):
            break

    if not rows:
        return f"雪球讨论（{thscode}）：窗口内无帖子（{cutoff:%Y-%m-%d} ~ {curr_date}）"
    return (
        f"## 雪球讨论（{thscode}）{cutoff:%Y-%m-%d} ~ {curr_date}（Top{len(rows)}）\n"
        + "\n".join(rows)
    )
