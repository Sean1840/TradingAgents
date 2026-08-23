"""Convert a TradingAgents run log into the per-stock output layout.

Writes, under ``output/<股票名>-<代码>/<生成时间>/``:

    分析报告.md    plain-markdown 文字版 (five sections + verdict card)
    分析报告.html  styled HTML version of the same content

The raw run log (and any trader-override markdown) is kept under
``output/<股票名>-<代码>/data/`` as intermediate data for future re-analysis.

Usage:
    python scripts/log_to_reports.py <run_log> <thscode> <name> \
        [--trader-md <trader_markdown>] [--output-dir <dir>]

Examples:
    python scripts/log_to_reports.py run.log 301308.SZ 江波龙
    python scripts/log_to_reports.py run.log 688825.SH 长鑫科技 --trader-md trader.md
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pandas as pd  # noqa: E402 — for the OHLCV-store fallback
except Exception:  # noqa: BLE001 — pandas absent; chart fallback disabled
    pd = None  # type: ignore[assignment]

from tradingagents import report_io  # noqa: E402
from tradingagents.report_chart import (  # noqa: E402
    ascii_sparkline,
    parse_stock_csv_blocks,
    svg_line_chart,
)

CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#1f2937;--muted:#6b7280;--accent:#2563eb;
--line:#e5e7eb;--sell:#d9262b;}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--ink);font-family:"PingFang SC","Microsoft YaHei",
"Segoe UI",system-ui,sans-serif;line-height:1.7;padding:24px;}
.wrap{max-width:1080px;margin:0 auto;}
h1{font-size:26px;margin-bottom:4px;}
h2{font-size:20px;margin:32px 0 12px;padding-left:10px;border-left:4px solid var(--accent);}
h3{font-size:16px;margin:18px 0 8px;color:#374151;}
h4{font-size:14px;margin:14px 0 6px;color:#4b5563;}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;
margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.04);}
.verdict{background:#fff1f2;border:1px solid #fecdd3;border-radius:10px;padding:18px 20px;
margin-bottom:16px;color:#881337;}
.verdict b{color:var(--sell);}
table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0;}
th{background:#f3f4f6;text-align:left;padding:8px 10px;border-bottom:2px solid var(--line);
font-weight:600;white-space:nowrap;}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
tr:hover td{background:#f9fafb;}
.scroll{overflow-x:auto;}
ul,ol{margin:8px 0 8px 22px;}
li{margin:3px 0;}
hr{border:none;border-top:1px solid var(--line);margin:14px 0;}
code{background:#f3f4f6;border-radius:4px;padding:0 4px;font-size:12px;}
p{margin:8px 0;}
.note{color:var(--muted);font-size:12px;margin-top:8px;}
footer{margin-top:32px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px;}
"""

NOISE = re.compile(
    r"(StockTwits|Reddit RSS|backing off|Yahoo Finance rate limited|Polymarket|"
    r"FRED_API_KEY|optional macro_data|HiThink transient|remote host|ConnectionReset)",
    re.IGNORECASE,
)
LEAK = re.compile(r"\s*hout it; do not fabricate values\.?", re.IGNORECASE)


def clean_segment(seg: str) -> str:
    seg = seg.strip()
    m = re.search(r"Tool Calls:\n", seg)
    if m:
        lines = seg[m.end():].splitlines()
        idx = 0
        while idx < len(lines) and (lines[idx].startswith(("  ", "\t")) or not lines[idx].strip()):
            idx += 1
        seg = "\n".join(lines[idx:]).strip()
    seg = re.sub(LEAK, "", seg)
    return "\n".join(l for l in seg.splitlines() if not NOISE.search(l)).strip()


def classify(seg: str) -> str | None:
    s = seg
    if "**Action**" in s and ("Reasoning" in s or "Position Sizing" in s):
        return "trader"
    # News first: its heading markers are unambiguous, and a news report may
    # mention 基本面/ROE in passing, which would otherwise be mis-routed to
    # fundamentals (observed with 德明利/江波龙/寒武纪).
    if "新闻与宏观" in s or "宏观与市场新闻" in s or "新闻研究" in s or "新闻与趋势" in s:
        return "news"
    # Sentiment next: the report is headed by 情绪分析/Overall Sentiment, a
    # strong marker. Checking it before fundamentals stops the broader
    # fundamentals markers (资产负债/现金流量/ROE appear in sentiment reports
    # too) from stealing the sentiment section.
    if "情绪分析报告" in s or "Overall Sentiment" in s or "总体情绪" in s or "市场情绪报告" in s:
        return "sentiment"
    # The fundamentals signature (基本面 + a statement-name/财务 marker) is far
    # more specific than the market heuristic below, so check it first: a
    # fundamentals report may contain leaked lines like "FINAL TRANSACTION
    # PROPOSAL" plus 均线/ATR words, which would otherwise be mis-routed to
    # market (observed with 长鑫科技 688825).
    if "基本面" in s and any(k in s for k in ("公司概况", "资产负债", "利润表", "现金流量", "历史财务", "营业成本")):
        return "fundamentals"
    if "技术分析报告" in s or ("FINAL TRANSACTION PROPOSAL" in s and
                               any(k in s for k in ("均线", "RSI", "ATR", "布林"))):
        return "market"
    return None


