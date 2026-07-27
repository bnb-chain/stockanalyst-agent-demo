"""Read-only chain tools exposed to this agent's LLM (ADK FunctionTool wrap).

Each entry in ``LLM_READ_TOOLS`` is a function wrapped as an ADK ``FunctionTool``. The
LLM may call any tool in this list while producing the deliverable (the
``notify_funded`` work step); each function's docstring becomes the description
the LLM sees.

You own this file — edit ``LLM_READ_TOOLS`` to control exactly what your
agent can read. Lines for features your project doesn't use are commented out
by default; uncomment after you've added the dependency to ``studio.toml``.

**All tools are read-only** by the studio definition: no
on-chain state change, no transferable authority, no transaction signing, no
EIP-712 typed-data signing. The agent IS the sole on-chain signer,
but ALL of its signing — quote-sign, submit_result, settle, plus the
automatic budget-gated Pieverse LLM-credit auto-renew inside ``load_model()`` —
lives in ``signing.py`` as FIXED entrypoint code and is NEVER a tool the LLM
can invoke. The LLM only produces work text after a job is verified funded; it
can never price, sign, spend, or mutate chain state. Keep this list read-only.

(``pieverse_usage`` is the one exception in the underlying module: it does a
SIWE EIP-191 personal_sign, domain-locked to llm.pieverse.io, no on-chain
effect. It is commented out below.)
"""
from __future__ import annotations

from bnbagent_studio_core.tools import chain_readonly as cr
from google.adk.tools import FunctionTool

from analysis import fetch_options_sentiment, fetch_quote, fetch_technical_signals
from data_sources import (
    fetch_alpha_vantage_sentiment,
    fetch_gnews_headlines,
    fetch_insider_trades,
    fetch_macro_context,
)

# ── Stock market data tools ───────────────────────────────────────────────────

def get_stock_quote(symbol: str) -> dict:
    """Get current price, fundamentals (PE, PB, forward PE, PEG, beta, short float),
    revenue growth, gross margins, market cap, analyst target, and 52-week range
    for a stock symbol (e.g. 'AAPL', 'TSLA', '0700.HK'). Call this for every
    symbol before writing the analysis."""
    return fetch_quote(symbol)


def get_technical_signals(symbol: str) -> dict:
    """Get 1-year price history with comprehensive technical indicators:
    RSI-14, weekly RSI, MACD with crossover signal, Bollinger Bands (position),
    MA50/MA200 (golden/death cross detection), ADX (trend strength + direction),
    OBV (volume divergence), ATR-14 (daily volatility), 95% VaR,
    1M/3M/6M price change %, 10-day avg volume, and 1-year Sharpe ratio.
    Call this for every symbol before writing the analysis."""
    return fetch_technical_signals(symbol)


def get_options_sentiment(symbol: str) -> dict:
    """Get options market sentiment for a stock: put/call ratio (volume and open
    interest), weighted-average implied volatility, and a bullish/bearish/neutral
    signal based on the PCR. Uses the nearest expiration date. Requires no API key."""
    return fetch_options_sentiment(symbol)


# ── Macro & alternative data tools ───────────────────────────────────────────

def get_macro_context() -> dict:
    """Get current macroeconomic indicators: Fed funds rate, 10Y treasury yield,
    CPI year-over-year inflation, unemployment rate, and VIX fear index.
    Requires FRED_API_KEY for FRED data; VIX is always available via yfinance.
    Call once per report to set the market backdrop."""
    return fetch_macro_context()


def get_insider_activity(symbol: str) -> dict:
    """Get recent corporate insider trading activity (Form 4 filings) from
    SEC EDGAR for the past 90 days. Returns filing count, recent dates, and an
    activity signal (high/moderate/low). No API key required. Only works for
    US-listed stocks."""
    return fetch_insider_trades(symbol, days=90)


def get_news_sentiment(symbol: str) -> dict:
    """Get news sentiment and recent headlines for a stock. Returns:
    - Alpha Vantage AI sentiment score [-1, 1] with bullish/bearish label (requires ALPHA_VANTAGE_API_KEY)
    - Top 5 recent news headlines from GNews (requires GNEWS_API_KEY)
    Call this to supplement quantitative signals with qualitative narrative."""
    av = fetch_alpha_vantage_sentiment(symbol)
    headlines = fetch_gnews_headlines(symbol)
    return {
        "symbol": symbol,
        "alpha_vantage_sentiment": av,
        "recent_headlines": headlines.get("headlines", []),
    }


LLM_READ_TOOLS = [
    # --- Stock analysis tools ---
    FunctionTool(get_stock_quote),
    FunctionTool(get_technical_signals),
    FunctionTool(get_options_sentiment),

    # --- Macro & alternative data ---
    FunctionTool(get_macro_context),
    FunctionTool(get_insider_activity),
    FunctionTool(get_news_sentiment),

    # --- Wallet & chain basics ---
    FunctionTool(cr.wallet_info),
    FunctionTool(cr.balance_native),
    FunctionTool(cr.balance_u),         # requires [u_token] in studio.toml
    FunctionTool(cr.network_info),
    FunctionTool(cr.tx_status),

    # --- LLM provider ---
    # FunctionTool(cr.pieverse_usage),  # SIWE personal_sign; requires [llm.provider=pieverse-llm]

    # --- ERC-8004 identity (read-only lookups the LLM may want for context) ---
    FunctionTool(cr.agent_info),        # requires [erc8004] in studio.toml
    FunctionTool(cr.agent_by_address),  # requires [erc8004] in studio.toml

    # --- ERC-8183 jobs (READ-ONLY status/list — writes live in signing.py) ---
    FunctionTool(cr.job_status),        # requires [erc8183] in studio.toml
    FunctionTool(cr.job_list),          # requires [erc8183] in studio.toml
    # FunctionTool(cr.job_count),       # network-wide stat — usually noise

    # --- Advanced / footguns (commented by default) ---
    # FunctionTool(cr.contract_call_view),  # accepts any ABI — LLM-callable footgun
    # FunctionTool(cr.block_info),
    # FunctionTool(cr.wallet_list),         # multi-wallet management — dev concern
    # FunctionTool(cr.wallet_address),      # alias of wallet_info
]
