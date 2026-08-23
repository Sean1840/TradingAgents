"""Tests for the shared report-chart helpers (SVG line chart, ASCII sparkline,
K-line parsing from run logs) and the log-to-report section classifier."""
import unittest

import pytest

from tradingagents.report_chart import ascii_sparkline, parse_stock_csv_blocks, svg_line_chart

SAMPLE_LOG = """
================================== Ai Message ==================================
Tool Calls:
  get_stock_data (call_00_xxx)
   Args:
    symbol: 600519.SH
    start_date: 2025-08-21
    end_date: 2026-08-21
================================= Tool Message =================================
Name: get_stock_data

# Stock data for 600519.SH from 2025-08-21 to 2026-08-21
# Total records: 2
# Adjustment: forward

Date,Open,High,Low,Close,Volume,Turnover
2026-08-20,1291.5,1291.5,1272.01,1272.83,3347231,4350012345.67
2026-08-21,1291.5,1291.5,1272.01,1272.83,3347231,4350012345.67
================================== Ai Message ==================================
"""


@pytest.mark.unit
class ReportChartTests(unittest.TestCase):
    def test_parse_stock_csv_blocks(self):
        bars = parse_stock_csv_blocks(SAMPLE_LOG)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["Date"], "2026-08-20")
        self.assertEqual(bars[-1]["Close"], 1272.83)

    def test_parse_merges_multiple_blocks_and_dedupes(self):
        log = SAMPLE_LOG + """
================================= Tool Message =================================
Name: get_stock_data

Date,Open,High,Low,Close,Volume,Turnover
2026-08-19,1300.0,1300.0,1280.0,1290.0,1000,2000.0
2026-08-20,1300.0,1300.0,1280.0,1299.0,1000,2000.0
"""
        bars = parse_stock_csv_blocks(log)
        dates = [b["Date"] for b in bars]
        self.assertEqual(dates, ["2026-08-19", "2026-08-20", "2026-08-21"])
        # later block wins on duplicate date
        self.assertEqual(bars[1]["Close"], 1299.0)

    def test_ascii_sparkline(self):
        out = ascii_sparkline([1.0, 2.0, 3.0, 4.0, 5.0], width=10)
        self.assertTrue(all(c in "▁▂▃▄▅▆▇█" for c in out))
        self.assertEqual(len(out), 5)

    def test_svg_chart_has_endpoint_marker(self):
        out = svg_line_chart(
            [("2026-08-20", 1272.83), ("2026-08-21", 1300.0)],
            endpoint_label="终点 2026-08-21 收 1300.00",
        )
        self.assertIn("<svg", out)
        self.assertIn("终点 2026-08-21 收 1300.00", out)
        self.assertIn('fill="#d9262b"', out)  # endpoint marker color


@pytest.mark.unit
class LogReportClassifierTests(unittest.TestCase):
    """classify() must route each analyst's report to the right section even
    when reports mention each other's keywords (news mentioning 基本面/ROE,
    fundamentals leaking FINAL TRANSACTION PROPOSAL + 均线)."""

    def _classify(self):
        import importlib
        import scripts.log_to_reports as mod
        mod = importlib.reload(mod)
        return mod.classify

    def test_news_report_with_fundamentals_keywords_stays_news(self):
        cls = self._classify()
        seg = (
            "# 新闻研究报告：德明利（001309.SZ）\n"
            "## 一、标的基本面定位\n"
            "警惕周期高点 PE 陷阱，需交叉验证 PB、ROE 与现金流。"
        )
        self.assertEqual(cls(seg), "news")

    def test_fundamentals_report_with_leaked_proposal_stays_fundamentals(self):
        cls = self._classify()
        seg = (
            "# 长鑫科技（688825.SH）基本面深度分析报告\n"
            "FINAL TRANSACTION PROPOSAL: **HOLD**\n"
            "## 一、公司概况\n## 三、利润表分析\n"
            "均线、ATR、RSI 等词不应改变路由。"
        )
        self.assertEqual(cls(seg), "fundamentals")

    def test_market_report_still_market(self):
        cls = self._classify()
        seg = "# 688432.SH 深度技术分析报告\nFINAL TRANSACTION PROPOSAL: **HOLD**\n均线与 RSI 解读"
        self.assertEqual(cls(seg), "market")

    def test_trader_and_sentiment_unaffected(self):
        cls = self._classify()
        self.assertEqual(
            cls("**Action**: Sell\n**Reasoning**: x\n**Position Sizing**: y"), "trader"
        )
        self.assertEqual(cls("# 情绪分析报告\n总体情绪：中性"), "sentiment")