def extract_reports(log_text: str, trader_override: str | None = None) -> dict[str, str]:
    reports: dict[str, str] = {}
    for seg in re.split(r"={10,}", log_text):
        cleaned = clean_segment(seg)
        if not cleaned or cleaned.startswith("Name: "):
            continue
        kind = classify(cleaned)
        if kind and kind not in reports:
            if kind == "trader":
                start = cleaned.find("**Action**")
                cleaned = cleaned[start:] if start >= 0 else cleaned
            else:
                m = re.search(r"^#\s+.*$", cleaned, flags=re.MULTILINE)
                if m:
                    cleaned = cleaned[m.start():].strip()
            reports[kind] = cleaned
    if "trader" not in reports and trader_override:
        reports["trader"] = trader_override.strip()
    return reports


def extract_analysis_date(log_text: str) -> str:
    """Pull the analysis/trade date from the run log (``Analyzing X on YYYY-MM-DD``
    or ``The analysis date is YYYY-MM-DD``); falls back to 'date-unknown'."""
    m = re.search(r"\b(?:on|date is)\s+(\d{4}-\d{2}-\d{2})\b", log_text)
    return m.group(1) if m else "date-unknown"


def extract_data_range(log_text: str) -> tuple[str | None, str | None]:
    """The actual data window the run used, from the log's get_stock_data calls
    (earliest start_date .. latest end_date across all requests)."""
    starts = [d for d in re.findall(r"start_date:\s*(\d{4}-\d{2}-\d{2})", log_text)]
    ends = [d for d in re.findall(r"end_date:\s*(\d{4}-\d{2}-\d{2})", log_text)]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def auto_verdict(trader: str) -> str:
    """Build the summary card from the trader block (no hand-written text)."""
    lines = trader.splitlines()
    action = next((l for l in lines if "FINAL TRANSACTION PROPOSAL" in l), "见交易员提案")
    entry = next((l.strip() for l in lines if l.startswith("**Entry Price**")), "")
    stop = next((l.strip() for l in lines if l.startswith("**Stop Loss**")), "")
    sizing = next((l.strip() for l in lines if l.startswith("**Position Sizing**")), "")
    reasoning = ""
    in_reason = False
    for l in lines:
        if l.startswith("**Reasoning**"):
            in_reason = True
            continue
        if in_reason and l.strip():
            reasoning = re.sub(r"\*\*", "", l.strip())
            break
    parts = [f"最终交易提案：{action.strip() or '见下方'}"]
    if entry:
        parts.append(entry.strip("* "))
    if stop:
        parts.append(stop.strip("* "))
    if sizing:
        parts.append(sizing.strip("* "))
    if reasoning:
        parts.append(f"核心逻辑：{reasoning[:160]}{'…' if len(reasoning) > 160 else ''}")
    return " ｜ ".join(parts)


# ---------------------------------------------------------------------------
# markdown -> html
# ---------------------------------------------------------------------------

def inline(text: str) -> str:
    import html as H

    text = H.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2" style="color:#2563eb">\1</a>', text)
    return text


def table_html(rows):
    data = [r for r in rows if not re.match(r"^\|[\s:|-]+\|$", r)]
    if not data:
        return ""
    cells = [c.strip() for c in data[0].strip().strip("|").split("|")]
    head = "".join(f"<th>{inline(c)}</th>" for c in cells)
    body = []
    for row in data[1:]:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        body.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def md_to_html(text: str) -> str:
    lines = text.splitlines()
    out, stack, i = [], [], 0

    def close_lists():
        while stack:
            out.append(f"</{stack.pop()}>")

    while i < len(lines):
        s = lines[i].strip()
        if not s:
            close_lists()
            i += 1
            continue
        if s.startswith("|") and s.count("|") >= 2:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            close_lists()
            out.append(table_html(rows))
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            close_lists()
            lvl = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            close_lists()
            out.append("<hr/>")
            i += 1
            continue
        if re.match(r"^[-*]\s+", s):
            if not stack or stack[-1] != "ul":
                close_lists()
                out.append("<ul>")
                stack.append("ul")
            out.append(f"<li>{inline(re.sub(r'^[-*]\s+', '', s))}</li>")
            i += 1
            continue
        if re.match(r"^\d+[.、)]\s+", s):
            if not stack or stack[-1] != "ol":
                close_lists()
                out.append("<ol>")
                stack.append("ol")
            out.append(f"<li>{inline(re.sub(r'^\d+[.、)]\s+', '', s))}</li>")
            i += 1
            continue
        close_lists()
        buf, i = [s], i + 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith("|") or re.match(r"^(#{1,4})\s+|^\d+[.、)]\s+|^[-*]\s+", nxt):
                break
            buf.append(nxt)
            i += 1
        out.append(f"<p>{inline(' '.join(buf))}</p>")
    close_lists()
    return "\n".join(out)


