"""
Liquidity Analysis — Multi-Exchange Order Book, Slippage & OTC Block Trade Comparison Engine.
"""

from .models import (
    DepthBand,
    HistoricalCaseRecord,
    HistoricalScenarioMatrix,
    OrderBookSnapshot,
    ScenarioExecutionEstimate,
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
from .historical_tracker import (
    classify_bars_into_scenarios,
    evaluate_order_across_4_scenarios,
    fetch_binance_agg_trades,
    fetch_binance_historical_bars,
    fetch_coinbase_historical_bars,
    fetch_hashkey_historical_bars,
    fetch_kraken_historical_bars_and_ticks,
    fetch_kraken_tick_trades_since,
    fetch_okx_historical_bars,
    load_historical_dataset,
    refresh_historical_dataset,
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
    "HistoricalCaseRecord",
    "HistoricalScenarioMatrix",
    "OrderBookSnapshot",
    "ScenarioExecutionEstimate",
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
    "classify_bars_into_scenarios",
    "evaluate_order_across_4_scenarios",
    "fetch_binance_agg_trades",
    "fetch_binance_historical_bars",
    "fetch_coinbase_historical_bars",
    "fetch_hashkey_historical_bars",
    "fetch_kraken_historical_bars_and_ticks",
    "fetch_kraken_tick_trades_since",
    "fetch_okx_historical_bars",
    "load_historical_dataset",
    "refresh_historical_dataset",
    "walk_book",
    "compute_depth_bands",
    "estimate_single",
    "compare_exchanges",
    "build_otc_pitch_lines",
    "verify_orderbook",
]
