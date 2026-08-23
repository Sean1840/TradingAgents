"""Tests for the shared report-chart helpers (SVG line chart, ASCII sparkline,
K-line parsing from run logs)."""
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
