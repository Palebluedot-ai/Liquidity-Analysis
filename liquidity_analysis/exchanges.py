# -*- coding: utf-8 -*-
"""
Exchange public L2 order book and recent trade adapters (no API key required).

Supported exchanges:
  - binance        : Binance Spot (data-api.binance.vision, up to 5,000 levels)
  - okx            : OKX Spot (www.okx.com, up to 5,000 levels via books-full)
  - coinbase       : Coinbase Exchange (api.exchange.coinbase.com, full L2 book)
  - kraken         : Kraken Spot (api.kraken.com, up to 500 levels)
  - hashkey        : HashKey Exchange HK (api-pro.hashkey.com, USD/USDT/HKD books, up to 500 levels)
  - hashkey_hkd    : HashKey Exchange HK Native HKD book (BTCHKD, ETHHKD, etc.)
  - hashkey_global : HashKey Global (api-glb.hashkey.com, USDT books, up to 500 levels)
  - bybit          : Bybit Spot (api.bybit.com, up to 200 levels)
"""

import json
import math
import statistics
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .models import OrderBookSnapshot
from .symbols import resolve_exchange_symbol

SUPPORTED_EXCHANGES: Dict[str, str] = {
    "binance": "Binance",
    "okx": "OKX",
    "coinbase": "Coinbase",
    "kraken": "Kraken",
    "hashkey": "HashKey HK",
    "hashkey_hkd": "HashKey HK (HKD Book)",
    "hashkey_global": "HashKey Global",
    "bybit": "Bybit",
}

DEFAULT_COMPARE_EXCHANGES: List[str] = [
    "binance",
    "okx",
    "coinbase",
    "kraken",
    "hashkey",
]

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) LiquidityAnalysis/2.0"


def _http_get_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} ({url}): {body}") from e
    except Exception as e:
        raise RuntimeError(f"网络请求失败 ({url}): {e}") from e


def fetch_orderbook(
    exchange: str,
    user_symbol: str,
    input_currency: str = "USD",
    limit: int = 5000,
    timeout: float = 15.0,
    prefer_native_hkd: bool = False,
) -> OrderBookSnapshot:
    """
    Fetch real-time L2 order book for `user_symbol` on `exchange`.
    Automatically tries candidate symbols in priority order (e.g. USDT -> USDC -> USD).
    """
    ex = exchange.lower().strip()
    if ex == "hashkey_hk":
        ex = "hashkey"
    if ex not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"不支持的交易所: {exchange!r}。可选: {', '.join(SUPPORTED_EXCHANGES)}"
        )

    candidates = resolve_exchange_symbol(
        user_symbol,
        ex,
        input_currency=input_currency,
        prefer_native_hkd=prefer_native_hkd,
    )
    last_err: Optional[Exception] = None

    for ex_sym, base_asset, quote_asset in candidates:
        try:
            t0 = time.time()
            snap = _fetch_single_symbol_book(
                ex, ex_sym, base_asset, quote_asset, limit=limit, timeout=timeout
            )
            t1 = time.time()
            snap.fetched_at_ms = int(t1 * 1000)
            snap.latency_ms = (t1 - t0) * 1000.0
            if snap.bids and snap.asks:
                return snap
        except Exception as e:
            last_err = e
            continue

    tried = ", ".join(c[0] for c in candidates)
    raise RuntimeError(
        f"[{SUPPORTED_EXCHANGES[ex]}] 无法获取 {user_symbol} 的有效盘口 (已尝试: {tried}): {last_err}"
    )


