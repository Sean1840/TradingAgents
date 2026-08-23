"""HiThink A-share fundamentals: income statement, balance sheet, cash flow
statement, and a company-overview summary.

Maps the framework's ``get_fundamentals`` / ``get_balance_sheet`` /
``get_cashflow`` / ``get_income_statement`` tools to:

    GET /api/a-share/financials/income-statements
    GET /api/a-share/financials/balance-sheets
    GET /api/a-share/financials/cash-flow-statements
    GET /api/a-share/financials/indicators
    GET /api/a-share/valuations/snapshot

Statements are returned as ``#``-header CSV (like the yfinance vendor). Rows
whose report period ends after ``curr_date`` are dropped so backtests never see
future financials (same look-ahead guard the other vendors apply).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from .errors import NoMarketDataError
from .hithink_common import (
    HithinkApiError,
    HithinkNoDataError,
    _ms_to_date,
    _request,
    resolve_symbol,
    resolve_symbol_info,
)

# Report periods are returned newest-first by the API; this many periods is a
# reasonable window for an analyst (about 2 years of quarterly reports).
STATEMENT_LIMIT = 8

# Endpoint -> (label, extra fields to keep). The statement endpoints share the
# envelope shape {timestamp, item[]} and differ only in the field names.
_STATEMENT_ENDPOINTS = {
    "income": ("/api/a-share/financials/income-statements", "Income Statement"),
    "balance": ("/api/a-share/financials/balance-sheets", "Balance Sheet"),
    "cashflow": ("/api/a-share/financials/cash-flow-statements", "Cash Flow"),
}


def _fmt_value(value):
    """Format a statement/valuation value for CSV: round floats to 2 decimals,
    keep ints and strings as-is (ticker, fiscal_year, currency, ...)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 2)
    return value


