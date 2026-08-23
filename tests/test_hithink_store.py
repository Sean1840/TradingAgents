"""Tests for the persistent per-stock OHLCV store (hithink_store)."""
import tempfile
import unittest
from pathlib import Path

import pytest

from tradingagents.dataflows import hithink_store


@pytest.mark.unit
class HithinkStoreTests(unittest.TestCase):
    def setUp(self):
        import os

        self._tmp = Path(tempfile.mkdtemp(prefix="ht-store-"))
        os.environ["TRADINGAGENTS_OUTPUT_DIR"] = str(self._tmp)

    def tearDown(self):
        import os
        import shutil

        os.environ.pop("TRADINGAGENTS_OUTPUT_DIR", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_merge_adds_and_dedupes(self):
        rows1 = [
            {"Date": "2026-08-19", "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5, "Volume": 10, "Turnover": 15.0},
            {"Date": "2026-08-20", "Open": 1.5, "High": 2.5, "Low": 1.0, "Close": 2.0, "Volume": 20, "Turnover": 40.0},
        ]
        total, added = hithink_store.merge_ohlcv("600519.SH", rows1)
        self.assertEqual((total, added), (2, 2))

        # same date with an updated close + one brand-new date
        rows2 = [
            {"Date": "2026-08-20", "Open": 1.5, "High": 2.5, "Low": 1.0, "Close": 2.2, "Volume": 20, "Turnover": 40.0},
            {"Date": "2026-08-21", "Open": 2.2, "High": 3.0, "Low": 2.0, "Close": 2.8, "Volume": 30, "Turnover": 84.0},
        ]
        total, added = hithink_store.merge_ohlcv("600519.SH", rows2)
        self.assertEqual((total, added), (3, 1))  # deduped 08-20, added 08-21

        df = hithink_store.load_ohlcv("600519.SH")
        self.assertEqual(len(df), 3)
        self.assertEqual(df.iloc[-1]["Close"], 2.8)  # latest kept

    def test_missing_windows_chunks_empty_window(self):
        import os

        ms = hithink_store
        start = 1700000000000  # arbitrary
        end = start + 400 * 24 * 3600 * 1000  # 400 days -> two ~360d chunks
        windows = ms.missing_windows("600519.SH", start, end)
        self.assertEqual(len(windows), 2)
        self.assertTrue(all((e - s) <= ms.CHUNK_DAYS * 86400000 for s, e in windows))

    def test_missing_windows_empty_when_stored(self):
        rows = [
            {"Date": "2026-07-01", "Open": 1, "High": 2, "Low": 0.5, "Close": 1.5, "Volume": 10, "Turnover": 15.0},
            {"Date": "2026-07-02", "Open": 1.5, "High": 2.5, "Low": 1.0, "Close": 2.0, "Volume": 20, "Turnover": 40.0},
        ]
        hithink_store.merge_ohlcv("600519.SH", rows)
        start = 1782864000000  # 2026-07-01 00:00 CST
        end = 1783036800000    # 2026-07-03 00:00 CST
        self.assertEqual(hithink_store.missing_windows("600519.SH", start, end), [])
