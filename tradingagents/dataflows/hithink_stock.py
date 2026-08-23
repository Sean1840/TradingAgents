"""HiThink A-share daily OHLCV history.

Maps the framework's ``get_stock_data`` tool to
``GET /api/a-share/prices/historical`` and formats the result like the other
vendors: ``#`` header comments followed by a CSV body.

Adjustment: forward (前复权), matching the framework's default adjustment.
The API accepts ``none`` / ``forward`` / ``backward``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd

from .errors import NoMarketDataError
from .hithink_common import (
    HithinkNoDataError,
    _date_to_ms,
    _ms_to_date,
    _request,
    resolve_symbol,
)
from .hithink_store import (
    load_ohlcv as load_store,
    merge_ohlcv,
    missing_windows,
    store_enabled,
)

logger = logging.getLogger(__name__)

# K-line interval the API supports for single stocks.
_INTERVAL = "1d"
_DAY_MS = 24 * 60 * 60 * 1000


def get_stock(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "forward",
) -> str:
    """Return daily OHLCV rows for an A-share symbol within [start_date, end_date].

    Args:
        symbol: Ticker symbol of the company (``600519.SH``, ``600519``,
            ``贵州茅台``, or any name resolvable to an A-share thscode).
        start_date: Start date in yyyy-mm-dd format.
        end_date: End date in yyyy-mm-dd format.
        adjust: Adjustment: ``forward`` (default) / ``backward`` / ``none``.

    Returns:
        A formatted CSV string (with ``#`` header comments) containing the
        daily OHLCV data for the requested range.

    Raises:
        NoMarketDataError: the symbol cannot be resolved or has no rows.
    """
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    thscode = resolve_symbol(symbol)

    # The API window is end-exclusive at Asia/Shanghai midnight, so request one
    # day past end_date to include the end_date row (same as the yfinance path).
    start_ms = _date_to_ms(start_date)
    end_ms = _date_to_ms(end_date) + _DAY_MS

    try:
        data = _request(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": _INTERVAL,
                "start": start_ms,
                "end": end_ms,
                "adjust": adjust,
            },
        )
    except HithinkNoDataError as exc:
        raise NoMarketDataError(symbol, thscode, exc.message) from exc

    items = data.get("item") if isinstance(data, dict) else None
    rows = []
    for item in items or []:
        date = _ms_to_date(item["date_ms"])
        if start_date <= date <= end_date:
            rows.append(
                {
                    "Date": date,
                    "Open": round(item["open_price"], 2),
                    "High": round(item["high_price"], 2),
                    "Low": round(item["low_price"], 2),
                    "Close": round(item["close_price"], 2),
                    "Volume": int(item["volume"]),
                    "Turnover": round(item["turnover"], 2),
                }
            )

    if not rows:
        raise NoMarketDataError(
            symbol, thscode, f"no rows between {start_date} and {end_date}"
        )

    # Accumulate into the persistent store (TRADINGAGENTS_HITHINK_STORE=1) so
    # repeated analyses grow the stored history beyond any single-window cap.
    if store_enabled():
        try:
            merge_ohlcv(thscode, rows)
        except Exception as exc:  # noqa: BLE001 — storage must never break the fetch
            logger.warning("hithink store merge failed for %s: %s", thscode, exc)

    df = pd.DataFrame(rows)
    header = f"# Stock data for {thscode} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Adjustment: {adjust}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + df.to_csv(index=False)


def fetch_ohlcv_frame(symbol: str, curr_date: str, lookback_days: int = 500) -> pd.DataFrame:
    """Fetch a normalized OHLCV frame (Date / Open / High / Low / Close / Volume)
    ending at ``curr_date`` for indicator computation.

    The window is generous (500 calendar days) so 200-day SMAs and MACD have
    enough history; rows after ``curr_date`` are dropped to prevent look-ahead.

    When the persistent store is enabled, previously accumulated history is
    reused and only missing window slices are fetched (≤360 days each), so
    repeated analyses cover progressively longer history without re-pulling.
    """
    datetime.strptime(curr_date, "%Y-%m-%d")
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start = (curr_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = (curr_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    if store_enabled():
        thscode = resolve_symbol(symbol)
        start_ms = _date_to_ms(start)
        end_ms = _date_to_ms(end)
        for ms_start, ms_end in missing_windows(thscode, start_ms, end_ms):
            # get_stock() merges each fetched slice into the store itself.
            get_stock(thscode, _ms_to_date(ms_start), _ms_to_date(ms_end - 1))
        stored = load_store(thscode)
        if not stored.empty:
            frame = stored[(stored["Date"] >= pd.Timestamp(start))
                           & (stored["Date"] <= pd.Timestamp(curr_date))]
            if not frame.empty:
                return frame[["Date", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)

    csv_text = get_stock(symbol, start, end)
    df = pd.read_csv(StringIO(csv_text), comment="#")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(curr_date)].reset_index(drop=True)
    if df.empty:
        raise NoMarketDataError(symbol, "", f"no rows on or before {curr_date}")
    return df