SECTION_TITLES = [
    ("market", "一、市场/技术面分析报告"),
    ("sentiment", "二、情绪分析报告"),
    ("news", "三、新闻与宏观环境研究报告"),
    ("fundamentals", "四、基本面深度分析报告"),
    ("trader", "五、交易员最终提案"),
]


def _bars_from_store(thscode: str, log_text: str) -> list[dict]:
    """Bars for ``thscode`` from the persistent OHLCV store, clipped to the
    log's data window (earliest start_date .. latest end_date of get_stock_data
    calls). Returns ``[]`` when the store is missing/empty."""
    try:
        from tradingagents.dataflows.hithink_store import load_ohlcv
    except Exception:  # noqa: BLE001 — store unavailable; chart simply omitted
        return []
    if pd is None:
        return []
    df = load_ohlcv(thscode)
    if df is None or df.empty:
        return []
    starts = [d for d in re.findall(r"start_date:\s*(\d{4}-\d{2}-\d{2})", log_text)]
    ends = [d for d in re.findall(r"end_date:\s*(\d{4}-\d{2}-\d{2})", log_text)]
    if starts:
        df = df[df["Date"] >= pd.to_datetime(min(starts))]
    if ends:
        df = df[df["Date"] <= pd.to_datetime(max(ends))]
    if df.empty:
        return []
    return [
        {
            "Date": row.Date.strftime("%Y-%m-%d"),
            "Open": row.Open, "High": row.High,
            "Low": row.Low, "Close": row.Close,
            "Volume": row.Volume,
        }
        for row in df.itertuples(index=False)
    ]


def build_price_chart(log_text: str, thscode: str = "") -> tuple[str, str, dict]:
    """Build the price-visualization section from the bars the run fetched.

    Returns ``(svg_html, md_block, meta)`` where ``meta`` has first/last date,
    low/high and the endpoint close, matching the report's data window.
    """
    bars = parse_stock_csv_blocks(log_text)
    # Newer runs may not embed the OHLCV CSV in the log (parallel tool calls
    # print only the last tool message); fall back to the persistent store so
    # the report still carries the price chart over the analysis window.
    if not bars and thscode:
        bars = _bars_from_store(thscode, log_text)
    if not bars:
        return "", "", {}
    closes = [b["Close"] for b in bars]
    low = min(closes)
    high = max(closes)
    low_date = next(b["Date"] for b in bars if b["Close"] == low)
    high_date = next(b["Date"] for b in bars if b["Close"] == high)
    first, last = bars[0], bars[-1]
    ret = (last["Close"] / first["Close"] - 1) * 100

    ma20 = []
    for i in range(len(closes)):
        win = closes[max(0, i - 19): i + 1]
        ma20.append(sum(win) / len(win))

    meta = {
        "first_date": first["Date"], "last_date": last["Date"],
        "low": low, "low_date": low_date, "high": high, "high_date": high_date,
        "end_close": last["Close"], "ret": ret, "n": len(bars),
    }
    svg = svg_line_chart(
        [(b["Date"], b["Close"]) for b in bars],
        min_label=f"最低 {low:,.2f}（{low_date}）",
        max_label=f"最高 {high:,.2f}（{high_date}）",
        endpoint_label=f"终点 {last['Date']} 收 {last['Close']:,.2f}",
        extra_lines=(("#f59e0b", ma20),),
    )
    md = (
        f"区间 {first['Date']} → {last['Date']}（{len(bars)} 个交易日）｜ "
        f"最低 {low:,.2f}（{low_date}）｜ 最高 {high:,.2f}（{high_date}）｜ "
        f"终点（{last['Date']}）收 {last['Close']:,.2f}，区间涨跌幅 {ret:+.2f}%\n\n"
        f"走势（ASCII，每格=交易日序列）：`{ascii_sparkline(closes)}`\n\n"
        f"> 完整走势曲线（含 MA20、最低/最高/终点标注）见 HTML 版图表。"
    )
    return svg, md, meta


