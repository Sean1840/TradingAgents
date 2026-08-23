"""Shared client, auth, error mapping, and symbol resolution for the HiThink
(Tonghuashun / 同花顺) A-share data vendor.

REST contract (authoritative source):
    https://github.com/HiThink-Tech/Financial-API
    Base URL : https://fuyao.aicubes.cn
    Auth     : HTTP header ``X-api-key: <API_KEY>``
    Envelope : ``{code, message, request_id, data}`` — business success is
               ``code == 0``; ``data`` is ``null`` on business errors.

Business error codes are mapped onto the vendor-error taxonomy in ``errors.py``
so the routing layer reacts by behavior, not by vendor:

    code range            meaning                     mapped to
    -----------           -------                     ---------
    0                     success                     -> data payload
    1xxx                  caller-fixable (params)     -> HithinkApiError (raise)
    2001 / 2003           auth / invalid key          -> HithinkNotConfiguredError
    3001 / 3002           symbol missing / not ready  -> HithinkNoDataError
                         (getters re-raise as NoMarketDataError w/ symbol)
    3004                  capability unsupported      -> HithinkApiError (raise)
    4001                  rate limit                  -> HithinkRateLimitError
    5001 / 5002 / 5003    server / upstream           -> retried, then rate-limit style
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from pytz import timezone as pytz_timezone

from tradingagents.report_io import cache_key, load_cached_json, output_root, save_json

from .errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)

logger = logging.getLogger(__name__)

API_BASE_URL = "https://fuyao.aicubes.cn"

# Network timeout (seconds) so a stalled request can't hang the CLI/agents.
REQUEST_TIMEOUT = 30

# Transient failures (network blips, HTTP 5xx, code 4001/5xxx) are retried with
# exponential backoff, mirroring the bounded-retry behavior of the other vendors.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0

# A full thscode carries the exchange suffix the API requires (e.g. 600519.SH).
_THSCODE_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_BARE_CODE_PATTERN = re.compile(r"^\d{6}$")

# Run-scoped cache of resolved symbols (symbol -> thscode). Symbol identity is
# stable, so a single run never repeats the same meta lookup.
_symbol_cache: dict[str, str] = {}

# Asia/Shanghai is the timezone the API uses for date fields (date_ms).
_SHANGHAI = pytz_timezone("Asia/Shanghai")


class HithinkApiError(Exception):
    """A non-zero business code the caller can fix (params, capability, ...).

    Not retried: the router logs it and moves to the next vendor.
    """

    def __init__(self, code: int, message: str, request_id: str = ""):
        self.code = code
        self.message = message
        self.request_id = request_id
        detail = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"HiThink API error code={code}: {message}{detail}")


class HithinkNoDataError(HithinkApiError):
    """Codes meaning the requested data does not exist or is not ready yet
    (3001 instrument not found, 3002 data not prepared). Carries no symbol
    context; the getters re-raise as ``NoMarketDataError`` with the symbol.
    """


class HithinkNotConfiguredError(VendorNotConfiguredError):
    """Raised when HiThink is selected but no API key is configured."""


class HithinkRateLimitError(VendorRateLimitError):
    """Raised when HiThink throttles the request (code 4001) or a retried
    server error never recovered."""


def _credentials_file_candidates() -> list[Path]:
    """Platform paths of the hithink-finance CLI's unified credentials file."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "hithink-finance" / "credentials.env"] if appdata else []
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return [
        home / "Library/Application Support/hithink-finance/credentials.env",
        Path(xdg) / "hithink-finance/credentials.env",
    ]


