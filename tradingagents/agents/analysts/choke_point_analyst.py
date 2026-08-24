"""Choke-point (卡脖子) analyst — supply-chain bottleneck view for A-shares.

A dedicated analyst that applies the Serenity supply-chain bottleneck lens
(adapted from fadewalk/serenity-stock-choke) to an A-share ticker: locate the
industry chain, identify the bottleneck node, position the company at that node,
and filter "true bottleneck" vs "theme-only" candidates using A-share flow
signals. Its output feeds the bull/bear researchers like the other analysts'
reports.

The agent only has data tools that actually exist (supply-chain context,
market context, dragon-tiger, hot stocks, verified snapshot). Supply-chain
facts with no data source (自给率/产能周期/寡头份额) are hard-constrained to
"待验证假设" — the prompt forbids fabricating numbers.
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.a_share_market_tools import (
    get_dragon_tiger,
    get_hot_stocks,
    get_market_context,
)
from tradingagents.agents.utils.a_share_rules import get_a_share_rules_context
from tradingagents.agents.utils.choke_point_rules import get_choke_point_rules_context
from tradingagents.agents.utils.supply_chain_tools import get_supply_chain_context

_AVAILABLE_TOOLS = [
    get_supply_chain_context,
    get_market_context,
    get_dragon_tiger,
    get_hot_stocks,
    get_verified_market_snapshot,
]


def create_choke_point_analyst(llm):
    def choke_point_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            """You are the Choke-Point (卡脖子) Analyst. Your job is to examine an A-share ticker through the supply-chain bottleneck lens and produce a focused supply-chain positioning report.

Follow this reasoning order:
1. **Industry chain location**: describe the chain the company belongs to (终端产品 → 组装/集成 → 核心零部件 → 关键材料/元器件 → 上游化工/矿产/稀有气体) and name the layer this company sits in, with the evidence you actually have (announcements, concept tags, business profile).
2. **Bottleneck candidate**: identify the link whose shortage would stall the whole industry, and judge whether this company is at that node. Apply the criteria (技术壁垒/扩产周期/自给率/寡头/不可替代/地缘风险) — but only as a checklist.
3. **Signals cross-check**: use the market context (limit-up pools, ladder), dragon-tiger (机构/游资), hot-rank (关注度) and the verified snapshot to judge whether the market is already pricing this thesis.
4. **True-bottleneck filter**: apply the six exclusion rules (蹭热点/格局分散/壁垒低/进口替代不成立/估值充分/流动性陷阱) and state which hold for this ticker.
5. **One-line 卡脖子定位**: the company's position at its node, plus 2-3 待验证假设 the trader should verify before acting.

Call get_supply_chain_context first for the ticker and current date. Use get_verified_market_snapshot as the source of truth for any exact price/indicator claim, exactly like the market analyst. Do not produce a BUY/HOLD/SELL proposal — your output feeds the researchers, who will weigh it against the other analysts' reports.
"""
            + get_language_instruction()
            + get_a_share_rules_context()
            + get_choke_point_rules_context()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([t.name for t in _AVAILABLE_TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(_AVAILABLE_TOOLS)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "choke_report": report,
        }

    return choke_point_analyst_node
