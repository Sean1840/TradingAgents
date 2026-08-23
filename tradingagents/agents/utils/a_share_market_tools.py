"""A-share market-structure tools for the analyst agents.

These give the analysts the market-environment and flow signals US-stock
analysis doesn't need but A-share analysis lives on: limit-up/down/break pools,
the consecutive-limit-up ladder, hot-stock rank, dragon-tiger lists, and the
trading calendar.
"""
from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows import hithink_special


@tool
def get_market_context(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
) -> str:
    """A-share market environment for a date: limit-up / limit-down / limit-break
    pools, the consecutive-limit-up ladder (连板天梯) and hot-money activity.

    Use it to judge whether the market is in a risk-on (many limit-ups, high
    ladder) or risk-off (limit-downs, broken boards) regime before sizing any
    trade on an A-share symbol.
    """
    parts = [
        hithink_special.limit_up_pool(curr_date, size=10),
        hithink_special.limit_down_pool(curr_date),
        hithink_special.limit_break_pool(curr_date),
        hithink_special.limit_up_ladder(curr_date),
    ]
    return "\n\n".join(parts)


@tool
def get_dragon_tiger(
    date: Annotated[str, "yyyy-mm-dd; omit to use the most recent trading day"] = "",
    thscode: Annotated[str, "optional A-share thscode to filter to one stock"] = "",
) -> str:
    """A-share 龙虎榜 (dragon-tiger list): exchange-disclosed top movers with
    net buy amounts split by institution (机构) and hot money (游资).

    Use it to gauge institutional vs speculative money flow for the symbol or
    the market.
    """
    return hithink_special.dragon_tiger(date or None, thscode or None, size=10)


@tool
def get_hot_stocks(
    period: Annotated[str, "'day' (24h list) or 'hour' (hourly list)"] = "day",
) -> str:
    """A-share hot-stock rank (热股榜): market attention leaderboard with heat
    and rank trend. Use as a retail-attention / sentiment proxy for A-shares.
    """
    return hithink_special.hot_stocks(period, size=15)


@tool
def is_trading_day(
    date: Annotated[str, "date in yyyy-mm-dd format"],
) -> str:
    """Whether a date is an A-share trading day (Chinese holiday calendar)."""
    return hithink_special.is_trading_day(date)
