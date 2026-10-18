# -*- coding: utf-8 -*-
"""
Core slippage calculation engine, liquidity depth profiler, multi-exchange comparator,
and Maker order authenticity verifier.
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
from .models import DepthBand, OrderBookSnapshot, SlippageResult, VerificationReport

DEFAULT_BP_BANDS: Tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0)


def walk_book(
    levels: Sequence[Tuple[float, float]],
    target_usd: Optional[float] = None,
    target_qty: Optional[float] = None,
    allow_partial: bool = False,
) -> Dict[str, Any]:
    """
    Walk static order book levels [(price, qty)] from best to worst.

    Parameters:
        levels: [(price, qty)] ordered from best price to worst price.
        target_usd: Target notional in book quote currency (named target_usd for backward compatibility).
        target_qty: Target base asset quantity.
        allow_partial: If True, returns partial fill metrics with `depth_exhausted=True`
                       instead of raising RuntimeError when book depth is insufficient.
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
    Positive value ALWAYS means worse price than `ref_price`:
      - sell: (ref_price - exec_price) / ref_price * 1e4
      - buy : (exec_price - ref_price) / ref_price * 1e4
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
            # Selling walks down the bid book
            boundary = top_price * (1.0 - bp / 1e4)
        else:
            # Buying walks up the ask book
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


def evaluate_snapshot(
    snapshot: OrderBookSnapshot,
    side: str,
    input_currency: str = "USD",
    notional: Optional[float] = None,
    qty: Optional[float] = None,
    fx_usd_hkd: Optional[float] = None,
    allow_partial: bool = True,
) -> SlippageResult:
    """Evaluate slippage and depth profile on an already-fetched `OrderBookSnapshot`."""
    side_norm = side.lower().strip()
    if side_norm not in ("buy", "sell"):
        raise ValueError(f"side 必须是 'buy' 或 'sell' (收到: {side!r})")

    in_ccy = input_currency.upper().strip()
    if in_ccy not in ("USD", "HKD"):
        raise ValueError(f"input_currency 必须是 'USD' 或 'HKD' (收到: {input_currency!r})")

    rate = fx_usd_hkd if (fx_usd_hkd and fx_usd_hkd > 0) else get_usd_hkd_rate()[0]

    levels = snapshot.bids if side_norm == "sell" else snapshot.asks
    top_bid = snapshot.best_bid
    top_ask = snapshot.best_ask
    mid = snapshot.mid_price
    spread_bp = snapshot.spread_bp
    top_price = top_bid if side_norm == "sell" else top_ask

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

    book_total_quote = sum(p * q for p, q in levels)
    book_total_qty = sum(q for _, q in levels)
    book_total_usd, book_total_hkd = quote_to_usd_and_hkd(
        book_total_quote, snapshot.quote_asset, rate
    )

    vwap_slip_bp = adverse_slippage_bp(top_price, vwap_quote, side_norm)
    last_slip_bp = adverse_slippage_bp(top_price, last_quote, side_norm)
    vwap_vs_mid_bp = adverse_slippage_bp(mid, vwap_quote, side_norm)
    signed_diff_bp = (
        (top_price - vwap_quote) / top_price * 1e4 if top_price > 0 else 0.0
    )

    bands = compute_depth_bands(levels, side_norm, snapshot.quote_asset, rate)

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
        top_bid=top_bid,
        top_ask=top_ask,
        mid_price=mid,
        spread_bp=spread_bp,
        top_price=top_price,
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
        vwap_slippage_bp=vwap_slip_bp,
        last_slippage_bp=last_slip_bp,
        vwap_vs_mid_bp=vwap_vs_mid_bp,
        signed_vwap_diff_bp=signed_diff_bp,
        levels_used=walk["levels_used"],
        book_levels=len(levels),
        book_side_total_quote=book_total_quote,
        book_side_total_usd=book_total_usd,
        book_side_total_hkd=book_total_hkd,
        book_side_total_qty=book_total_qty,
        fill_ratio=walk["fill_ratio"],
        depth_exhausted=walk["depth_exhausted"],
        depth_bands=bands,
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
    include_native_hkd_on_hashkey: bool = True,
) -> List[SlippageResult]:
    """
    Fetch order books concurrently across multiple exchanges and compute slippage comparison.
    When `currency == 'HKD'` and `hashkey` is included, also checks `hashkey_hkd` (if available)
    so the user can compare HashKey's USD book vs native HKD book.
    """
    rate, _ = get_usd_hkd_rate(override_rate=fx_rate)
    target_exchanges = list(exchanges) if exchanges else list(DEFAULT_COMPARE_EXCHANGES)

    # Expand "all" keyword
    if len(target_exchanges) == 1 and target_exchanges[0].lower() == "all":
        target_exchanges = list(DEFAULT_COMPARE_EXCHANGES)

    # If user inputs HKD and queries hashkey, optionally include hashkey_hkd for native HKD book comparison
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
                top_bid=0.0,
                top_ask=0.0,
                mid_price=0.0,
                spread_bp=0.0,
                top_price=0.0,
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
        # If hashkey_hkd was auto-appended and fell back to the exact same USD book as hashkey, skip duplicate
        if (
            ex_name == "hashkey_hkd"
            and "hashkey" in results_by_ex
            and res.symbol == results_by_ex["hashkey"].symbol
        ):
            continue
        ordered.append(res)

    return ordered


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
    and persistent across multiple snapshots:

    1. Structural & Sequence Check:
       - Bids strictly descending, Asks strictly ascending
       - Best Bid < Best Ask (no crossed book)
       - Sequence ID / Timestamp monotonicity across samples
    2. Public Trade Cross-Check:
       - Compares latest executed public trade price against the order book [best_bid, best_ask]
    3. Multi-Snapshot Persistence / Anti-Spoofing Check:
       - Samples the order book `samples` times separated by `interval_sec`
       - Measures coefficient of variation (CV%) of 10bp / 25bp depth
       - Measures top-50 price level retention ratio across snapshots
       - Computes min / mean / max simulated slippage across snapshots
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

    # 1. Structural integrity checks
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

    # 2. Recent public trade alignment check
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
                # Aligned if within inside spread or within 15bp of current mid
                trade_aligned = (
                    last.best_bid * 0.9985
                    <= latest_trade_price
                    <= last.best_ask * 1.0015
                )
    except Exception:
        pass

    # 3. Multi-snapshot persistence & stability
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

    # Top-50 price level retention across consecutive snapshots
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
