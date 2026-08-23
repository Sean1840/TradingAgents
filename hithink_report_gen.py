"""Generate a self-contained research report for an A-share symbol using the
HiThink (Tonghuashun / 同花顺) data service.

Usage:
    python hithink_report_gen.py [thscode] [name] [--fresh] [--backfill N]

Examples:
    python hithink_report_gen.py 688432.SH 有研硅
    python hithink_report_gen.py 600519.SH 贵州茅台 --backfill 1

Output goes to the project output layout:
    output/<股票名>-<代码>/<生成时间>/<股票名>-<代码>_数据报告_<起>_<止>.html
    output/<股票名>-<代码>/<生成时间>/<股票名>-<代码>_数据报告_<起>_<止>.md
Raw API responses are cached under ``output/<股票名>-<代码>/data/`` (TTL
``HITHINK_REPORT_CACHE_TTL`` seconds, default 6h) so a re-run reuses prior
fetches and only pulls new data. Pass ``--fresh`` to bypass the cache.

Every fetched K-line bar is also merged into a persistent per-stock OHLCV
store, so accumulated history grows past the API's single-window cap; with
``--backfill N`` the report first pulls N extra ~360-day windows of older
history it does not yet have, then covers the FULL stored range.

The API key is read from HITHINK_FINANCE_API_KEY or the CLI credentials file
(%APPDATA%\\hithink-finance\\credentials.env).
"""
import html
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from tradingagents.dataflows.hithink_common import _date_to_ms
from tradingagents.dataflows.hithink_store import (
    as_bars,
    load_ohlcv as load_store,
    merge_ohlcv,
    missing_windows,
)
from tradingagents.report_chart import svg_line_chart
from tradingagents.report_io import (
    cache_key,
    code_of,
    data_dir,
    load_cached_json,
    run_dir,
    save_json,
)

API = "https://fuyao.aicubes.cn"
# The special-data endpoints cap ranges at "within one year"; leave margin.
ONE_YEAR_MS = 360 * 24 * 3600 * 1000
DAY_MS = 24 * 3600 * 1000

# Intermediate-data cache (per-stock output/<stock>/data/). Disabled until
# main() points it at the stock's data dir.
_CACHE_DIR: Path | None = None
_FRESH = False
_CACHE_TTL = int(os.environ.get("HITHINK_REPORT_CACHE_TTL", "21600"))


def get_key() -> str:
    key = os.environ.get("HITHINK_FINANCE_API_KEY")
    if key:
        return key
    cred = Path(os.environ.get("APPDATA", "")) / "hithink-finance" / "credentials.env"
    if cred.exists():
        for line in cred.read_text(encoding="utf-8").splitlines():
            if line.startswith("HITHINK_FINANCE_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(
        "HITHINK_FINANCE_API_KEY not set. Get a key at https://fuyao.aicubes.cn/admin"
    )


def api(path: str, params: dict, retries: int = 4, base_delay: float = 2.0):
    """GET a HiThink endpoint with bounded exponential-backoff retry on
    transient failures (HTTP 429 / 5xx / network), per the API contract.

    When ``_CACHE_DIR`` is set (the per-stock ``data/`` folder), raw responses
    are cached there keyed by endpoint+params; a fresh-enough cache hit skips
    the network call entirely, so re-running a report reuses prior fetches and
    only pulls what is new (saves API calls / rate-limit pressure).
    """
    cache_path = None
    if _CACHE_DIR and not _FRESH:
        cache_path = _CACHE_DIR / f"{cache_key(path, sorted(params.items()))}.json"
        cached = load_cached_json(cache_path, _CACHE_TTL)
        if cached is not None:
            print(f"cache hit: {path}", file=sys.stderr)
            return cached

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                API + path, params=params, headers={"X-api-key": get_key()}, timeout=30
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"{path} -> code={payload.get('code')}: {payload.get('message')}")
            data = payload.get("data")
            if cache_path is not None:
                save_json(cache_path, data)
            return data
        except requests.HTTPError as exc:
            last = exc
            if exc.response is not None and exc.response.status_code == 429 and attempt < retries:
                delay = base_delay * (2 ** attempt)
                print(f"warning: rate limited on {path}, retrying in {delay:.0f}s", file=sys.stderr)
                time.sleep(delay)
                continue
            raise
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(base_delay * (2 ** attempt))
                continue
            raise
    raise last  # pragma: no cover


def ms_to_date(ms) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def fmt_amount(v) -> str:
    if v is None:
        return "N/A"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:,.2f}万"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}"


