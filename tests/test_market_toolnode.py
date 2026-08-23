"""The market analyst is bound (and prompt-instructed) to call
get_verified_market_snapshot; if the executor ToolNode doesn't register it, the
call fails and the model reports the tool "unavailable" and skips verification.

Regression guard for that wiring gap (snapshot bound to the LLM but missing from
the market ToolNode).
"""
import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
def test_market_toolnode_can_execute_verified_snapshot():
    # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    market_tools = set(nodes["market"].tools_by_name)
    assert "get_verified_market_snapshot" in market_tools, (
        "get_verified_market_snapshot is bound to the market analyst but not "
        "registered in the market ToolNode, so the model's call fails."
    )
    # the other core market tools must remain too
    assert {"get_stock_data", "get_indicators"} <= market_tools


@pytest.mark.unit
def test_a_share_tools_registered_in_toolnodes():
    """The A-share microstructure tools are bound to the analyst LLMs via
    bind_tools; if the executable ToolNodes don't register them, every call
    fails with 'not a valid tool' and the analysts report them 'unavailable'
    (observed in the 2026-08 re-runs before this wiring gap was fixed)."""
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    market_tools = set(nodes["market"].tools_by_name)
    news_tools = set(nodes["news"].tools_by_name)

    assert {"get_market_context", "get_dragon_tiger", "get_hot_stocks",
            "is_trading_day"} <= market_tools, (
        "A-share market tools are bound to the market analyst but missing from "
        "the market ToolNode, so the model's calls fail."
    )
    assert {"get_policy_news", "get_market_context", "is_trading_day"} <= news_tools, (
        "A-share policy/market tools are bound to the news analyst but missing "
        "from the news ToolNode."
    )
