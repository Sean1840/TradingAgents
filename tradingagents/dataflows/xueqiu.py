"""Xueqiu (雪球) retail-investor discussion data for A-share sentiment analysis.

Xueqiu is the largest Chinese retail-investor community; its per-stock
discussion stream is a high-quality A-share sentiment signal that the overseas
sources (StockTwits / Reddit) cannot cover. This module fetches the public
discussion feed for a thscode and formats it for the sentiment analyst.

Access model (two backends, tried in order):

1. **CDP backend (primary)** — the xueqiu API sits behind an Aliyun WAF that
   requires a JS challenge signature and a logged-in cookie chain, which plain
   HTTP clients cannot reproduce. A real Chrome with a logged-in session can.
   This backend drives Chrome over the DevTools Protocol: it performs the API
   fetch *inside* the page context (``fetch(..., credentials: 'include')``),
   which carries the full cookie chain and the WAF signature automatically.
   Chrome is expected to be reachable at ``XUEQIU_CDP_PORT`` (default 9333),
   e.g. launched with::

       chrome --remote-debugging-port=9333 --user-data-dir=<profile> https://xueqiu.com/

   and logged in once. The ``.pydeps/login_xueqiu.py`` helper automates that.

2. **HTTP backend (fallback)** — uses ``XUEQIU_A_TOKEN`` / ``XUEQIU_COOKIE``
   with plain requests. Works only if the WAF is not enforcing for the
   session; usually returns DATA_UNAVAILABLE now that the WAF is active.

Both degrade to a ``DATA_UNAVAILABLE: ...`` sentinel instead of raising, so
the analyst turn never crashes and the report clearly marks the gap. Only
titles/summaries/heat are captured (cached for analysis, not re-published),
and only items published on/before ``curr_date`` are kept (look-ahead safe).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
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


# ---------------------------------------------------------------------------
# CDP backend: fetch inside a real Chrome page (passes WAF + login chain)
# ---------------------------------------------------------------------------

def _cdp_port() -> int | None:
    raw = os.environ.get("XUEQIU_CDP_PORT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cdp_targets(port: int) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            return [t for t in json.loads(r.read().decode("utf-8")) if t.get("type") == "page"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("xueqiu CDP: devtools not reachable on port %s: %s", port, exc)
        return []


async def _cdp_fetch_page(ws_url: str, code: str, count: int) -> dict | None:
    """Run the API fetch inside the page context so cookies + WAF signature apply."""
    expr = (
        f"(async () => {{"
        f"  const r = await fetch('{XUEQIU_API}?count={count}&comment=0&symbol={code}"
        f"&hl=0&source=all&sort=time&page=1&q={code}&type=11', "
        f"{{ credentials: 'include', headers: {{ 'Accept': 'application/json' }} }});"
        f"  const j = await r.json();"
        f"  return JSON.stringify(j);"
        f"}})()"
    )
    import websockets

    try:
        async with websockets.connect(ws_url, max_size=20_000_000) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "awaitPromise": True}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=40))
                if msg.get("id") != 1:
                    continue
                result = (msg.get("result") or {}).get("result", {})
                value = result.get("value")
                if result.get("exceptionDetails"):
                    logger.warning("xueqiu CDP: page fetch threw: %s",
                                   result["exceptionDetails"].get("text"))
                    return None
                if isinstance(value, str):
                    return json.loads(value)
                return value
    except Exception as exc:  # noqa: BLE001
        logger.warning("xueqiu CDP: websocket fetch failed: %s", exc)
        return None


def _cdp_fetch(code: str, count: int) -> dict | None:
    port = _cdp_port()
    if port is None:
        return None
    for target in _cdp_targets(port):
        ws = target.get("webSocketDebuggerUrl")
        if not ws:
            continue
        try:
            payload = asyncio.run(_cdp_fetch_page(ws, code, count))
        except Exception as exc:  # noqa: BLE001
            logger.warning("xueqiu CDP: target %s failed: %s", target.get("url", "?"), exc)
            continue
        if payload is not None:
            return payload
    return None


def _http_fetch(code: str, count: int, token: str) -> dict | None:
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
        session.get(XUEQIU_HOME, timeout=REQUEST_TIMEOUT)
        resp = session.get(
            XUEQIU_API,
            params={
                "count": str(count), "comment": "0", "symbol": code,
                "hl": "0", "source": "all", "sort": "time", "page": "1",
                "q": code, "type": "11",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("xueqiu http fetch failed for %s: %s", code, exc)
        return None


def get_xueqiu_sentiment(thscode: str, curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    """Xueqiu discussion stream for ``thscode`` in the window ending at ``curr_date``.

    Returns a formatted list of recent posts (title, tone hint, heat, time) or
    a ``DATA_UNAVAILABLE: ...`` sentinel when no backend is usable.
    """
    try:
        code = _resolve_code(thscode)
    except ValueError as exc:
        return f"DATA_UNAVAILABLE: {exc}"

    count = max(int(limit), 20)
    payload = _cdp_fetch(code, count)
    backend = "cdp"
    if payload is None:
        token = _token()
        if not token:
            return (
                "DATA_UNAVAILABLE: 雪球数据不可用（CDP 未配置 XUEQIU_CDP_PORT，"
                "且缺 XUEQIU_A_TOKEN / XUEQIU_COOKIE）。请以热股榜为准，勿臆造讨论内容。"
            )
        payload = _http_fetch(code, count, token)
        backend = "http"
    if payload is None:
        return "DATA_UNAVAILABLE: 雪球数据获取失败（CDP 与 HTTP 后端均不可用）。请以热股榜为准。"

    items = ((payload.get("data") or {}).get("list")) or payload.get("list") or []
    cutoff = datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=int(look_back_days))
    rows = []
    seen = set()
    for it in items:
        title = (it.get("title") or it.get("description") or "").strip()
        # Strip inline links/tags that pollute titles (e.g. <a href=...>).
        title = re.sub(r"<[^>]+>", "", title).strip()
        if not title or title in seen:
            continue
        seen.add(title)
        published = _fmt_ts(it.get("created_at") or it.get("time"))
        if published:
            try:
                published_dt = datetime.strptime(published, "%Y-%m-%d %H:%M")
                # Date-level look-ahead safety: posts published on the analysis
                # day itself (any hour, local time) are valid; only strictly
                # later calendar days are dropped.
                if published_dt.date() > datetime.strptime(curr_date, "%Y-%m-%d").date():
                    continue
                if published_dt < cutoff:
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

    tag = f"[雪球·{backend}]"
    if not rows:
        return f"雪球讨论（{thscode}）：窗口内无帖子（{cutoff:%Y-%m-%d} ~ {curr_date}） {tag}"
    return (
        f"## 雪球讨论（{thscode}）{cutoff:%Y-%m-%d} ~ {curr_date}（Top{len(rows)}） {tag}\n"
        + "\n".join(rows)
    )
