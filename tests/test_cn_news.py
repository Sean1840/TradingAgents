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
        self.content = payload if isinstance(payload, bytes) else None

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

    def test_get_policy_news_aggregates_rss_and_keyword_filter(self):
        rss = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>国务院常务会议：部署稳增长政策</title>
    <pubDate>Fri, 21 Aug 2026 09:00:00 GMT</pubDate>
    <link>http://example.gov.cn/a</link></item>
  <item><title>央行宣布降准0.5个百分点</title>
    <pubDate>Sat, 22 Aug 2026 10:00:00 GMT</pubDate>
    <link>http://example.gov.cn/b</link></item>
  <item><title>无日期的旧闻</title>
    <link>http://example.gov.cn/d</link></item>
  <item><title>普通科技新闻一则</title>
    <pubDate>Fri, 01 Aug 2026 08:00:00 GMT</pubDate>
    <link>http://example.gov.cn/c</link></item>
</channel></rss>""".encode("utf-8")
        zhibo = {"result": {"data": {"feed": {"list": [
            {"rich_text": "国常会：加大逆周期调节力度", "create_time": "2026-08-21 10:00:00"},
            {"rich_text": "某公司发布新品", "create_time": "2026-08-21 10:00:00"},
            {"rich_text": "央行降准落地", "create_time": "2026-08-22 10:00:00"},  # future -> dropped
        ]}}}}
        roll = {"result": {"data": [
            {"title": "证监会：稳步推进注册制改革", "ctime": "1787241600", "media_name": "新浪财经"},
            {"title": "某公司发布新品", "ctime": "1787241600", "media_name": "新浪财经"},
        ]}}

        def fake_get(url, **kwargs):
            if "xinhuanet.com" in url or "people.com.cn" in url:
                return FakeResponse(rss)
            if "zhibo.sina.com.cn" in url:
                return FakeResponse(zhibo)
            if "feed.mix.sina.com.cn" in url:
                return FakeResponse(roll)
            raise AssertionError(f"unexpected URL: {url}")

        with mock.patch("tradingagents.dataflows.cn_news.requests.get", side_effect=fake_get):
            out = cn_news.get_policy_news("2026-08-21", look_back_days=7, limit=10)
        self.assertIn("国务院常务会议：部署稳增长政策", out)
        self.assertIn("国常会：加大逆周期调节力度", out)
        self.assertIn("证监会：稳步推进注册制改革", out)
        # 2026-08-22 is after curr_date 2026-08-21 -> dropped (look-ahead)
        self.assertNotIn("央行宣布降准", out)
        self.assertNotIn("央行降准落地", out)
        # undated item dropped (look-ahead safety)
        self.assertNotIn("无日期的旧闻", out)
        # non-policy items dropped
        self.assertNotIn("某公司发布新品", out)
        # RSS item outside the look-back window dropped by the date filter
        self.assertNotIn("普通科技新闻一则", out)
