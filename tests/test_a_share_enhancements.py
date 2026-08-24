"""Tests for the A-share enhancements: supply-chain (卡脖子) context, the
overnight global-market snapshot, Xueqiu (雪球) sentiment degradation, and the
new choke-point analyst wiring (ToolNode registration + section classifier).
"""
import unittest
import os
from unittest import mock

import pytest

from tradingagents.agents.analysts.choke_point_analyst import _AVAILABLE_TOOLS


@pytest.mark.unit
class SupplyChainContextTests(unittest.TestCase):
    def test_supply_chain_aggregates_announcements_and_constraint(self):
        from tradingagents.dataflows.supply_chain import get_supply_chain_context

        with mock.patch(
            "tradingagents.dataflows.supply_chain.get_news",
            return_value=(
                "### 某某:关于收购标的公司股权的公告 (source: 东方财富公告, 2026-08-01)\n"
                "### 某某:2026年半年度报告 (source: 东方财富公告, 2026-08-08)\n"
            ),
        ), mock.patch(
            "tradingagents.dataflows.supply_chain.dragon_tiger",
            return_value="龙虎榜（2026-08-21）：无上榜记录",
        ), mock.patch(
            "tradingagents.dataflows.supply_chain.resolve_symbol",
            return_value="688432.SH",
        ), mock.patch(
            "tradingagents.dataflows.supply_chain.resolve_symbol_info",
            return_value={"thscode": "688432.SH", "name": "有研硅"},
        ):
            out = get_supply_chain_context("688432.SH", "2026-08-21")
        self.assertIn("收购", out)                    # keyword-matched announcement
        self.assertNotIn("半年度报告", out)           # non-matching title dropped
        self.assertIn("待验证假设", out)              # honesty constraint present
        self.assertIn("禁止编造", out)

    def test_supply_chain_degrades_for_unresolvable_symbol(self):
        from tradingagents.dataflows.supply_chain import get_supply_chain_context

        with mock.patch(
            "tradingagents.dataflows.supply_chain.resolve_symbol",
            side_effect=RuntimeError("boom"),
        ):
            out = get_supply_chain_context("NOT_A_SHARE", "2026-08-21")
        self.assertTrue(out.startswith("DATA_UNAVAILABLE"))


@pytest.mark.unit
class GlobalMarketContextTests(unittest.TestCase):
    def test_snapshot_fast_fails_on_rate_limit(self):
        from tradingagents.dataflows.global_market import get_global_market_context

        with mock.patch("tradingagents.dataflows.global_market.yf_retry",
                        side_effect=RuntimeError("rate limited")):
            out = get_global_market_context("2026-08-21")
        self.assertTrue(out.startswith("## 隔夜外盘环境"))
        self.assertIn("DATA_UNAVAILABLE", out)
        self.assertIn("非对称", out)  # asymmetric-spillover caveat

    def test_snapshot_renders_symbol_rows(self):
        from tradingagents.dataflows.global_market import get_global_market_context
        import pandas as pd

        fake_df = pd.DataFrame(
            {"Open": [100.0], "Close": [105.0]},
            index=[pd.Timestamp("2026-08-20")],
        )
        calls = {"n": 0}

        def fake_yf_retry(func, max_retries=1, base_delay=1.0):
            calls["n"] += 1
            if calls["n"] <= 2:  # first two symbols succeed, third raises -> abort
                return fake_df
            raise RuntimeError("rate limited")

        with mock.patch("tradingagents.dataflows.global_market.yf_retry", fake_yf_retry):
            out = get_global_market_context("2026-08-21")
        self.assertIn("标普500", out)
        self.assertIn("+5.00%", out)


@pytest.mark.unit
class XueqiuSentimentTests(unittest.TestCase):
    def test_missing_token_degrades(self):
        from tradingagents.dataflows.xueqiu import get_xueqiu_sentiment

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XUEQIU_A_TOKEN", None)
            os.environ.pop("XUEQIU_COOKIE", None)
            out = get_xueqiu_sentiment("688432.SH", "2026-08-21")
        self.assertTrue(out.startswith("DATA_UNAVAILABLE"))
        self.assertIn("XUEQIU_A_TOKEN", out)

    def test_token_parsed_from_cookie_string(self):
        from tradingagents.dataflows.xueqiu import _token

        with mock.patch.dict(
            os.environ,
            {"XUEQIU_COOKIE": "xq_a_token=abc123; other=1"},
            clear=False,
        ):
            self.assertEqual(_token(), "abc123")

    def test_bad_thscode_degrades(self):
        from tradingagents.dataflows.xueqiu import get_xueqiu_sentiment

        with mock.patch.dict(os.environ, {"XUEQIU_A_TOKEN": "t"}, clear=False):
            out = get_xueqiu_sentiment("AAPL", "2026-08-21")
        self.assertTrue(out.startswith("DATA_UNAVAILABLE"))


@pytest.mark.unit
class ChokeAnalystWiringTests(unittest.TestCase):
    def test_tool_names_available(self):
        names = {t.name for t in _AVAILABLE_TOOLS}
        self.assertIn("get_supply_chain_context", names)
        self.assertIn("get_market_context", names)
        self.assertIn("get_verified_market_snapshot", names)

    def test_toolnodes_register_choke_tools(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        nodes = TradingAgentsGraph._create_tool_nodes(None)
        choke_tools = set(nodes["choke"].tools_by_name)
        self.assertIn("get_supply_chain_context", choke_tools)
        self.assertIn("get_market_context", choke_tools)
        # global-market tool registered for market + news; supply for market+fundamentals
        self.assertIn("get_global_market_context", set(nodes["market"].tools_by_name))
        self.assertIn("get_global_market_context", set(nodes["news"].tools_by_name))
        self.assertIn("get_supply_chain_context", set(nodes["fundamentals"].tools_by_name))

    def test_choke_report_in_initial_state(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        ta = TradingAgentsGraph(config=DEFAULT_CONFIG.copy())
        st = ta.propagator.create_initial_state("688432.SH", "2026-08-21")
        self.assertIn("choke_report", st)
        self.assertEqual(st["choke_report"], "")

    def test_section_classifier_routes_choke(self):
        from scripts.log_to_reports import classify

        seg = (
            "# 688432.SH 供应链卡脖子分析报告\n"
            "## 一、产业链定位\n瓶颈环节：半导体硅片。"
        )
        self.assertEqual(classify(seg), "choke")
        # a market report must not be stolen
        self.assertEqual(classify("# 688432.SH 深度技术分析报告\n均线与 RSI"), "market")


if __name__ == "__main__":
    unittest.main()