def _fetch_single_symbol_book(
    ex: str,
    symbol: str,
    base_asset: str,
    quote_asset: str,
    limit: int,
    timeout: float,
) -> OrderBookSnapshot:
    label = SUPPORTED_EXCHANGES[ex]

    if ex == "binance":
        # Valid Binance depth limits: 5, 10, 20, 50, 100, 500, 1000, 5000
        valid_limits = [5, 10, 20, 50, 100, 500, 1000, 5000]
        bn_limit = next((v for v in valid_limits if v >= min(max(limit, 5), 5000)), 5000)
        url = f"https://data-api.binance.vision/api/v3/depth?symbol={symbol}&limit={bn_limit}"
        data = _http_get_json(url, timeout=timeout)
        if "bids" not in data or "asks" not in data:
            raise RuntimeError(f"Binance 返回异常: {str(data)[:160]}")
        bids = [(float(p), float(q)) for p, q in data["bids"] if float(q) > 0]
        asks = [(float(p), float(q)) for p, q in data["asks"] if float(q) > 0]
        seq = str(data.get("lastUpdateId")) if "lastUpdateId" in data else None
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=seq,
            exchange_ts_ms=None,
            max_levels_requested=bn_limit,
        )

    if ex == "okx":
        okx_sz = min(max(limit, 1), 5000)
        if okx_sz > 400:
            url = f"https://www.okx.com/api/v5/market/books-full?instId={symbol}&sz={okx_sz}"
        else:
            url = f"https://www.okx.com/api/v5/market/books?instId={symbol}&sz={okx_sz}"
        data = _http_get_json(url, timeout=timeout)
        if data.get("code") != "0" or not data.get("data"):
            raise RuntimeError(f"OKX 返回异常: {data.get('msg') or str(data)[:160]}")
        book = data["data"][0]
        bids = [(float(row[0]), float(row[1])) for row in book.get("bids", []) if float(row[1]) > 0]
        asks = [(float(row[0]), float(row[1])) for row in book.get("asks", []) if float(row[1]) > 0]
        seq = str(book.get("seqId")) if "seqId" in book else None
        ts_ms = int(book["ts"]) if "ts" in book and str(book["ts"]).isdigit() else None
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=seq,
            exchange_ts_ms=ts_ms,
            max_levels_requested=okx_sz,
        )

    if ex == "coinbase":
        # level=2 returns full aggregated order book
        url = f"https://api.exchange.coinbase.com/products/{symbol}/book?level=2"
        data = _http_get_json(url, timeout=timeout)
        if "bids" not in data or "asks" not in data:
            raise RuntimeError(f"Coinbase 返回异常: {str(data)[:160]}")
        bids = [(float(row[0]), float(row[1])) for row in data["bids"] if float(row[1]) > 0]
        asks = [(float(row[0]), float(row[1])) for row in data["asks"] if float(row[1]) > 0]
        if limit and limit < len(bids):
            bids = bids[:limit]
        if limit and limit < len(asks):
            asks = asks[:limit]
        seq = str(data.get("sequence")) if "sequence" in data else None
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=seq,
            exchange_ts_ms=None,
            max_levels_requested=limit,
        )

    if ex == "kraken":
        kr_count = min(max(limit, 1), 500)
        url = f"https://api.kraken.com/0/public/Depth?pair={symbol}&count={kr_count}"
        data = _http_get_json(url, timeout=timeout)
        if data.get("error"):
            raise RuntimeError(f"Kraken 错误: {data['error']}")
        result = data.get("result", {})
        if not result:
            raise RuntimeError("Kraken result 为空")
        pair_key = next(iter(result.keys()))
        book = result[pair_key]
        bids = [(float(row[0]), float(row[1])) for row in book.get("bids", []) if float(row[1]) > 0]
        asks = [(float(row[0]), float(row[1])) for row in book.get("asks", []) if float(row[1]) > 0]
        # Latest level timestamp in seconds -> ms
        latest_ts = 0
        for row in (book.get("bids", [])[:5] + book.get("asks", [])[:5]):
            if len(row) >= 3:
                latest_ts = max(latest_ts, int(float(row[2]) * 1000))
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=None,
            exchange_ts_ms=latest_ts or None,
            max_levels_requested=kr_count,
        )

    if ex in ("hashkey", "hashkey_hkd"):
        hk_limit = min(max(limit, 1), 500)
        url = f"https://api-pro.hashkey.com/quote/v1/depth?symbol={symbol}&limit={hk_limit}"
        data = _http_get_json(url, timeout=timeout)
        if "b" not in data or "a" not in data:
            raise RuntimeError(f"HashKey HK 返回异常: {str(data)[:160]}")
        bids = [(float(p), float(q)) for p, q in data["b"] if float(q) > 0]
        asks = [(float(p), float(q)) for p, q in data["a"] if float(q) > 0]
        ts_ms = int(data["t"]) if "t" in data else None
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=str(ts_ms) if ts_ms else None,
            exchange_ts_ms=ts_ms,
            max_levels_requested=hk_limit,
        )

    if ex == "hashkey_global":
        hk_limit = min(max(limit, 1), 500)
        url = f"https://api-glb.hashkey.com/quote/v1/depth?symbol={symbol}&limit={hk_limit}"
        data = _http_get_json(url, timeout=timeout)
        if "b" not in data or "a" not in data:
            raise RuntimeError(f"HashKey Global 返回异常: {str(data)[:160]}")
        bids = [(float(p), float(q)) for p, q in data["b"] if float(q) > 0]
        asks = [(float(p), float(q)) for p, q in data["a"] if float(q) > 0]
        ts_ms = int(data["t"]) if "t" in data else None
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=str(ts_ms) if ts_ms else None,
            exchange_ts_ms=ts_ms,
            max_levels_requested=hk_limit,
        )

    if ex == "bybit":
        by_limit = min(max(limit, 1), 200)
        url = (
            f"https://api.bybit.com/v5/market/orderbook"
            f"?category=spot&symbol={symbol}&limit={by_limit}"
        )
        data = _http_get_json(url, timeout=timeout)
        if data.get("retCode") != 0 or "result" not in data:
            raise RuntimeError(f"Bybit 返回异常: {str(data)[:160]}")
        res = data["result"]
        bids = [(float(p), float(q)) for p, q in res.get("b", []) if float(q) > 0]
        asks = [(float(p), float(q)) for p, q in res.get("a", []) if float(q) > 0]
        seq = str(res.get("u")) if "u" in res else None
        ts_ms = int(res["ts"]) if "ts" in res else None
        return OrderBookSnapshot(
            exchange=ex,
            exchange_label=label,
            symbol=symbol,
            base_asset=base_asset,
            quote_asset=quote_asset,
            bids=bids,
            asks=asks,
            sequence_id=seq,
            exchange_ts_ms=ts_ms,
            max_levels_requested=by_limit,
        )

    raise ValueError(f"未知的交易所: {ex}")


