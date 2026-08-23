"""HiThink technical indicators.

Maps the framework's ``get_indicators`` tool to HiThink OHLCV history plus the
same ``stockstats`` computation the yfinance path uses. The supported indicator
set and their descriptions come from ``indicator_descriptions.BEST_IND_PARAMS``
(single source of truth shared with the yfinance vendor).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from stockstats import wrap

from .hithink_stock import fetch_ohlcv_frame
from .indicator_descriptions import BEST_IND_PARAMS


def get_indicator(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int = 30,
) -> str:
    """Return daily values of a single technical indicator for an A-share symbol.

    Args:
        symbol: Ticker symbol of the company (thscode, bare code, or name).
        indicator: A single technical indicator name, e.g. 'rsi', 'macd'.
        curr_date: The current trading date you are trading on, YYYY-mm-dd.
        look_back_days: How many days to look back, default 30.

    Returns:
        A formatted report of ``date: value`` lines for every calendar day in
        the window (non-trading days marked as such), plus a description of the
        indicator.

    Raises:
        ValueError: the indicator is not supported.
    """
    if indicator not in BEST_IND_PARAMS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: "
            f"{list(BEST_IND_PARAMS.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - timedelta(days=look_back_days)

    # stockstats needs enough history for the slowest indicators (200 SMA), so
    # fetch a generous window ending at curr_date and compute on all of it.
    df = fetch_ohlcv_frame(symbol, curr_date)
    stock_df = wrap(df.copy())
    stock_df["Date"] = stock_df["Date"].dt.strftime("%Y-%m-%d")

    stock_df[indicator]  # triggers stockstats to calculate the indicator

    indicator_by_date: dict[str, str] = {}
    for _, row in stock_df.iterrows():
        value = row[indicator]
        indicator_by_date[row["Date"]] = "N/A" if pd.isna(value) else str(value)

    # Emit one line per calendar day in the window, marking non-trading days.
    lines: list[str] = []
    current = before
    while current <= curr_date_dt:
        date_str = current.strftime("%Y-%m-%d")
        lines.append(
            f"{date_str}: {indicator_by_date.get(date_str, 'N/A: Not a trading day (weekend or holiday)')}"
        )
        current += timedelta(days=1)

    result = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + BEST_IND_PARAMS.get(indicator, "No description available.")
    )
    return result
