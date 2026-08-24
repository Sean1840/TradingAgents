"""Supply-chain ("卡脖子") context for A-share analysis.

The serenity-stock-choke framework asks: which upstream link, if it ran dry,
would stop an entire industry — and which A-share company sits at that node?
This module gives the analyst agents a structured view of the *available*
supply-chain evidence for one ticker:

  - company identity (name / exchange) from the HiThink catalog;
  - recent announcements filtered by supply-chain keywords (产能/扩产/收购/
    国产替代/供应商/订单/募投/投产/技术突破...);
  - dragon-tiger (龙虎榜) entry with its concept tags, when the stock appeared.

It deliberately does NOT invent supply-chain facts (自给率/产能周期/寡头地位):
those have no data source here, so the analyst must label them "待验证假设"
instead of fabricating numbers. Every fetcher degrades to a
``DATA_UNAVAILABLE: ...`` sentinel rather than raising.
"""

from __future__ import annotations

import logging
import re

from .cn_news import get_news
from .hithink_common import resolve_symbol, resolve_symbol_info
from .hithink_special import dragon_tiger

logger = logging.getLogger(__name__)

# Announcement-title keywords that hint at supply-chain / capacity / policy
# relevance (used to pick items out of the raw announcement feed).
_SUPPLY_CHAIN_KEYWORDS = re.compile(
    r"产能|扩产|投产|收购|并购|股权|增资|募投|定增|国产替代|自主可控|供应链|供应商|"
    r"订单|中标|合同|技术突破|研发|专利|许可证|环评|开工|试产|客户|大基金|"
    r"卡脖子|瓶颈|原材料|价格|涨价|降价|进口|出口|关税",
    re.IGNORECASE,
)


def _announcement_hits(thscode: str, curr_date: str, look_back_days: int = 90) -> list[str]:
    """Announcements for ``thscode`` whose titles match supply-chain keywords."""
    from datetime import datetime, timedelta

    start = (datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=int(look_back_days))).strftime("%Y-%m-%d")
    try:
        text = get_news(thscode, start, curr_date)
    except Exception as exc:  # noqa: BLE001 — sentinel
        logger.warning("supply-chain announcements failed for %s: %s", thscode, exc)
        return []
    if not text or text.startswith("Error") or text.startswith("No A-share"):
        return []
    hits = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("### ") and _SUPPLY_CHAIN_KEYWORDS.search(line):
            hits.append(line[4:])
    return hits[:12]


def get_supply_chain_context(thscode: str, curr_date: str, look_back_days: int = 90) -> str:
    """Supply-chain / 卡脖子 evidence context for an A-share ticker.

    Aggregates company identity, supply-chain-keyword-filtered announcements
    and dragon-tiger concept tags. Facts not present in these sources (自给率、
    产能周期、寡头地位) must be treated by the analyst as unverified hypotheses
    and labelled 待验证, never fabricated.
    """
    try:
        thscode = resolve_symbol(thscode)
    except Exception as exc:  # noqa: BLE001 — non-A-share / unresolvable
        return f"DATA_UNAVAILABLE: 标的无法解析为 A 股 thscode（{exc}）"

    info = {}
    try:
        info = resolve_symbol_info(thscode)
    except Exception:  # noqa: BLE001 — identity is best-effort
        pass
    name = info.get("name") or thscode

    lines = [f"## {name}（{thscode}）供应链 / 卡脖子背景（截至 {curr_date}）", ""]

    # 1) Recent announcements with supply-chain keywords
    hits = _announcement_hits(thscode, curr_date, look_back_days)
    if hits:
        lines += ["### 近 90 日供应链相关公告（标题关键词过滤）", ""]
        lines += [f"- {h}" for h in hits]
    else:
        lines += ["### 供应链相关公告", "无（近 90 日标题未命中产能/收购/订单/国产替代等关键词，或公告源不可用）"]

    # 2) Dragon-tiger entry + concept tags (institutional/hot-money flow signal)
    try:
        dt = dragon_tiger(None, thscode, size=3)
    except Exception as exc:  # noqa: BLE001 — sentinel
        dt = f"DATA_UNAVAILABLE: 龙虎榜获取失败（{exc}）"
    lines += ["", "### 龙虎榜 / 题材标签（近 1 个交易日）", "", dt]

    # 3) Hard honesty constraint
    lines += [
        "",
        "> ⚠️ 供应链事实约束：自给率、产能建设周期、寡头/壁垒地位、供需缺口等数据"
        "**本工具不提供**。若分析中需要这些数字，必须显式标注为『待验证假设』并给出理由，"
        "**禁止编造具体数值**。此背景仅用于定位公司在产业链中的环节与近期事件信号。",
    ]
    return "\n".join(lines)
