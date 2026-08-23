"""Chinese-language news sources for the news analyst (A-share focus).

The HiThink vendor does not provide stock-news article text (documented
capability boundary), so for A-share analysis this module fills the gap with
keyless public Chinese sources:

  - ``get_news``:        东方财富 (Eastmoney) 公司公告 — company announcements
  - ``get_global_news``: 新浪财经 7x24 快讯 — Chinese-language market news feed

Non-A-share symbols raise ``NoMarketDataError`` so the vendor router falls
through to yfinance for US/global tickers (configure
``news_data="cnnews,yfinance"`` in ``TRADINGAGENTS_DATA_VENDORS``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import requests

from .config import get_config
from .errors import NoMarketDataError

ANNOUNCE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
SINA_ROLL_URL = "https://feed.mix.sina.com.cn/api/roll/get"
REQUEST_TIMEOUT = 20

_A_SHARE_CODE = re.compile(r"^(\d{6})(\.(SH|SZ|BJ))?$", re.IGNORECASE)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _a_share_code(ticker: str) -> str | None:
    """The 6-digit A-share code from a thscode/bare code, else None."""
    m = _A_SHARE_CODE.fullmatch((ticker or "").strip())
    return m.group(1) if m else None


def _resolve_a_share_code(ticker: str) -> str:
    """Resolve a symbol to an A-share code.

    Codes/thscodes pass through; Chinese names resolve via the HiThink catalog
    (requires the HiThink key). Anything else raises NoMarketDataError so the
    router falls through to yfinance.
    """
    code = _a_share_code(ticker)
    if code:
        return code
    if _CJK.search(ticker or ""):
        try:
            from .hithink_common import resolve_symbol

            return _a_share_code(resolve_symbol(ticker)) or ""
        except Exception:  # noqa: BLE001 — unresolvable; fall through
            pass
    raise NoMarketDataError(
        ticker, ticker, "not an A-share symbol (cnnews covers A-shares only)"
    )


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """A-share company announcements from 东方财富 for ``ticker`` in the window."""
    try:
        code = _resolve_a_share_code(ticker)
    except NoMarketDataError:
        raise
    try:
        response = requests.get(
            ANNOUNCE_URL,
            params={
                "sr": "-1", "page_size": "50", "page_index": "1", "ann_type": "A",
                "client_source": "web", "stock_list": code,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        items = (response.json().get("data") or {}).get("list") or []
    except Exception as exc:  # noqa: BLE001 — report, don't crash the analyst
        return f"Error fetching A-share announcements for {ticker}: {exc}"

    rows = []
    for item in items:
        notice_date = (item.get("notice_date") or "")[:10]
        if not (start_date <= notice_date <= end_date):
            continue
        art = item.get("art_code")
        link = f"https://data.eastmoney.com/notices/detail/{code}/{art}.html" if art else ""
        rows.append(
            f"### {item.get('title')} (source: 东方财富公告, {notice_date})\n"
            f"{('Link: ' + link) if link else ''}\n"
        )
    if not rows:
        return f"No A-share announcements for {ticker} between {start_date} and {end_date}"
    return f"## {ticker} A-share announcements（东方财富）, from {start_date} to {end_date}:\n\n" + "\n".join(rows)


def get_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Chinese-language market news (新浪财经 7x24) in the window ending at curr_date.

    Look-ahead safe: items newer than ``curr_date`` are dropped, so historical
    runs never see future headlines.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    cutoff = curr_dt - timedelta(days=int(look_back_days))

    try:
        response = requests.get(
            SINA_ROLL_URL,
            params={
                "pageid": "153", "lid": "2516",
                "num": str(max(50, int(limit) * 3)), "page": "1",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        items = (response.json().get("result") or {}).get("data") or []
    except Exception as exc:  # noqa: BLE001 — report, don't crash the analyst
        return f"Error fetching Chinese 7x24 news: {exc}"

    rows = []
    seen = set()
    for item in items:
        title = item.get("title")
        if not title or title in seen:
            continue
        try:
            ts = int(item.get("ctime") or 0)
            published = datetime.fromtimestamp(ts)
        except (TypeError, ValueError):
            published = curr_dt
        if published.date() > curr_dt.date() or published < cutoff:
            continue
        seen.add(title)
        intro = (item.get("intro") or "").strip()
        rows.append(
            f"### {title} (source: {item.get('media_name') or '新浪财经'}, "
            f"{published.strftime('%Y-%m-%d %H:%M')})\n{intro + chr(10) if intro else ''}\n"
        )
        if len(rows) >= int(limit):
            break

    if not rows:
        return f"No Chinese 7x24 news between {cutoff:%Y-%m-%d} and {curr_date}"
    return f"## 中文财经快讯（新浪 7x24）, from {cutoff:%Y-%m-%d} to {curr_date}:\n\n" + "\n".join(rows)
