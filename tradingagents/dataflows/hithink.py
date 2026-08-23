# Aggregates the per-category HiThink (Tonghuashun / 同花顺) implementations
# into one module the vendor router imports from; the imports below are the
# public surface. All functions raise the vendor-error taxonomy in errors.py
# so the routing layer reacts by behavior, exactly like the other vendors.
from .hithink_fundamentals import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
)
from .hithink_indicator import get_indicator
from .hithink_stock import get_stock

__all__ = [
    "get_balance_sheet",
    "get_cashflow",
    "get_fundamentals",
    "get_income_statement",
    "get_indicator",
    "get_stock",
]
