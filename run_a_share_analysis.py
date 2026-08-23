"""Run the full TradingAgents multi-agent analysis for an A-share symbol,
combining the HiThink (同花顺) data vendor with an LLM provider.

This is the "两者结合" entry point: data comes from HiThink, analysis comes
from the TradingAgents LLM agent pipeline (fundamentals / market / news /
sentiment analysts -> bull & bear researchers -> trader -> risk management
team -> portfolio manager final decision).

Usage:
    python run_a_share_analysis.py [ticker] [date]

Examples:
    python run_a_share_analysis.py 600519.SH 2026-08-21
    python run_a_share_analysis.py 688432.SH 2026-08-21
    python run_a_share_analysis.py 贵州茅台 2026-08-21

Required setup (in this repo's .env, or as exported env vars):

    # --- LLM provider (pick one; keys at each provider's console) ---
    TRADINGAGENTS_LLM_PROVIDER=deepseek
    DEEPSEEK_API_KEY=sk-xxxxxxxx
    TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-pro
    TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4-flash

    # Other supported providers: qwen/qwen-cn (DASHSCOPE_API_KEY), glm/glm-cn
    # (ZHIPU_API_KEY), kimi (MOONSHOT_API_KEY), openai, anthropic, minimax,
    # openrouter, groq, nvidia, mistral, ollama (no key), openai_compatible.

    # --- Output language ---
    TRADINGAGENTS_OUTPUT_LANGUAGE=Chinese

    # --- Data vendors: HiThink for A-share data, fall back to yfinance ---
    # (this JSON override is applied by TRADINGAGENTS_DATA_VENDORS; the
    # HiThink key itself lives at %APPDATA%\\hithink-finance\\credentials.env
    # or in HITHINK_FINANCE_API_KEY)
    TRADINGAGENTS_DATA_VENDORS={"core_stock_apis":"hithink,yfinance","technical_indicators":"hithink,yfinance","fundamental_data":"hithink,yfinance"}

First install the project deps:  pip install -e .
"""
import sys

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

ticker = sys.argv[1] if len(sys.argv) > 1 else "600519.SH"
date = sys.argv[2] if len(sys.argv) > 2 else "2026-08-21"

# DEFAULT_CONFIG already applied the TRADINGAGENTS_* env overrides (LLM
# provider, models, data vendors) at import time, so .env alone configures
# the whole run — no code edits needed.
config = DEFAULT_CONFIG.copy()

print(f"Analyzing {ticker} on {date} ...")
print(f"LLM provider: {config['llm_provider']} | deep_think: {config['deep_think_llm']} | quick_think: {config['quick_think_llm']}")
print(f"Data vendors: {config['data_vendors']}")

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate(ticker, date)
print(decision)
