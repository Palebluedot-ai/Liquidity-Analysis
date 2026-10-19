# -*- coding: utf-8 -*-
"""Data models for multi-exchange order book depth, realistic MM withdrawal & full-chain execution cost, and OTC comparison."""

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
    Simulated execution outcome under a specific Market Maker withdrawal / retreat regime.
    """

    fade_rate: float
    label: str
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
    amplification_vs_static: float


@dataclass
class RealExecutionBreakdown:
    """
    Full-chain realistic order-book execution cost breakdown vs. OTC Block Trade quote.

    Why static order book slippage (1~3 bp) is an illusion for large orders:
      1. `static_slippage_bp`: Walking resting quotes as if HFTs stand still.
      2. `mm_retreat_bp`: Market Maker Cancel-All & Price Retreat (after first ~$100k-$150k probe
         is hit, VPIN/OFI toxicity triggers mass-cancel of the near book and shifts quotes wider).
      3. `hft_frontrun_bp`: Predatory HFT momentum ignition / cross-exchange sniping triggered by
         the first sweep wave's public trade print.
      4. `taker_fee_bp`: Exchange Taker execution fee (8~10 bp on Binance/OKX, 12~20 bp on fiat exchanges).
      5. `fiat_usdt_friction_bp`: Fiat (USD/HKD) <-> USDT conversion spread, fee & stablecoin basis
         (applies when executing USD/HKD capital on a USDT-quoted book like Binance/OKX).
    """

    # 5 cost components in basis points (bp)
    static_slippage_bp: float
    mm_retreat_bp: float
    hft_frontrun_bp: float
    taker_fee_bp: float
    fiat_usdt_friction_bp: float

    # Totals for realistic order-book execution
    total_orderbook_cost_bp: float
    total_orderbook_cost_usd: float
    total_orderbook_cost_hkd: float
    realistic_all_in_vwap_usd: float
    realistic_all_in_vwap_hkd: float
    realistic_extra_per_coin_usd: float
    realistic_extra_per_coin_hkd: float
    estimated_waves: int  # Number of child bursts / waves before completion
    probe_depth_usd: float  # Top-of-book probe liquidity before MM toxicity trigger (~5bp band)

    # Benchmark against OTC Block Trade quote (e.g. 30 bp all-in net price)
    otc_quote_bp: float
    otc_cost_usd: float
    otc_cost_hkd: float
    otc_all_in_price_usd: float
    otc_all_in_price_hkd: float
    otc_savings_bp: float  # total_orderbook_cost_bp - otc_quote_bp (positive = OTC saves money)
    otc_savings_usd: float
    otc_savings_hkd: float
    otc_savings_per_coin_usd: float

    # Live empirical market microstructure & stablecoin telemetry (zero magic numbers)
    avg_1m_vol_usd: float = 0.0
    avg_1m_ofi_usd: float = 0.0
    order_to_1m_ofi_multiple: float = 0.0
    display_to_trade_ratio: float = 0.0
    sigma_1m_bp: float = 0.0
    empirical_impact_y: float = 0.0
    kyle_lambda_bp_per_1m: float = 0.0
    twap_exec_mins: float = 0.0
    micro_order_pct: float = 0.0  # % of levels within 10bp that are <$5,000 HFT grid quotes
    usdt_book_slippage_bp: float = 0.0
    usdt_basis_bp: float = 0.0
    permanent_impact_ratio: float = 0.55
    ofi_autocorr: float = 0.0
    mm_perp_hedge_floor_bp: float = 2.5
    optimal_exec_mins: float = 2.0
    slow_twap_mins: float = 15.0
    strategy_sweep_1m_bp: float = 0.0
    strategy_optimal_bp: float = 0.0
    strategy_slow_twap_bp: float = 0.0
    data_source_note: str = "live_kline_and_l2_calibration"


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
    target_notional_input: Optional[float]
    target_notional_usd: Optional[float]
    target_notional_hkd: Optional[float]
    target_notional_quote: Optional[float]
    target_qty: Optional[float]
    fx_usd_hkd: float
    applied_fade_rate: float

    # Book top & Level-1 liquidity ("screen price illusion" metrics)
    top_bid: float
    top_ask: float
    mid_price: float
    spread_bp: float
    top_price: float
    top_price_usd: float
    top_price_hkd: float
    level1_qty: float
    level1_notional_usd: float
    level1_notional_hkd: float
    level1_fill_pct: float

    # Execution metrics (static book)
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

    # Extra cost vs Level-1 screen price
    vwap_extra_per_coin_usd: float
    vwap_extra_per_coin_hkd: float
    last_extra_per_coin_usd: float
    last_extra_per_coin_hkd: float
    slippage_loss_usd: float
    slippage_loss_hkd: float
    taker_fee_bp: float
    taker_fee_usd: float
    taker_fee_hkd: float
    all_in_cost_bp: float
    all_in_loss_usd: float
    all_in_loss_hkd: float
    all_in_vwap_usd: float

    # Slippage metrics
    vwap_slippage_bp: float
    last_slippage_bp: float
    vwap_vs_mid_bp: float
    signed_vwap_diff_bp: float

    # Book capacity & fill status
    levels_used: int
    book_levels: int
    book_side_total_quote: float
    book_side_total_usd: float
    book_side_total_hkd: float
    book_side_total_qty: float
    fill_ratio: float
    depth_exhausted: bool

    # Depth ladder, MM withdrawal scenarios, and realistic full-chain breakdown vs OTC
    depth_bands: List[DepthBand] = field(default_factory=list)
    fade_scenarios: List[FadeScenario] = field(default_factory=list)
    real_breakdown: Optional[RealExecutionBreakdown] = None
    latency_ms: float = 0.0
    sequence_id: Optional[str] = None
    note: str = (
        "静态盘口滑点仅反映挂单不动的理想假象；真实大单进场会触发做市商毫秒级撤空前排与退避重挂(Price Retreat)、HFT动量抢跑、Taker手续费及USDT换汇损耗"
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

    is_crossed: bool
    bids_strictly_descending: bool
    asks_strictly_ascending: bool
    spread_bp: float
    best_bid: float
    best_ask: float
    sequence_ids: List[Optional[str]]
    sequence_monotonic: bool
    exchange_clock_skew_ms: Optional[float]

    latest_trade_price: Optional[float]
    latest_trade_ts_ms: Optional[int]
    trade_vs_mid_bp: Optional[float]
    trade_within_inside_spread_or_10bp: Optional[bool]

    depth_10bp_usd_samples: List[float]
    depth_25bp_usd_samples: List[float]
    depth_10bp_cv_pct: float
    depth_25bp_cv_pct: float
    level_retention_ratio: float
    simulated_slippage_bp_samples: List[float]
    simulated_slippage_bp_min: Optional[float]
    simulated_slippage_bp_max: Optional[float]
    simulated_slippage_bp_mean: Optional[float]

    verdict: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
