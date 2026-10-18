# -*- coding: utf-8 -*-
"""
Symbol catalog, alias normalization, pair parsing, and human-friendly amount parsing.

Supports:
1. Preset dropdown selection (BTC, ETH, Solana/SOL, BNB, XRP, DOGE, SUI, AVAX, LINK, HSK, etc.)
2. Free-form custom input (e.g. "Solana", "PEPE", "BTCUSDT", "BTC/USD", "ETH-HKD", "ARB/USDT")
"""

import re
from typing import Dict, List, Optional, Tuple

# Preset assets shown in UI / Lark Bot dropdown menu
PRESET_ASSETS: List[Dict[str, str]] = [
    {"value": "BTC", "label": "BTC (Bitcoin)"},
    {"value": "ETH", "label": "ETH (Ethereum)"},
    {"value": "SOL", "label": "SOL (Solana)"},
    {"value": "BNB", "label": "BNB (BNB)"},
    {"value": "XRP", "label": "XRP (Ripple)"},
    {"value": "DOGE", "label": "DOGE (Dogecoin)"},
    {"value": "SUI", "label": "SUI (Sui)"},
    {"value": "AVAX", "label": "AVAX (Avalanche)"},
    {"value": "LINK", "label": "LINK (Chainlink)"},
    {"value": "TON", "label": "TON (Toncoin)"},
    {"value": "ADA", "label": "ADA (Cardano)"},
    {"value": "LTC", "label": "LTC (Litecoin)"},
    {"value": "UNI", "label": "UNI (Uniswap)"},
    {"value": "AAVE", "label": "AAVE (Aave)"},
    {"value": "APT", "label": "APT (Aptos)"},
    {"value": "ARB", "label": "ARB (Arbitrum)"},
    {"value": "OP", "label": "OP (Optimism)"},
    {"value": "HSK", "label": "HSK (HashKey Platform Token)"},
]

# Full-name or alternative ticker aliases -> canonical base ticker
ASSET_ALIASES: Dict[str, str] = {
    "BITCOIN": "BTC",
    "XBT": "BTC",
    "ETHEREUM": "ETH",
    "ETHER": "ETH",
    "SOLANA": "SOL",
    "RIPPLE": "XRP",
    "DOGECOIN": "DOGE",
    "AVALANCHE": "AVAX",
    "CHAINLINK": "LINK",
    "TONCOIN": "TON",
    "CARDANO": "ADA",
    "POLKADOT": "DOT",
    "LITECOIN": "LTC",
    "UNISWAP": "UNI",
    "APTOS": "APT",
    "ARBITRUM": "ARB",
    "OPTIMISM": "OP",
    "POLYGON": "POL",
    "MATIC": "POL",
    "HASHKEY": "HSK",
}

KNOWN_QUOTES = ("USDT", "USDC", "FDUSD", "RLUSD", "USD", "HKD", "EUR", "BTC", "ETH")


def normalize_base_asset(raw: str) -> str:
    """Normalize an asset name or ticker (e.g. 'Solana' -> 'SOL', 'bitcoin' -> 'BTC')."""
    cleaned = raw.strip().upper()
    return ASSET_ALIASES.get(cleaned, cleaned)


def parse_user_symbol(user_input: str) -> Tuple[str, Optional[str]]:
    """
    Parse user symbol input into (base_asset, explicit_quote_or_None).

    Examples:
        "BTC"        -> ("BTC", None)
        "Solana"     -> ("SOL", None)
        "BTCUSDT"    -> ("BTC", "USDT")
        "BTC/USD"    -> ("BTC", "USD")
        "ETH-HKD"    -> ("ETH", "HKD")
        "SOL_USDC"   -> ("SOL", "USDC")
    """
    if not user_input or not user_input.strip():
        raise ValueError("币对不能为空")

    raw = user_input.strip().upper()
    # Strip parenthetical notes like "SOL (Solana)" -> "SOL"
    if "(" in raw:
        raw = raw.split("(", 1)[0].strip()

    # Check if separated by '/', '-', '_', or space
    for sep in ("/", "-", "_", " "):
        if sep in raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
            if len(parts) == 2:
                base = normalize_base_asset(parts[0])
                quote = parts[1].upper()
                return base, quote

    # Check if raw itself is a known alias (e.g. "SOLANA", "BITCOIN")
    if raw in ASSET_ALIASES:
        return ASSET_ALIASES[raw], None

    # Check concatenated pair like BTCUSDT, BTCHKD, ETHUSD
    for q in KNOWN_QUOTES:
        if raw.endswith(q) and len(raw) > len(q):
            base_part = raw[: -len(q)]
            # Avoid splitting short base tickers wrongly if base_part is empty
            if len(base_part) >= 2:
                return normalize_base_asset(base_part), q

    return normalize_base_asset(raw), None


