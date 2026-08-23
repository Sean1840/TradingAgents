"""Persistent per-stock OHLCV store.

Each HiThink request is window-bounded, so a single run can only ever see a
limited slice of history. This store keeps every bar ever fetched, keyed by
thscode, under ``output/.store/ohlcv/<code>.csv`` (CSV: Date/Open/High/Low/
Close/Volume/Turnover). Repeated runs merge new bars in, so the accumulated
history grows past any single-request cap — later analyses can then cover a
longer time range (e.g. a 200-day SMA over 400 days of stored history) without
re-fetching anything.

Usage (framework layer, env-gated)::
    TRADINGAGENTS_HITHINK_STORE=1
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from tradingagents.report_io import output_root

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume", "Turnover"]
CHUNK_DAYS = 360  # keep single fetches within the API's window cap


def store_enabled() -> bool:
    import os

    return os.environ.get("TRADINGAGENTS_HITHINK_STORE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def store_path(thscode: str) -> Path:
    code = thscode.split(".")[0]
    return output_root() / ".store" / "ohlcv" / f"{code}.csv"


def load_ohlcv(thscode: str) -> pd.DataFrame:
    """All stored bars for a thscode (Date as datetime, sorted), or empty frame."""
    path = store_path(thscode)
    try:
        if not path.exists():
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        df = pd.read_csv(path)
        if df.empty:
            return pd.DataFrame(columns=OHLCV_COLUMNS)
        for col in ("Open", "High", "Low", "Close", "Volume", "Turnover"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"]).sort_values("Date").drop_duplicates("Date", keep="last")
        return df.reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001 — a corrupt cache must not break the run
        logger.warning("hithink store read failed for %s: %s", thscode, exc)
        return pd.DataFrame(columns=OHLCV_COLUMNS)


def merge_ohlcv(thscode: str, rows: list[dict]) -> tuple[int, int]:
    """Merge fetched rows (dicts with Date/OHLCV/Turnover) into the store.

    Returns ``(total_stored, newly_added)``.
    """
    if not rows:
        return 0, 0
    incoming = pd.DataFrame(rows)
    for col in OHLCV_COLUMNS:
        if col not in incoming.columns:
            incoming[col] = None
    incoming = incoming[OHLCV_COLUMNS]
    incoming["Date"] = pd.to_datetime(incoming["Date"], errors="coerce")
    incoming = incoming.dropna(subset=["Date"])

    stored = load_ohlcv(thscode)
    before = set(stored["Date"]) if not stored.empty else set()
    merged = pd.concat([stored, incoming], ignore_index=True)
    merged = merged.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    after = set(merged["Date"])
    added = len(after - before)

    path = store_path(thscode)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    if added:
        logger.info("hithink store %s: %d new bars (total %d)", thscode, added, len(merged))
    return len(merged), added


def missing_windows(thscode: str, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Windows of ``[start_ms, end_ms]`` that still need fetching.

    Granularity is the whole window: if the store already holds any bar inside
    it, the window counts as accumulated (it was fetched whole by an earlier
    run), so repeated runs never re-fetch. Otherwise the window is returned
    split into ``<= CHUNK_DAYS`` slices — the exact requests that grow the
    store over time (e.g. a ``--backfill`` of older history).
    """
    stored = load_ohlcv(thscode)
    if not stored.empty:
        start_dt = datetime.fromtimestamp(start_ms / 1000).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_dt = datetime.fromtimestamp(end_ms / 1000).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        has_any = bool(((stored["Date"] >= start_dt) & (stored["Date"] <= end_dt)).any())
        if has_any:
            return []

    chunks: list[tuple[int, int]] = []
    step = CHUNK_DAYS * 86400000
    cursor = start_ms
    while cursor < end_ms:
        chunks.append((cursor, min(cursor + step, end_ms)))
        cursor += step
    return chunks


def as_bars(df: pd.DataFrame) -> list[dict]:
    """Serialize a store frame into the row dicts get_stock produces."""
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "Date": r["Date"].strftime("%Y-%m-%d"),
            "Open": r["Open"], "High": r["High"], "Low": r["Low"], "Close": r["Close"],
            "Volume": r["Volume"], "Turnover": r["Turnover"],
        })
    return rows
