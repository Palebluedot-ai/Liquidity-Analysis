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