def render_text(name, thscode, verdict, reports, order, analysis_date, data_range,
                chart_md: str = ""):
    line = "=" * 72
    range_txt = f" ｜ 数据有效范围：{data_range[0]} ~ {data_range[1]}" if data_range[0] else ""
    parts = [line, f"{name}（{thscode}）TradingAgents 多智能体分析报告",
             f"分析日期：{analysis_date}{range_txt} ｜ 数据源：同花顺 HiThink + 东方财富/新浪中文新闻 ｜ 引擎：DeepSeek LLM", line,
             "", "【最终交易提案】", verdict, ""]
    if chart_md:
        parts += ["", "◆ 股价走势（本次分析的数据区间）", "", chart_md, ""]
    for kind, title in order:
        content = reports.get(kind)
        if not content:
            continue
        parts += [line, title, line, "", content, ""]
    parts += [line, "本报告由 TradingAgents 多智能体框架自动生成，仅供信息参考，不构成投资建议。", line]
    return "\n".join(parts)


def render_html(name, thscode, verdict, reports, order, analysis_date, data_range,
                chart_html: str = ""):
    body = []
    if chart_html:
        body.append(
            "<h2>数据快照 · 股价走势（本次分析的数据区间）</h2>"
            f"<div class='card'>{chart_html}"
            "<div class='note'>曲线为本次分析所用前复权日线（黄线 MA20）；红点为终点（数据截至日），"
            "绿/红标注分别为区间最低/最高，可与分析内容对照。</div></div>"
        )
    for kind, title in order:
        content = reports.get(kind)
        if not content:
            continue
        body.append(f"<h2>{title}</h2><div class='card'>{md_to_html(content)}</div>")
    range_txt = f" ｜ 数据有效范围：{data_range[0]} ~ {data_range[1]}" if data_range[0] else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{name}（{thscode}）TradingAgents 多智能体分析报告</title>
<style>{CSS}</style>
</head>
<body><div class="wrap">
<h1>{name}（{thscode}）TradingAgents 多智能体分析报告</h1>
<div class="sub">分析日期：{analysis_date}{range_txt} ｜ 数据源：同花顺 HiThink（行情/财务）+ 东方财富/新浪（中文新闻）｜ 分析引擎：DeepSeek LLM 多智能体 ｜ 全程中文输出</div>
<div class="verdict"><b>{verdict}</b></div>
{''.join(body)}
<footer>本报告由 TradingAgents 多智能体框架自动生成（数据：同花顺 HiThink + 东方财富/新浪中文新闻；分析：DeepSeek LLM）。仅供信息参考，不构成任何投资建议。分析日期 {analysis_date}，行情为前复权口径。</footer>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", help="path to the run log / spill file")
    ap.add_argument("thscode", help="e.g. 301308.SZ")
    ap.add_argument("name", help="stock display name, e.g. 江波龙")
    ap.add_argument("--trader-md", default=None,
                    help="markdown file with the trader proposal (when the log lacks it)")
    ap.add_argument("--output-dir", default=None, help="override TRADINGAGENTS_OUTPUT_DIR")
    args = ap.parse_args()

    if args.output_dir:
        import os
        os.environ["TRADINGAGENTS_OUTPUT_DIR"] = args.output_dir

    log_path = Path(args.log)
    text = log_path.read_text(encoding="utf-8", errors="replace")

    trader_override = None
    if args.trader_md:
        trader_override = Path(args.trader_md).read_text(encoding="utf-8").strip()

    reports = extract_reports(text, trader_override)
    missing = [k for k, _ in SECTION_TITLES if not reports.get(k)]
    if missing:
        print(f"WARN {args.thscode}: missing sections {missing}", file=sys.stderr)

    # fresh run folder for this generation
    run = report_io.run_dir(args.name, args.thscode)
    stamp = run.name
    analysis_date = extract_analysis_date(text)
    data_start, data_end = extract_data_range(text)

    verdict = auto_verdict(reports.get("trader", ""))
    chart_html, chart_md, _meta = build_price_chart(text, args.thscode)
    md = render_text(args.name, args.thscode, verdict, reports, SECTION_TITLES,
                     analysis_date, (data_start, data_end), chart_md=chart_md)
    html_doc = render_html(args.name, args.thscode, verdict, reports, SECTION_TITLES,
                           analysis_date, (data_start, data_end), chart_html=chart_html)

    # File names carry the stock identity (name + code) and the data validity
    # range (actual window the analysis was based on) so files can be shared
    # without losing context.
    valid = f"{data_start}_{data_end}" if data_start else analysis_date
    base = f"{args.name}-{report_io.code_of(args.thscode)}_TradingAgents分析报告_{valid}"
    (run / f"{base}.md").write_text(md, encoding="utf-8")
    (run / f"{base}.html").write_text(html_doc, encoding="utf-8")

    # intermediate data for future runs
    data = report_io.data_dir(args.name, args.thscode)
    shutil.copy2(log_path, data / f"{stamp}-run.log")
    if args.trader_md:
        shutil.copy2(args.trader_md, data / f"{stamp}-trader.md")

    print(f"written: {(run / f'{base}.md')}")
    print(f"written: {(run / f'{base}.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
