"""Tests for the cnnews vendor (Chinese-language news: Eastmoney announcements
+ Sina 7x24 feed)."""
import sys
import types
import unittest
from unittest import mock

import pytest

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

from tradingagents.dataflows import cn_news  # noqa: E402
from tradingagents.dataflows.errors import NoMarketDataError  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _patch_get(routes):
    def fake_get(url, **kwargs):
        for substring, payload in routes:
            if substring in url:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected URL: {url}")

    return mock.patch("tradingagents.dataflows.cn_news.requests.get", side_effect=fake_get)


@pytest.mark.unit
class CnNewsTests(unittest.TestCase):
    def test_get_news_formats_announcements_and_filters_date(self):
        payload = {"data": {"list": [
            {"title": "贵州茅台:贵州茅台2026年半年度报告摘要", "notice_date": "2026-08-15 00:00:00",
             "art_code": "AB123"},
            {"title": "贵州茅台:2025年度分红方案公告", "notice_date": "2026-06-01 00:00:00",
             "art_code": "CD456"},
        ]}}
        with _patch_get([("np-anotice-stock.eastmoney.com", payload)]):
            out = cn_news.get_news("600519.SH", "2026-08-01", "2026-08-21")
        self.assertIn("半年度报告摘要", out)
        self.assertIn("东方财富公告", out)
        self.assertNotIn("分红方案", out)  # outside the window

    def test_get_news_non_a_share_raises_no_data(self):
        with self.assertRaises(NoMarketDataError):
            cn_news.get_news("AAPL", "2026-08-01", "2026-08-21")

    def test_get_global_news_filters_lookahead_and_window(self):
        payload = {"result": {"data": [
            {"title": "美联储官员讲话", "ctime": "1787155200", "media_name": "新浪财经", "intro": "摘要1"},   # 2026-08-20
            {"title": "茅台批价企稳", "ctime": "1787241600", "media_name": "上证报", "intro": "摘要2"},    # 2026-08-21
            {"title": "未来新闻", "ctime": "1787414400", "media_name": "新浪财经", "intro": ""},             # 2026-08-23 (future)
        ]}}
        with _patch_get([("feed.mix.sina.com.cn", payload)]):
            out = cn_news.get_global_news("2026-08-21", look_back_days=7, limit=5)
        self.assertIn("美联储官员讲话", out)
        self.assertIn("茅台批价企稳", out)
        self.assertNotIn("未来新闻", out)

    def test_get_global_news_empty_result(self):
        payload = {"result": {"data": []}}
        with _patch_get([("feed.mix.sina.com.cn", payload)]):
            out = cn_news.get_global_news("2026-08-21", look_back_days=7, limit=5)
        self.assertIn("No Chinese 7x24 news", out)
