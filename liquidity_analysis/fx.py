# -*- coding: utf-8 -*-
"""
FX rate service for USD <-> HKD conversion and quote currency normalization.

 Fetches live USD/HKD spot FX rate from open public APIs with caching and deterministic fallback.
"""

import json
import time
import urllib.request
from typing import Optional, Tuple

DEFAULT_USD_HKD_RATE = 7.80  # Linked exchange rate band 7.75 - 7.85
_FX_CACHE: Tuple[float, float, str] = (0.0, DEFAULT_USD_HKD_RATE, "default")
_FX_TTL_SEC = 300.0


def get_usd_hkd_rate(
    override_rate: Optional[float] = None, timeout: float = 5.0
) -> Tuple[float, str]:
    """
    Return (usd_hkd_rate, source_description).

    1. Uses `override_rate` if explicitly supplied by caller.
    2. Checks 5-minute in-memory cache.
    3. Queries open.er-api.com USD/HKD spot rate.
    4. Fallback to HashKey HK USDTHKD mid price.
    5. Fallback to DEFAULT_USD_HKD_RATE (7.80).
    """
    global _FX_CACHE
    if override_rate is not None and override_rate > 0:
        return float(override_rate), "user_override"

    now = time.time()
    cached_ts, cached_rate, cached_src = _FX_CACHE
    if now - cached_ts < _FX_TTL_SEC and cached_src != "default":
        return cached_rate, cached_src

    # Source 1: open.er-api.com
    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "liquidity-analysis/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            hkd = float(data["rates"]["HKD"])
            if 7.5 <= hkd <= 8.1:
                _FX_CACHE = (now, hkd, "open.er-api.com")
                return hkd, "open.er-api.com"
    except Exception:
        pass

    # Source 2: HashKey HK live USDTHKD order book mid
    try:
        req = urllib.request.Request(
            "https://api-pro.hashkey.com/quote/v1/depth?symbol=USDTHKD&limit=5",
            headers={"User-Agent": "liquidity-analysis/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
            bids = data.get("b", [])
            asks = data.get("a", [])
            if bids and asks:
                mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
                if 7.5 <= mid <= 8.1:
                    _FX_CACHE = (now, mid, "hashkey:USDTHKD")
                    return mid, "hashkey:USDTHKD"
    except Exception:
        pass

    return DEFAULT_USD_HKD_RATE, "fallback_peg_7.80"


def convert_notional_to_quote(
    amount: float,
    input_currency: str,
    book_quote_asset: str,
    usd_hkd_rate: float,
) -> float:
    """
    Convert target notional in `input_currency` ("USD" or "HKD") into the order book's
    `book_quote_asset` ("USD", "USDT", "USDC", "FDUSD", "HKD").

    Note: USD stablecoins (USDT/USDC/FDUSD) are treated at 1:1 USD par for order book traversal,
    which matches standard multi-exchange slippage estimation practice.
    """
    in_ccy = input_currency.upper().strip()
    quote = book_quote_asset.upper().strip()

    if in_ccy == "USD":
        amount_usd = amount
    elif in_ccy == "HKD":
        amount_usd = amount / usd_hkd_rate
    else:
        raise ValueError(f"不支持的输入货币: {input_currency} (仅支持 USD 或 HKD)")

    if quote == "HKD":
        return amount_usd * usd_hkd_rate
    if quote in ("USD", "USDT", "USDC", "FDUSD", "RLUSD"):
        return amount_usd
    raise ValueError(f"无法将 {input_currency} 换算为盘口计价币种 {book_quote_asset}")


def quote_to_usd_and_hkd(
    value_in_quote: float,
    book_quote_asset: str,
    usd_hkd_rate: float,
) -> Tuple[float, float]:
    """Convert a price or notional in `book_quote_asset` into (value_usd, value_hkd)."""
    quote = book_quote_asset.upper().strip()
    if quote == "HKD":
        usd = value_in_quote / usd_hkd_rate
        return usd, value_in_quote
    # USD or USD-pegged stablecoins
    return value_in_quote, value_in_quote * usd_hkd_rate