def fetch_recent_trades(
    exchange: str, symbol: str, limit: int = 10, timeout: float = 10.0
) -> List[Dict[str, Any]]:
    """
    Fetch recent public trades for `symbol` on `exchange` to cross-verify order book prices.
    Returns list of dicts: [{"price": float, "qty": float, "ts_ms": Optional[int]}]
    """
    ex = exchange.lower().strip()
    if ex == "hashkey_hk":
        ex = "hashkey"

    if ex == "binance":
        url = f"https://data-api.binance.vision/api/v3/trades?symbol={symbol}&limit={limit}"
        data = _http_get_json(url, timeout=timeout)
        return [
            {
                "price": float(t["price"]),
                "qty": float(t["qty"]),
                "ts_ms": int(t["time"]) if "time" in t else None,
            }
            for t in data
        ]

    if ex == "okx":
        url = f"https://www.okx.com/api/v5/market/trades?instId={symbol}&limit={limit}"
        data = _http_get_json(url, timeout=timeout)
        return [
            {
                "price": float(t["px"]),
                "qty": float(t["sz"]),
                "ts_ms": int(t["ts"]) if "ts" in t else None,
            }
            for t in data.get("data", [])
        ]

    if ex == "coinbase":
        url = f"https://api.exchange.coinbase.com/products/{symbol}/trades?limit={limit}"
        data = _http_get_json(url, timeout=timeout)
        return [
            {
                "price": float(t["price"]),
                "qty": float(t["size"]),
                "ts_ms": None,
            }
            for t in data
        ]

    if ex == "kraken":
        url = f"https://api.kraken.com/0/public/Trades?pair={symbol}&count={limit}"
        data = _http_get_json(url, timeout=timeout)
        res = data.get("result", {})
        pair_key = next((k for k in res.keys() if k != "last"), None)
        if not pair_key:
            return []
        rows = res[pair_key][-limit:]
        return [
            {
                "price": float(r[0]),
                "qty": float(r[1]),
                "ts_ms": int(float(r[2]) * 1000),
            }
            for r in reversed(rows)
        ]

    if ex in ("hashkey", "hashkey_hkd"):
        url = f"https://api-pro.hashkey.com/quote/v1/trades?symbol={symbol}&limit={limit}"
        data = _http_get_json(url, timeout=timeout)
        return [
            {
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "ts_ms": int(t["t"]) if "t" in t else None,
            }
            for t in reversed(data)
        ]

    if ex == "hashkey_global":
        url = f"https://api-glb.hashkey.com/quote/v1/trades?symbol={symbol}&limit={limit}"
        data = _http_get_json(url, timeout=timeout)
        return [
            {
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "ts_ms": int(t["t"]) if "t" in t else None,
            }
            for t in reversed(data)
        ]

    if ex == "bybit":
        url = f"https://api.bybit.com/v5/market/recent-trade?category=spot&symbol={symbol}&limit={limit}"
        data = _http_get_json(url, timeout=timeout)
        return [
            {
                "price": float(t["price"]),
                "qty": float(t["size"]),
                "ts_ms": int(t["time"]) if "time" in t else None,
            }
            for t in data.get("result", {}).get("list", [])
        ]

    return []


