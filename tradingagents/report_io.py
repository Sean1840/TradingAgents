"""Output layout for per-stock research deliverables and intermediate data.

Layout (root is ``TRADINGAGENTS_OUTPUT_DIR``, default ``<project>/output``)::

    output/
    ├── <股票名>-<代码>/              # per-stock folder (e.g. 江波龙-301308)
    │   ├── <YYYYMMDD-HHMMSS>/      # one folder per generation run
    │   │   ├── 分析报告.md
    │   │   └── 分析报告.html
    │   └── data/                   # intermediate data, reused across runs
    │       ├── <endpoint>_<hash>.json   # cached raw API responses
    │       └── <timestamp>-run.log      # raw run logs kept for re-analysis

Reusing ``data/`` lets a later analysis merge previously fetched data with
only the new fetches, instead of re-fetching everything (saves API calls).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

# Project root: tradingagents/report_io.py -> <project>/tradingagents
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def output_root() -> Path:
    """The deliverables root; override with TRADINGAGENTS_OUTPUT_DIR."""
    root = os.environ.get("TRADINGAGENTS_OUTPUT_DIR")
    return Path(root) if root else PROJECT_ROOT / "output"


def code_of(thscode: str) -> str:
    """Strip the exchange suffix: 301308.SZ -> 301308."""
    return thscode.split(".")[0] if "." in thscode else thscode


def stock_dir(name: str, thscode: str) -> Path:
    """``output/<股票名>-<代码>/`` — created on demand."""
    d = output_root() / f"{name}-{code_of(thscode)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir(name: str, thscode: str) -> Path:
    """``output/<股票名>-<代码>/data/`` — per-stock intermediate data."""
    d = stock_dir(name, thscode) / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(name: str, thscode: str, when: datetime | None = None) -> Path:
    """A fresh ``output/<股票名>-<代码>/<YYYYMMDD-HHMMSS>/`` run folder."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    d = stock_dir(name, thscode) / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_key(*parts) -> str:
    """A stable filename hash for a cache entry (endpoint + params, ...)."""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]
    return digest


def save_json(path: Path, payload) -> Path:
    """Write a JSON payload alongside a fetch-time stamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump({"fetched_at": int(time.time()), "data": payload}, fh, ensure_ascii=False)
    return path


def load_cached_json(path: Path, ttl_seconds: int) -> object | None:
    """Return the cached payload if fresh (within ``ttl_seconds``), else None."""
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            record = json.load(fh)
        if int(time.time()) - int(record.get("fetched_at", 0)) > ttl_seconds:
            return None
        return record.get("data")
    except (OSError, ValueError, KeyError, TypeError):
        return None