def fmt_num(v, nd: int = 2) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return html.escape(str(v))


def fmt_ratio(v) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return html.escape(str(v))


def fmt_ind_value(v) -> str:
    """Format a financial-indicator value (API returns strings like '9.10…')."""
    if v is None:
        return "N/A"
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
        return f"{f:.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return html.escape(str(v))


# ---------------------------------------------------------------------------
# Chinese field-name maps (API field -> 中文). Unknown fields fall back to
# their raw name so nothing is ever hidden.
# ---------------------------------------------------------------------------

STATEMENT_LABELS = {
    # common
    "Date": "报告期",
    "thscode": "代码",
    "ticker": "代码",
    "period": "报告周期",
    "report_date_ms": "披露日期",
    "fiscal_year": "会计年度",
    "fiscal_period": "会计期间",
    "currency": "币种",
    # income statement
    "basic_eps": "基本每股收益",
    "operating_income": "营业收入",
    "operating_costs": "营业成本",
    "operating_expenses": "营业支出",
    "operating_profit": "营业利润",
    "profit_total": "利润总额",
    "net_profit": "净利润",
    "parent_holder_net_profit": "归母净利润",
    "income_tax_expense": "所得税费用",
    "interest_expenses": "利息支出",
    "manage_fee": "管理费用",
    "sales_fee": "销售费用",
    "research_and_development_expenses": "研发费用",
    # balance sheet
    "total_current_assets": "流动资产合计",
    "non_current_nets_total": "非流动资产合计",
    "assets_total": "资产总计",
    "total_debt": "负债合计",
    "holder_equity_total": "所有者权益合计",
    "cash": "货币资金",
    "accounts_receivable": "应收账款",
    # cash flow
    "act_cash_flow_net": "经营活动现金流量净额",
    "invest_cash_flow_net": "投资活动现金流量净额",
    "financing_cash_flow_net": "筹资活动现金流量净额",
    "cash_equivalents_net_addition": "现金及现金等价物净增加额",
    "pay_dividends_profits_interest_cash": "分配股利/利润/偿付利息支付现金",
    "pay_fixed_assets_etc_cash": "购建固定资产等支付现金",
}

VALUE_MAPS = {
    "currency": {"CNY": "人民币"},
    "period": {"quarterly": "季度", "annual": "年度"},
    "fiscal_period": {
        "Q1": "一季报", "Q2": "中报", "Q3": "三季报", "Q4": "年报", "FY": "年报",
    },
}

# Columns that are fully redundant once shown elsewhere.
STATEMENT_SKIP = {"period_end_ms", "ticker"}  # period_end_ms == Date; ticker ⊂ thscode

ABILITY_LABELS = {
    "growth": "成长能力",
    "profitability": "盈利能力",
    "solvency": "偿债能力",
    "operation": "营运能力",
    "cash-flow": "现金流能力",
}

