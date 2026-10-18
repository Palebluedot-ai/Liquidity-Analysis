"""
Liquidity Analysis — Multi-Exchange Order Book, Slippage & OTC Block Trade Comparison Engine.
"""

from .models import (
    DepthBand,
    OrderBookSnapshot,
    SlippageResult,
    VerificationReport,
)
from .symbols import (
    PRESET_ASSETS,
    parse_amount,
    parse_user_symbol,
    resolve_exchange_symbol,
)
from .fx import get_usd_hkd_rate
from .exchanges import (
    SUPPORTED_EXCHANGES,
    DEFAULT_COMPARE_EXCHANGES,
    fetch_orderbook,
    fetch_recent_trades,
)
from .engine import (
    walk_book,
    compute_depth_bands,
    estimate_single,
    compare_exchanges,
    build_otc_pitch_lines,
    verify_orderbook,
)

__all__ = [
    "DepthBand",
    "OrderBookSnapshot",
    "SlippageResult",
    "VerificationReport",
    "PRESET_ASSETS",
    "parse_amount",
    "parse_user_symbol",
    "resolve_exchange_symbol",
    "get_usd_hkd_rate",
    "SUPPORTED_EXCHANGES",
    "DEFAULT_COMPARE_EXCHANGES",
    "fetch_orderbook",
    "fetch_recent_trades",
    "walk_book",
    "compute_depth_bands",
    "estimate_single",
    "compare_exchanges",
    "build_otc_pitch_lines",
    "verify_orderbook",
]
