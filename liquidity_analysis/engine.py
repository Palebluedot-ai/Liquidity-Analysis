# -*- coding: utf-8 -*-
"""
Core slippage calculation engine, liquidity depth profiler, multi-exchange comparator,
Market Maker Cancel-All & Price Retreat microstructure model, and Maker order verifier.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .exchanges import (
    DEFAULT_COMPARE_EXCHANGES,
    SUPPORTED_EXCHANGES,
    fetch_orderbook,
    fetch_recent_trades,
)
from .fx import convert_notional_to_quote, get_usd_hkd_rate, quote_to_usd_and_hkd
from .models import (
    DepthBand,
    FadeScenario,
    OrderBookSnapshot,
    RealExecutionBreakdown,
    SlippageResult,
    VerificationReport,
)

DEFAULT_BP_BANDS: Tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0)
DEFAULT_TAKER_FEE_BP: float = 10.0  # Standard Spot Taker fee (10 bp = 0.10%)
DEFAULT_OTC_QUOTE_BP: float = 30.0  # Standard OTC Block Trade all-in quote benchmark (30 bp)

# Exchange-specific realistic Taker fee defaults (bp) when user doesn't override
EXCHANGE_TAKER_FEE_BP: Dict[str, float] = {
    "binance": 10.0,
    "okx": 10.0,
    "coinbase": 15.0,
    "kraken": 16.0,
    "hashkey": 10.0,
    "hashkey_hkd": 10.0,
    "hashkey_global": 10.0,
    "bybit": 10.0,
}

DEFAULT_FADE_SCENARIOS: Tuple[Tuple[float, str], ...] = (
    (0.0, "0% 静态理想盘口 (假定挂单不动)"),
    (0.50, "常规做市商撤单 + 退价重挂 (MM Retreat)"),
    (0.70, "剧烈撤单 + 跨所联动 + HFT抢跑 (Full Impact)"),
)


def apply_maker_fade(
    levels: Sequence[Tuple[float, float]],
    fade_rate: float,
    side: str = "buy",
    retreat_bp: float = 0.0,
) -> List[Tuple[float, float]]:
    """
    Simulate Market Maker withdrawal (Cancel-All Vacuum + Quantity Haircut + Price Retreat):
    When a large order sweeps the top probe levels, Market Makers do NOT merely leave half
    their size at the exact same $0.01 tick — they cancel the near-book grid and re-quote
    at wider prices (`retreat_bp` away) with reduced size (`1 - fade_rate`).
    """
    if fade_rate <= 0.0 and retreat_bp <= 0.0:
        return list(levels)
    keep = max(0.05, 1.0 - min(fade_rate, 0.95))
    is_sell = side.lower() == "sell"
    shift_mult = (1.0 - retreat_bp / 1e4) if is_sell else (1.0 + retreat_bp / 1e4)
    if not levels:
        return []
    # Level 1 probe is partially hit before full retreat kicks in on subsequent levels
    top_p, top_q = levels[0]
    first_level = [(top_p, top_q * max(0.15, keep * 0.5))]
    rest = [(p * shift_mult, q * keep) for p, q in levels[1:]]
    return first_level + rest


def walk_book(
    levels: Sequence[Tuple[float, float]],
    target_usd: Optional[float] = None,
    target_qty: Optional[float] = None,
    allow_partial: bool = False,
) -> Dict[str, Any]:
    """Walk order book levels [(price, qty)] from best to worst."""
    if (target_usd is None) == (target_qty is None):
        raise ValueError("target_usd 与 target_qty 必须且只能指定一个")

    filled_qty = 0.0
    cost = 0.0
    last_price: Optional[float] = None
    used = 0

    for price, qty in levels:
        if price <= 0 or qty <= 0:
            continue
        if target_qty is not None:
            take = min(qty, target_qty - filled_qty)
        else:
            take = min(qty, (target_usd - cost) / price)
        if take <= 0:
            break
        filled_qty += take
        cost += take * price
        last_price = price
        used += 1
        done = (target_qty is not None and filled_qty >= target_qty * (1 - 1e-12)) or (
            target_usd is not None and cost >= target_usd * (1 - 1e-12)
        )
        if done:
            break

    want = target_qty if target_qty is not None else target_usd
    got = filled_qty if target_qty is not None else cost
    fill_ratio = min(1.0, got / want) if (want and want > 0) else 0.0
    depth_exhausted = got < want * (1 - 1e-9)

    if depth_exhausted and not allow_partial:
        unit = "qty" if target_qty is not None else "quote"
        raise RuntimeError(
            f"盘口深度不足: 目标 {want:,.4f} {unit} 只吃到 {got:,.4f} (已用 {used} 档)"
        )

    vwap = (cost / filled_qty) if filled_qty > 0 else 0.0
    return {
        "filled_qty": filled_qty,
        "cost_usd": cost,
        "vwap": vwap,
        "last_price": last_price or 0.0,
        "levels_used": used,
        "fill_ratio": fill_ratio,
        "depth_exhausted": depth_exhausted,
    }


def adverse_slippage_bp(ref_price: float, exec_price: float, side: str) -> float:
    """
    Calculate adverse execution slippage in basis points (bp).
    Positive value ALWAYS means worse price than `ref_price`.
    """
    if ref_price <= 0 or exec_price <= 0:
        return 0.0
    if side.lower() == "sell":
        return (ref_price - exec_price) / ref_price * 1e4
    return (exec_price - ref_price) / ref_price * 1e4


def compute_depth_bands(
    levels: Sequence[Tuple[float, float]],
    side: str,
    quote_asset: str,
    usd_hkd_rate: float,
    bp_thresholds: Sequence[float] = DEFAULT_BP_BANDS,
) -> List[DepthBand]:
    """
    Compute cumulative available Maker liquidity within each basis-point threshold
    relative to the top of the book (`levels[0][0]`).
    """
    if not levels:
        return []
    top_price = levels[0][0]
    is_sell = side.lower() == "sell"
    bands: List[DepthBand] = []

    for bp in bp_thresholds:
        if is_sell:
            boundary = top_price * (1.0 - bp / 1e4)
        else:
            boundary = top_price * (1.0 + bp / 1e4)

        cum_qty = 0.0
        cum_quote = 0.0
        count = 0
        for price, qty in levels:
            if is_sell and price < boundary:
                break
            if not is_sell and price > boundary:
                break
            cum_qty += qty
            cum_quote += price * qty
            count += 1

        cum_usd, cum_hkd = quote_to_usd_and_hkd(cum_quote, quote_asset, usd_hkd_rate)
        bands.append(
            DepthBand(
                bp=float(bp),
                boundary_price=boundary,
                cum_qty=cum_qty,
                cum_quote=cum_quote,
                cum_usd=cum_usd,
                cum_hkd=cum_hkd,
                levels_count=count,
            )
        )
    return bands


def compute_real_execution_breakdown(
    exchange: str,
    quote_asset: str,
    side_norm: str,
    order_usd: float,
    top_price_usd: float,
    top_price_hkd: float,
    fx_usd_hkd: float,
    static_slip_bp: float,
    depth_bands: Sequence[DepthBand],
    taker_fee_bp: float,
    otc_quote_bp: float = DEFAULT_OTC_QUOTE_BP,
) -> Tuple[RealExecutionBreakdown, float, float]:
    """
    Compute realistic institutional order-book execution breakdown (5 components)
    and return `(RealExecutionBreakdown, mm_retreat_bp, full_dynamic_shift_bp)`:

    Why a $1M-$10M order on an order book actually costs 32~60+ bp instead of 1~3 bp:
      1. `static_slippage_bp`: Naive static walk of resting L2 grid quotes.
      2. `mm_retreat_bp`: Market Maker Cancel-All & Price Retreat.
         Top-of-book HFT grid quotes have a per-burst risk limit (`probe_depth_usd`, ~$100k-$150k).
         Once the first burst sweeps the probe layer, VPIN/OFI toxicity triggers `<5ms` mass
         cancellation of the near book and widens quotes by `10 ~ 25+ bp` depending on order
         size vs 10bp book depth and wave count.
      3. `hft_frontrun_bp`: Predatory HFT momentum ignition & cross-exchange contagion.
         Other HFTs detect the sweep print and snipe remaining resting orders across Binance/OKX/etc.
         ahead of subsequent child waves (`5 ~ 14 bp`).
      4. `taker_fee_bp`: Exchange Taker execution fee (`10 ~ 16 bp`).
      5. `fiat_usdt_friction_bp`: When client holds USD/HKD fiat and trades on a USDT book
         (Binance/OKX/Bybit), converting `USD/HKD -> USDT` incurs stablecoin basis + swap fee
         + spread (`8.0 bp`).
    """
    d5_usd = next((b.cum_usd for b in depth_bands if abs(b.bp - 5.0) < 1e-6), 0.0)
    d10_usd = next((b.cum_usd for b in depth_bands if abs(b.bp - 10.0) < 1e-6), 0.0)

    # Probe liquidity before HFT toxicity alarm triggers (~$120k cap or 25% of 5bp band)
    probe_depth_usd = max(25_000.0, min(150_000.0, d5_usd * 0.25 if d5_usd > 0 else 100_000.0))
    estimated_waves = max(1, int(math.ceil(order_usd / probe_depth_usd)))

    # Ratio of order size to 10bp displayed depth (measures book stress)
    effective_d10 = max(d10_usd, 200_000.0)
    stress_ratio = order_usd / effective_d10

    # 2. Market Maker Cancel-All & Price Retreat (Inventory Skew + Adverse Selection Widening)
    # Once order exceeds probe depth, MMs pull the near grid and retreat by:
    #   - Base toxicity widening: ~8.5 bp (for multi-wave institutional sweep)
    #   - Depth depletion skew: 10.0 * sqrt(stress_ratio) bp
    #   - Multi-wave ratcheting: 2.2 * ln(estimated_waves) bp
    if order_usd <= probe_depth_usd * 0.5:
        mm_retreat_bp = max(2.0, 4.0 * math.sqrt(stress_ratio))
    else:
        mm_retreat_bp = (
            8.5
            + 10.0 * math.sqrt(stress_ratio)
            + 2.2 * math.log(max(1, estimated_waves))
        )
    mm_retreat_bp = min(mm_retreat_bp, 120.0)

    # 3. Predatory HFT Front-Running & Cross-Venue Contagion
    # Sniper bots react to Wave 1 trade prints and front-run Waves 2..N across venues
    if estimated_waves <= 1:
        hft_frontrun_bp = 2.0
    else:
        hft_frontrun_bp = 4.5 + 4.5 * math.sqrt(stress_ratio) + 1.2 * math.log(estimated_waves)
    hft_frontrun_bp = min(hft_frontrun_bp, 60.0)

    # 4. Fiat (USD/HKD) <-> USDT conversion & stablecoin basis friction
    # USDT books require Fiat <-> USDT leg (~8.0 bp all-in swap spread + fee + USDT basis)
    is_usdt_book = quote_asset.upper() in ("USDT", "FDUSD", "USDC")
    fiat_usdt_friction_bp = 8.0 if is_usdt_book else 0.0

    total_ob_bp = (
        static_slip_bp
        + mm_retreat_bp
        + hft_frontrun_bp
        + taker_fee_bp
        + fiat_usdt_friction_bp
    )

    order_hkd = order_usd * fx_usd_hkd
    total_ob_usd = order_usd * (total_ob_bp / 1e4)
    total_ob_hkd = order_hkd * (total_ob_bp / 1e4)

    if side_norm == "buy":
        real_vwap_usd = top_price_usd * (1.0 + total_ob_bp / 1e4)
        otc_px_usd = top_price_usd * (1.0 + otc_quote_bp / 1e4)
    else:
        real_vwap_usd = top_price_usd * (1.0 - total_ob_bp / 1e4)
        otc_px_usd = top_price_usd * (1.0 - otc_quote_bp / 1e4)

    real_vwap_hkd = real_vwap_usd * fx_usd_hkd
    otc_px_hkd = otc_px_usd * fx_usd_hkd
    real_extra_coin_usd = abs(real_vwap_usd - top_price_usd)
    real_extra_coin_hkd = abs(real_vwap_hkd - top_price_hkd)

    otc_cost_usd = order_usd * (otc_quote_bp / 1e4)
    otc_cost_hkd = order_hkd * (otc_quote_bp / 1e4)

    otc_savings_bp = total_ob_bp - otc_quote_bp
    otc_savings_usd = total_ob_usd - otc_cost_usd
    otc_savings_hkd = total_ob_hkd - otc_cost_hkd
    otc_savings_per_coin_usd = top_price_usd * (otc_savings_bp / 1e4)

    breakdown = RealExecutionBreakdown(
        static_slippage_bp=static_slip_bp,
        mm_retreat_bp=mm_retreat_bp,
        hft_frontrun_bp=hft_frontrun_bp,
        taker_fee_bp=taker_fee_bp,
        fiat_usdt_friction_bp=fiat_usdt_friction_bp,
        total_orderbook_cost_bp=total_ob_bp,
        total_orderbook_cost_usd=total_ob_usd,
        total_orderbook_cost_hkd=total_ob_hkd,
        realistic_all_in_vwap_usd=real_vwap_usd,
        realistic_all_in_vwap_hkd=real_vwap_hkd,
        realistic_extra_per_coin_usd=real_extra_coin_usd,
        realistic_extra_per_coin_hkd=real_extra_coin_hkd,
        estimated_waves=estimated_waves,
        probe_depth_usd=probe_depth_usd,
        otc_quote_bp=otc_quote_bp,
        otc_cost_usd=otc_cost_usd,
        otc_cost_hkd=otc_cost_hkd,
        otc_all_in_price_usd=otc_px_usd,
        otc_all_in_price_hkd=otc_px_hkd,
        otc_savings_bp=otc_savings_bp,
        otc_savings_usd=otc_savings_usd,
        otc_savings_hkd=otc_savings_hkd,
        otc_savings_per_coin_usd=otc_savings_per_coin_usd,
    )
    return breakdown, mm_retreat_bp, (mm_retreat_bp + hft_frontrun_bp)


def _compute_fade_scenario(
    raw_levels: Sequence[Tuple[float, float]],
    fade_rate: float,
    retreat_bp: float,
    label: str,
    side_norm: str,
    quote_asset: str,
    rate: float,
    top_price: float,
    top_price_usd: float,
    top_price_hkd: float,
    target_quote: Optional[float],
    qty: Optional[float],
    taker_fee_bp: float,
    static_slip_bp: Optional[float] = None,
) -> FadeScenario:
    adj_levels = apply_maker_fade(raw_levels, fade_rate, side=side_norm, retreat_bp=retreat_bp)
    w = walk_book(
        adj_levels,
        target_usd=target_quote,
        target_qty=qty,
        allow_partial=True,
    )
    cost_usd, cost_hkd = quote_to_usd_and_hkd(w["cost_usd"], quote_asset, rate)
    vwap_usd, vwap_hkd = quote_to_usd_and_hkd(w["vwap"], quote_asset, rate)
    last_usd, last_hkd = quote_to_usd_and_hkd(w["last_price"], quote_asset, rate)

    if side_norm == "buy":
        vwap_extra_usd = vwap_usd - top_price_usd
        vwap_extra_hkd = vwap_hkd - top_price_hkd
        last_extra_usd = last_usd - top_price_usd
    else:
        vwap_extra_usd = top_price_usd - vwap_usd
        vwap_extra_hkd = top_price_hkd - vwap_hkd
        last_extra_usd = top_price_usd - last_usd

    vwap_slip_bp = adverse_slippage_bp(top_price, w["vwap"], side_norm)
    last_slip_bp = adverse_slippage_bp(top_price, w["last_price"], side_norm)
    slip_loss_usd = cost_usd * (vwap_slip_bp / 1e4)
    slip_loss_hkd = cost_hkd * (vwap_slip_bp / 1e4)
    all_in_bp = vwap_slip_bp + taker_fee_bp
    all_in_usd = slip_loss_usd + cost_usd * (taker_fee_bp / 1e4)
    all_in_hkd = slip_loss_hkd + cost_hkd * (taker_fee_bp / 1e4)

    base_bp = static_slip_bp if (static_slip_bp is not None and static_slip_bp > 1e-4) else vwap_slip_bp
    amp = (vwap_slip_bp / base_bp) if base_bp > 1e-4 else 1.0

    return FadeScenario(
        fade_rate=fade_rate,
        label=label,
        levels_used=w["levels_used"],
        filled_qty=w["filled_qty"],
        fill_ratio=w["fill_ratio"],
        depth_exhausted=w["depth_exhausted"],
        vwap_usd=vwap_usd,
        vwap_hkd=vwap_hkd,
        last_price_usd=last_usd,
        last_price_hkd=last_hkd,
        vwap_extra_per_coin_usd=vwap_extra_usd,
        vwap_extra_per_coin_hkd=vwap_extra_hkd,
        last_extra_per_coin_usd=last_extra_usd,
        vwap_slippage_bp=vwap_slip_bp,
        last_slippage_bp=last_slip_bp,
        slippage_loss_usd=slip_loss_usd,
        slippage_loss_hkd=slip_loss_hkd,
        all_in_cost_bp=all_in_bp,
        all_in_loss_usd=all_in_usd,
        all_in_loss_hkd=all_in_hkd,
        amplification_vs_static=amp,
    )


def evaluate_snapshot(
    snapshot: OrderBookSnapshot,
    side: str,
    input_currency: str = "USD",
    notional: Optional[float] = None,
    qty: Optional[float] = None,
    fx_usd_hkd: Optional[float] = None,
    taker_fee_bp: Optional[float] = None,
    otc_quote_bp: float = DEFAULT_OTC_QUOTE_BP,
    fade_rate: float = 0.0,
    allow_partial: bool = True,
) -> SlippageResult:
    """Evaluate static slippage, realistic MM withdrawal + price retreat, and OTC comparison."""
    side_norm = side.lower().strip()
    if side_norm not in ("buy", "sell"):
        raise ValueError(f"side 必须是 'buy' 或 'sell' (收到: {side!r})")

    in_ccy = input_currency.upper().strip()
    if in_ccy not in ("USD", "HKD"):
        raise ValueError(f"input_currency 必须是 'USD' 或 'HKD' (收到: {input_currency!r})")

    rate = fx_usd_hkd if (fx_usd_hkd and fx_usd_hkd > 0) else get_usd_hkd_rate()[0]
    eff_taker_fee_bp = (
        taker_fee_bp
        if taker_fee_bp is not None
        else EXCHANGE_TAKER_FEE_BP.get(snapshot.exchange, DEFAULT_TAKER_FEE_BP)
    )

    raw_levels = snapshot.bids if side_norm == "sell" else snapshot.asks
    levels = apply_maker_fade(raw_levels, fade_rate, side=side_norm)

    top_bid = snapshot.best_bid
    top_ask = snapshot.best_ask
    mid = snapshot.mid_price
    spread_bp = snapshot.spread_bp
    top_price = top_bid if side_norm == "sell" else top_ask
    top_price_usd, top_price_hkd = quote_to_usd_and_hkd(
        top_price, snapshot.quote_asset, rate
    )

    level1_qty = raw_levels[0][1] if raw_levels else 0.0
    level1_quote = top_price * level1_qty
    level1_usd, level1_hkd = quote_to_usd_and_hkd(
        level1_quote, snapshot.quote_asset, rate
    )

    target_quote: Optional[float] = None
    target_usd: Optional[float] = None
    target_hkd: Optional[float] = None

    if notional is not None:
        target_quote = convert_notional_to_quote(
            notional, in_ccy, snapshot.quote_asset, rate
        )
        if in_ccy == "USD":
            target_usd = notional
            target_hkd = notional * rate
        else:
            target_hkd = notional
            target_usd = notional / rate

    walk = walk_book(
        levels,
        target_usd=target_quote,
        target_qty=qty,
        allow_partial=allow_partial,
    )

    cost_quote = walk["cost_usd"]
    cost_usd, cost_hkd = quote_to_usd_and_hkd(cost_quote, snapshot.quote_asset, rate)
    vwap_quote = walk["vwap"]
    vwap_usd, vwap_hkd = quote_to_usd_and_hkd(vwap_quote, snapshot.quote_asset, rate)
    last_quote = walk["last_price"]
    last_usd, last_hkd = quote_to_usd_and_hkd(last_quote, snapshot.quote_asset, rate)

    if target_usd and target_usd > 0:
        level1_fill_pct = min(100.0, (level1_usd / target_usd) * 100.0)
    elif qty and qty > 0:
        level1_fill_pct = min(100.0, (level1_qty / qty) * 100.0)
    else:
        level1_fill_pct = 0.0

    if side_norm == "buy":
        vwap_extra_usd = vwap_usd - top_price_usd
        vwap_extra_hkd = vwap_hkd - top_price_hkd
        last_extra_usd = last_usd - top_price_usd
        last_extra_hkd = last_hkd - top_price_hkd
    else:
        vwap_extra_usd = top_price_usd - vwap_usd
        vwap_extra_hkd = top_price_hkd - vwap_hkd
        last_extra_usd = top_price_usd - last_usd
        last_extra_hkd = top_price_hkd - last_hkd

    vwap_slip_bp = adverse_slippage_bp(top_price, vwap_quote, side_norm)
    last_slip_bp = adverse_slippage_bp(top_price, last_quote, side_norm)
    vwap_vs_mid_bp = adverse_slippage_bp(mid, vwap_quote, side_norm)
    signed_diff_bp = (
        (top_price - vwap_quote) / top_price * 1e4 if top_price > 0 else 0.0
    )

    slippage_loss_usd = cost_usd * (vwap_slip_bp / 1e4)
    slippage_loss_hkd = cost_hkd * (vwap_slip_bp / 1e4)

    taker_fee_usd = cost_usd * (eff_taker_fee_bp / 1e4)
    taker_fee_hkd = cost_hkd * (eff_taker_fee_bp / 1e4)
    all_in_cost_bp = vwap_slip_bp + eff_taker_fee_bp
    all_in_loss_usd = slippage_loss_usd + taker_fee_usd
    all_in_loss_hkd = slippage_loss_hkd + taker_fee_hkd
    if side_norm == "buy":
        all_in_vwap_usd = vwap_usd * (1.0 + eff_taker_fee_bp / 1e4)
    else:
        all_in_vwap_usd = vwap_usd * (1.0 - eff_taker_fee_bp / 1e4)

    book_total_quote = sum(p * q for p, q in raw_levels)
    book_total_qty = sum(q for _, q in raw_levels)
    book_total_usd, book_total_hkd = quote_to_usd_and_hkd(
        book_total_quote, snapshot.quote_asset, rate
    )

    bands = compute_depth_bands(raw_levels, side_norm, snapshot.quote_asset, rate)

    # Compute realistic 5-component institutional execution breakdown vs OTC quote
    order_usd_for_model = target_usd if (target_usd and target_usd > 0) else cost_usd
    real_bd, mm_retreat_bp, full_dyn_shift_bp = compute_real_execution_breakdown(
        exchange=snapshot.exchange,
        quote_asset=snapshot.quote_asset,
        side_norm=side_norm,
        order_usd=order_usd_for_model,
        top_price_usd=top_price_usd,
        top_price_hkd=top_price_hkd,
        fx_usd_hkd=rate,
        static_slip_bp=vwap_slip_bp,
        depth_bands=bands,
        taker_fee_bp=eff_taker_fee_bp,
        otc_quote_bp=otc_quote_bp,
    )

    # Compute 3 scenarios:
    #   0: 0% Static Ideal
    #   1: 50% MM Cancel + Price Retreat (`mm_retreat_bp`)
    #   2: 70% Heavy MM Cancel + Retreat + HFT Front-running (`full_dyn_shift_bp`)
    fade_scenarios: List[FadeScenario] = []
    static_bp_ref: Optional[float] = None
    scenario_specs = [
        (0.0, 0.0, DEFAULT_FADE_SCENARIOS[0][1]),
        (0.50, mm_retreat_bp, DEFAULT_FADE_SCENARIOS[1][1]),
        (0.70, full_dyn_shift_bp, DEFAULT_FADE_SCENARIOS[2][1]),
    ]
    for f_rate, r_bp, f_label in scenario_specs:
        sc = _compute_fade_scenario(
            raw_levels=raw_levels,
            fade_rate=f_rate,
            retreat_bp=r_bp,
            label=f_label,
            side_norm=side_norm,
            quote_asset=snapshot.quote_asset,
            rate=rate,
            top_price=top_price,
            top_price_usd=top_price_usd,
            top_price_hkd=top_price_hkd,
            target_quote=target_quote,
            qty=qty,
            taker_fee_bp=eff_taker_fee_bp,
            static_slip_bp=static_bp_ref,
        )
        if f_rate == 0.0:
            static_bp_ref = sc.vwap_slippage_bp
        fade_scenarios.append(sc)

    return SlippageResult(
        exchange=snapshot.exchange,
        exchange_label=snapshot.exchange_label,
        symbol=snapshot.symbol,
        base_asset=snapshot.base_asset,
        quote_asset=snapshot.quote_asset,
        side=side_norm,
        input_currency=in_ccy,
        target_notional_input=notional,
        target_notional_usd=target_usd,
        target_notional_hkd=target_hkd,
        target_notional_quote=target_quote,
        target_qty=qty,
        fx_usd_hkd=rate,
        applied_fade_rate=fade_rate,
        top_bid=top_bid,
        top_ask=top_ask,
        mid_price=mid,
        spread_bp=spread_bp,
        top_price=top_price,
        top_price_usd=top_price_usd,
        top_price_hkd=top_price_hkd,
        level1_qty=level1_qty,
        level1_notional_usd=level1_usd,
        level1_notional_hkd=level1_hkd,
        level1_fill_pct=level1_fill_pct,
        filled_qty=walk["filled_qty"],
        cost_quote=cost_quote,
        cost_usd=cost_usd,
        cost_hkd=cost_hkd,
        vwap_quote=vwap_quote,
        vwap_usd=vwap_usd,
        vwap_hkd=vwap_hkd,
        last_price_quote=last_quote,
        last_price_usd=last_usd,
        last_price_hkd=last_hkd,
        vwap_extra_per_coin_usd=vwap_extra_usd,
        vwap_extra_per_coin_hkd=vwap_extra_hkd,
        last_extra_per_coin_usd=last_extra_usd,
        last_extra_per_coin_hkd=last_extra_hkd,
        slippage_loss_usd=slippage_loss_usd,
        slippage_loss_hkd=slippage_loss_hkd,
        taker_fee_bp=eff_taker_fee_bp,
        taker_fee_usd=taker_fee_usd,
        taker_fee_hkd=taker_fee_hkd,
        all_in_cost_bp=all_in_cost_bp,
        all_in_loss_usd=all_in_loss_usd,
        all_in_loss_hkd=all_in_loss_hkd,
        all_in_vwap_usd=all_in_vwap_usd,
        vwap_slippage_bp=vwap_slip_bp,
        last_slippage_bp=last_slip_bp,
        vwap_vs_mid_bp=vwap_vs_mid_bp,
        signed_vwap_diff_bp=signed_diff_bp,
        levels_used=walk["levels_used"],
        book_levels=len(raw_levels),
        book_side_total_quote=book_total_quote,
        book_side_total_usd=book_total_usd,
        book_side_total_hkd=book_total_hkd,
        book_side_total_qty=book_total_qty,
        fill_ratio=walk["fill_ratio"],
        depth_exhausted=walk["depth_exhausted"],
        depth_bands=bands,
        fade_scenarios=fade_scenarios,
        real_breakdown=real_bd,
        latency_ms=snapshot.latency_ms,
        sequence_id=snapshot.sequence_id,
    )


def estimate_single(
    exchange: str,
    symbol: str,
    side: str = "sell",
    notional: Optional[float] = None,
    qty: Optional[float] = None,
    currency: str = "USD",
    limit: int = 5000,
    fx_rate: Optional[float] = None,
    taker_fee_bp: Optional[float] = None,
    otc_quote_bp: float = DEFAULT_OTC_QUOTE_BP,
    fade_rate: float = 0.0,
    allow_partial: bool = True,
    prefer_native_hkd: bool = False,
) -> SlippageResult:
    """Fetch order book and estimate slippage on a single exchange."""
    rate, _ = get_usd_hkd_rate(override_rate=fx_rate)
    snap = fetch_orderbook(
        exchange=exchange,
        user_symbol=symbol,
        input_currency=currency,
        limit=limit,
        prefer_native_hkd=prefer_native_hkd,
    )
    return evaluate_snapshot(
        snapshot=snap,
        side=side,
        input_currency=currency,
        notional=notional,
        qty=qty,
        fx_usd_hkd=rate,
        taker_fee_bp=taker_fee_bp,
        otc_quote_bp=otc_quote_bp,
        fade_rate=fade_rate,
        allow_partial=allow_partial,
    )


def compare_exchanges(
    symbol: str,
    side: str = "sell",
    notional: Optional[float] = None,
    qty: Optional[float] = None,
    currency: str = "USD",
    exchanges: Optional[Sequence[str]] = None,
    limit: int = 5000,
    fx_rate: Optional[float] = None,
    taker_fee_bp: Optional[float] = None,
    otc_quote_bp: float = DEFAULT_OTC_QUOTE_BP,
    fade_rate: float = 0.0,
    include_native_hkd_on_hashkey: bool = True,
) -> List[SlippageResult]:
    """
    Fetch order books concurrently across multiple exchanges and compute slippage,
    realistic MM retreat & full-chain execution breakdown, and OTC comparison.
    """
    rate, _ = get_usd_hkd_rate(override_rate=fx_rate)
    target_exchanges = list(exchanges) if exchanges else list(DEFAULT_COMPARE_EXCHANGES)

    if len(target_exchanges) == 1 and target_exchanges[0].lower() == "all":
        target_exchanges = list(DEFAULT_COMPARE_EXCHANGES)

    if (
        include_native_hkd_on_hashkey
        and currency.upper() == "HKD"
        and "hashkey" in target_exchanges
        and "hashkey_hkd" not in target_exchanges
    ):
        target_exchanges.append("hashkey_hkd")

    results_by_ex: Dict[str, SlippageResult] = {}

    def _worker(ex_name: str) -> Tuple[str, SlippageResult]:
        try:
            res = estimate_single(
                exchange=ex_name,
                symbol=symbol,
                side=side,
                notional=notional,
                qty=qty,
                currency=currency,
                limit=limit,
                fx_rate=rate,
                taker_fee_bp=taker_fee_bp,
                otc_quote_bp=otc_quote_bp,
                fade_rate=fade_rate,
                allow_partial=True,
            )
            return ex_name, res
        except Exception as e:
            label = SUPPORTED_EXCHANGES.get(ex_name, ex_name)
            err_res = SlippageResult(
                exchange=ex_name,
                exchange_label=label,
                symbol=symbol.upper(),
                base_asset=symbol.upper(),
                quote_asset="",
                side=side.lower(),
                input_currency=currency.upper(),
                target_notional_input=notional,
                target_notional_usd=None,
                target_notional_hkd=None,
                target_notional_quote=None,
                target_qty=qty,
                fx_usd_hkd=rate,
                applied_fade_rate=fade_rate,
                top_bid=0.0,
                top_ask=0.0,
                mid_price=0.0,
                spread_bp=0.0,
                top_price=0.0,
                top_price_usd=0.0,
                top_price_hkd=0.0,
                level1_qty=0.0,
                level1_notional_usd=0.0,
                level1_notional_hkd=0.0,
                level1_fill_pct=0.0,
                filled_qty=0.0,
                cost_quote=0.0,
                cost_usd=0.0,
                cost_hkd=0.0,
                vwap_quote=0.0,
                vwap_usd=0.0,
                vwap_hkd=0.0,
                last_price_quote=0.0,
                last_price_usd=0.0,
                last_price_hkd=0.0,
                vwap_extra_per_coin_usd=0.0,
                vwap_extra_per_coin_hkd=0.0,
                last_extra_per_coin_usd=0.0,
                last_extra_per_coin_hkd=0.0,
                slippage_loss_usd=0.0,
                slippage_loss_hkd=0.0,
                taker_fee_bp=taker_fee_bp or DEFAULT_TAKER_FEE_BP,
                taker_fee_usd=0.0,
                taker_fee_hkd=0.0,
                all_in_cost_bp=0.0,
                all_in_loss_usd=0.0,
                all_in_loss_hkd=0.0,
                all_in_vwap_usd=0.0,
                vwap_slippage_bp=0.0,
                last_slippage_bp=0.0,
                vwap_vs_mid_bp=0.0,
                signed_vwap_diff_bp=0.0,
                levels_used=0,
                book_levels=0,
                book_side_total_quote=0.0,
                book_side_total_usd=0.0,
                book_side_total_hkd=0.0,
                book_side_total_qty=0.0,
                fill_ratio=0.0,
                depth_exhausted=True,
                error=str(e),
            )
            return ex_name, err_res

    with ThreadPoolExecutor(max_workers=min(8, len(target_exchanges) or 1)) as pool:
        futures = [pool.submit(_worker, ex) for ex in target_exchanges]
        for fut in as_completed(futures):
            ex_name, res = fut.result()
            results_by_ex[ex_name] = res

    ordered: List[SlippageResult] = []
    for ex_name in target_exchanges:
        res = results_by_ex.get(ex_name)
        if not res:
            continue
        if (
            ex_name == "hashkey_hkd"
            and "hashkey" in results_by_ex
            and res.symbol == results_by_ex["hashkey"].symbol
        ):
            continue
        ordered.append(res)

    return ordered


def build_otc_pitch_lines(results: Sequence[SlippageResult]) -> List[str]:
    """
    Generate sales-ready bullet points proving why OTC Block Trade (~30 bp all-in)
    is cheaper than real-world order-book execution (35 ~ 60+ bp all-in).
    """
    valid = [r for r in results if not r.error and r.real_breakdown is not None]
    if not valid:
        return []

    first = valid[0]
    bd0 = first.real_breakdown
    assert bd0 is not None
    is_buy = first.side == "buy"
    worse_word = "多付" if is_buy else "少收"
    top_label = "卖一 (Ask 1)" if is_buy else "买一 (Bid 1)"

    lines: List[str] = []

    # 1. Why static slippage is a cognitive trap
    l1_parts = [
        f"{r.exchange_label} {top_label} (`${r.top_price_usd:,.2f}`) 仅挂单 `{r.level1_qty:,.4f} {r.base_asset}` "
        f"(仅占本单 **{r.level1_fill_pct:.1f}%**，静态需吃穿 **{r.levels_used} 档**)"
        for r in valid[:4]
    ]
    lines.append(
        "1. **为什么「静态盘口滑点 (1~3 bp)」是假象？**\n"
        "   - 静态盘口前百档挂单并非散户死单，而是 3~5 家高频做市商（HFT）铺设的 `$0.01` 微网格探针单（"
        + "；".join(l1_parts)
        + "）。\n"
        f"   - 做市商在紧贴盘口处的**单波风险容忍上限（Probe Limit）仅约 `${bd0.probe_depth_usd:,.0f}`**。"
        f"当本单（`${first.cost_usd:,.0f}`）分约 **{bd0.estimated_waves} 波**打入时，首波成交后 **< 5 毫秒**内做市商的 **VPIN/OFI 订单流毒性模型**即触发 **`Cancel-All & Re-quote Wider`（前排网格全撤 + 整体向后退价重挂）**，同时触发动量 HFT 跨所抢跑（Front-running）。"
    )

    # 2. Full-chain 5-component cost breakdown per exchange vs OTC 30bp
    detail_parts = []
    for r in valid:
        b = r.real_breakdown
        assert b is not None
        fiat_txt = (
            f" + 法币换USDT摩擦 `{b.fiat_usdt_friction_bp:.1f}bp`"
            if b.fiat_usdt_friction_bp > 0
            else " + 原生法币盘口(0换汇费)"
        )
        if b.otc_savings_bp > 0:
            vs_txt = (
                f"👉 **走 OTC ({b.otc_quote_bp:.0f}bp 一口价) 比在 {r.exchange_label} 盘面拿货直接净省 "
                f"`{b.otc_savings_bp:.1f} bp` (`${b.otc_savings_usd:,.0f} USD` / `HK${b.otc_savings_hkd:,.0f} HKD`，"
                f"每颗 {r.base_asset} 省 `${b.otc_savings_per_coin_usd:,.2f}`)**！"
            )
        else:
            vs_txt = (
                f"👉 盘面真实全包成本 `{b.total_orderbook_cost_bp:.1f} bp` 已逼近 OTC `{b.otc_quote_bp:.0f} bp` 锁价成本，且盘面承担剧烈撤单踏空风险。"
            )
        detail_parts.append(
            f"   - **{r.exchange_label} (`{r.symbol}`) 真实全口径损耗 = `{b.total_orderbook_cost_bp:.1f} bp` (`${b.total_orderbook_cost_usd:,.0f} USD` / `HK${b.total_orderbook_cost_hkd:,.0f} HKD`)**：\n"
            f"     拆解：`静态表层滑点 {b.static_slippage_bp:.1f}bp` + **`做市商撤空退价(MM Retreat) {b.mm_retreat_bp:.1f}bp`** + "
            f"**`HFT抢跑与跨所传染 {b.hft_frontrun_bp:.1f}bp`** + `交易所Taker费 {b.taker_fee_bp:.1f}bp`{fiat_txt} "
            f"→ 真实落地全包均价 **`${b.realistic_all_in_vwap_usd:,.2f}`**（比屏幕第 1 档每颗 {r.base_asset} {worse_word} **`+${b.realistic_extra_per_coin_usd:,.2f}`**）。\n"
            f"     {vs_txt}"
        )
    lines.append(
        f"2. **盘面五项真实全链路成本 vs OTC `{bd0.otc_quote_bp:.0f} bp` 一口价对标**：\n"
        + "\n".join(detail_parts)
    )

    lines.append(
        f"3. **结论：为什么 `{bd0.otc_quote_bp:.0f} bp` 的 OTC Block Trade 比盘面更划算？**\n"
        f"   - 客户看到盘面滑点只有几个 bp，是因为漏算了 **做市商毫秒级撤空退价（+{bd0.mm_retreat_bp:.0f}bp）**、**HFT 动量抢跑（+{bd0.hft_frontrun_bp:.0f}bp）**、**交易所 Taker 手续费（+{bd0.taker_fee_bp:.0f}bp）** 以及 **USD/HKD 换 USDT 的双腿汇率与点差摩擦（+{bd0.fiat_usdt_friction_bp:.0f}bp）**。\n"
        f"   - 五项叠加后，盘面真实执行成本高达 **{min(r.real_breakdown.total_orderbook_cost_bp for r in valid):.1f} ~ {max(r.real_breakdown.total_orderbook_cost_bp for r in valid):.1f} bp**；而 **OTC {bd0.otc_quote_bp:.0f} bp 是一口价全包净价（0 Taker 费、0 换汇摩擦、0 撤单抢跑冲击、100% 锁价交割）**，既省钱又彻底消除执行不确定性。"
    )
    return lines


def verify_orderbook(
    exchange: str,
    symbol: str,
    side: str = "sell",
    notional: Optional[float] = 1_000_000.0,
    qty: Optional[float] = None,
    currency: str = "USD",
    samples: int = 3,
    interval_sec: float = 1.2,
    limit: int = 1000,
) -> VerificationReport:
    """
    Verify that the Maker orders displayed in the order book are authentic, fresh,
    and persistent across multiple snapshots.
    """
    samples = max(1, min(samples, 10))
    rate, _ = get_usd_hkd_rate()

    snapshots: List[OrderBookSnapshot] = []
    eval_results: List[SlippageResult] = []

    for i in range(samples):
        if i > 0 and interval_sec > 0:
            time.sleep(interval_sec)
        snap = fetch_orderbook(
            exchange=exchange,
            user_symbol=symbol,
            input_currency=currency,
            limit=limit,
        )
        snapshots.append(snap)
        res = evaluate_snapshot(
            snapshot=snap,
            side=side,
            input_currency=currency,
            notional=notional if qty is None else None,
            qty=qty,
            fx_usd_hkd=rate,
            allow_partial=True,
        )
        eval_results.append(res)

    first = snapshots[0]
    last = snapshots[-1]

    bids_desc = all(
        first.bids[i][0] > first.bids[i + 1][0] for i in range(len(first.bids) - 1)
    )
    asks_asc = all(
        first.asks[i][0] < first.asks[i + 1][0] for i in range(len(first.asks) - 1)
    )
    is_crossed = first.best_bid >= first.best_ask

    seq_ids = [s.sequence_id for s in snapshots]
    numeric_seqs = [int(s) for s in seq_ids if s and s.isdigit()]
    seq_monotonic = (
        all(numeric_seqs[i] <= numeric_seqs[i + 1] for i in range(len(numeric_seqs) - 1))
        if len(numeric_seqs) >= 2
        else True
    )

    clock_skew_ms: Optional[float] = None
    if last.exchange_ts_ms:
        clock_skew_ms = float(last.fetched_at_ms - last.exchange_ts_ms)

    latest_trade_price: Optional[float] = None
    latest_trade_ts_ms: Optional[int] = None
    trade_vs_mid_bp: Optional[float] = None
    trade_aligned: Optional[bool] = None
    try:
        trades = fetch_recent_trades(first.exchange, first.symbol, limit=5)
        if trades:
            latest_trade_price = trades[0]["price"]
            latest_trade_ts_ms = trades[0].get("ts_ms")
            if last.mid_price > 0 and latest_trade_price > 0:
                trade_vs_mid_bp = (
                    abs(latest_trade_price - last.mid_price) / last.mid_price * 1e4
                )
                trade_aligned = (
                    last.best_bid * 0.9985
                    <= latest_trade_price
                    <= last.best_ask * 1.0015
                )
    except Exception:
        pass

    depth_10bp_samples: List[float] = []
    depth_25bp_samples: List[float] = []
    slip_samples: List[float] = []

    for r in eval_results:
        slip_samples.append(r.vwap_slippage_bp)
        b10 = next((b.cum_usd for b in r.depth_bands if abs(b.bp - 10.0) < 1e-6), 0.0)
        b25 = next((b.cum_usd for b in r.depth_bands if abs(b.bp - 25.0) < 1e-6), 0.0)
        depth_10bp_samples.append(b10)
        depth_25bp_samples.append(b25)

    def _cv_pct(vals: List[float]) -> float:
        if len(vals) < 2:
            return 0.0
        mean = sum(vals) / len(vals)
        if mean <= 0:
            return 0.0
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return (math.sqrt(var) / mean) * 100.0

    cv_10bp = _cv_pct(depth_10bp_samples)
    cv_25bp = _cv_pct(depth_25bp_samples)

    retention_scores: List[float] = []
    for i in range(len(snapshots) - 1):
        s_a = snapshots[i]
        s_b = snapshots[i + 1]
        lv_a = s_a.bids[:50] if side.lower() == "sell" else s_a.asks[:50]
        lv_b = s_b.bids[:50] if side.lower() == "sell" else s_b.asks[:50]
        prices_a = {round(p, 8) for p, _ in lv_a}
        prices_b = {round(p, 8) for p, _ in lv_b}
        if prices_a:
            retention_scores.append(len(prices_a & prices_b) / len(prices_a))
    retention_ratio = (
        sum(retention_scores) / len(retention_scores) if retention_scores else 1.0
    )

    warnings: List[str] = []
    if is_crossed:
        warnings.append("盘口出现买卖倒挂 (Best Bid >= Best Ask)，快照可能存在异常")
    if not bids_desc or not asks_asc:
        warnings.append("盘口档位价格排序异常 (非严格递减/递增)")
    if not seq_monotonic:
        warnings.append("多次采样序列号未单调递增，可能命中了陈旧缓存节点")
    if trade_aligned is False:
        warnings.append(
            f"最新公开成交价 ({latest_trade_price}) 偏离当前盘口中间价 {trade_vs_mid_bp:.1f} bp"
        )
    if cv_25bp > 25.0:
        warnings.append(
            f"25bp 内挂单深度在采样期间波动较大 (CV={cv_25bp:.1f}%)，存在高频闪单/撤单现象"
        )

    if not warnings:
        verdict = (
            "PASS — 盘口结构严密、最新逐笔成交价与盘口吻合、多帧采样深度稳定"
        )
    else:
        verdict = f"WARN — 发现 {len(warnings)} 项需要注意的盘口特征"

    return VerificationReport(
        exchange=first.exchange,
        exchange_label=first.exchange_label,
        symbol=first.symbol,
        samples_count=samples,
        interval_sec=interval_sec,
        is_crossed=is_crossed,
        bids_strictly_descending=bids_desc,
        asks_strictly_ascending=asks_asc,
        spread_bp=last.spread_bp,
        best_bid=last.best_bid,
        best_ask=last.best_ask,
        sequence_ids=seq_ids,
        sequence_monotonic=seq_monotonic,
        exchange_clock_skew_ms=clock_skew_ms,
        latest_trade_price=latest_trade_price,
        latest_trade_ts_ms=latest_trade_ts_ms,
        trade_vs_mid_bp=trade_vs_mid_bp,
        trade_within_inside_spread_or_10bp=trade_aligned,
        depth_10bp_usd_samples=depth_10bp_samples,
        depth_25bp_usd_samples=depth_25bp_samples,
        depth_10bp_cv_pct=cv_10bp,
        depth_25bp_cv_pct=cv_25bp,
        level_retention_ratio=retention_ratio,
        simulated_slippage_bp_samples=slip_samples,
        simulated_slippage_bp_min=min(slip_samples) if slip_samples else None,
        simulated_slippage_bp_max=max(slip_samples) if slip_samples else None,
        simulated_slippage_bp_mean=(
            sum(slip_samples) / len(slip_samples) if slip_samples else None
        ),
        verdict=verdict,
        warnings=warnings,
    )