INDICATOR_LABELS = {
    # growth
    "calculate_operating_income_yoy_growth_ratio": "营业收入同比增长率",
    "calculate_operating_profit_yoy_growth_ratio": "营业利润同比增长率",
    "total_assets_growth_ratio": "总资产增长率",
    "fixed_asset_invest_expansion_ratio": "固定资产扩张率",
    "calculate_parent_holder_net_profit_yoy_growth_ratio": "归母净利润同比增长率",
    # profitability
    "total_assets_net_ratio": "总资产净利率",
    "index_deduct_weighted_avg_roe": "扣非加权平均 ROE",
    "sale_gross_margin": "销售毛利率",
    "sale_net_interest_ratio": "销售净利率",
    "index_weighted_avg_roe": "加权平均 ROE",
    # solvency
    "current_ratio": "流动比率",
    "cash_ratio": "现金比率",
    "quick_ratio": "速动比率",
    "earned_interest_multiple": "利息保障倍数",
    "assets_debt_ratio": "资产负债率",
    # operation
    "total_assets_turnover_ratio": "总资产周转率",
    "inventory_turnover_ratio": "存货周转率",
    "long_term_debt_equity_ratio": "长期债务权益比",
    "current_assets_turnover_ratio": "流动资产周转率",
    "receive_account_turnover_ratio": "应收账款周转率",
    # cash-flow
    "net_profit_cash_content": "净利润现金含量",
    "cash_operating_index": "现金营运指数",
    "operating_cash_flow_net_divide_income": "经营现金流净额/营业收入",
    "cash_meet_invest_ratio": "现金满足投资比率",
}


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def fetch_report(thscode: str, name: str) -> dict:
    info = api("/api/meta/tickers/search", {"q": thscode, "limit": 5})
    identity = next(
        (it for it in (info or {}).get("item") or [] if it.get("thscode") == thscode),
        {"thscode": thscode, "name": name},
    )

    now_ms = int(datetime.now().timestamp() * 1000)
    start_ms = now_ms - ONE_YEAR_MS

    snapshot = api("/api/a-share/prices/snapshot", {"thscodes": thscode})
    quote = next(
        (it for it in (snapshot or {}).get("item") or [] if it.get("thscode") == thscode),
        {},
    )

    valuation = api("/api/a-share/valuations/snapshot", {"thscodes": thscode})
    val = next(
        (it for it in (valuation or {}).get("item") or [] if it.get("thscode") == thscode),
        {},
    )

    # Latest disclosed report period, best effort (Q1~Apr, H1~Aug, Q3~Oct, FY~next Apr).
    today = datetime.now()
    if today.month >= 10:
        report = f"{today.year}-3"
    elif today.month >= 8:
        report = f"{today.year}-2"
    elif today.month >= 4:
        report = f"{today.year}-1"
    else:
        report = f"{today.year - 1}-4"
    indicators = {}
    for _ in range(4):
        try:
            indicators = api("/api/a-share/financials/indicators", {"thscode": thscode, "report": report})
            break
        except RuntimeError:
            y, q = report.split("-")
            report = f"{y}-{int(q) - 1}" if int(q) > 1 else f"{int(y) - 1}-4"

    statements = {
        "income": api(
            "/api/a-share/financials/income-statements",
            {"thscode": thscode, "period": "quarterly", "limit": 8},
        ),
        "balance": api(
            "/api/a-share/financials/balance-sheets",
            {"thscode": thscode, "period": "quarterly", "limit": 4},
        ),
        "cashflow": api(
            "/api/a-share/financials/cash-flow-statements",
            {"thscode": thscode, "period": "quarterly", "limit": 8},
        ),
    }

    bars = api(
        "/api/a-share/prices/historical",
        {"thscode": thscode, "interval": "1d", "start": start_ms, "end": now_ms + DAY_MS, "adjust": "forward"},
    )

    actions = api("/api/a-share/corporate-actions/adjustment-factors", {"thscode": thscode})

    try:
        rank = api(
            "/api/a-share/special-data/hot-stock-rank-trend",
            {"thscode": thscode, "start_date": datetime.fromtimestamp(start_ms / 1000).strftime("%Y-%m-%d"),
             "end_date": today.strftime("%Y-%m-%d")},
        )
    except RuntimeError as exc:
        print(f"warning: hot-stock-rank-trend unavailable: {exc}", file=sys.stderr)
        rank = {"item": []}

    try:
        anomaly = api("/api/a-share/special-data/anomaly-analysis-stock", {"thscodes": thscode})
    except RuntimeError as exc:
        print(f"warning: anomaly-analysis unavailable: {exc}", file=sys.stderr)
        anomaly = {"item": []}

    dragon = None
    try:
        dragon = api("/api/a-share/special-data/dragon-tiger-list", {"board_type": "all"})
    except RuntimeError as exc:
        print(f"warning: dragon-tiger-list unavailable: {exc}", file=sys.stderr)

    return {
        "identity": identity, "quote": quote, "val": val,
        "indicators": indicators, "statements": statements,
        "bars": (bars or {}).get("item") or [], "actions": (actions or {}).get("item") or [],
        "rank": (rank or {}).get("item") or [], "anomaly": (anomaly or {}).get("item") or [],
        "dragon": dragon, "report_period": report,
    }