def _read_key_from_credentials_file() -> str | None:
    """Read HITHINK_FINANCE_API_KEY from the CLI credentials file, if present.

    Lets the vendor reuse the key someone already configured for the
    hithink-finance CLI instead of requiring a duplicate env var.
    """
    for path in _credentials_file_candidates():
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("HITHINK_FINANCE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        return key
        except OSError:
            continue
    return None


def get_api_key() -> str:
    """Retrieve the HiThink API key from the environment or the CLI credentials file.

    Raises:
        HithinkNotConfiguredError: when no key is available from either source.
    """
    api_key = os.getenv("HITHINK_FINANCE_API_KEY") or _read_key_from_credentials_file()
    if not api_key:
        raise HithinkNotConfiguredError(
            "HITHINK_FINANCE_API_KEY is not set. Export it, add it to your .env, "
            "or configure it via the hithink-finance CLI (which stores it in "
            "hithink-finance/credentials.env)."
        )
    return api_key


def _request(path: str, params: dict | None = None) -> dict | list | None:
    """GET a HiThink endpoint and return the ``data`` payload of a successful
    envelope (``code == 0``).

    Raises the typed vendor errors documented in the module docstring. The
    ``data`` payload may be ``None`` even on success (per the envelope
    contract), so callers must not treat a missing field as an old-style error.

    Optional disk cache: set ``TRADINGAGENTS_HITHINK_CACHE=1`` to reuse raw
    responses from ``output/.cache/hithink/`` within
    ``TRADINGAGENTS_HITHINK_CACHE_TTL`` seconds (default 3600), so repeated
    analyses merge prior fetches with only the new data instead of re-pulling
    everything.
    """
    cache_on = os.environ.get("TRADINGAGENTS_HITHINK_CACHE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    ttl = int(os.environ.get("TRADINGAGENTS_HITHINK_CACHE_TTL", "3600"))
    headers = {"X-api-key": get_api_key()}
    query = {k: v for k, v in (params or {}).items() if v is not None}
    url = f"{API_BASE_URL}{path}"

    cache_path: Path | None = None
    if cache_on:
        key = cache_key(path, sorted(query.items()))
        cache_path = output_root() / ".cache" / "hithink" / f"{key}.json"
        cached = load_cached_json(cache_path, ttl)
        if cached is not None:
            logger.info("HiThink cache hit for %s", path)
            return cached

    delay = RETRY_BASE_DELAY
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=query, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                logger.warning(
                    "HiThink network error (attempt %d/%d): %s",
                    attempt + 1, MAX_RETRIES + 1, exc,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise HithinkApiError(0, f"network error: {exc}") from exc
        except ValueError as exc:
            raise HithinkApiError(0, f"non-JSON response: {exc}") from exc

        code = payload.get("code")
        if code == 0:
            data = payload.get("data")
            if cache_path is not None:
                try:
                    save_json(cache_path, data)
                except OSError:
                    pass
            return data

        message = payload.get("message") or "unknown error"
        request_id = payload.get("request_id") or ""
        if code in (2001, 2003):
            raise HithinkNotConfiguredError(
                f"HiThink authentication failed (code={code}): {message}"
            )
        if code == 4001 or code in (5001, 5002, 5003):
            if attempt < MAX_RETRIES:
                logger.warning(
                    "HiThink transient code=%s (attempt %d/%d): %s",
                    code, attempt + 1, MAX_RETRIES + 1, message,
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise HithinkRateLimitError(
                f"HiThink code={code} after {MAX_RETRIES + 1} attempts: {message}"
            )
        if code in (3001, 3002):
            raise HithinkNoDataError(code, message, request_id)
        raise HithinkApiError(code, message, request_id)

    # Unreachable: every retryable path raises above. Kept for the type checker.
    raise HithinkApiError(0, "unreachable")  # pragma: no cover


def _date_to_ms(date_str: str) -> int:
    """Millisecond Unix timestamp for the Asia/Shanghai midnight of a date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt = _SHANGHAI.localize(dt)
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: int) -> str:
    """Format a millisecond Unix timestamp as a ``YYYY-MM-DD`` date."""
    return datetime.fromtimestamp(ms / 1000, tz=_SHANGHAI).strftime("%Y-%m-%d")


def _search_a_share(symbol: str) -> list[dict]:
    """Search the HiThink ticker catalog for an A-share symbol; returns matches."""
    data = _request("/api/meta/tickers/search", {"q": symbol, "limit": 10})
    if not isinstance(data, dict):
        return []
    items = data.get("item") or []
    return [it for it in items if it.get("asset_type") == "a-share"]


def _resolve(symbol: str) -> tuple[str, dict | None]:
    """Resolve ``symbol`` to an A-share thscode plus the matched catalog item.

    Resolution order (first match wins):
      1. Already a full thscode (``600519.SH`` / ``600519.sz``) — returned as-is.
      2. A bare 6-digit code (``600519``) or a name (``贵州茅台`` / English name)
         — searched via ``/api/meta/tickers/search``; the unique A-share match
         wins, an exact ticker/name match wins over partial matches.
      3. Anything else (``AAPL``, ``XAUUSD``, …) — ``NoMarketDataError``, so the
         vendor router falls through to the next vendor for non-A-share symbols.

    Raises:
        NoMarketDataError: the symbol cannot be resolved to an A-share thscode.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise NoMarketDataError(str(symbol), "", "empty symbol")

    key = symbol.strip()
    upper = key.upper()

    cached = _symbol_cache.get(key)
    if cached:
        return cached, None

    if _THSCODE_PATTERN.fullmatch(upper):
        _symbol_cache[key] = upper
        return upper, None

    try:
        matches = _search_a_share(key)
    except HithinkNoDataError as exc:
        raise NoMarketDataError(key, "", exc.message) from exc

    if not matches:
        raise NoMarketDataError(
            key, "", f"no A-share match for {key!r}; not covered by HiThink"
        )

    if len(matches) == 1:
        thscode = str(matches[0].get("thscode") or "")
        if not thscode:
            raise NoMarketDataError(key, "", "catalog match has no thscode")
        _symbol_cache[key] = thscode
        return thscode, matches[0]

    # Multiple candidates: prefer an exact ticker or exact name match.
    for it in matches:
        if str(it.get("ticker")) == key or str(it.get("name")) == key:
            thscode = str(it.get("thscode") or "")
            if thscode:
                _symbol_cache[key] = thscode
                return thscode, it

    candidates = "; ".join(
        f"{it.get('name')}({it.get('thscode')})" for it in matches[:5]
    )
    raise NoMarketDataError(
        key, "", f"ambiguous A-share match for {key!r}: {candidates}"
    )


def resolve_symbol(symbol: str) -> str:
    """Map a user/broker symbol to a HiThink A-share thscode (e.g. ``600519.SH``)."""
    thscode, _ = _resolve(symbol)
    return thscode


def resolve_symbol_info(symbol: str) -> dict:
    """Resolve a symbol to its catalog item (thscode, name, exchange, ...).

    Unlike :func:`resolve_symbol`, this always attempts to return the catalog
    item (even for a passthrough thscode), so callers like the fundamentals
    overview can show the company name.
    """
    thscode, item = _resolve(symbol)
    if item:
        return item
    try:
        for candidate in _search_a_share(thscode):
            if str(candidate.get("thscode")) == thscode:
                return candidate
    except HithinkApiError:
        pass
    return {"thscode": thscode}