def resolve_exchange_symbol(
    user_input: str,
    exchange: str,
    input_currency: str = "USD",
    prefer_native_hkd: bool = False,
) -> List[Tuple[str, str, str]]:
    """
    Return ordered candidate list of (exchange_symbol, base_asset, quote_asset)
    for a given exchange.

    Exchanges have different symbol conventions and primary quote currencies:
    - binance:        BTCUSDT, BTCUSDC, BTCFDUSD
    - okx:            BTC-USDT, BTC-USDC, BTC-USD
    - coinbase:       BTC-USD, BTC-USDT, BTC-USDC
    - kraken:         BTCUSD, BTCUSDT, BTCUSDC
    - hashkey:        BTCUSD, BTCHKD, BTCUSDT (HashKey HK)
    - hashkey_hkd:    BTCHKD, BTCUSD (HashKey HK native HKD book first)
    - hashkey_global: BTCUSDT, BTCUSDC
    - bybit:          BTCUSDT, BTCUSDC
    """
    base, explicit_quote = parse_user_symbol(user_input)
    ex = exchange.lower().strip()
    ccy = input_currency.upper().strip()

    def fmt(b: str, q: str) -> str:
        if ex in ("okx", "coinbase"):
            return f"{b}-{q}"
        return f"{b}{q}"

    if explicit_quote:
        # User explicitly requested a quote currency (e.g. BTCUSDT, BTCHKD, BTC-USD)
        quotes = [explicit_quote]
        # Add fallbacks in case the user runs multi-exchange comparison with e.g. --symbol BTCUSDT
        for fallback_q in _default_quotes_for_exchange(ex, ccy, prefer_native_hkd):
            if fallback_q not in quotes:
                quotes.append(fallback_q)
    else:
        quotes = _default_quotes_for_exchange(ex, ccy, prefer_native_hkd)

    return [(fmt(base, q), base, q) for q in quotes]


def _default_quotes_for_exchange(
    ex: str, input_currency: str, prefer_native_hkd: bool
) -> List[str]:
    if ex == "binance":
        return ["USDT", "USDC", "FDUSD"]
    if ex == "okx":
        return ["USDT", "USDC", "USD"]
    if ex == "coinbase":
        return ["USD", "USDT", "USDC"]
    if ex == "kraken":
        return ["USD", "USDT", "USDC"]
    if ex == "hashkey_hkd":
        return ["HKD", "USD", "USDT"]
    if ex in ("hashkey", "hashkey_hk"):
        if prefer_native_hkd and input_currency == "HKD":
            return ["HKD", "USD", "USDT"]
        return ["USD", "USDT", "HKD"]
    if ex == "hashkey_global":
        return ["USDT", "USDC"]
    if ex == "bybit":
        return ["USDT", "USDC"]
    return ["USDT", "USD"]


_AMOUNT_RE = re.compile(
    r"^\s*(?:US\$|HK\$|\$|USD|HKD)?\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z\u4e00-\u9fa5]*)\s*$",
    re.IGNORECASE,
)


def parse_amount(val: str | float | int) -> float:
    """
    Parse numeric or shorthand amount string into float.

    Supports:
      - Plain numbers: 15000000, "15,000,000", "$15,000,000"
      - English suffixes: "500k" -> 5e5, "15m" / "15M" -> 1.5e7, "1.2b" -> 1.2e9
      - Chinese suffixes: "1000万" / "1000w" -> 1e7, "1.5亿" -> 1.5e8
    """
    if isinstance(val, (int, float)):
        if val <= 0:
            raise ValueError(f"金额/数量必须大于 0 (收到: {val})")
        return float(val)

    s = str(val).strip().replace(",", "").replace("_", "")
    m = _AMOUNT_RE.match(s)
    if not m:
        raise ValueError(f"无法解析金额/数量: {val!r}")

    num = float(m.group(1))
    suffix = m.group(2).lower().strip()
    multipliers = {
        "": 1.0,
        "k": 1e3,
        "千": 1e3,
        "w": 1e4,
        "万": 1e4,
        "m": 1e6,
        "mn": 1e6,
        "b": 1e9,
        "bn": 1e9,
        "亿": 1e8,
        "usd": 1.0,
        "hkd": 1.0,
    }
    if suffix not in multipliers:
        raise ValueError(f"未知的金额单位后缀 {suffix!r} (原输入: {val!r})")
    result = num * multipliers[suffix]
    if result <= 0:
        raise ValueError(f"金额/数量必须大于 0 (收到: {val})")
    return result