# ---------------------------------------------------------------------------
# html helpers
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#1f2937;--muted:#6b7280;--accent:#2563eb;
--up:#d9262b;--down:#0aa06e;--line:#e5e7eb;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--ink);font-family:"PingFang SC","Microsoft YaHei",
"Segoe UI",system-ui,sans-serif;line-height:1.6;padding:24px;}
.wrap{max-width:1080px;margin:0 auto;}
h1{font-size:26px;margin-bottom:4px;}
h2{font-size:19px;margin:28px 0 12px;padding-left:10px;border-left:4px solid var(--accent);}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;text-align:center;}
.kpi .label{color:var(--muted);font-size:12px;}
.kpi .value{font-size:20px;font-weight:700;margin-top:2px;}
.up{color:var(--up);} .down{color:var(--down);}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{background:#f3f4f6;text-align:right;padding:8px 10px;border-bottom:2px solid var(--line);
font-weight:600;white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;}
tr:hover td{background:#f9fafb;}
.scroll{overflow-x:auto;}
.id{font-size:11px;color:#9ca3af;font-weight:400;}
.note{color:var(--muted);font-size:12px;margin-top:8px;}
.banner{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;margin-bottom:16px;color:#78350f;font-size:13px;}
.banner b{color:#92400e;}
.chart{width:100%;height:auto;}
footer{margin-top:32px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px;}
.tag{display:inline-block;background:#eff6ff;color:var(--accent);border-radius:6px;
padding:1px 8px;font-size:12px;margin-left:6px;}
"""


def statements_table(items):
    if not items:
        return "<p class='note'>无数据</p>"
    keys = []
    for it in items:
        for k in it:
            if k not in STATEMENT_SKIP and k not in keys:
                keys.append(k)
    head = "".join(f"<th>{html.escape(STATEMENT_LABELS.get(k, k))}</th>" for k in keys)
    rows = []
    for it in items:
        cells = []
        for k in keys:
            if k == "Date":
                cells.append(f"<td>{html.escape(str(it.get(k, '')))}</td>")
            elif k == "report_date_ms":
                cells.append(f"<td>{html.escape(ms_to_date(it.get(k)) if it.get(k) else 'N/A')}</td>")
            else:
                v = it.get(k)
                if isinstance(v, (int, float)):
                    cells.append(f"<td>{fmt_amount(v)}</td>")
                else:
                    label = VALUE_MAPS.get(k, {}).get(v)
                    text = label if label is not None else ("N/A" if v is None else str(v))
                    cells.append(f"<td>{html.escape(text)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def indicators_html(data):
    if not data or not isinstance(data, dict):
        return "<p class='note'>暂无财务指标</p>"
    abilities = data.get("abilities") or []
    out = []
    for block in abilities:
        ability = block.get("ability") or "未知"
        ability_cn = ABILITY_LABELS.get(ability, ability)
        inds = block.get("indicators") or []
        rows = "".join(
            f"<tr><td>{html.escape(INDICATOR_LABELS.get(i.get('index_id') or '', i.get('index_id') or ''))}"
            f"<div class='id'>{html.escape(i.get('index_id') or '')}</div></td>"
            f"<td>{fmt_ind_value(i.get('value'))}</td></tr>"
            for i in inds
        )
        out.append(
            f"<h3 style='font-size:14px;margin:10px 0 6px;color:#374151'>{html.escape(ability_cn)}</h3>"
            f"<div class='scroll'><table><thead><tr><th>指标</th><th>数值</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
    return "".join(out)


def statements_section(title, data):
    items = []
    if isinstance(data, dict):
        raw = data.get("item") or []
        for it in raw:
            row = {"Date": ms_to_date(it["period_end_ms"])}
            row.update(it)
            items.append(row)
    items.sort(key=lambda r: r.get("Date", ""), reverse=True)
    return statements_table(items)


def _md_table(rows) -> str:
    if not rows:
        return "(无数据)"
    head = rows[0]
    out = ["| " + " | ".join(str(c) for c in head) + " |", "|" + "---|" * len(head)]
    for r in rows[1:]:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _statement_md(data, label: str) -> str:
    items = []
    if isinstance(data, dict):
        for it in data.get("item") or []:
            row = {"Date": ms_to_date(it["period_end_ms"])}
            row.update(it)
            items.append(row)
    items.sort(key=lambda r: r.get("Date", ""), reverse=True)
    if not items:
        return f"**{label}**：无数据"
    keys = []
    for it in items:
        for k in it:
            if k not in STATEMENT_SKIP and k not in keys:
                keys.append(k)
    rows = [[STATEMENT_LABELS.get(k, k) for k in keys]]
    for it in items:
        cells = []
        for k in keys:
            if k == "Date":
                cells.append(str(it.get(k, "")))
            elif k == "report_date_ms":
                cells.append(ms_to_date(it.get(k)) if it.get(k) else "N/A")
            else:
                v = it.get(k)
                if isinstance(v, (int, float)):
                    cells.append(fmt_amount(v))
                else:
                    cn = VALUE_MAPS.get(k, {}).get(v)
                    cells.append(cn if cn is not None else ("N/A" if v is None else str(v)))
        rows.append(cells)
    return f"**{label}**\n\n{_md_table(rows)}"


def _indicators_md(data) -> str:
    if not data or not isinstance(data, dict):
        return "暂无财务指标"
    blocks = []
    for block in data.get("abilities") or []:
        ability = ABILITY_LABELS.get(block.get("ability"), block.get("ability") or "未知")
        rows = [["指标", "数值"]]
        for ind in block.get("indicators") or []:
            iid = ind.get("index_id") or ""
            rows.append([f"{INDICATOR_LABELS.get(iid, iid)}（{iid}）", fmt_ind_value(ind.get("value"))])
        blocks.append(f"**{ability}**\n\n{_md_table(rows)}")
    return "\n\n".join(blocks)


def render_md(d, display_name: str, thscode: str, period: str, now_str: str,
              data_range: tuple[str, str]) -> str:
    """Markdown twin of the HTML data report (charts are HTML-only)."""
    quote, val = d["quote"], d["val"]
    bars, rank_items, actions = d["bars"], d["rank"], d["actions"]

    closes = [b["close_price"] for b in bars]
    if closes:
        hi, lo = max(closes), min(closes)
        ret = (bars[-1]["close_price"] / bars[0]["close_price"] - 1) * 100
        range_txt = (f"区间最低 {lo:,.2f} ｜ 区间最高 {hi:,.2f} ｜ 区间涨跌幅 {fmt_ratio(ret)}%"
                     f"（{ms_to_date(bars[0]['date_ms'])} → {ms_to_date(bars[-1]['date_ms'])}，前复权）")
    else:
        range_txt = "无行情数据"

    ranks = [r["rank"] for r in rank_items]
    rank_txt = "无排名数据"
    if ranks:
        best, worst = min(ranks), max(ranks)
        best_date = next(r["date"] for r in rank_items if r["rank"] == best)
        rank_txt = f"最热排名 {best}（{best_date}）｜ 最冷排名 {worst}（排名越小越热）"

    action_txt = "；".join(
        f"{ms_to_date(a['ex_date_ms'])}：每股分红 {fmt_ratio(a.get('dividend_per_share'))} 元"
        + (f"、10送{float(a.get('per_share_bonus', 0)) * 10:g}" if (a.get("per_share_bonus") or 0) > 0 else "")
        for a in sorted(actions, key=lambda x: x.get("ex_date_ms") or 0, reverse=True)
    ) or "暂无分红记录"

    anomaly_note = "当日无异动" if not d["anomaly"] else "；".join(str(a) for a in d["anomaly"])
    dragon = d["dragon"]
    if dragon and isinstance(dragon, dict):
        on_board = any(it.get("thscode") == thscode for it in dragon.get("stock_items") or [])
        dragon_note = f"最近一期龙虎榜（{dragon.get('trade_date')}）上榜：{'是' if on_board else '否'}"
    else:
        dragon_note = "龙虎榜数据不可用"

    last = quote.get("last_price")
    chg = quote.get("price_change_ratio_pct")
    quote_rows = [
        ["指标", "数值"],
        ["最新价", f"{last:,.2f}" if last is not None else "N/A"],
        ["涨跌幅", f"{chg:+.2f}%" if chg is not None else "N/A"],
        ["今开/最高/最低", f"{fmt_num(quote.get('open_price'))} / {fmt_num(quote.get('high_price'))} / {fmt_num(quote.get('low_price'))}"],
        ["成交量/成交额", f"{fmt_amount(quote.get('volume'))}股 / {fmt_amount(quote.get('turnover'))}"],
    ]

    val_rows = [["指标", "数值"]]
    for label, k in (("市盈率 PE (TTM)", "pe_ttm"), ("市盈率 PE (MRQ)", "pe_mrq"),
                     ("市净率 PB (MRQ)", "pb_mrq"), ("市销率 PS (TTM)", "ps_ttm"),
                     ("市现率 PCF (TTM)", "pcf_ttm")):
        val_rows.append([label, fmt_ratio(val.get(k))])

    return "\n".join([
        f"# {display_name}（{thscode}）数据报告",
        "",
        f"> 数据来源：同花顺 HiThink 金融数据 ｜ 生成时间：{now_str} ｜ 行情为前复权日线 ｜ 财务为最新披露报告期（{period}）",
        f"> 数据有效范围：{data_range[0]} ~ {data_range[1]}（本次分析基于该时间段的数据）",
        "> 说明：本报告为 HiThink 接口直出的原始数据，未经过 TradingAgents 多智能体分析；走势图见 HTML 版。",
        "",
        "## 一、最新行情",
        "",
        _md_table(quote_rows),
        "",
        "## 二、估值",
        "",
        _md_table(val_rows),
        "",
        f"## 三、财务指标（{period} 报告期）",
        "",
        _indicators_md(d["indicators"]),
        "",
        "## 四、利润表（季度，最近 8 期）",
        "",
        _statement_md(d["statements"]["income"], "利润表"),
        "",
        "## 五、资产负债表（季度，最近 4 期）",
        "",
        _statement_md(d["statements"]["balance"], "资产负债表"),
        "",
        "## 六、现金流量表（季度，最近 8 期）",
        "",
        _statement_md(d["statements"]["cashflow"], "现金流量表"),
        "",
        "## 七、近一年股价走势（前复权日线，图见 HTML 版）",
        "",
        range_txt,
        "",
        "## 八、热股榜排名走势（近一年，排名越小越热）",
        "",
        rank_txt,
        "",
        "## 九、分红送配历史",
        "",
        action_txt,
        "",
        "## 十、盘面异动与龙虎榜",
        "",
        f"- 当日个股异动：{anomaly_note}",
        f"- {dragon_note}",
        "",
        "---",
        "",
        "本报告由 HiThink（同花顺）金融数据自动生成，仅供信息参考，不构成任何投资建议。",
    ])


def merge_bars_into_store(thscode: str, bars: list) -> None:
    """Persist fetched bars into output/<股票名>-<代码>/data via the store."""
    rows = [{
        "Date": ms_to_date(b["date_ms"]),
        "Open": b["open_price"], "High": b["high_price"], "Low": b["low_price"],
        "Close": b["close_price"], "Volume": b["volume"], "Turnover": b["turnover"],
    } for b in bars]
    merge_ohlcv(thscode, rows)


def backfill_history(thscode: str, bars: list, extra_chunks: int) -> list:
    """Fetch older history not yet stored (``extra_chunks`` x ~360-day windows
    before the earliest fetched bar) and merge it into the store, so the
    accumulated dataset grows beyond the single-request cap.

    Returns the merged bar list (oldest first).
    """
    if not bars or extra_chunks <= 0:
        return bars
    earliest = ms_to_date(min(b["date_ms"] for b in bars))
    end_ms = _date_to_ms(earliest) - DAY_MS
    start_ms = end_ms - extra_chunks * 360 * DAY_MS
    for ms_start, ms_end in missing_windows(thscode, start_ms, end_ms):
        data = api("/api/a-share/prices/historical", {
            "thscode": thscode, "interval": "1d",
            "start": ms_start, "end": ms_end, "adjust": "forward",
        })
        items = data.get("item") if isinstance(data, dict) else []
        rows = [{
            "Date": ms_to_date(it["date_ms"]),
            "Open": it["open_price"], "High": it["high_price"], "Low": it["low_price"],
            "Close": it["close_price"], "Volume": it["volume"], "Turnover": it["turnover"],
        } for it in items]
        merge_ohlcv(thscode, rows)
    return bars_from_store(thscode) or bars


def bars_from_store(thscode: str) -> list | None:
    """Full accumulated history as the report's bar dicts (oldest first)."""
    stored = load_store(thscode)
    if stored is None or stored.empty:
        return None
    out = []
    for _, row in stored.iterrows():
        out.append({
            "date_ms": _date_to_ms(row["Date"].strftime("%Y-%m-%d")),
            "open_price": row["Open"], "high_price": row["High"], "low_price": row["Low"],
            "close_price": row["Close"], "volume": row["Volume"], "turnover": row["Turnover"],
        })
    return out


def main():
    global _CACHE_DIR, _FRESH
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    _FRESH = "--fresh" in sys.argv
    extra_chunks = 0
    if "--backfill" in args:
        idx = args.index("--backfill")
        extra_chunks = int(args[idx + 1]) if len(args) > idx + 1 else 1
        del args[idx:idx + 2]
    thscode = args[0] if args else "688432.SH"
    name = args[1] if len(args) > 1 else thscode.split(".")[0]
    # Intermediate data: raw API responses cached under output/<股票名>-<代码>/data/
    _CACHE_DIR = data_dir(name, thscode)
    d = fetch_report(thscode, name)

    # Persistent OHLCV store: accumulate every fetched bar (and, with
    # --backfill N, pull N older ~360-day windows not yet stored) so history
    # grows past any single-request cap. The report then covers the FULL range.
    bars_now = d["bars"]
    merge_bars_into_store(thscode, bars_now)
    if extra_chunks:
        d["bars"] = backfill_history(thscode, bars_now, extra_chunks)
    else:
        d["bars"] = bars_from_store(thscode) or bars_now

    identity, quote, val = d["identity"], d["quote"], d["val"]
    display_name = identity.get("name") or name
    bars = d["bars"]
    rank_items = d["rank"]

    # quote card
    last = quote.get("last_price")
    chg = quote.get("price_change_ratio_pct")
    chg_cls = "up" if (chg or 0) > 0 else ("down" if (chg or 0) < 0 else "")
    chg_sign = "+" if (chg or 0) > 0 else ""
    kpis = [
        ("最新价", f"{last:,.2f}" if last is not None else "N/A", f"{chg_sign}{chg:.2f}%" if chg is not None else ""),
        ("今开", fmt_num(quote.get("open_price")), ""),
        ("最高", fmt_num(quote.get("high_price")), ""),
        ("最低", fmt_num(quote.get("low_price")), ""),
        ("成交量", fmt_amount(quote.get("volume")) + "股" if quote.get("volume") else "N/A", ""),
        ("成交额", fmt_amount(quote.get("turnover")), ""),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="label">{html.escape(l)}</div>'
        f'<div class="value {chg_cls if i == 0 else ""}">{v}{" <span class=\'muted\' style=\'font-size:12px\'>" + s + "</span>" if s else ""}</div></div>'
        for i, (l, v, s) in enumerate(kpis)
    )

    # 1-year price stats from bars
    closes = [b["close_price"] for b in bars]
    if closes:
        hi = max(closes); lo = min(closes)
        first_bar, last_bar = bars[0], bars[-1]
        ret = (last_bar["close_price"] / first_bar["close_price"] - 1) * 100
    else:
        hi = lo = ret = None

    # MA20 for chart
    ma20 = []
    for i in range(len(closes)):
        window = closes[max(0, i - 19): i + 1]
        ma20.append(sum(window) / len(window))

    # rank stats
    ranks = [r["rank"] for r in rank_items]
    if ranks:
        best = min(ranks); worst = max(ranks)
        best_date = next(r["date"] for r in rank_items if r["rank"] == best)
    else:
        best = worst = best_date = None

    # dividends
    actions = sorted(d["actions"], key=lambda a: a.get("ex_date_ms") or 0, reverse=True)
    action_rows = "".join(
        f"<tr><td>{ms_to_date(a['ex_date_ms'])}</td>"
        f"<td>{fmt_ratio(a.get('dividend_per_share'))} 元</td>"
        f"<td>{'10送' + str(a.get('per_share_bonus') * 10) if (a.get('per_share_bonus') or 0) > 0 else '—'}</td></tr>"
        for a in actions
    )

    # anomaly / dragon-tiger notes
    anomaly_note = "当日无异动" if not d["anomaly"] else "；".join(
        html.escape(str(a)) for a in d["anomaly"]
    )
    dragon = d["dragon"]
    if dragon and isinstance(dragon, dict):
        on_board = any(
            it.get("thscode") == thscode for it in dragon.get("stock_items") or []
        )
        dragon_note = (
            f"最近一期龙虎榜（{dragon.get('trade_date')}）上榜：{'是' if on_board else '否'}"
        )
    else:
        dragon_note = "龙虎榜数据不可用"

    period = d.get("report_period", "")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    val_rows = "".join(
        f"<tr><td>{html.escape(l)}</td><td>{fmt_ratio(val.get(k))}</td></tr>"
        for l, k in (
            ("市盈率 PE (TTM)", "pe_ttm"), ("市盈率 PE (MRQ)", "pe_mrq"),
            ("市净率 PB (MRQ)", "pb_mrq"), ("市销率 PS (TTM)", "ps_ttm"),
            ("市现率 PCF (TTM)", "pcf_ttm"),
        )
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(display_name)}（{thscode}）数据报告</title>
<style>{CSS}</style>
</head>
<body><div class="wrap">
<h1>{html.escape(display_name)} <span style="font-size:15px;color:#6b7280">{thscode}</span>
<span class="tag">{html.escape(str(identity.get('exchange') or ''))} · {html.escape(str(identity.get('currency') or ''))}</span></h1>
<div class="sub">数据来源：同花顺 HiThink 金融数据 ｜ 生成时间：{now_str} ｜ 行情为前复权日线 ｜ 财务为最新披露报告期（{period}）</div>

<div class="banner"><b>报告性质说明：</b>本报告全部内容为同花顺 HiThink 接口直出的<b>原始数据</b>（行情/估值/财报/热度/分红），<b>未经过 TradingAgents 多智能体分析</b>。TradingAgents 的原生产出是各分析师（基本面/技术面/新闻情绪/多空研究/交易员/风控/组合经理）基于这些数据生成的<b>分析报告与交易决策</b>，需运行框架本体并配置 LLM API Key 才会产生。财务指标括号内为原始字段 ID，便于核对口径。</div>

<h2>一、最新行情</h2>
<div class="grid">{kpi_html}</div>

<h2>二、估值</h2>
<div class="card"><div class="scroll"><table>
<thead><tr><th>指标</th><th>数值</th></tr></thead><tbody>{val_rows}</tbody></table></div>
<div class="note">估值快照时点：{now_str}</div></div>

<h2>三、财务指标（{period} 报告期）</h2>
<div class="card">{indicators_html(d["indicators"])}</div>

<h2>四、利润表（季度，最近 8 期）</h2>
<div class="card">{statements_section("income", d["statements"]["income"])}</div>

<h2>五、资产负债表（季度，最近 4 期）</h2>
<div class="card">{statements_section("balance", d["statements"]["balance"])}</div>

<h2>六、现金流量表（季度，最近 8 期）</h2>
<div class="card">{statements_section("cashflow", d["statements"]["cashflow"])}</div>

<h2>七、近一年股价走势（前复权日线）</h2>
<div class="card">
{svg_line_chart([(ms_to_date(b["date_ms"]), b["close_price"]) for b in bars],
                min_label=f"区间最低 {lo:,.2f}" if lo is not None else None,
                max_label=f"区间最高 {hi:,.2f}" if hi is not None else None,
                endpoint_label=(f"终点 {ms_to_date(bars[-1]['date_ms'])} 收 {bars[-1]['close_price']:,.2f}"
                                if bars else None),
                extra_lines=(("#f59e0b", ma20),))}
<div class="note">黄线为 MA20。区间涨跌幅：{fmt_ratio(ret)}%（{ms_to_date(bars[0]['date_ms'])} → {ms_to_date(bars[-1]['date_ms'])}，前复权口径）</div>
</div>

<h2>八、热股榜排名走势（近一年，排名越小越热）</h2>
<div class="card">
{svg_line_chart([(r["date"], r["rank"]) for r in rank_items], color="#8b5cf6",
                min_label=f"最热排名 {best}（{best_date}）" if best else None,
                max_label=f"最冷排名 {worst}" if worst else None)}
<div class="note">排名为同花顺热股榜日排名，数值越小代表当日热度越高。</div>
</div>

<h2>九、分红送配历史</h2>
<div class="card"><div class="scroll"><table>
<thead><tr><th>除权除息日</th><th>每股现金分红</th><th>送股</th></tr></thead>
<tbody>{action_rows if action_rows else "<tr><td colspan='3'>暂无记录</td></tr>"}</tbody>
</table></div></div>

<h2>十、盘面异动与龙虎榜</h2>
<div class="card">
<div class="note">当日个股异动：{anomaly_note}</div>
<div class="note">{dragon_note}</div>
</div>

<footer>
本报告由 HiThink（同花顺）金融数据自动生成，仅供信息参考，不构成任何投资建议。
报告期/行情快照时点见各节标注；财务为披露数据（null 表示未披露）。
</footer>
</div></body></html>"""

    # 数据有效范围 = the actual window the report's bars cover.
    if d["bars"]:
        data_start = ms_to_date(d["bars"][0]["date_ms"])
        data_end = ms_to_date(d["bars"][-1]["date_ms"])
    else:
        data_start = data_end = "N/A"

    # Deliverables -> output/<股票名>-<代码>/<生成时间>/, named with stock
    # identity + the data validity range.
    run = run_dir(name, thscode)
    base = f"{name}-{code_of(thscode)}_数据报告_{data_start}_{data_end}"
    md_doc = render_md(d, display_name, thscode, period, now_str, (data_start, data_end))
    html_doc = html_doc.replace(
        '<div class="sub">',
        f'<div class="sub">数据有效范围：{data_start} ~ {data_end} ｜ ',
        1,
    )
    (run / f"{base}.md").write_text(md_doc, encoding="utf-8")
    (run / f"{base}.html").write_text(html_doc, encoding="utf-8")
    print(f"written: {run / f'{base}.md'}")
    print(f"written: {run / f'{base}.html'}")
    print(f"cached API data: {_CACHE_DIR}")


if __name__ == "__main__":
    main()
