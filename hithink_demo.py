"""How to use the HiThink (Tonghuashun / 同花顺) A-share data vendor.

Setup:
  1. Get a free API key at https://fuyao.aicubes.cn/admin
  2. Make it available to the process (either):
       set HITHINK_FINANCE_API_KEY=xxxx          # Windows
       export HITHINK_FINANCE_API_KEY=xxxx       # macOS / Linux
     ...or install the hithink-finance CLI once, which stores the key in
     %APPDATA%/hithink-finance/credentials.env (this script also reads that).
  3. Run:  python hithink_demo.py

The script configures the data_vendors so A-share data flows through HiThink
(with fallback to yfinance for symbols HiThink does not cover), then calls the
same route_to_vendor() entry points the agent tools (get_stock_data,
get_indicators, get_fundamentals, ...) use under the hood.
"""
import os
from pathlib import Path

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor

# Route prices / technical indicators / fundamentals through HiThink, falling
# back to yfinance for symbols HiThink does not cover (US tickers, forex, ...).
set_config({
    "data_vendors": {
        "core_stock_apis": "hithink,yfinance",
        "technical_indicators": "hithink,yfinance",
        "fundamental_data": "hithink,yfinance",
    },
})

SYMBOL = "600519.SH"        # 贵州茅台 — full thscode (ticker + exchange suffix)
# SYMBOL = "贵州茅台"        # a Chinese name works too
# SYMBOL = "000001.SZ"       # 平安银行


def _ensure_key() -> None:
    """Locate the API key (env var first, then the CLI credentials file) and
    make it visible to the vendor via HITHINK_FINANCE_API_KEY."""
    key = os.environ.get("HITHINK_FINANCE_API_KEY")
    if not key:
        cred = Path(os.environ.get("APPDATA", "")) / "hithink-finance" / "credentials.env"
        if cred.exists():
            for line in cred.read_text(encoding="utf-8").splitlines():
                if line.startswith("HITHINK_FINANCE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        raise SystemExit(
            "HITHINK_FINANCE_API_KEY is not set. Get a key at "
            "https://fuyao.aicubes.cn/admin and export it (see top of this file)."
        )
    os.environ["HITHINK_FINANCE_API_KEY"] = key


def _show(title: str, text: str, max_lines: int = 12) -> None:
    print("=" * 70)
    print(title)
    print("=" * 70)
    lines = text.splitlines()
    for line in lines[:max_lines]:
        print(line)
    if len(lines) > max_lines:
        print(f"... ({len(lines) - max_lines} more lines)")
    print()


if __name__ == "__main__":
    _ensure_key()

    _show(
        f"1) get_stock_data({SYMBOL!r}, 2025-09-15, 2025-09-26) — daily OHLCV",
        route_to_vendor("get_stock_data", SYMBOL, "2025-09-15", "2025-09-26"),
    )

    _show(
        f"2) get_indicators({SYMBOL!r}, 'rsi', 2025-09-26, 30) — technical indicators",
        route_to_vendor("get_indicators", SYMBOL, "rsi", "2025-09-26", 30),
        max_lines=10,
    )

    _show(
        f"3) get_fundamentals({SYMBOL!r}, 2025-09-26) — company overview",
        route_to_vendor("get_fundamentals", SYMBOL, "2025-09-26"),
        max_lines=25,
    )

    _show(
        f"4) get_balance_sheet({SYMBOL!r}, annual)",
        route_to_vendor("get_balance_sheet", SYMBOL, "annual", None),
    )

    _show(
        f"5) get_cashflow({SYMBOL!r}, quarterly)",
        route_to_vendor("get_cashflow", SYMBOL, "quarterly", None),
    )

    _show(
        f"6) get_income_statement({SYMBOL!r}, quarterly)",
        route_to_vendor("get_income_statement", SYMBOL, "quarterly", None),
    )

    # Fallback: HiThink covers A-shares only. For a US ticker the router tries
    # hithink (no match -> NoMarketDataError) then falls back to yfinance. In
    # this sandbox yfinance is stubbed, so you see the graceful NO_DATA
    # sentinel the agent receives instead of a crash.
    _show(
        "7) get_stock_data('AAPL', ...) — non-A-share symbol (fallback demo)",
        route_to_vendor("get_stock_data", "AAPL", "2025-09-15", "2025-09-26"),
    )

    print("DONE")
