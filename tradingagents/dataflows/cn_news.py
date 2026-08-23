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
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import requests

from .config import get_config
from .errors import NoMarketDataError

ANNOUNCE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
SINA_ROLL_URL = "https://feed.mix.sina.com.cn/api/roll/get"
SINA_ZHIBO_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
REQUEST_TIMEOUT = 20

# Official Chinese government / central-media sources (keyless RSS; best-effort,
# items without a parseable date are dropped for look-ahead safety).
OFFICIAL_FEEDS = [
    ("新华社·时政", "http://www.xinhuanet.com/politics/news_politics.xml"),
    ("人民日报·时政", "http://www.people.com.cn/rss/politics.xml"),
]

# Policy / big-meeting keywords used to pick government & macro-policy items out
# of the Sina 7x24 live feed (covers 两会, 政治局会议, 中央经济工作会议, 国常会,
# 央行/证监会/发改委..., 降准降息, 外贸/关税...).
POLICY_KEYWORDS = [
    "政策", "国务院", "央行", "证监会", "财政部", "发改委", "商务部", "工信部",
    "国常会", "政治局", "两会", "中央经济工作会议", "政府工作报告", "金融监管",
    "资本市场", "降准", "降息", "LPR", "MLF", "专项债", "减税", "关税", "A股",
    "宏观经济", "利率", "汇率", "通胀", "CPI", "PMI", "外贸", "进出口", "外资",
]

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


def _fetch_rss_feed(url: str) -> list[dict]:
    """Parse an RSS 2.0 feed into ``[{title, pub_date, link, source}]``.

    pubDate may be RFC822 or a bare ``YYYY-MM-DD``; unparseable dates are kept
    as ``None`` and dropped by the caller (look-ahead safety).
    """
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001 — a broken feed must not break the analyst
        print(f"cnnews: RSS fetch failed for {url}: {exc}", file=sys.stderr)
        return []
    items = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = node.findtext("pubDate") or node.findtext("dc:date")
        pub_date = None
        if pub:
            pub = pub.strip()
            try:
                pub_date = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                try:
                    pub_date = datetime.strptime(pub, "%Y-%m-%d")
                except (TypeError, ValueError):
                    pub_date = None
        if title:
            items.append({"title": title, "link": link, "pub_date": pub_date})
    return items


def get_policy_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Official Chinese policy / government / big-meeting news.

    Aggregates central-media RSS (新华社、人民日报 时政) plus Sina 7x24 items
    matching policy keywords (国务院/央行/证监会/两会/政治局会议/中央经济工作会议/
    国常会/降准降息...), look-ahead filtered to ``curr_date``.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    cutoff = curr_dt - timedelta(days=int(look_back_days))
    seen = set()
    rows = []

    for source, url in OFFICIAL_FEEDS:
        for it in _fetch_rss_feed(url):
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", it["title"]).strip()
            if not title or title in seen:
                continue
            pub = it["pub_date"]
            # Undated items are dropped (cannot prove they are not future news).
            if pub is None or pub.date() > curr_dt.date() or pub.date() < cutoff.date():
                continue
            seen.add(title)
            date_txt = pub.strftime("%Y-%m-%d %H:%M")
            rows.append(f"### {title} (source: {source}, {date_txt})\n{('Link: ' + it['link']) if it.get('link') else ''}\n")
            if len(rows) >= int(limit):
                break
        if len(rows) >= int(limit):
            break

    # Sina 7x24 live feed (macro/policy/market telegraph) matching policy
    # keywords — the primary, always-fresh policy source.
    try:
        resp = requests.get(SINA_ZHIBO_URL, params={
            "page": "1", "page_size": "100", "zhibo_id": "152",
            "tag_id": "0", "dire": "f", "dpc": "1",
        }, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        feed = ((resp.json().get("result") or {}).get("data") or {}).get("feed") or {}
        zhibo_items = feed.get("list") or []
        for item in zhibo_items:
            title = (item.get("rich_text") or item.get("text") or "").strip()
            if not title or title in seen:
                continue
            if not any(kw in title for kw in POLICY_KEYWORDS):
                continue
            raw = item.get("create_time") or ""
            pub = None
            if raw:
                try:
                    pub = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    pub = None
            if pub is None or pub.date() > curr_dt.date() or pub.date() < cutoff.date():
                continue
            seen.add(title)
            rows.append(f"### {title} (source: 新浪7x24·政策/宏观, {pub.strftime('%Y-%m-%d %H:%M')})\n")
            if len(rows) >= int(limit):
                break
    except Exception as exc:  # noqa: BLE001
        print(f"cnnews: policy zhibo feed failed: {exc}", file=sys.stderr)

    # Fallback: Sina roll (lid=2516) items matching policy keywords.
    if len(rows) < int(limit):
        try:
            resp = requests.get(SINA_ROLL_URL, params={
                "pageid": "153", "lid": "2516", "num": "100", "page": "1",
            }, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            sina_items = (resp.json().get("result") or {}).get("data") or []
            for item in sina_items:
                title = (item.get("title") or "").strip()
                if not title or title in seen:
                    continue
                if not any(kw in title for kw in POLICY_KEYWORDS):
                    continue
                try:
                    ts = int(item.get("ctime") or 0)
                    pub = datetime.fromtimestamp(ts)
                except (TypeError, ValueError):
                    pub = curr_dt
                if pub.date() > curr_dt.date() or pub.date() < cutoff.date():
                    continue
                seen.add(title)
                rows.append(f"### {title} (source: {item.get('media_name') or '新浪财经'}, "
                            f"{pub.strftime('%Y-%m-%d %H:%M')})\n")
                if len(rows) >= int(limit):
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"cnnews: policy sina roll failed: {exc}", file=sys.stderr)

    if not rows:
        return f"No official Chinese policy news between {cutoff:%Y-%m-%d} and {curr_date}"
    return f"## 官方政策与央媒新闻（新华社/人民日报/政策动态）, from {cutoff:%Y-%m-%d} to {curr_date}:\n\n" + "\n".join(rows)
