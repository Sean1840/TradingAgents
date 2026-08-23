"""A-share adaptation coverage: the market-rules context is applied to every
agent whose output reaches the report, and the special-data formatters render
the HiThink payloads the analysts consume."""
import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

from tradingagents.dataflows import hithink_special

# Every agent whose output reaches the saved report must apply the A-share
# rules context (T+1, price limits, ST, disclosure rhythm, cyclical PE trap).
AGENT_SOURCES = [
    "tradingagents/agents/analysts/fundamentals_analyst.py",
    "tradingagents/agents/analysts/market_analyst.py",
    "tradingagents/agents/analysts/news_analyst.py",
    "tradingagents/agents/analysts/sentiment_analyst.py",
    "tradingagents/agents/researchers/bull_researcher.py",
    "tradingagents/agents/researchers/bear_researcher.py",
    "tradingagents/agents/managers/research_manager.py",
    "tradingagents/agents/managers/portfolio_manager.py",
    "tradingagents/agents/trader/trader.py",
    "tradingagents/agents/risk_mgmt/aggressive_debator.py",
    "tradingagents/agents/risk_mgmt/conservative_debator.py",
    "tradingagents/agents/risk_mgmt/neutral_debator.py",
]

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.unit
@pytest.mark.parametrize("rel", AGENT_SOURCES)
def test_a_share_rules_applied_to_all_agents(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "get_a_share_rules_context" in src, (
        f"{rel} does not apply get_a_share_rules_context(); its output would "
        f"ignore A-share trading rules (T+1, price limits, ...)"
    )


class _Fake:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _patch_request(routes):
    def fake(path, params):
        for sub, payload in routes:
            if sub in path:
                return payload
        raise AssertionError(f"unexpected path: {path}")

    return mock.patch.object(hithink_special, "_request", side_effect=fake)


@pytest.mark.unit
class HithinkSpecialFormattingTests:
    def test_limit_up_pool_formats(self):
        payload = {
            "pagination": {"total": 54},
            "item": [
                {"thscode": "002412.SZ", "name": "汉森制药", "continue_day_text": "3连板",
                 "limit_up_time": "09:25", "seal_money": 150393620,
                 "limit_up_reason": "创新药"},
            ],
        }
        with _patch_request([("limit-up-pool", payload)]):
            out = hithink_special.limit_up_pool("2026-08-21")
        assert "涨停池" in out and "共 54 家" in out and "汉森制药" in out and "3连板" in out

    def test_limit_up_ladder_formats(self):
        payload = {
            "window": {"board_caps": {"two_board": 4, "three_board": 1}},
            "item": [{"date": "2026-08-21", "boards": {
                "three_board": [{"name": "汉森制药"}], "two_board": [], "four_board": [],
                "five_board": [], "six_board": [], "seven_over": [],
            }}],
        }
        with _patch_request([("limit-up-ladder", payload)]):
            out = hithink_special.limit_up_ladder("2026-08-21")
        assert "连板天梯" in out and "2板 4 家" in out and "3板 1 家" in out

    def test_dragon_tiger_filters_by_code(self):
        payload = {
            "trade_date": "2026-08-21",
            "stock_items": [
                {"thscode": "600664.SH", "ticker": "600664", "name": "哈药股份",
                 "change": 0.05, "net_value": 149273537.66},
                {"thscode": "688432.SH", "ticker": "688432", "name": "有研硅",
                 "change": -0.02, "net_value": -1000000.0},
            ],
        }
        with _patch_request([("dragon-tiger-list", payload)]):
            out = hithink_special.dragon_tiger("2026-08-21", thscode="688432.SH")
        assert "有研硅" in out and "哈药股份" not in out

    def test_unavailable_degrades_to_sentinel(self):
        with mock.patch.object(hithink_special, "_request", side_effect=RuntimeError("boom")):
            out = hithink_special.hot_stocks("day")
        assert out.startswith("DATA_UNAVAILABLE")
