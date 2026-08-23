"""Shared report-visualization helpers: SVG line charts, ASCII sparklines, and
parsing the price bars the run logs carry.

The SVG chart marks the min / max / endpoint (最后日期) of the covered window so
the price curve can be matched against the analysis text. The ASCII sparkline
gives the Markdown twin a compact visual without an image.
"""

from __future__ import annotations

import html as H
import re


def parse_stock_csv_blocks(log_text: str) -> list[dict]:
    """Extract and merge the OHLCV bars the run fetched (get_stock_data tool
    output inside the log). Returns sorted, de-duplicated bars:
    ``[{"Date": "YYYY-MM-DD", "Open": .., "High": .., "Low": .., "Close": ..}]``.
    """
    bars: dict[str, dict] = {}
    header_re = re.compile(r"^Date,Open,High,Low,Close,Volume(?:,Turnover)?$")
    for block in re.split(r"={10,}", log_text):
        lines = block.splitlines()
        start = next((i for i, l in enumerate(lines) if header_re.match(l.strip())), None)
        if start is None:
            continue
        for line in lines[start + 1:]:
            line = line.strip()
            # The stream interleaves blank lines between CSV rows; keep going.
            if not line:
                continue
            if line.startswith("="):
                break
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                bars[parts[0]] = {
                    "Date": parts[0],
                    "Open": float(parts[1]),
                    "High": float(parts[2]),
                    "Low": float(parts[3]),
                    "Close": float(parts[4]),
                }
            except (TypeError, ValueError):
                continue
    return sorted(bars.values(), key=lambda b: b["Date"])


def ascii_sparkline(values: list[float], width: int = 60) -> str:
    """A compact unicode-block sparkline for Markdown output."""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values
    lo, hi = min(sampled), max(sampled)
    span = (hi - lo) or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(7, int((v - lo) / span * 8))] for v in sampled)


def svg_line_chart(
    points: list[tuple[str, float]],
    width: int = 960,
    height: int = 300,
    color: str = "#2563eb",
    label: str = "收盘价",
    min_label: str | None = None,
    max_label: str | None = None,
    endpoint_label: str | None = None,
    extra_lines: tuple = (),
) -> str:
    """Render an SVG polyline chart with grid, min/max and endpoint markers."""
    pad_l, pad_r, pad_t, pad_b = 46, 14, 18, 26
    if not points:
        return "<p class='note'>无数据</p>"
    values = [p[1] for p in points]
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1.0
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)
    step = iw / (n - 1) if n > 1 else 0

    def xy(i, v):
        return pad_l + i * step, pad_t + (vmax - v) / span * ih

    path = "M " + " L ".join(f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}" for i, v in enumerate(values))
    extra_paths = []
    for color2, vals in extra_lines:
        if len(vals) != n:
            continue
        pts = [(i, vals[i]) for i in range(n) if vals[i] is not None]
        if len(pts) >= 2:
            extra_paths.append(
                (color2, "M " + " L ".join(f"{xy(i, v)[0]:.1f},{xy(i, v)[1]:.1f}" for i, v in pts))
            )

    grid = ""
    for k in range(5):
        y = pad_t + ih * k / 4
        v = vmax - span * k / 4
        grid += f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#eef0f3"/>'
        grid += f'<text x="{pad_l - 6}" y="{y + 4:.1f}" font-size="11" fill="#9ca3af" text-anchor="end">{v:,.2f}</text>'
    for xi, lab in zip((0, n // 2, n - 1), (points[0][0], points[n // 2][0], points[-1][0])):
        grid += f'<text x="{pad_l + step * xi:.1f}" y="{height - 8}" font-size="11" fill="#9ca3af" text-anchor="middle">{lab}</text>'

    markers = ""
    # endpoint marker (explicit date + close, per requirement)
    if endpoint_label is not None:
        ex, ey = xy(n - 1, values[-1])
        markers += (
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4.5" fill="#d9262b"/>'
            f'<text x="{ex - 6:.1f}" y="{ey - 10:.1f}" font-size="12" fill="#d9262b" text-anchor="end" '
            f'font-weight="600">{H.escape(endpoint_label)}</text>'
        )
    # min / max markers
    i_min, i_max = values.index(vmin), values.index(vmax)
    for i, v, fill, tag in ((i_max, vmax, "#d9262b", max_label),
                            (i_min, vmin, "#0aa06e", min_label)):
        if not tag:
            continue
        x, y = xy(i, v)
        markers += (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{fill}"/>'
            f'<text x="{x + 6:.1f}" y="{y + 4:.1f}" font-size="11" fill="{fill}">{H.escape(tag)}</text>'
        )

    return f"""
<svg class="chart" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <rect width="{width}" height="{height}" fill="#fff"/>
  {grid}
  {''.join(f'<path d="{p}" fill="none" stroke="{c}" stroke-width="1.4"/>' for c, p in extra_paths)}
  <path d="{path}" fill="none" stroke="{color}" stroke-width="1.8"/>
  {markers}
  <text x="{pad_l}" y="14" font-size="12" fill="#6b7280">{H.escape(label)}（{points[0][0]} → {points[-1][0]}）</text>
</svg>"""
