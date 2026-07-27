"""Pydantic schema for the structured stock analysis report.

The LLM outputs a JSON object matching StockReport; code validates it and
passes it to report_renderer.render_report() for deterministic Markdown output.
All validation happens here — the renderer trusts the models are clean.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_MAX_MODEL_STRING_CHARS = 8_192


def _validate_bounded_value(value: Any) -> None:
    if isinstance(value, str):
        if len(value) > _MAX_MODEL_STRING_CHARS:
            raise ValueError("model string exceeds 8192 characters")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("model number must be finite")
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_bounded_value(item)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_bounded_value(item)


class _BoundedModel(BaseModel):
    @model_validator(mode="after")
    def validate_bounded_values(self):
        for name in type(self).model_fields:
            _validate_bounded_value(getattr(self, name))
        return self


class ClientPosition(_BoundedModel):
    shares: float
    avg_cost: float
    unrealised_pnl_pct: float
    stop_loss: float
    stop_loss_basis: str        # e.g. "MA-200 at $175.80"
    action_summary: str         # e.g. "Hold — technical setup intact above stop-loss"


class MacroSnapshot(_BoundedModel):
    vix: str
    vix_signal: str
    fed_rate: str
    fed_rate_signal: str
    treasury_10y: str
    treasury_10y_signal: str
    cpi_yoy: str                # "—" when unavailable
    unemployment: str           # "—" when unavailable
    macro_posture: str          # 1-2 sentences: overall risk-on / risk-off verdict


class SymbolAnalysis(_BoundedModel):
    # ── Header ───────────────────────────────────────────────────────────────
    symbol: str
    company_name: str
    rating: Literal["Buy", "Hold", "Sell"]
    price_target: float
    implied_return_pct: float
    horizon_months: int
    risk_level: Literal["Low", "Moderate", "High", "Very High"]
    rating_rationale: str       # 2-3 sentences driving the rating

    # ── Fundamentals (null when data unavailable) ─────────────────────────
    current_price: float | None = None
    week_52_low: float | None = None
    week_52_high: float | None = None
    market_cap: str | None = None           # e.g. "2.85T"
    pe_trailing: float | None = None
    pe_forward: float | None = None
    peg: float | None = None
    analyst_target: float | None = None
    analyst_upside_pct: float | None = None
    revenue_growth_pct: float | None = None
    gross_margin_pct: float | None = None
    beta: float | None = None
    short_float_pct: float | None = None
    fundamentals_commentary: str            # 2-3 analytical sentences on valuation

    # ── Technicals (null when data unavailable) ───────────────────────────
    rsi_14: float | None = None
    rsi_14_signal: str | None = None        # e.g. "Approaching overbought — momentum intact"
    rsi_weekly: float | None = None
    rsi_weekly_signal: str | None = None
    macd_signal: str | None = None          # e.g. "Bullish crossover, histogram expanding"
    bollinger_position: float | None = None # 0.0 = lower band, 1.0 = upper band
    bollinger_signal: str | None = None
    ma_50: float | None = None
    ma_200: float | None = None
    ma_cross: str | None = None             # "Golden Cross" | "Death Cross" | "None"
    adx: float | None = None
    adx_signal: str | None = None           # e.g. "Strong trend (>25), +DI dominant"
    obv_trend: str | None = None            # e.g. "Rising — volume confirms uptrend"
    atr_pct: float | None = None            # daily ATR as % of price
    var_95_pct: float | None = None         # 1-day VaR at 95%, expressed as negative %
    technicals_commentary: str              # 2-3 sentences on the overall technical picture

    # ── Thesis ────────────────────────────────────────────────────────────
    upside_catalysts: list[str]             # 3-10 items, formal numbered prose
    principal_risks: list[str]              # 3-10 items, formal numbered prose

    # ── Sentiment ─────────────────────────────────────────────────────────
    insider_activity: str                   # e.g. "5 buy transactions by CFO/CEO (90 days)"
    options_pcr: float | None = None
    implied_vol_pct: float | None = None
    news_sentiment_score: float | None = None   # –1.0 bearish → +1.0 bullish
    top_headline: str | None = None
    sentiment_summary: str                  # 2-3 sentences synthesising all sentiment signals

    # ── Client position (None when stock not held) ────────────────────────
    client_position: ClientPosition | None = None

    @field_validator("upside_catalysts", "principal_risks")
    @classmethod
    def between_three_and_ten(cls, v: list[str]) -> list[str]:
        if not 3 <= len(v) <= 10:
            raise ValueError(f"must have between 3 and 10 items, got {len(v)}")
        return v


class PortfolioAction(_BoundedModel):
    priority: int
    action: Literal["Trim", "Add", "New Buy", "Hold"]
    symbol: str
    quantity: str       # e.g. "Reduce by 20 shares" or "Add 10 shares on dip"
    price_level: str    # e.g. "Current ~$185" or "On pullback to $175"
    capital_impact: str # e.g. "Free ~$3,640" or "Deploy ~$1,820"
    rationale: str      # one sentence, references a specific finding


class StopLossEntry(_BoundedModel):
    symbol: str
    avg_cost: float
    stop_loss_level: float
    risk_per_share: float
    position_size: str      # e.g. "50 shares"
    max_loss_at_stop: str   # e.g. "$1,000 (10.8%)"
    technical_basis: str    # e.g. "MA-200 at $175.80 — closes below on weekly basis"


class WatchlistEntry(_BoundedModel):
    ticker: str
    company: str
    strategic_rationale: str    # one sentence: why relevant to this portfolio
    key_catalyst: str
    entry_zone: str             # e.g. "$170 – $178 (MA-50 support)"
    risk: str                   # one-phrase risk summary
    thesis: str                 # 2 sentences of investment thesis


class RiskEntry(_BoundedModel):
    factor: str
    assessment: Literal["Low", "Moderate", "High"]
    supporting_observation: str # specific data point
    threshold_to_act: str       # the trigger level or event


class StockReport(_BoundedModel):
    executive_summary: str      # 3-5 sentences: macro backdrop + one-line per stock + top action
    macro_snapshot: MacroSnapshot
    analyses: list[SymbolAnalysis] = Field(max_length=10)  # one entry per requested symbol
    portfolio_actions: list[PortfolioAction] = Field(max_length=50)
    stop_losses: list[StopLossEntry] = Field(max_length=50)
    watchlist: list[WatchlistEntry] = Field(max_length=5)     # 3-5 entries
    risk_factors: list[RiskEntry] = Field(max_length=5)       # 5 dashboard rows
