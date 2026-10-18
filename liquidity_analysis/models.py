# -*- coding: utf-8 -*-
"""Data models for multi-exchange order book depth, slippage estimation, Maker withdrawal simulation, and verification."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class DepthBand:
    """Cumulative liquidity available within `bp` basis points from top of book."""

    bp: float
    boundary_price: float
    cum_qty: float
    cum_quote: float
    cum_usd: float
    cum_hkd: float
    levels_count: int


@dataclass
class FadeScenario:
    """
    Simulated execution outcome when Market Makers pull `fade_rate` (e.g. 50% or 70%)
    of resting liquidity after the initial fill / across child sweeps.
    """

    fade_rate: float  # 0.0 ~ 0.90 (e.g. 0.50 = 50% of deeper maker orders cancelled)
    label: str  # e.g. "静态理想 (0%撤单)", "常规HFT撤单 (50%撤单)", "剧烈联动撤单 (70%撤单)"
    levels_used: int
    filled_qty: float
    fill_ratio: float
    depth_exhausted: bool
    vwap_usd: float
    vwap_hkd: float
    last_price_usd: float
    last_price_hkd: float
    vwap_extra_per_coin_usd: float
    vwap_extra_per_coin_hkd: float
    last_extra_per_coin_usd: float
    vwap_slippage_bp: float
    last_slippage_bp: float
    slippage_loss_usd: float
    slippage_loss_hkd: float
    all_in_cost_bp: float
    all_in_loss_usd: float
    all_in_loss_hkd: float
    amplification_vs_static: float  # Multiple of static slippage bp (e.g. 2.4x)


@dataclass
class OrderBookSnapshot:
    """Standardized L2 order book snapshot from an exchange."""

    exchange: str
    exchange_label: str
    symbol: str
    base_asset: str
    quote_asset: str
    bids: List[Tuple[float, float]]  # [(price, qty)], sorted high -> low
    asks: List[Tuple[float, float]]  # [(price, qty)], sorted low -> high
    sequence_id: Optional[str] = None
    exchange_ts_ms: Optional[int] = None
    fetched_at_ms: int = 0
    latency_ms: float = 0.0
    max_levels_requested: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask

    @property
    def spread_bp(self) -> float:
        mid = self.mid_price
        if mid <= 0 or not self.bids or not self.asks:
            return 0.0
        return (self.best_ask - self.best_bid) / mid * 1e4


@dataclass
class SlippageResult:
    """Complete result of walking an exchange order book vs Level-1 and OTC Block Trade."""

    exchange: str
    exchange_label: str
    symbol: str
    base_asset: str
    quote_asset: str
    side: str  # "buy" or "sell"

    # Input specification
    input_currency: str  # "USD" or "HKD"
    target_notional_input: Optional[float]  # Target in input_currency (if notional mode)
    target_notional_usd: Optional[float]
    target_notional_hkd: Optional[float]
    target_notional_quote: Optional[float]  # Target converted to book quote currency
    target_qty: Optional[float]  # Target in base asset units (if qty mode)
    fx_usd_hkd: float
    applied_fade_rate: float  # Primary fade rate applied to main metrics (default 0.0)

    # Book top & Level-1 liquidity ("screen price illusion" metrics)
    top_bid: float
    top_ask: float
    mid_price: float
    spread_bp: float
    top_price: float  # Level-1 price in quote currency
    top_price_usd: float  # Level-1 price in USD
    top_price_hkd: float  # Level-1 price in HKD
    level1_qty: float  # Quantity resting at Level 1
    level1_notional_usd: float  # USD notional resting at Level 1
    level1_notional_hkd: float  # HKD notional resting at Level 1
    level1_fill_pct: float  # % of target order that Level 1 alone can satisfy (0~100)

    # Execution metrics (in book quote currency, USD, and HKD)
    filled_qty: float
    cost_quote: float
    cost_usd: float
    cost_hkd: float
    vwap_quote: float
    vwap_usd: float
    vwap_hkd: float
    last_price_quote: float
    last_price_usd: float
    last_price_hkd: float

    # Extra cost vs Level-1 screen price (OTC Block Trade comparison metrics)
    vwap_extra_per_coin_usd: float  # |vwap_usd - top_price_usd| per 1 base coin
    vwap_extra_per_coin_hkd: float  # |vwap_hkd - top_price_hkd| per 1 base coin
    last_extra_per_coin_usd: float  # |last_price_usd - top_price_usd| per 1 base coin
    last_extra_per_coin_hkd: float  # |last_price_hkd - top_price_hkd| per 1 base coin
    slippage_loss_usd: float  # Total extra USD lost due to walking book vs Level-1 price
    slippage_loss_hkd: float  # Total extra HKD lost due to walking book vs Level-1 price
    taker_fee_bp: float  # Assumed exchange taker fee in bp (e.g. 5.0 or 10.0)
    taker_fee_usd: float  # Exchange taker fee in USD
    taker_fee_hkd: float  # Exchange taker fee in HKD
    all_in_cost_bp: float  # vwap_slippage_bp + taker_fee_bp
    all_in_loss_usd: float  # slippage_loss_usd + taker_fee_usd
    all_in_loss_hkd: float  # slippage_loss_hkd + taker_fee_hkd
    all_in_vwap_usd: float  # Effective all-in price per coin including taker fee

    # Slippage metrics (positive bp = adverse execution cost relative to reference)
    vwap_slippage_bp: float
    last_slippage_bp: float
    vwap_vs_mid_bp: float
    signed_vwap_diff_bp: float  # (top_price - vwap) / top_price * 1e4 (compatible with legacy script)

    # Book capacity & fill status
    levels_used: int
    book_levels: int
    book_side_total_quote: float
    book_side_total_usd: float
    book_side_total_hkd: float
    book_side_total_qty: float
    fill_ratio: float  # 0.0 ~ 1.0
    depth_exhausted: bool

    # Depth ladder & Market Maker withdrawal stress-test scenarios
    depth_bands: List[DepthBand] = field(default_factory=list)
    fade_scenarios: List[FadeScenario] = field(default_factory=list)
    latency_ms: float = 0.0
    sequence_id: Optional[str] = None
    note: str = (
        "静态盘口估算，真正在盘面扫单时高频做市商会动态撤单，实际滑点通常显著高于静态值"
    )
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VerificationReport:
    """Verification report checking whether displayed Maker orders are valid and persistent."""

    exchange: str
    exchange_label: str
    symbol: str
    samples_count: int
    interval_sec: float

    # 1. Book structural integrity
    is_crossed: bool
    bids_strictly_descending: bool
    asks_strictly_ascending: bool
    spread_bp: float
    best_bid: float
    best_ask: float
    sequence_ids: List[Optional[str]]
    sequence_monotonic: bool
    exchange_clock_skew_ms: Optional[float]

    # 2. Public trade alignment check
    latest_trade_price: Optional[float]
    latest_trade_ts_ms: Optional[int]
    trade_vs_mid_bp: Optional[float]
    trade_within_inside_spread_or_10bp: Optional[bool]

    # 3. Multi-snapshot persistence / anti-spoofing stability
    depth_10bp_usd_samples: List[float]
    depth_25bp_usd_samples: List[float]
    depth_10bp_cv_pct: float  # Coefficient of variation (%) across snapshots
    depth_25bp_cv_pct: float
    level_retention_ratio: float  # Fraction of top-50 price levels remaining present across consecutive snapshots
    simulated_slippage_bp_samples: List[float]
    simulated_slippage_bp_min: Optional[float]
    simulated_slippage_bp_max: Optional[float]
    simulated_slippage_bp_mean: Optional[float]

    verdict: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