_KLINE_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_KLINE_TTL_SEC = 30.0

_USDT_BOOK_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_USDT_BOOK_TTL_SEC = 30.0


def fetch_kline_microstructure(
    exchange: str,
    symbol: str,
    quote_asset: str = "USDT",
    usd_hkd_rate: float = 7.80,
    limit: int = 180,
    timeout: float = 8.0,
) -> Optional[Dict[str, Any]]:
    """
    Fetch real-time 1-minute K-lines (default 180 mins = 3 hours) from the exchange's
    public REST API and calibrate empirical market microstructure parameters:
      - avg_1m_vol_usd: Average 1-minute traded volume in USD
      - avg_1m_ofi_usd: Average 1-minute Net Order Flow Imbalance (OFI) in USD
                        (exact |taker_buy - taker_sell| on Binance; Bulk Volume
                        Classification |C-O|/(H-L)*Vol per Easley et al. 2012 on others)
      - sigma_1m_bp   : 1-minute return volatility in basis points (bp)
      - empirical_impact_y: 75th-percentile Square-Root Impact Law coefficient Y
                            calibrated directly from active 1m candles on this exchange:
                            Adverse_Excursion_bp = Y * sigma_1m_bp * sqrt(OFI_t / avg_1m_ofi_usd)
      - kyle_lambda_bp_per_1m: Median realized adverse price excursion (bp) per $1M of Net OFI
    """
    ex = exchange.lower().strip()
    if ex == "hashkey_hk":
        ex = "hashkey"
    cache_key = (ex, symbol.upper())
    now = time.time()
    cached = _KLINE_CACHE.get(cache_key)
    if cached and (now - cached[0] < _KLINE_TTL_SEC):
        return cached[1]

    div = usd_hkd_rate if quote_asset.upper() == "HKD" else 1.0
    bars: List[Dict[str, Any]] = []

    try:
        if ex == "binance":
            url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1m&limit={limit}"
            data = _http_get_json(url, timeout=timeout)
            for x in data:
                qvol_usd = float(x[7]) / div
                tbuy_usd = float(x[10]) / div
                tsell_usd = max(0.0, qvol_usd - tbuy_usd)
                bars.append({
                    "o": float(x[1]),
                    "h": float(x[2]),
                    "l": float(x[3]),
                    "c": float(x[4]),
                    "vol_usd": qvol_usd,
                    "net_ofi_usd": abs(tbuy_usd - tsell_usd),
                    "is_net_buy": tbuy_usd >= tsell_usd,
                })
        elif ex == "okx":
            url = f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit={limit}"
            data = _http_get_json(url, timeout=timeout).get("data", [])
            for x in data:
                qvol_usd = (float(x[7]) if len(x) > 7 else float(x[6])) / div
                bars.append({
                    "o": float(x[1]),
                    "h": float(x[2]),
                    "l": float(x[3]),
                    "c": float(x[4]),
                    "vol_usd": qvol_usd,
                    "net_ofi_usd": None,
                    "is_net_buy": float(x[4]) >= float(x[1]),
                })
        elif ex == "bybit":
            url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval=1&limit={limit}"
            data = _http_get_json(url, timeout=timeout).get("result", {}).get("list", [])
            for x in data:
                bars.append({
                    "o": float(x[1]),
                    "h": float(x[2]),
                    "l": float(x[3]),
                    "c": float(x[4]),
                    "vol_usd": float(x[6]) / div,
                    "net_ofi_usd": None,
                    "is_net_buy": float(x[4]) >= float(x[1]),
                })
        elif ex == "coinbase":
            url = f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity=60"
            data = _http_get_json(url, timeout=timeout)[:limit]
            for x in data:
                c = float(x[4])
                bars.append({
                    "o": float(x[3]),
                    "h": float(x[2]),
                    "l": float(x[1]),
                    "c": c,
                    "vol_usd": (float(x[5]) * c) / div,
                    "net_ofi_usd": None,
                    "is_net_buy": c >= float(x[3]),
                })
        elif ex == "kraken":
            url = f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval=1"
            res = _http_get_json(url, timeout=timeout).get("result", {})
            pair_key = next((k for k in res.keys() if k != "last"), None)
            if pair_key:
                for x in res[pair_key][-limit:]:
                    c = float(x[4])
                    bars.append({
                        "o": float(x[1]),
                        "h": float(x[2]),
                        "l": float(x[3]),
                        "c": c,
                        "vol_usd": (float(x[6]) * c) / div,
                        "net_ofi_usd": None,
                        "is_net_buy": c >= float(x[1]),
                    })
        elif ex in ("hashkey", "hashkey_hkd"):
            url = f"https://api-pro.hashkey.com/quote/v1/klines?symbol={symbol}&interval=1m&limit={limit}"
            data = _http_get_json(url, timeout=timeout)
            for x in data:
                bars.append({
                    "o": float(x[1]),
                    "h": float(x[2]),
                    "l": float(x[3]),
                    "c": float(x[4]),
                    "vol_usd": float(x[7]) / div,
                    "net_ofi_usd": None,
                    "is_net_buy": float(x[4]) >= float(x[1]),
                })
        elif ex == "hashkey_global":
            url = f"https://api-glb.hashkey.com/quote/v1/klines?symbol={symbol}&interval=1m&limit={limit}"
            data = _http_get_json(url, timeout=timeout)
            for x in data:
                bars.append({
                    "o": float(x[1]),
                    "h": float(x[2]),
                    "l": float(x[3]),
                    "c": float(x[4]),
                    "vol_usd": float(x[7]) / div,
                    "net_ofi_usd": None,
                    "is_net_buy": float(x[4]) >= float(x[1]),
                })
    except Exception:
        return None

    if not bars:
        return None

    for b in bars:
        if b["net_ofi_usd"] is None:
            hl = b["h"] - b["l"]
            co = abs(b["c"] - b["o"])
            ofi_frac = (co / hl) if hl > 0 else 0.5
            b["net_ofi_usd"] = b["vol_usd"] * max(0.25, ofi_frac)

    avg_vol_1m = max(1.0, statistics.mean(b["vol_usd"] for b in bars))
    avg_ofi_1m = max(1.0, statistics.mean(b["net_ofi_usd"] for b in bars))

    rets_bp = [(b["c"] - b["o"]) / b["o"] * 1e4 for b in bars if b["o"] > 0]
    sigma_1m_bp = statistics.stdev(rets_bp) if len(rets_bp) > 1 else 5.0
    if sigma_1m_bp < 1.0:
        nz_rets = [r for r in rets_bp if abs(r) > 0.01]
        if len(nz_rets) > 2:
            sigma_1m_bp = statistics.stdev(nz_rets)
        else:
            sigma_1m_bp = max(sigma_1m_bp, 4.5)

    y_samples: List[float] = []
    lambda_samples: List[float] = []
    for b in bars:
        if b["o"] <= 0 or b["net_ofi_usd"] <= max(500.0, avg_ofi_1m * 0.2):
            continue
        adv_bp = (
            (b["h"] - b["o"]) / b["o"] * 1e4
            if b["is_net_buy"]
            else (b["o"] - b["l"]) / b["o"] * 1e4
        )
        if adv_bp <= 0.05:
            continue
        ratio = b["net_ofi_usd"] / avg_ofi_1m
        if ratio > 0.1 and sigma_1m_bp > 0:
            y_samples.append(adv_bp / (sigma_1m_bp * math.sqrt(ratio)))
        lambda_samples.append(adv_bp / (b["net_ofi_usd"] / 1e6))

    y_p75 = (
        sorted(y_samples)[int(0.75 * len(y_samples))]
        if y_samples
        else 1.50
    )
    y_med = statistics.median(y_samples) if y_samples else 1.10
    lam_med = statistics.median(lambda_samples) if lambda_samples else 8.0

    result = {
        "avg_1m_vol_usd": avg_vol_1m,
        "avg_1m_ofi_usd": avg_ofi_1m,
        "sigma_1m_bp": sigma_1m_bp,
        "empirical_impact_y": y_p75,
        "empirical_impact_y_median": y_med,
        "kyle_lambda_bp_per_1m": lam_med,
        "bars_sampled": len(bars),
    }
    _KLINE_CACHE[cache_key] = (now, result)
    return result


