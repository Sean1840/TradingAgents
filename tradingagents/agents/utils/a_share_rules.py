"""A-share market-rules context injected into every agent's system prompt.

The framework was designed for US equities (T+0, continuous pricing, no price
limits). These rules make the analysts, trader and risk team respect the A-share
microstructure (T+1, 涨跌停, ST, stamp duty, disclosure rhythm, cyclical
valuation traps). The wording is conditional so the same prompt stays harmless
for non-A-share instruments.
"""

A_SHARE_RULES_CONTEXT = """
MARKET RULES — APPLY ONLY WHEN THE INSTRUMENT IS AN A-SHARE (thscode ends with .SH/.SZ/.BJ):
- T+1 settlement: shares bought today cannot be sold until the next trading day; any buy proposal must survive an overnight gap.
- Price limits (涨跌停): main board ±10%, ChiNext/STAR (创业板/科创板) ±20%, ST ±5%, Beijing Stock Exchange ±30%. A stock at limit-up cannot be bought and at limit-down cannot be sold — never propose trades that cannot fill, and flag limit-up/down days explicitly.
- Trading unit: 100 shares per lot (科创板 200 shares); odd lots can only be sold.
- Costs: 0.05% stamp duty on sells (since 2023-08), plus commission and transfer fees — significant for high-turnover strategies.
- Disclosure rhythm: Q1 (~April), H1 (~August), Q3 (~October), annual (~next April), plus earnings previews (业绩预告) and express reports (业绩快报). A 'past week' fundamentals view is rarely meaningful — anchor on the latest reported period.
- Risk flags: ST/*ST designation and delisting rules, goodwill impairment (商誉减值), restricted-share lockups (限售解禁), major-shareholder reductions (大股东减持), pledge forced-liquidation (质押爆仓), and the '-U' suffix for not-yet-profitable companies.
- Cyclical stocks: TTM PE at a cycle peak is misleading (the 'PE trap'); cross-check PB, ROE, dividend yield and where the price cycle sits before calling a stock cheap.
- Market microstructure: retail and hot money (游资) dominate short-term moves; use limit-up pools, dragon-tiger lists and hot-rank as flow/sentiment signals when available.
"""


def get_a_share_rules_context() -> str:
    return A_SHARE_RULES_CONTEXT
