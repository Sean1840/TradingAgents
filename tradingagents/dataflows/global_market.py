"""Overnight global-market context for A-share analysis.

A-shares open after the US session closes, so the overnight moves in US
indices, VIX, treasury yields, the dollar, commodities and the Asia-Pacific
benchmarks (Nikkei / Hang Seng) are a first-order input for how the A-share
open is likely to behave. This module fetches that "overnight snapshot" via
yfinance (the framework's existing fallback vendor) and formats it for the
analyst agents.

The effect is asymmetric (a big overnight drop tends to drag A-shares down,
but an overnight rally does not guarantee A-share gains), so the snapshot is
deliberately presented as *facts + an event list* and the prompt (not this
module) tells the LLM how to weight it.

Every function degrades to a ``DATA_UNAVAILABLE: ...`` sentinel instead of
raising, so a rate-limited or missing feed never crashes an agent turn.
"""

from __future__ import annotations

import logging
import re

import yfinance as yf

from .stockstats_utils import yf_retry

logger = logging.getLogger(__name__)

# Symbol -> (label, fmt) for the overnight snapshot. fmt "pct" shows a daily
# change percentage, "abs" shows the raw level (yields, index levels).
_INDEXES: dict[str, tuple[str, str]] = {
    "^GSPC": ("标普500", "pct"),
    "^IXIC": ("纳斯达克", "pct"),
    "^DJI": ("道琼斯", "pct"),
    "^SOX": ("费城半导体", "pct"),
    "^VIX": ("VIX 恐慌指数", "abs"),
    "^TNX": ("美债10Y收益率", "abs"),
    "DX-Y.NYB": ("美元指数", "abs"),
    "CL=F": ("WTI 原油", "pct"),
    "GC=F": ("COMEX 黄金", "pct"),
    "^N225": ("日经225", "pct"),
    "^HSI": ("恒生指数", "pct"),
}

# Event keywords that mark a headline as market-moving for the overnight block.
_EVENT_KEYWORDS = re.compile(
    r"fed|fomc|rate (cut|hike)|nonfarm|payrolls|inflation|cpi|ppi|tariff|trade war|"
    r"export control|chip|semiconductor|geopolitic|russia|ukraine|middle east|iran|"
    r"treasury yield|recession|shutdown|debt (ceiling|default)",
    re.IGNORECASE,
)


def _fetch_snapshot(curr_date: str) -> list[str]:
    """Per-symbol daily change on/before ``curr_date`` (look-ahead safe).

    Fast-fail friendly: this is flavour context, not core data, so each symbol
    gets at most one short retry and the whole snapshot aborts after two
    consecutive failures (typically a yfinance-wide rate limit) instead of
    burning minutes on 11 symbols × 3 backoff retries.
    """
    rows: list[str] = []
    consecutive_failures = 0
    for symbol, (label, fmt) in _INDEXES.items():
        try:
            df = yf_retry(
                lambda s=symbol, d=curr_date: yf.Ticker(s).history(
                    start=_minus_days(curr_date, 10), end=curr_date, auto_adjust=False
                ),
                max_retries=1,
                base_delay=1.0,
            )
        except Exception as exc:  # noqa: BLE001 — sentinel per symbol, keep going
            logger.warning("global market %s failed: %s", symbol, exc)
            rows.append(f"- {label}（{symbol}）: DATA_UNAVAILABLE")
            consecutive_failures += 1
            if consecutive_failures >= 2:
                rows.append("- …（连续失败，疑似 yfinance 全局限流，其余外盘数据省略）")
                break
            continue
        consecutive_failures = 0
        if df is None or df.empty:
            rows.append(f"- {label}（{symbol}）: DATA_UNAVAILABLE（无 ≤ {curr_date} 数据）")
            continue
        last = df.iloc[-1]
        close = float(last["Close"])
        prev = float(last["Open"])
        if fmt == "pct" and prev:
            chg = (close / prev - 1) * 100
            rows.append(f"- {label}（{symbol}）: 收 {close:,.2f}，日内 {chg:+.2f}%")
        else:
            rows.append(f"- {label}（{symbol}）: {close:,.2f}")
    return rows


def _minus_days(date_str: str, days: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")


def _fetch_events(curr_date: str, limit: int = 8) -> list[str]:
    """Market-moving global headlines on/before ``curr_date`` (keyword-filtered)."""
    try:
        search = yf_retry(
            lambda d=curr_date: yf.Search("global markets", news_count=limit * 4, enable_fuzzy_query=True),
            max_retries=1,
            base_delay=1.0,
        )
    except Exception as exc:  # noqa: BLE001 — sentinel
        logger.warning("global market events failed: %s", exc)
        return []
    out: list[str] = []
    for article in (search.news if search and search.news else [])[: limit * 4]:
        title = ""
        if isinstance(article, dict):
            content = article.get("content") or {}
            title = (
                content.get("title")
                or content.get("headline")
                or article.get("title")
                or ""
            )
        if not title or not _EVENT_KEYWORDS.search(title):
            continue
        if title not in out:
            out.append(title)
        if len(out) >= limit:
            break
    return out


def get_global_market_context(curr_date: str, look_back_days: int = 10) -> str:
    """Overnight global-market snapshot ending at ``curr_date`` (Asia/Shanghai).

    Returns a formatted text block: index/yield/commodity levels with daily
    changes, plus a keyword-filtered list of market-moving global headlines.
    Look-ahead safe: only data on/before ``curr_date`` is included.
    """
    try:
        rows = _fetch_snapshot(curr_date)
    except Exception as exc:  # noqa: BLE001 — never raise into an agent turn
        logger.warning("global market snapshot failed: %s", exc)
        return f"DATA_UNAVAILABLE: 外盘快照获取失败（{exc}）"
    events = _fetch_events(curr_date)
    block = [
        f"## 隔夜外盘环境（截至 {curr_date}，前一日/最近交易日收盘）",
        "",
        "### 指数 / 收益率 / 商品（yfinance，前复权前收盘 vs 当日开）",
        "",
        "\n".join(rows),
    ]
    if events:
        block += [
            "",
            "### 全球市场重大事件（标题关键词过滤，可能与行情相关）",
            "",
            "\n".join(f"- {t}" for t in events),
        ]
    block += [
        "",
        "> 外盘对 A 股的影响是非对称的：隔夜大跌通常拖累 A 股开盘（风险偏好 + 北向情绪），"
        "但隔夜上涨并不保证 A 股上涨（政策与内资主导时传导弱）。上述为事实快照，"
        "对 A 股的具体影响需结合政策新闻（get_policy_news）与市场情绪（热股榜/涨停池）综合判断，"
        "不得把外盘涨跌直接等同于 A 股方向。",
    ]
    return "\n".join(block)