def _statements_to_csv(
    statement: str,
    symbol: str,
    freq: str = "quarterly",
    curr_date: str = None,
) -> str:
    """Fetch a multi-period statement and render it as a CSV string."""
    endpoint, label = _STATEMENT_ENDPOINTS[statement]
    thscode = resolve_symbol(symbol)

    try:
        data = _request(
            endpoint,
            {"thscode": thscode, "period": freq, "limit": STATEMENT_LIMIT},
        )
    except HithinkNoDataError as exc:
        raise NoMarketDataError(symbol, thscode, exc.message) from exc

    items = data.get("item") if isinstance(data, dict) else None
    if not items:
        raise NoMarketDataError(symbol, thscode, f"no {label} data")

    # Look-ahead guard: drop report periods that end after curr_date.
    if curr_date:
        cutoff = datetime.strptime(curr_date, "%Y-%m-%d").date()
        items = [
            it
            for it in items
            if datetime.fromtimestamp(it["period_end_ms"] / 1000).date() <= cutoff
        ]

    if not items:
        raise NoMarketDataError(
            symbol, thscode, f"no {label} periods on or before {curr_date}"
        )

    rows = []
    for it in items:
        row = {"Date": _ms_to_date(it["period_end_ms"])}
        for key, value in it.items():
            if key in ("thscode", "period_end_ms", "report_date_ms"):
                continue
            row[key] = _fmt_value(value)
        rows.append(row)

    df = pd.DataFrame(rows)
    header = f"# {label} data for {thscode} ({freq})\n"
    header += f"# Total periods: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + df.to_csv(index=False)


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    """Retrieve balance sheet data for a given A-share symbol."""
    return _statements_to_csv("balance", ticker, freq, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    """Retrieve cash flow statement data for a given A-share symbol."""
    return _statements_to_csv("cashflow", ticker, freq, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    """Retrieve income statement data for a given A-share symbol."""
    return _statements_to_csv("income", ticker, freq, curr_date)


def _latest_report(curr_date: str) -> str:
    """Best-effort latest disclosed report period (``YYYY-[1-4]``) on ``curr_date``.

    Follows the typical A-share disclosure schedule: Q1 (~April), H1 (~August),
    Q3 (~October), FY (~next April). The caller falls back one period when the
    chosen period is not yet published.
    """
    dt = datetime.strptime(curr_date, "%Y-%m-%d")
    if dt.month >= 10:
        return f"{dt.year}-3"
    if dt.month >= 8:
        return f"{dt.year}-2"
    if dt.month >= 4:
        return f"{dt.year}-1"
    return f"{dt.year - 1}-4"


def _previous_report(report: str) -> str | None:
    year, quarter = report.split("-")
    quarter = int(quarter)
    if quarter > 1:
        return f"{year}-{quarter - 1}"
    return f"{int(year) - 1}-4"


def _fetch_financial_indicators(thscode: str, curr_date: str) -> str:
    """Financial indicators (growth / profitability / solvency / operation /
    cash-flow) for the latest disclosed report period on or before curr_date.

    Returns a formatted block, or ``None`` when no report period is available
    yet (never raises: this is an enrichment layer on top of the name and
    valuation snapshot).
    """
    report = _latest_report(curr_date)
    for _ in range(4):  # at most four periods back
        try:
            data = _request(
                "/api/a-share/financials/indicators",
                {"thscode": thscode, "report": report},
            )
            break
        except (HithinkNoDataError, HithinkApiError):
            report = _previous_report(report)
            if report is None:
                return None
    else:
        return None

    if not isinstance(data, dict):
        return None
    abilities = data.get("abilities") or []
    lines = [f"### Financial indicators (report {data.get('report') or report})"]
    for block in abilities:
        ability = block.get("ability") or "unknown"
        lines.append(f"\n[{ability}]")
        for indicator in block.get("indicators") or []:
            index_id = indicator.get("index_id")
            value = indicator.get("value")
            if index_id is None:
                continue
            if value is None:
                lines.append(f"  {index_id}: N/A")
            else:
                lines.append(f"  {index_id}: {value}")
    return "\n".join(lines)


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """Retrieve a company-overview summary for a given A-share symbol.

    Combines the catalog identity (name / exchange / currency), the latest
    valuation snapshot (PE / PB / PS / PCF), and the latest disclosed financial
    indicators into the ``Label: value`` style the other vendors use.
    """
    info = resolve_symbol_info(ticker)
    thscode = info.get("thscode") or resolve_symbol(ticker)
    name = info.get("name")

    lines: list[str] = []
    if name:
        lines.append(f"Name: {name}")
    lines.append(f"Thscode: {thscode}")
    if info.get("exchange"):
        lines.append(f"Exchange: {info['exchange']}")
    if info.get("currency"):
        lines.append(f"Currency: {info['currency']}")

    try:
        valuation_data = _request(
            "/api/a-share/valuations/snapshot", {"thscodes": thscode}
        )
        item = None
        if isinstance(valuation_data, dict):
            for candidate in valuation_data.get("item") or []:
                if candidate.get("thscode") == thscode:
                    item = candidate
                    break
        if item:
            for label, key in (
                ("PE Ratio (TTM)", "pe_ttm"),
                ("PE Ratio (MRQ)", "pe_mrq"),
                ("Price to Book (MRQ)", "pb_mrq"),
                ("PS Ratio (TTM)", "ps_ttm"),
                ("PCF Ratio (TTM)", "pcf_ttm"),
            ):
                value = _fmt_value(item.get(key))
                lines.append(f"{label}: {'N/A' if value is None else value}")
    except (HithinkApiError, HithinkNoDataError):
        pass  # valuation is an enrichment layer; proceed without it

    if curr_date:
        indicators_block = _fetch_financial_indicators(thscode, curr_date)
        if indicators_block:
            lines.append("\n" + indicators_block)

    header = f"# Company Fundamentals for {thscode}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + "\n".join(lines)
