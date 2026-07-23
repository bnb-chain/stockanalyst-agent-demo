"""External market data sources beyond yfinance.

All functions degrade gracefully when API keys are absent — they return a dict
with a 'note' or 'error' key so the LLM skips the section and continues with
whatever data it has. Never raise from these functions.

Required env vars (add to .studio/.env.local):
    FRED_API_KEY            — https://fred.stlouisfed.org/docs/api/api_key.html (free)
    ALPHA_VANTAGE_API_KEY   — https://www.alphavantage.co/support/#api-key (free, 25 req/day)
    NEWS_API_KEY            — https://newsapi.org/register (free, 100 req/day)

SEC EDGAR is fully public — no key required. A User-Agent header is mandatory.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

try:
    from .untrusted_text import normalize_untrusted_text
except ImportError:
    from untrusted_text import normalize_untrusted_text

logger = logging.getLogger("seller-agent.data_sources")

_EDGAR_HEADERS = {
    "User-Agent": "stockanalyst-agent contact@bnbchain.org",
    "Accept-Encoding": "gzip, deflate",
}
_TIMEOUT = 10
_MAX_HEADLINES = 5
_MAX_HEADLINE_CHARS = 300
_MAX_SOURCE_CHARS = 100
_MAX_DATE_CHARS = 10


# ── Macro context (FRED + VIX) ────────────────────────────────────────────────

def fetch_macro_context() -> dict[str, Any]:
    """Fetch key macroeconomic indicators.

    Primary source: FRED (requires FRED_API_KEY env var).
    Fallback: yfinance CBOE yield indices — no key needed, available always.
      ^VIX  = CBOE Volatility Index
      ^TNX  = 10-Year Treasury Yield (value already in %, e.g. 4.35 = 4.35%)
      ^IRX  = 13-Week T-Bill Yield (proxy for Fed Funds rate)

    Returns a dict with numeric values and plain-English signals.
    """
    import yfinance as yf

    result: dict[str, Any] = {}

    # ── Primary: FRED ─────────────────────────────────────────────────────────
    api_key = os.environ.get("FRED_API_KEY", "")
    if api_key:
        try:
            from fredapi import Fred
            fred = Fred(api_key=api_key)
            result["fed_funds_rate"] = round(float(fred.get_series("FEDFUNDS").iloc[-1]), 2)
            result["treasury_10y_yield"] = round(float(fred.get_series("DGS10").iloc[-1]), 2)
            cpi = fred.get_series("CPIAUCSL")
            result["cpi_yoy_pct"] = round(float(cpi.pct_change(12).iloc[-1] * 100), 2)
            result["unemployment_pct"] = round(float(fred.get_series("UNRATE").iloc[-1]), 2)
        except Exception as e:
            logger.warning("FRED fetch failed: %s", e)
            result["fred_error"] = str(e)
    else:
        logger.warning("FRED_API_KEY not set — using yfinance fallbacks for rate data")

    # ── Fallback: yfinance yield indices (no key needed) ──────────────────────
    def _yf_price(ticker: str) -> float | None:
        try:
            info = yf.Ticker(ticker).info
            val = info.get("regularMarketPrice") or info.get("previousClose")
            return round(float(val), 2) if val else None
        except Exception:
            return None

    # 10-Year Treasury — ^TNX reports yield already in % (e.g. 4.35)
    if "treasury_10y_yield" not in result:
        val = _yf_price("^TNX")
        if val is not None:
            # Sanity check: TNX sometimes returns tenths-of-percent (e.g. 43.5)
            result["treasury_10y_yield"] = round(val / 10, 2) if val > 20 else val
            result["treasury_10y_source"] = "yfinance ^TNX"

    # Fed Funds proxy — 3-month T-bill (^IRX) closely tracks the Fed Funds rate
    if "fed_funds_rate" not in result:
        val = _yf_price("^IRX")
        if val is not None:
            result["fed_funds_rate"] = round(val / 10, 2) if val > 20 else val
            result["fed_funds_source"] = "yfinance ^IRX (3-month T-bill proxy)"

    # CPI and Unemployment have no good yfinance substitute — leave blank if FRED unavailable
    # The LLM will write "—" for those cells per the prompt rules.

    # ── VIX (yfinance, always attempted) ──────────────────────────────────────
    try:
        vix_info = yf.Ticker("^VIX").info
        vix = vix_info.get("regularMarketPrice") or vix_info.get("previousClose")
        if vix:
            result["vix"] = round(float(vix), 2)
            result["vix_signal"] = (
                "extreme_fear (>30)"    if result["vix"] > 30 else
                "fear (20-30)"          if result["vix"] > 20 else
                "neutral (15-20)"       if result["vix"] > 15 else
                "complacency (<15)"
            )
    except Exception as e:
        logger.warning("VIX fetch failed: %s", e)

    # ── Rate environment label (derived once all rate data is collected) ───────
    ffr = result.get("fed_funds_rate")
    if ffr is not None:
        result["rate_environment"] = (
            "restrictive" if ffr > 4 else
            "neutral"     if ffr > 2 else
            "accommodative"
        )

    return result


# ── SEC EDGAR — insider trading (Form 4) ─────────────────────────────────────

_CIK_CACHE: dict[str, str] = {}


def _get_cik(symbol: str) -> str | None:
    """Resolve a ticker symbol to its SEC CIK (zero-padded to 10 digits)."""
    key = symbol.upper()
    if key in _CIK_CACHE:
        return _CIK_CACHE[key]
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_EDGAR_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        for entry in resp.json().values():
            if str(entry.get("ticker", "")).upper() == key:
                cik = str(entry["cik_str"]).zfill(10)
                _CIK_CACHE[key] = cik
                return cik
    except Exception as e:
        logger.warning("CIK lookup failed for %s: %s", symbol, e)
    return None


def fetch_insider_trades(symbol: str, days: int = 90) -> dict[str, Any]:
    """Fetch recent insider Form 4 filings from SEC EDGAR.

    Form 4 is filed whenever a corporate insider (executive, director, or
    10%+ shareholder) buys or sells company stock. High filing frequency
    signals meaningful insider activity.

    Args:
        symbol: Ticker symbol (e.g. 'AAPL').
        days: Look-back window in calendar days.

    Returns a dict with filing count, dates, and an activity signal.
    """
    cik = _get_cik(symbol)
    if not cik:
        return {"symbol": symbol, "error": f"SEC CIK not found for {symbol}"}

    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=_EDGAR_HEADERS, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])

        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        form4s = [
            dates[i]
            for i, f in enumerate(forms)
            if f in ("4", "4/A") and i < len(dates) and dates[i] >= cutoff
        ]

        return {
            "symbol": symbol,
            "period_days": days,
            "form4_filings": len(form4s),
            "recent_dates": form4s[:5],
            "activity_signal": (
                "high — 5+ filings suggest significant insider moves"   if len(form4s) >= 5 else
                "moderate — 2-4 filings, worth monitoring"              if len(form4s) >= 2 else
                "low — fewer than 2 Form 4s in the period"
            ),
        }
    except Exception as e:
        logger.warning("EDGAR Form 4 fetch failed for %s: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}


# ── Alpha Vantage — AI-scored news sentiment ──────────────────────────────────

def fetch_alpha_vantage_sentiment(symbol: str) -> dict[str, Any]:
    """Fetch AI-scored news sentiment for a symbol from Alpha Vantage.

    Returns a sentiment score in [-1, 1] (negative = bearish, positive = bullish),
    a human-readable label, and the top headlines analysed.

    Requires ALPHA_VANTAGE_API_KEY in environment.
    Free tier: 25 requests/day.
    """
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        return {"symbol": symbol, "note": "ALPHA_VANTAGE_API_KEY not set"}

    try:
        resp = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "NEWS_SENTIMENT",
                "tickers": symbol,
                "limit": "20",
                "apikey": api_key,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if "Information" in data:  # rate-limited
            return {"symbol": symbol, "note": data["Information"]}

        feed = data.get("feed", [])
        if not feed:
            return {"symbol": symbol, "article_count": 0}

        ticker_scores: list[float] = []
        headlines: list[str] = []
        for article in feed[:20]:
            if not isinstance(article, dict):
                continue
            for ts in article.get("ticker_sentiment", []):
                if not isinstance(ts, dict):
                    continue
                if ts.get("ticker", "").upper() == symbol.upper():
                    try:
                        ticker_scores.append(float(ts["ticker_sentiment_score"]))
                    except (KeyError, ValueError):
                        pass
            if len(headlines) < _MAX_HEADLINES:
                headlines.append(
                    normalize_untrusted_text(
                        article.get("title", ""),
                        max_chars=_MAX_HEADLINE_CHARS,
                    )
                )

        avg = sum(ticker_scores) / len(ticker_scores) if ticker_scores else 0.0
        label = (
            "Bullish"          if avg >  0.15 else
            "Somewhat_Bullish" if avg >  0.05 else
            "Bearish"          if avg < -0.15 else
            "Somewhat_Bearish" if avg < -0.05 else
            "Neutral"
        )

        return {
            "symbol": symbol,
            "sentiment_score": round(avg, 3),
            "sentiment_label": label,
            "article_count": len(feed),
            "top_headlines": headlines,
        }
    except Exception as e:
        logger.warning("Alpha Vantage sentiment failed for %s: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}


# ── GNews — latest headlines ──────────────────────────────────────────────────

def fetch_gnews_headlines(symbol: str, company_name: str = "") -> dict[str, Any]:
    """Fetch the most relevant recent news headlines for a stock from GNews.io.

    Searches by company name (more precise than ticker). Returns raw titles
    so the LLM can incorporate them into its narrative.

    Requires GNEWS_API_KEY in environment.
    Free tier: 100 requests/day, up to 10 articles per request.
    Docs: https://gnews.io/docs/v4
    """
    api_key = os.environ.get("GNEWS_API_KEY", "")
    if not api_key:
        return {"symbol": symbol, "note": "GNEWS_API_KEY not set"}

    query = company_name or symbol
    try:
        resp = requests.get(
            "https://gnews.io/api/v4/search",
            params={
                "q": query,
                "lang": "en",
                "max": "5",
                "sortby": "relevance",
                "token": api_key,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        articles = data.get("articles", [])
        headlines: list[dict[str, str]] = []
        for article in articles[:_MAX_HEADLINES]:
            if not isinstance(article, dict):
                headlines.append({"title": "", "source": "", "published": ""})
                continue
            source = article.get("source")
            source_name = source.get("name", "") if isinstance(source, dict) else ""
            headlines.append({
                "title": normalize_untrusted_text(
                    article.get("title", ""),
                    max_chars=_MAX_HEADLINE_CHARS,
                ),
                "source": normalize_untrusted_text(
                    source_name,
                    max_chars=_MAX_SOURCE_CHARS,
                ),
                "published": normalize_untrusted_text(
                    article.get("publishedAt", "")[:_MAX_DATE_CHARS]
                    if isinstance(article.get("publishedAt"), str) else "",
                    max_chars=_MAX_DATE_CHARS,
                ),
            })
        return {
            "symbol": symbol,
            "query": query,
            "total_results": data.get("totalArticles", len(articles)),
            "headlines": headlines,
        }
    except Exception as e:
        logger.warning("GNews fetch failed for %s: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}
