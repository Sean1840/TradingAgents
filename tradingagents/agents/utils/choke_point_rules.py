"""Serenity-style supply-chain ("卡脖子") analysis framework for A-share agents.

Adapted from fadewalk/serenity-stock-choke (Serenity Choke Point Theory).
Injected into the fundamentals / market / choke-point analyst prompts (A-shares
only) so the LLM examines a ticker through the bottleneck lens: locate the
industry chain, find the link whose shortage would stall the whole industry,
and judge whether this company sits at that node.

The framework is a *thinking scaffold*: it must not lead to fabricated
supply-chain facts. The honesty constraint is part of the injected text.
"""

CHOKE_POINT_RULES_CONTEXT = """
SUPPLY-CHAIN (卡脖子) FRAMEWORK — APPLY ONLY FOR A-SHARES (.SH/.SZ/.BJ), AND ONLY AS A LENS, NOT AS LICENSE TO FABRICATE:

Think through the industry chain in layers: 终端产品 → 组装/集成 → 核心零部件 → 关键材料/元器件 → 上游化工/矿产/稀有气体.

For each layer ask: would a shortage here stall the entire industry? A link is a candidate "卡脖子" node when it has:
- 技术壁垒极高（专利、know-how）; or
- 产能建设周期长（2-5 年扩产窗口）; or
- 国内自给率低（依赖进口）; or
- 单一供应商 / 寡头垄断; or
- 不可替代性高（无备选方案）; or
- 地缘政治风险（出口管制、制裁）.

Then locate the ticker: which layer is this company in, and what is its one-line "卡脖子定位" (share/tech/scale at that node)?

Six exclusion rules for "真瓶颈 vs 伪概念" — exclude when any holds:
1. 有业务但不是主营（蹭热点）; 2. 国内竞争格局分散、无护城河;
3. 产能扩张太容易（壁垒低）; 4. 进口替代逻辑不成立（国外也无货）;
5. 估值已充分反映（人尽皆知的龙头）; 6. 无机构/资金关注（流动性陷阱，如日均成交额 < 5000 万）.

Cross-check with A-share flow signals when available: 龙虎榜（游资/机构席位）、融资融券、主力净流入、热股榜关注度、政策文件（国产替代/卡脖子清单）。

⚠️ HARD CONSTRAINT: 自给率、产能周期、寡头份额、供需缺口等 supply-chain facts are NOT in the data tools. If your analysis needs such numbers, state them as『待验证假设』with your reasoning — NEVER invent concrete figures. The supply-chain view complements, and must not override, the verified market snapshot and financial statements.
"""


def get_choke_point_rules_context() -> str:
    return CHOKE_POINT_RULES_CONTEXT
