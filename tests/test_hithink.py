"""Tests for the HiThink (Tonghuashun / 同花顺) A-share data vendor.

Covers the vendor-error mapping, symbol resolution, OHLCV/fundamentals
formatting, the look-ahead guard, indicator output, and the router integration
(fallback to the next vendor when HiThink has no data).
"""
import copy
import os
import sys
import types
import unittest
from unittest import mock

import pytest

# The router imports yfinance-backed modules; stub them out when yfinance is
# not installed so these tests run in a minimal environment too.
try:  # pragma: no cover - environment probe
    import yfinance  # noqa: F401

    _HAS_YFINANCE = True
except ImportError:  # pragma: no cover
    _HAS_YFINANCE = False

if not _HAS_YFINANCE:  # pragma: no cover
    _fake = types.ModuleType("yfinance")
    _fake_exc = types.ModuleType("yfinance.exceptions")
    _fake_exc.YFRateLimitError = type("YFRateLimitError", (Exception,), {})
    _fake.exceptions = _fake_exc
    sys.modules.setdefault("yfinance", _fake)
    sys.modules.setdefault("yfinance.exceptions", _fake_exc)

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import hithink_common, interface
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.hithink import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_indicator,
    get_stock,
)
from tradingagents.dataflows.hithink_common import (
    HithinkApiError,
    HithinkNoDataError,
    HithinkNotConfiguredError,
    HithinkRateLimitError,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _ok(data):
    return {"code": 0, "message": "success", "request_id": "rid", "data": data}


def _patch_api(routes):
    """Patch requests.get with a dispatcher over URL substrings.

    ``routes`` is a list of ``(url_substring, payload_or_callable)``; the first
    match wins. A callable receives ``(url, kwargs)`` and returns the payload.
    """
    def fake_get(url, **kwargs):
        for substring, payload in routes:
            if substring in url:
                body = payload(url, kwargs) if callable(payload) else payload
                return FakeResponse(body)
        raise AssertionError(f"unexpected HiThink URL: {url}")

    return mock.patch("tradingagents.dataflows.hithink_common.requests.get", side_effect=fake_get)


def _reset_config():
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _clear_symbol_cache():
    hithink_common._symbol_cache.clear()


def _patch_key(value="test-key"):
    return mock.patch.dict(os.environ, {"HITHINK_FINANCE_API_KEY": value}, clear=False)


@pytest.mark.unit
class HithinkCommonTests(unittest.TestCase):
    def setUp(self):
        _clear_symbol_cache()

    def test_api_key_missing_raises_not_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch(
                    "tradingagents.dataflows.hithink_common._read_key_from_credentials_file",
                    return_value=None,
                ):
            with self.assertRaises(HithinkNotConfiguredError):
                hithink_common.get_api_key()

    def test_api_key_present(self):
        with _patch_key("secret"):
            self.assertEqual(hithink_common.get_api_key(), "secret")

    def test_api_key_falls_back_to_credentials_file(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch(
                    "tradingagents.dataflows.hithink_common._read_key_from_credentials_file",
                    return_value="file-key",
                ):
            self.assertEqual(hithink_common.get_api_key(), "file-key")

    def test_request_success_returns_data(self):
        with _patch_key(), _patch_api([("/api/meta/tickers/search", _ok({"item": []}))]):
            data = hithink_common._request("/api/meta/tickers/search", {"q": "x"})
        self.assertEqual(data, {"item": []})

    def test_request_auth_error_maps_to_not_configured(self):
        bad = {"code": 2003, "message": "invalid key", "request_id": "r"}
        with _patch_key(), _patch_api([("/api/x", bad)]):
            with self.assertRaises(HithinkNotConfiguredError):
                hithink_common._request("/api/x", {})

    def test_request_rate_limit_retries_then_raises(self):
        bad = {"code": 4001, "message": "slow down", "request_id": "r"}
        with _patch_key(), \
                _patch_api([("/api/x", bad)]), \
                mock.patch("tradingagents.dataflows.hithink_common.time.sleep"):
            with self.assertRaises(HithinkRateLimitError):
                hithink_common._request("/api/x", {})

    def test_request_no_data_code_raises_hithink_no_data(self):
        bad = {"code": 3001, "message": "no such symbol", "request_id": "r"}
        with _patch_key(), _patch_api([("/api/x", bad)]):
            with self.assertRaises(HithinkNoDataError):
                hithink_common._request("/api/x", {})

    def test_request_caller_fixable_code_raises_api_error(self):
        bad = {"code": 1002, "message": "bad param", "request_id": "r"}
        with _patch_key(), _patch_api([("/api/x", bad)]):
            with self.assertRaises(HithinkApiError):
                hithink_common._request("/api/x", {})

    def test_resolve_thscode_passthrough(self):
        with _patch_key():
            self.assertEqual(hithink_common.resolve_symbol("600519.SH"), "600519.SH")
            self.assertEqual(hithink_common.resolve_symbol("000001.sz"), "000001.SZ")

    def test_resolve_bare_code_via_search(self):
        routes = [
            (
                "/api/meta/tickers/search",
                _ok({"item": [
                    {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
                     "exchange": "SH", "asset_type": "a-share", "currency": "CNY"},
                ]}),
            )
        ]
        with _patch_key(), _patch_api(routes):
            self.assertEqual(hithink_common.resolve_symbol("600519"), "600519.SH")

    def test_resolve_name_via_search(self):
        routes = [
            (
                "/api/meta/tickers/search",
                _ok({"item": [
                    {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
                     "exchange": "SH", "asset_type": "a-share", "currency": "CNY"},
                ]}),
            )
        ]
        with _patch_key(), _patch_api(routes):
            self.assertEqual(hithink_common.resolve_symbol("贵州茅台"), "600519.SH")

    def test_resolve_no_match_raises_no_data(self):
        routes = [("/api/meta/tickers/search", _ok({"item": []}))]
        with _patch_key(), _patch_api(routes):
            with self.assertRaises(NoMarketDataError):
                hithink_common.resolve_symbol("AAPL")

    def test_resolve_ambiguous_raises_no_data(self):
        routes = [
            (
                "/api/meta/tickers/search",
                _ok({"item": [
                    {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行",
                     "asset_type": "a-share"},
                    {"thscode": "600036.SH", "ticker": "600036", "name": "招商银行",
                     "asset_type": "a-share"},
                ]}),
            )
        ]
        # "银行" matches several A-share names but equals none of them.
        with _patch_key(), _patch_api(routes):
            with self.assertRaises(NoMarketDataError):
                hithink_common.resolve_symbol("银行")


@pytest.mark.unit
class HithinkStockTests(unittest.TestCase):
    def setUp(self):
        _clear_symbol_cache()

    def test_get_stock_formats_csv(self):
        item = {
            "date_ms": hithink_common._date_to_ms("2025-09-15"),  # Asia/Shanghai midnight
            "open_price": 1463.12, "high_price": 1469.01, "low_price": 1444.23,
            "close_price": 1448.0, "volume": 3271789, "turnover": 4928374477.56,
        }
        routes = [("/api/a-share/prices/historical", _ok({"item": [item]}))]
        with _patch_key(), _patch_api(routes):
            out = get_stock("600519.SH", "2025-09-15", "2025-09-15")
        self.assertIn("# Stock data for 600519.SH", out)
        self.assertIn("2025-09-15", out)
        self.assertIn("1448.0", out)

    def test_get_stock_filters_rows_outside_range(self):
        items = [
            {
                "date_ms": hithink_common._date_to_ms("2025-09-15"),  # in range
                "open_price": 1.0, "high_price": 2.0, "low_price": 0.5,
                "close_price": 1.5, "volume": 10, "turnover": 15.0,
            },
            {
                "date_ms": hithink_common._date_to_ms("2025-09-18"),  # out of range
                "open_price": 3.0, "high_price": 4.0, "low_price": 2.5,
                "close_price": 3.5, "volume": 20, "turnover": 70.0,
            },
        ]
        routes = [("/api/a-share/prices/historical", _ok({"item": items}))]
        with _patch_key(), _patch_api(routes):
            out = get_stock("600519.SH", "2025-09-15", "2025-09-16")
        self.assertNotIn("2025-09-18", out)
        self.assertIn("# Total records: 1", out)

    def test_get_stock_empty_raises_no_data(self):
        routes = [("/api/a-share/prices/historical", _ok({"item": []}))]
        with _patch_key(), _patch_api(routes):
            with self.assertRaises(NoMarketDataError):
                get_stock("600519.SH", "2025-09-15", "2025-09-16")


@pytest.mark.unit
class HithinkFundamentalsTests(unittest.TestCase):
    def setUp(self):
        _clear_symbol_cache()

    def _statement_item(self, period_end_ms, **fields):
        base = {
            "thscode": "600519.SH", "ticker": "600519", "period": "quarterly",
            "fiscal_year": 2025, "fiscal_period": "Q3", "currency": "CNY",
        }
        base.update(fields)
        base["period_end_ms"] = period_end_ms
        return base

    def test_get_balance_sheet_lookahead_filter(self):
        # period_end 2025-09-30 is after curr_date 2025-08-01 -> must be dropped.
        items = [
            self._statement_item(1759161600000, assets_total=100.0),   # 2025-09-30
            self._statement_item(1751328000000, assets_total=99.0),    # 2025-07-01
        ]
        routes = [("/api/a-share/financials/balance-sheets", _ok({"item": items}))]
        with _patch_key(), _patch_api(routes):
            out = get_balance_sheet("600519.SH", "quarterly", "2025-08-01")
        self.assertIn("# Balance Sheet data for 600519.SH", out)
        self.assertNotIn("2025-09-30", out)
        self.assertIn("2025-07-01", out)

    def test_get_cashflow(self):
        items = [self._statement_item(1751328000000, act_cash_flow_net=12.5)]
        routes = [("/api/a-share/financials/cash-flow-statements", _ok({"item": items}))]
        with _patch_key(), _patch_api(routes):
            out = get_cashflow("600519.SH")
        self.assertIn("# Cash Flow data for 600519.SH", out)
        self.assertIn("act_cash_flow_net", out)

    def test_get_income_statement(self):
        items = [self._statement_item(1751328000000, operating_income=888.8)]
        routes = [("/api/a-share/financials/income-statements", _ok({"item": items}))]
        with _patch_key(), _patch_api(routes):
            out = get_income_statement("600519.SH")
        self.assertIn("# Income Statement data for 600519.SH", out)
        self.assertIn("operating_income", out)

    def test_get_fundamentals_composes_overview(self):
        search = _ok({"item": [
            {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
             "exchange": "SH", "asset_type": "a-share", "currency": "CNY"},
        ]})
        valuation = _ok({"timestamp": 1, "total": 1, "item": [
            {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
             "pe_ttm": 19.539033, "pb_mrq": 6.33281, "ps_ttm": 9.184688,
             "pcf_ttm": 13.360394, "pe_mrq": 17.871214},
        ]})
        indicators = _ok({
            "thscode": "600519.SH", "report": "2024-4",
            "abilities": [
                {"ability": "growth", "indicators": [
                    {"index_id": "revenue_yoy", "value": "15.4"},
                    {"index_id": "profit_yoy", "value": 10.2},
                    {"index_id": "missing", "value": None},
                ]},
            ],
        })
        routes = [
            ("/api/meta/tickers/search", search),
            ("/api/a-share/valuations/snapshot", valuation),
            ("/api/a-share/financials/indicators", indicators),
        ]
        with _patch_key(), _patch_api(routes):
            out = get_fundamentals("600519.SH", "2025-08-01")
        self.assertIn("Name: 贵州茅台", out)
        self.assertIn("PE Ratio (TTM): 19.54", out)
        self.assertIn("[growth]", out)
        self.assertIn("revenue_yoy: 15.4", out)
        self.assertIn("profit_yoy: 10.2", out)


@pytest.mark.unit
class HithinkIndicatorTests(unittest.TestCase):
    def test_unsupported_indicator_raises_value_error(self):
        with self.assertRaises(ValueError):
            get_indicator("600519.SH", "not_an_indicator", "2025-08-01", 30)

    def test_get_indicator_emits_daily_lines(self):
        import pandas as pd
        from datetime import datetime, timedelta

        start = datetime(2025, 5, 1)
        rows = []
        for i in range(60):
            base = 10.0 + i * 0.1
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            rows.append({
                "Date": d, "Open": base, "High": base + 0.5,
                "Low": base - 0.5, "Close": base + 0.2, "Volume": 1000 + i,
            })
        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["Date"])

        with mock.patch(
            "tradingagents.dataflows.hithink_indicator.fetch_ohlcv_frame",
            return_value=df,
        ):
            out = get_indicator("600519.SH", "rsi", "2025-06-29", look_back_days=3)
        self.assertIn("RSI: Measures momentum", out)
        for day in ("2025-06-27:", "2025-06-28:", "2025-06-29:"):
            self.assertIn(day, out)


@pytest.mark.unit
class HithinkLoadOhlcvTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        _reset_config()
        _clear_symbol_cache()
        self._cache_dir = tempfile.mkdtemp(prefix="hithink-cache-")
        config_module._config["data_cache_dir"] = self._cache_dir

    def tearDown(self):
        import shutil

        shutil.rmtree(self._cache_dir, ignore_errors=True)
        _reset_config()

    def test_load_ohlcv_uses_hithink_for_a_share(self):
        import pandas as pd

        from tradingagents.dataflows import stockstats_utils

        frame = pd.DataFrame([
            {"Date": pd.Timestamp("2026-08-19"), "Open": 10.0, "High": 11.0,
             "Low": 9.5, "Close": 10.5, "Volume": 100},
            {"Date": pd.Timestamp("2026-08-20"), "Open": 10.5, "High": 11.5,
             "Low": 10.0, "Close": 11.0, "Volume": 120},
        ])
        with mock.patch.dict(
            config_module._config["data_vendors"], {"core_stock_apis": "hithink"}
        ), mock.patch(
            "tradingagents.dataflows.stockstats_utils._download_ohlcv_provider_aware",
            return_value=frame,
        ) as download_mock:
            out = stockstats_utils.load_ohlcv("600519.SH", "2026-08-21")
        self.assertEqual(len(out), 2)
        download_mock.assert_called_once()

    def test_provider_aware_skips_hithink_for_non_a_share(self):
        from tradingagents.dataflows import stockstats_utils

        # "hithink" not configured and symbol is not A-share-shaped -> the
        # provider-aware download must go straight to yfinance (no HiThink call).
        with mock.patch.dict(
            config_module._config["data_vendors"], {"core_stock_apis": "yfinance"}
        ), mock.patch(
            "tradingagents.dataflows.stockstats_utils._download_ohlcv_provider_aware",
            wraps=stockstats_utils._download_ohlcv_provider_aware,
        ) as download_mock, mock.patch(
            "tradingagents.dataflows.stockstats_utils.yf_retry",
            side_effect=NoMarketDataError("AAPL", "AAPL", "no rows"),
        ):
            with self.assertRaises(NoMarketDataError):
                stockstats_utils.load_ohlcv("AAPL", "2026-08-21")
        download_mock.assert_called_once()


@pytest.mark.unit
class HithinkRouterTests(unittest.TestCase):
    def setUp(self):
        _reset_config()
        _clear_symbol_cache()

    def tearDown(self):
        _reset_config()

    def test_hithink_registered_for_core_methods(self):
        self.assertIn("hithink", interface.VENDOR_LIST)
        for method in ("get_stock_data", "get_indicators", "get_fundamentals",
                       "get_balance_sheet", "get_cashflow", "get_income_statement"):
            self.assertIn("hithink", interface.VENDOR_METHODS[method])

    def test_route_hithink_single_vendor(self):
        def _no_data(symbol, *a, **k):
            raise NoMarketDataError(symbol, symbol, "no rows")

        def _returns(value):
            def impl(symbol, *a, **k):
                return value
            return impl

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"hithink": _no_data, "yfinance": _returns("YF_DATA")}},
            clear=False,
        ), mock.patch.dict(
            config_module._config["data_vendors"], {"core_stock_apis": "hithink"}
        ):
            result = interface.route_to_vendor("get_stock_data", "600519.SH", "2025-01-01", "2025-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)

    def test_route_falls_back_within_chain(self):
        def _no_data(symbol, *a, **k):
            raise NoMarketDataError(symbol, symbol, "no rows")

        def _returns(value):
            def impl(symbol, *a, **k):
                return value
            return impl

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"hithink": _no_data, "yfinance": _returns("YF_DATA")}},
            clear=False,
        ), mock.patch.dict(
            config_module._config["data_vendors"], {"core_stock_apis": "hithink,yfinance"}
        ):
            result = interface.route_to_vendor("get_stock_data", "600519.SH", "2025-01-01", "2025-01-10")
        self.assertEqual(result, "YF_DATA")

    def test_route_hithink_success(self):
        with _patch_key(), _patch_api([
            ("/api/a-share/prices/historical", _ok({"item": [
                {
                    "date_ms": 1735689600000,  # 2025-01-01
                    "open_price": 10.0, "high_price": 11.0, "low_price": 9.5,
                    "close_price": 10.5, "volume": 100, "turnover": 1050.0,
                },
            ]})),
        ]), mock.patch.dict(
            config_module._config["data_vendors"], {"core_stock_apis": "hithink"}
        ):
            result = interface.route_to_vendor("get_stock_data", "600519.SH", "2025-01-01", "2025-01-02")
        self.assertIn("# Stock data for 600519.SH", result)