def fetch_live_usdt_fiat_friction(
    order_usd: float,
    side: str = "buy",
    input_currency: str = "USD",
    usd_hkd_rate: float = 7.80,
    timeout: float = 6.0,
) -> Dict[str, float]:
    """
    Measure live Fiat (USD/HKD) <-> USDT order book conversion slippage and USDT par basis
    directly from live exchange stablecoin order books (Kraken USDTZUSD / HashKey USDTHKD).

    Returns:
      - usdt_book_slippage_bp: Live L2 order book walk slippage + half-spread for converting
                               `order_usd` between Fiat and USDT.
      - usdt_basis_bp        : Live |1.0 - USDT/USD mid| basis (when USDT trades at a discount,
                               e.g. 0.9988 USD, BTCUSDT nominal price is inflated by +11~12 bp
                               relative to true BTCUSD spot).
      - total_fiat_usdt_bp   : Total verifiable Fiat <-> USDT conversion + basis friction in bp.
    """
    ccy = input_currency.upper().strip()
    cache_key = ccy
    now = time.time()
    cached = _USDT_BOOK_CACHE.get(cache_key)
    raw_book = None
    if cached and (now - cached[0] < _USDT_BOOK_TTL_SEC):
        raw_book = cached[1]
    else:
        try:
            if ccy == "HKD":
                data = _http_get_json(
                    "https://api-pro.hashkey.com/quote/v1/depth?symbol=USDTHKD&limit=500",
                    timeout=timeout,
                )
                bids = [(float(p), float(q)) for p, q in data.get("b", [])]
                asks = [(float(p), float(q)) for p, q in data.get("a", [])]
                par = usd_hkd_rate
            else:
                data = _http_get_json(
                    "https://api.kraken.com/0/public/Depth?pair=USDTZUSD&count=500",
                    timeout=timeout,
                )
                res = data.get("result", {}).get("USDTZUSD", {})
                bids = [(float(r[0]), float(r[1])) for r in res.get("bids", [])]
                asks = [(float(r[0]), float(r[1])) for r in res.get("asks", [])]
                par = 1.0
            if bids and asks:
                raw_book = {"bids": bids, "asks": asks, "par": par}
                _USDT_BOOK_CACHE[cache_key] = (now, raw_book)
        except Exception:
            raw_book = None

    if not raw_book:
        # Fallback if offline: conservative Tether institutional mint/redeem + basis
        return {
            "usdt_book_slippage_bp": 1.5,
            "usdt_basis_bp": 8.5,
            "total_fiat_usdt_bp": 10.0,
        }

    bids = raw_book["bids"]
    asks = raw_book["asks"]
    par = raw_book["par"]
    mid = (bids[0][0] + asks[0][0]) / 2.0
    half_spread_bp = (asks[0][0] - bids[0][0]) / (2.0 * mid) * 1e4 if mid > 0 else 0.5

    # Walk the USDT/Fiat book for `order_usd` (in USDT units ~ order_usd)
    levels = asks if side.lower() == "buy" else bids
    rem_usdt = max(1000.0, order_usd)
    spent = 0.0
    got_qty = 0.0
    for p, q in levels:
        take = min(q, rem_usdt - got_qty)
        if take <= 0:
            break
        got_qty += take
        spent += take * p
        if got_qty >= rem_usdt * (1 - 1e-9):
            break

    vwap = (spent / got_qty) if got_qty > 0 else levels[0][0]
    walk_slip_bp = abs(vwap - levels[0][0]) / levels[0][0] * 1e4 + half_spread_bp

    # Live USDT basis relative to USD/HKD par (explains why BTCUSDT > BTCUSD when USDT < $1.00)
    usdt_basis_bp = abs(mid - par) / par * 1e4 if par > 0 else 0.0

    # Total verifiable Fiat <-> USDT friction:
    # A fiat investor either:
    #   (a) walks the secondary USDT/USD order book (paying `walk_slip_bp` + stablecoin pair fee ~10bp), or
    #   (b) absorbs the live `BTCUSDT vs BTCUSD` USDT par basis (`usdt_basis_bp`) + institutional swap spread.
    # Taking `walk_slip_bp + max(usdt_basis_bp, 6.0)` reflects the live measured book walk + live USDT basis.
    total_fiat_usdt_bp = walk_slip_bp + max(usdt_basis_bp, 6.0)

    return {
        "usdt_book_slippage_bp": walk_slip_bp,
        "usdt_basis_bp": usdt_basis_bp,
        "total_fiat_usdt_bp": total_fiat_usdt_bp,
    }

