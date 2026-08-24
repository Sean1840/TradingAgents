"""Global-market context tools for the analyst agents.

Adds the overnight international-market snapshot (US indices, VIX, yields,
dollar, commodities, Nikkei/Hang Seng + market-moving headlines) so A-share
analysis can weigh the asymmetric spillover from overnight overseas moves.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.global_market import get_global_market_context as _impl


@tool
def get_global_market_context(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format (Asia/Shanghai)"],
    look_back_days: Annotated[int, "Days of history to include in the snapshot"] = 10,
) -> str:
    """Overnight global-market environment ending at ``curr_date``.

    Returns the latest US index levels (S&P 500, Nasdaq, Dow, Philadelphia
    Semiconductor), VIX, 10Y Treasury yield, US dollar index, WTI crude and
    gold, plus Nikkei 225 / Hang Seng, each with its daily change, followed by
    keyword-filtered market-moving global headlines.

    Use it to judge the overnight risk backdrop before sizing any trade on an
    A-share symbol: a large overnight drop usually drags the A-share open
    (risk appetite + northbound sentiment), while an overnight rally does NOT
    guarantee A-share gains (policy/domestic-funds can dominate). Treat the
    snapshot as facts to weigh alongside policy news and market sentiment, not
    as a direct A-share direction call.
    """
    return _impl(curr_date, look_back_days)
