"""Supply-chain (卡脖子) tools for the analyst agents.

Brings the serenity-stock-choke "bottleneck" lens into the existing agents:
``get_supply_chain_context`` aggregates the available supply-chain evidence
for an A-share ticker (identity + keyword-filtered announcements + dragon-tiger
concept tags) and hard-constrains the analyst from fabricating facts.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.supply_chain import get_supply_chain_context as _impl


@tool
def get_supply_chain_context(
    thscode: Annotated[str, "A-share thscode (e.g. 688432.SH), bare code, or Chinese name"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int, "How many days of announcements to scan"] = 90,
) -> str:
    """A-share supply-chain / 卡脖子 background for a ticker.

    Returns the company's recent announcements whose titles match supply-chain
    keywords (产能/扩产/收购/订单/国产替代/募投/技术突破...), plus its
    dragon-tiger entry and concept tags when available.

    Use it to locate the company's position in its industry chain and to spot
    recent capacity / M&A / order / localization signals. Facts that are NOT in
    the returned data (自给率, 产能建设周期, 寡头地位, 供需缺口) must be
    labelled 待验证假设 and never invented.
    """
    return _impl(thscode, curr_date, look_back_days)
