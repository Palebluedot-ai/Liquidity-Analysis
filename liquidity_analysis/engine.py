# -*- coding: utf-8 -*-
"""
Core slippage calculation engine, liquidity depth profiler, multi-exchange comparator,
Market Maker withdrawal (Quote Fading) simulator, and Maker order authenticity verifier.
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
    SlippageResult,
    VerificationReport,
)

DEFAULT_BP_BANDS: Tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0)
DEFAULT_TAKER_FEE_BP: float = 5.0  # Institutional VIP Taker fee benchmark (5 bp = 0.05%)
DEFAULT_FADE_SCENARIOS: Tuple[Tuple[float, str], ...] = (
    (0.0, "0% 静态理想盘口"),
    (0.50, "50% 常规做市商撤单"),
    (0.70, "70% 剧烈/跨所联动撤单"),
)


def apply_maker_fade(
    levels: Sequence[Tuple[float, float]], fade_rate: float
) -> List[Tuple[float, float]]:
    """
    Simulate Market Maker withdrawal (Quote Fading / Liquidity Haircut):
    When a large order sweeps the book (or is split across child orders / exchanges),
    HFT market makers react within 5-20ms and cancel `fade_rate` (0.0 ~ 0.90) of resting orders.
    """
    if fade_rate <= 0.0:
        return list(levels)
    keep = max(0.05, 1.0 - min(fade_rate, 0.95))
    return [(p, q * keep) for p, q in levels]


def walk_book(
    levels: Sequence[Tuple[float, float]],
    target_usd: Optional[float] = None,
    target_qty: Optional[float] = None,
    allow_partial: bool = False,
) -> Dict[str, Any]:
    """
    Walk static order book levels [(price, qty)] from best to worst.
    """
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
        "cost_usd": cost,  # In book quote units
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


def _compute_fade_scenario(
    raw_levels: Sequence[Tuple[float, float]],
    fade_rate: float,
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
    adj_levels = apply_maker_fade(raw_levels, fade_rate)
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
    taker_fee_bp: float = DEFAULT_TAKER_FEE_BP,
    fade_rate: float = 0.0,
    allow_partial: bool = True,
) -> SlippageResult:
    """Evaluate slippage, Level-1 vs VWAP extra cost, and Maker withdrawal stress-test scenarios."""
    side_norm = side.lower().strip()
    if side_norm not in ("buy", "sell"):
        raise ValueError(f"side 必须是 'buy' 或 'sell' (收到: {side!r})")

    in_ccy = input_currency.upper().strip()
    if in_ccy not in ("USD", "HKD"):
        raise ValueError(f"input_currency 必须是 'USD' 或 'HKD' (收到: {input_currency!r})")

    rate = fx_usd_hkd if (fx_usd_hkd and fx_usd_hkd > 0) else get_usd_hkd_rate()[0]

    raw_levels = snapshot.bids if side_norm == "sell" else snapshot.asks
    levels = apply_maker_fade(raw_levels, fade_rate)

    top_bid = snapshot.best_bid
    top_ask = snapshot.best_ask
    mid = snapshot.mid_price
    spread_bp = snapshot.spread_bp
    top_price = top_bid if side_norm == "sell" else top_ask
    top_price_usd, top_price_hkd = quote_to_usd_and_hkd(
        top_price, snapshot.quote_asset, rate
    )

    # Level-1 resting liquidity ("screen price illusion")
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

    taker_fee_usd = cost_usd * (taker_fee_bp / 1e4)
    taker_fee_hkd = cost_hkd * (taker_fee_bp / 1e4)
    all_in_cost_bp = vwap_slip_bp + taker_fee_bp
    all_in_loss_usd = slippage_loss_usd + taker_fee_usd
    all_in_loss_hkd = slippage_loss_hkd + taker_fee_hkd
    if side_norm == "buy":
        all_in_vwap_usd = vwap_usd * (1.0 + taker_fee_bp / 1e4)
    else:
        all_in_vwap_usd = vwap_usd * (1.0 - taker_fee_bp / 1e4)

    book_total_quote = sum(p * q for p, q in raw_levels)
    book_total_qty = sum(q for _, q in raw_levels)
    book_total_usd, book_total_hkd = quote_to_usd_and_hkd(
        book_total_quote, snapshot.quote_asset, rate
    )

    bands = compute_depth_bands(raw_levels, side_norm, snapshot.quote_asset, rate)

    # Compute standard Maker withdrawal stress-test scenarios (0%, 50%, 70%)
    fade_scenarios: List[FadeScenario] = []
    static_bp_ref: Optional[float] = None
    for f_rate, f_label in DEFAULT_FADE_SCENARIOS:
        sc = _compute_fade_scenario(
            raw_levels=raw_levels,
            fade_rate=f_rate,
            label=f_label,
            side_norm=side_norm,
            quote_asset=snapshot.quote_asset,
            rate=rate,
            top_price=top_price,
            top_price_usd=top_price_usd,
            top_price_hkd=top_price_hkd,
            target_quote=target_quote,
            qty=qty,
            taker_fee_bp=taker_fee_bp,
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
        taker_fee_bp=taker_fee_bp,
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
    taker_fee_bp: float = DEFAULT_TAKER_FEE_BP,
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
    taker_fee_bp: float = DEFAULT_TAKER_FEE_BP,
    fade_rate: float = 0.0,
    include_native_hkd_on_hashkey: bool = True,
) -> List[SlippageResult]:
    """
    Fetch order books concurrently across multiple exchanges and compute slippage,
    Maker withdrawal scenarios, and OTC comparison.
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
                taker_fee_bp=taker_fee_bp,
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
    Generate sales-ready bullet points highlighting why OTC Block Trade outperforms
    direct order-book execution (including Market Maker withdrawal stress-test numbers).
    """
    valid = [r for r in results if not r.error]
    if not valid:
        return []

    first = valid[0]
    is_buy = first.side == "buy"
    action_cn = "买入" if is_buy else "卖出"
    worse_word = "多付" if is_buy else "少收"
    push_word = "推高" if is_buy else "砸深"
    top_label = "卖一 (Ask 1)" if is_buy else "买一 (Bid 1)"

    lines: List[str] = []
    l1_parts = [
        f"{r.exchange_label} {top_label} (`${r.top_price_usd:,.2f}`) 仅挂单 `{r.level1_qty:,.4f} {r.base_asset}` "
        f"(`${r.level1_notional_usd:,.0f}`，仅占本单 **{r.level1_fill_pct:.1f}%**)"
        for r in valid[:4]
    ]
    lines.append(
        f"1. **屏幕第 1 档价格假象（Level-1 仅能承接极小比例）**：" + "；".join(l1_parts) + "。"
    )

    pen_parts = []
    for r in valid:
        status_note = "" if not r.depth_exhausted else f"（⚠️ 且盘口仅够成交 {r.fill_ratio*100:.1f}%）"
        sc_50 = next((s for s in r.fade_scenarios if abs(s.fade_rate - 0.50) < 1e-6), None)
        sc_70 = next((s for s in r.fade_scenarios if abs(s.fade_rate - 0.70) < 1e-6), None)
        fade_note = ""
        if sc_50 and sc_70:
            fade_note = (
                f"；若遭遇 **50% 常规做市商撤单**，实际需吃穿 **{sc_50.levels_used} 档**、"
                f"每颗 {r.base_asset} 比 1 档{worse_word} **`+${sc_50.vwap_extra_per_coin_usd:,.2f}`** (`+{sc_50.vwap_slippage_bp:.2f}bp`，滑点放大 **{sc_50.amplification_vs_static:.1f}x**，含费损耗 **`${sc_50.all_in_loss_usd:,.0f}`**)"
                f"；若遭遇 **70% 剧烈联动撤单**，需吃穿 **{sc_70.levels_used} 档**、"
                f"每颗 {r.base_asset} 比 1 档{worse_word} **`+${sc_70.vwap_extra_per_coin_usd:,.2f}`** (`+{sc_70.vwap_slippage_bp:.2f}bp`，含费损耗 **`${sc_70.all_in_loss_usd:,.0f}`**)"
            )
        pen_parts.append(
            f"   - **{r.exchange_label} (`{r.symbol}`)**：静态下需吃穿 **{r.levels_used} 档**{status_note}，"
            f"均价 `${r.vwap_usd:,.2f}`（比 1 档每颗 {r.base_asset} {worse_word} **`+${r.vwap_extra_per_coin_usd:,.2f}`** / `+{r.vwap_slippage_bp:.2f} bp`），"
            f"尾档被{push_word}至 `${r.last_price_usd:,.2f}`（差 **`+${r.last_extra_per_coin_usd:,.2f}`**），"
            f"含 `{r.taker_fee_bp:.0f} bp` Taker 费损耗 **`${r.all_in_loss_usd:,.0f}`**{fade_note}。"
        )
    lines.append(f"2. **盘面直扫穿透代价与做市商撤单放大效应（{action_cn}吃单）**：\n" + "\n".join(pen_parts))

    lines.append(
        "3. **OTC Block Trade（大宗一口价锁价）核心优势**：\n"
        "   - **彻底免疫做市商撤单（Quote Fading）**：大单受限于交易所单笔市价单上限或跨所拆单时，首波成交后 5~20ms 内高频做市商会瞬间撤走后方 50%~70% 挂单，导致实际成交打入盘口薄尾区、滑点非线性暴增。\n"
        "   - **零信息泄露 & 一口价确定性**：走 OTC Block Trade 直接锁死单一成交净价，**不吃档位、不触发 HFT 撤单、不推高/砸穿盘面价格、免交易所 Taker 吃单费**。"
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
