# -*- coding: utf-8 -*-
"""
Long-Horizon (365-Day / Multi-Year) Historical Large-Order Event Study & 4-Scenario Tracker.

Architectural Design (High Cohesion, Low Coupling / 高内聚、低耦合):
  1. Layer 1 — Exchange Historical API Adapters (`ExchangeHistoricalAdapters`):
     Encapsulates each exchange's specific pagination protocol (`startTime`/`endTime`/`after`/`since`)
     and Two-Stage Hierarchical Funnel (Stage 1 Macro/Regime Scan -> Stage 2 High-Frequency 5m/15m
     & Tick-by-Tick Trade Drill-Down). Outputs a unified bar dictionary schema.
       - Binance  : `GET /api/v3/klines` (`startTime` pagination, 2017~present) + `GET /api/v3/aggTrades`
       - OKX      : `GET /api/v5/market/history-candles` (`after` backward pagination, multi-year)
       - Coinbase : `GET /products/{id}/candles` (`start`/`end` ISO pagination, 2015~present)
       - Kraken   : `GET /0/public/OHLC` (`1d`/`4h`/`1h`/`15m`) + `GET /0/public/Trades?since=<ns>` (2013~present)
       - HashKey  : `GET /quote/v1/klines` (`endTime` backward pagination, 365d)

  2. Layer 2 — 4-Scenario Regime Classifier & Impact Scaling Engine (`ScenarioEventAnalyzer`):
     Stateless quantitative domain logic completely decoupled from network I/O.
     Classifies institutional Taker sweeps into 4 distinct market regimes:
       - `normal_calm`      : ① 正常平稳震荡期 (盘前波动温和、无单边趋势，突发大单扫盘)
       - `momentum_chase`   : ② 上涨追高 / 顺势突破抢筹期 (顺着前序上涨/突破方向同向追单，MM 库存偏斜撤单)
       - `extreme_stampede` : ③ 极端踩踏 / 恐慌清算真空期 (高波动或连环清算触发 MM 熔断撤单，订单簿流动性真空)
       - `contrarian_dip`   : ④ 逆势抄底 / 逢高派发期 (逆着前序趋势反向大额吃单，触发高频毒性防卫)

  3. Layer 3 — Dataset Repository & Formatter (`HistoricalScenarioRepository`):
     Manages reading/writing the local 365-day benchmark store (`data/historical_scenarios_1y.json`)
     with an in-memory cache so normal CLI/Lark evaluations incur zero HTTP latency, while supporting
     on-demand live API refresh (`refresh_historical_dataset`).
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .models import (
    HistoricalCaseRecord,
    HistoricalScenarioMatrix,
    ScenarioExecutionEstimate,
)

DAY_MS = 86400 * 1000

SCENARIO_DEFINITIONS: List[Tuple[str, str]] = [
    ("normal_calm", "① 正常平稳震荡期 (Normal/Calm)"),
    ("momentum_chase", "② 上涨追高/顺势突破期 (Momentum Chase)"),
    ("extreme_stampede", "③ 极端踩踏/恐慌清算期 (Stampede/Vacuum)"),
    ("contrarian_dip", "④ 逆势抄底/逢高派发期 (Contrarian)"),
]

DEFAULT_DATASET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "historical_scenarios_1y.json",
)

_MEMORY_DATASET_CACHE: Optional[Dict[str, Any]] = None


def _ts_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pct(vals: Sequence[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(len(s) * q)))
    return s[idx]


# ============================================================================
# LAYER 1: Exchange Long-Horizon Historical API Adapters (Low Coupling)
# ============================================================================

def fetch_binance_agg_trades(
    symbol: str = "BTCUSDT",
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: int = 1000,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch millisecond-level aggregated trades from Binance (`GET /api/v3/aggTrades`).
    Supports arbitrary historical `startTime` and `endTime` back to 2017.
    """
    sess = session or requests.Session()
    params: Dict[str, Any] = {"symbol": symbol.upper(), "limit": min(1000, limit)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    r = sess.get(
        "https://data-api.binance.vision/api/v3/aggTrades",
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    out = []
    for t in data:
        price = float(t["p"])
        qty = float(t["q"])
        is_buyer_maker = bool(t["m"])
        out.append({
            "agg_id": int(t["a"]),
            "price": price,
            "qty": qty,
            "quote_usd": price * qty,
            "ts_ms": int(t["T"]),
            "taker_side": "SELL" if is_buyer_maker else "BUY",
        })
    return out


def fetch_kraken_tick_trades_since(
    pair: str = "XBTUSD",
    since_ns: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Fetch historical tick-by-tick trades from Kraken (`GET /0/public/Trades?since=<ns>`).
    Supports jumping to ANY historical nanosecond timestamp back to 2013.
    Returns `(trades_list, next_last_ns_cursor)`.
    """
    sess = session or requests.Session()
    params: Dict[str, Any] = {"pair": pair}
    if since_ns is not None:
        params["since"] = str(int(since_ns))
    r = sess.get("https://api.kraken.com/0/public/Trades", params=params, timeout=10)
    r.raise_for_status()
    res = r.json().get("result", {})
    keys = [k for k in res.keys() if k != "last"]
    if not keys:
        return [], None
    raw_trades = res[keys[0]]
    next_cursor = res.get("last")
    parsed = []
    for t in raw_trades:
        p = float(t[0])
        v = float(t[1])
        ts_sec = float(t[2])
        side = "BUY" if t[3] == "b" else "SELL"
        ord_type = "MARKET" if t[4] == "m" else "LIMIT"
        parsed.append({
            "price": p,
            "qty": v,
            "quote_usd": p * v,
            "ts_ms": int(ts_sec * 1000),
            "taker_side": side,
            "order_type": ord_type,
        })
    return parsed, next_cursor


def fetch_binance_historical_bars(
    symbol: str = "BTCUSDT",
    horizon_days: int = 365,
    high_freq_interval: str = "5m",
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Two-Stage Funnel for Binance (`GET /api/v3/klines` with `startTime` pagination):
      - Stage 1: Scans `horizon_days` of `1h` bars across the full horizon.
      - Stage 2: Drills down with `5m` bars across regular 15-day epochs + top high-activity windows.
    """
    sess = session or requests.Session()
    now_ms = int(time.time() * 1000)
    start_horizon_ms = now_ms - horizon_days * DAY_MS

    bars_1h = []
    cur = start_horizon_ms
    while cur < now_ms:
        r = sess.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "startTime": cur, "limit": 1000},
            timeout=10,
        ).json()
        if not r or not isinstance(r, list):
            break
        bars_1h.extend(r)
        cur = int(r[-1][0]) + 3600_000
        if len(r) < 1000:
            break

    target_starts = set()
    step_days = max(7, horizon_days // 24)
    for d in range(0, horizon_days, step_days):
        target_starts.add(start_horizon_ms + d * DAY_MS)

    scored_1h = []
    for b in bars_1h:
        ts = int(b[0])
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        q_vol = float(b[7])
        rng_bp = (h - l) / o * 10000.0 if o > 0 else 0.0
        scored_1h.append((q_vol * (1.0 + rng_bp / 50.0), ts))
    scored_1h.sort(reverse=True)
    for _, ts in scored_1h[:40]:
        win_start = ts - 12 * 3600_000
        if all(abs(win_start - existing) > 2 * DAY_MS for existing in target_starts):
            target_starts.add(win_start)
        if len(target_starts) >= 36:
            break

    bars_hf_map: Dict[int, list] = {}

    def _fetch_chunk(st: int) -> list:
        try:
            res = sess.get(
                "https://data-api.binance.vision/api/v3/klines",
                params={"symbol": symbol, "interval": high_freq_interval, "startTime": st, "limit": 1000},
                timeout=10,
            ).json()
            return res if isinstance(res, list) else []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=6) as ex:
        for chunk in ex.map(_fetch_chunk, sorted(target_starts)):
            for b in chunk:
                bars_hf_map[int(b[0])] = b

    raw_hf = [bars_hf_map[k] for k in sorted(bars_hf_map.keys())]
    parsed = []
    for b in raw_hf:
        ts = int(b[0])
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        quote_vol = float(b[7])
        trades = int(b[8])
        taker_buy = float(b[10])
        taker_sell = max(0.0, quote_vol - taker_buy)
        net_flow = taker_buy - taker_sell
        abs_net = abs(net_flow)
        if o <= 0:
            continue
        is_buy = net_flow >= 0
        parsed.append({
            "ts": ts,
            "dt": _ts_str(ts),
            "open": o, "high": h, "low": l, "close": c,
            "quote_vol": quote_vol,
            "abs_net": abs_net,
            "net_flow": net_flow,
            "dir": "BUY" if is_buy else "SELL",
            "trades": trades,
            "peak_bp": ((h - o) if is_buy else (o - l)) / o * 10000.0,
            "perm_bp": ((c - o) if is_buy else (o - c)) / o * 10000.0,
            "range_bp": (h - l) / o * 10000.0,
            "ret_bp": (c - o) / o * 10000.0,
        })
    return parsed, len(bars_1h)


def fetch_okx_historical_bars(
    inst_id: str = "BTC-USDT",
    horizon_days: int = 365,
    high_freq_interval: str = "15m",
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Two-Stage Funnel for OKX (`GET /api/v5/market/history-candles` with `after` pagination):
      - Stage 1: Scans `horizon_days` of `4H` bars backwards via `after`.
      - Stage 2: Drills down with `15m` bars across regular epochs + top active windows.
    """
    sess = session or requests.Session()
    now_ms = int(time.time() * 1000)
    start_horizon_ms = now_ms - horizon_days * DAY_MS

    bars_4h = []
    after_ts = now_ms
    max_pages = max(5, (horizon_days * 6) // 100 + 2)
    for _ in range(max_pages):
        r = sess.get(
            "https://www.okx.com/api/v5/market/history-candles",
            params={"instId": inst_id, "bar": "4H", "after": str(after_ts), "limit": "100"},
            timeout=10,
        ).json()
        data = r.get("data", [])
        if not data:
            break
        bars_4h.extend(data)
        after_ts = int(data[-1][0])
        if after_ts < start_horizon_ms:
            break
        time.sleep(0.06)
    bars_4h.reverse()

    target_afters = set()
    step_days = max(7, horizon_days // 24)
    for d in range(step_days, horizon_days + 1, step_days):
        target_afters.add(start_horizon_ms + d * DAY_MS)
    target_afters.add(now_ms)

    scored_4h = []
    for b in bars_4h:
        ts = int(b[0])
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        q_vol = float(b[7]) if len(b) > 7 else float(b[6])
        rng_bp = (h - l) / o * 10000.0 if o > 0 else 0.0
        scored_4h.append((q_vol * (1.0 + rng_bp / 50.0), ts + 12 * 3600_000))
    scored_4h.sort(reverse=True)
    for _, ts in scored_4h[:25]:
        if all(abs(ts - ex_ts) > 2 * DAY_MS for ex_ts in target_afters):
            target_afters.add(min(now_ms, ts))
        if len(target_afters) >= 36:
            break

    bars_hf_map: Dict[int, list] = {}

    def _fetch_okx_hf(after_val: int) -> list:
        out = []
        cur_a = after_val
        for _ in range(2):
            try:
                res = sess.get(
                    "https://www.okx.com/api/v5/market/history-candles",
                    params={"instId": inst_id, "bar": high_freq_interval, "after": str(cur_a), "limit": "100"},
                    timeout=10,
                ).json()
                d = res.get("data", [])
                if not d:
                    break
                out.extend(d)
                cur_a = int(d[-1][0])
                time.sleep(0.12)
            except Exception:
                break
        return out

    with ThreadPoolExecutor(max_workers=2) as ex:
        for chunk in ex.map(_fetch_okx_hf, sorted(target_afters)):
            for b in chunk:
                bars_hf_map[int(b[0])] = b

    raw_hf = [bars_hf_map[k] for k in sorted(bars_hf_map.keys())]
    parsed = []
    for b in raw_hf:
        ts = int(b[0])
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        quote_vol = float(b[7]) if len(b) > 7 else float(b[6])
        if o <= 0 or (h - l) <= 0:
            continue
        clv = abs(c - o) / (h - l)
        est_imb = max(0.18, min(0.85, 0.20 + 0.65 * clv))
        abs_net = quote_vol * est_imb
        is_buy = c >= o
        net_flow = abs_net if is_buy else -abs_net
        parsed.append({
            "ts": ts,
            "dt": _ts_str(ts),
            "open": o, "high": h, "low": l, "close": c,
            "quote_vol": quote_vol,
            "abs_net": abs_net,
            "net_flow": net_flow,
            "dir": "BUY" if is_buy else "SELL",
            "trades": 0,
            "peak_bp": ((h - o) if is_buy else (o - l)) / o * 10000.0,
            "perm_bp": ((c - o) if is_buy else (o - c)) / o * 10000.0,
            "range_bp": (h - l) / o * 10000.0,
            "ret_bp": (c - o) / o * 10000.0,
        })
    return parsed, len(bars_4h)


def fetch_coinbase_historical_bars(
    product_id: str = "BTC-USD",
    horizon_days: int = 365,
    granularity: int = 900,
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Two-Stage Funnel for Coinbase (`GET /products/{id}/candles` with ISO `start`/`end`):
      - Stage 1: Scans `horizon_days` of `6h` (21600s) candles.
      - Stage 2: Drills down with `15m` (900s) candles across regular epochs + top active windows.
    """
    sess = session or requests.Session()
    now_ms = int(time.time() * 1000)
    start_horizon_ms = now_ms - horizon_days * DAY_MS

    bars_6h = []
    cur = start_horizon_ms
    step_ms = 300 * 21600 * 1000
    while cur < now_ms:
        end_c = min(now_ms, cur + step_ms)
        st_iso = datetime.fromtimestamp(cur / 1000.0, tz=timezone.utc).isoformat()
        en_iso = datetime.fromtimestamp(end_c / 1000.0, tz=timezone.utc).isoformat()
        r = sess.get(
            f"https://api.exchange.coinbase.com/products/{product_id}/candles",
            params={"granularity": 21600, "start": st_iso, "end": en_iso},
            timeout=10,
        ).json()
        if isinstance(r, list) and r:
            bars_6h.extend(r)
        cur = end_c
    bars_6h.sort(key=lambda x: x[0])

    target_starts = set()
    step_days = max(7, horizon_days // 24)
    for d in range(0, horizon_days, step_days):
        target_starts.add(start_horizon_ms + d * DAY_MS)

    scored_6h = []
    for b in bars_6h:
        ts = int(b[0]) * 1000
        l, h, o, c, vol = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
        q_vol = vol * ((o + c) / 2.0)
        rng_bp = (h - l) / o * 10000.0 if o > 0 else 0.0
        scored_6h.append((q_vol * (1.0 + rng_bp / 50.0), ts - 12 * 3600_000))
    scored_6h.sort(reverse=True)
    for _, ts in scored_6h[:30]:
        if all(abs(ts - ex_ts) > 2 * DAY_MS for ex_ts in target_starts):
            target_starts.add(max(start_horizon_ms, ts))
        if len(target_starts) >= 38:
            break

    bars_hf_map: Dict[int, list] = {}

    def _fetch_cb_hf(st_ms: int) -> list:
        en_ms = min(now_ms, st_ms + 300 * granularity * 1000)
        st_iso = datetime.fromtimestamp(st_ms / 1000.0, tz=timezone.utc).isoformat()
        en_iso = datetime.fromtimestamp(en_ms / 1000.0, tz=timezone.utc).isoformat()
        try:
            res = sess.get(
                f"https://api.exchange.coinbase.com/products/{product_id}/candles",
                params={"granularity": granularity, "start": st_iso, "end": en_iso},
                timeout=10,
            ).json()
            return res if isinstance(res, list) else []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=6) as ex:
        for chunk in ex.map(_fetch_cb_hf, sorted(target_starts)):
            for b in chunk:
                bars_hf_map[int(b[0])] = b

    raw_hf = [bars_hf_map[k] for k in sorted(bars_hf_map.keys())]
    parsed = []
    for b in raw_hf:
        ts = int(b[0]) * 1000
        l, h, o, c, vol = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
        if o <= 0 or (h - l) <= 0:
            continue
        quote_vol = vol * ((o + c) / 2.0)
        clv = abs(c - o) / (h - l)
        est_imb = max(0.18, min(0.85, 0.20 + 0.65 * clv))
        abs_net = quote_vol * est_imb
        is_buy = c >= o
        net_flow = abs_net if is_buy else -abs_net
        parsed.append({
            "ts": ts,
            "dt": _ts_str(ts),
            "open": o, "high": h, "low": l, "close": c,
            "quote_vol": quote_vol,
            "abs_net": abs_net,
            "net_flow": net_flow,
            "dir": "BUY" if is_buy else "SELL",
            "trades": 0,
            "peak_bp": ((h - o) if is_buy else (o - l)) / o * 10000.0,
            "perm_bp": ((c - o) if is_buy else (o - c)) / o * 10000.0,
            "range_bp": (h - l) / o * 10000.0,
            "ret_bp": (c - o) / o * 10000.0,
        })
    return parsed, len(bars_6h)


def fetch_kraken_historical_bars_and_ticks(
    pair: str = "XBTUSD",
    horizon_days: int = 365,
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Two-Stage Funnel for Kraken combining `/0/public/OHLC` (`1d`, `4h`, `1h`, `15m`)
    with nanosecond tick-by-tick trade reconstruction via `/0/public/Trades?since=<ns>`
    across the top historical activity days of the year.
    """
    sess = session or requests.Session()
    now_ms = int(time.time() * 1000)
    start_horizon_ms = now_ms - horizon_days * DAY_MS

    r_1d = sess.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 1440}, timeout=10).json()
    res_1d = r_1d.get("result", {})
    k_1d = [k for k in res_1d.keys() if k != "last"][0]
    bars_1d = [b for b in res_1d[k_1d] if int(b[0]) * 1000 >= start_horizon_ms]

    r_4h = sess.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 240}, timeout=10).json()
    res_4h = r_4h.get("result", {})
    bars_4h = res_4h[[k for k in res_4h.keys() if k != "last"][0]]

    r_1h = sess.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 60}, timeout=10).json()
    res_1h = r_1h.get("result", {})
    bars_1h = res_1h[[k for k in res_1h.keys() if k != "last"][0]]

    r_15m = sess.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 15}, timeout=10).json()
    res_15m = r_15m.get("result", {})
    bars_15m = res_15m[[k for k in res_15m.keys() if k != "last"][0]]

    scored_days = []
    for idx_d, b in enumerate(bars_1d):
        ts_sec = int(b[0])
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        vol = float(b[6])
        q_vol = vol * ((o + c) / 2.0)
        rng_bp = (h - l) / o * 10000.0 if o > 0 else 0.0
        prev_o = float(bars_1d[idx_d - 1][1]) if idx_d > 0 else o
        prior_day_ret = (o - prev_o) / prev_o * 10000.0 if prev_o > 0 else 0.0
        scored_days.append((q_vol * (1.0 + rng_bp / 100.0), ts_sec, prior_day_ret, rng_bp))
    scored_days.sort(reverse=True)

    sample_days = scored_days[:14] + scored_days[len(scored_days) // 2 : len(scored_days) // 2 + 6]

    def _fetch_trade_burst(item: Tuple[float, int, float, float]) -> list:
        _, day_sec, prior_day_ret, day_rng_bp = item
        since_ns = int((day_sec + 13 * 3600 + 30 * 60) * 1_000_000_000)
        try:
            trades, _ = fetch_kraken_tick_trades_since(pair=pair, since_ns=since_ns, session=sess)
            buckets: Dict[int, Dict[str, Any]] = {}
            for t in trades:
                p = t["price"]
                q_usd = t["quote_usd"]
                b_sec = (int(t["ts_ms"] // 1000) // 900) * 900
                if b_sec not in buckets:
                    buckets[b_sec] = {
                        "ts": b_sec * 1000,
                        "open": p, "high": p, "low": p, "close": p,
                        "buy_usd": 0.0, "sell_usd": 0.0, "trades": 0,
                        "fallback_prior_ret_bp": prior_day_ret,
                        "fallback_prior_rng_bp": day_rng_bp / 4.0,
                    }
                bk = buckets[b_sec]
                bk["high"] = max(bk["high"], p)
                bk["low"] = min(bk["low"], p)
                bk["close"] = p
                bk["trades"] += 1
                if t["taker_side"] == "BUY":
                    bk["buy_usd"] += q_usd
                else:
                    bk["sell_usd"] += q_usd
            return list(buckets.values())
        except Exception:
            return []

    tick_reconstructed_15m = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for bks in ex.map(_fetch_trade_burst, sample_days):
            tick_reconstructed_15m.extend(bks)

    parsed_map: Dict[int, Dict[str, Any]] = {}
    for b in bars_4h + bars_1h + bars_15m:
        ts = int(b[0]) * 1000
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        vol = float(b[6])
        trades = int(b[7])
        if o <= 0 or (h - l) <= 0:
            continue
        quote_vol = vol * ((o + c) / 2.0)
        clv = abs(c - o) / (h - l)
        est_imb = max(0.18, min(0.85, 0.20 + 0.65 * clv))
        abs_net = quote_vol * est_imb
        is_buy = c >= o
        net_flow = abs_net if is_buy else -abs_net
        parsed_map[ts] = {
            "ts": ts, "dt": _ts_str(ts),
            "open": o, "high": h, "low": l, "close": c,
            "quote_vol": quote_vol, "abs_net": abs_net, "net_flow": net_flow,
            "dir": "BUY" if is_buy else "SELL", "trades": trades,
            "peak_bp": ((h - o) if is_buy else (o - l)) / o * 10000.0,
            "perm_bp": ((c - o) if is_buy else (o - c)) / o * 10000.0,
            "range_bp": (h - l) / o * 10000.0,
            "ret_bp": (c - o) / o * 10000.0,
        }

    for bk in tick_reconstructed_15m:
        ts = bk["ts"]
        o, h, l, c = bk["open"], bk["high"], bk["low"], bk["close"]
        if o <= 0:
            continue
        quote_vol = bk["buy_usd"] + bk["sell_usd"]
        net_flow = bk["buy_usd"] - bk["sell_usd"]
        abs_net = abs(net_flow)
        is_buy = net_flow >= 0
        parsed_map[ts] = {
            "ts": ts, "dt": _ts_str(ts),
            "open": o, "high": h, "low": l, "close": c,
            "quote_vol": quote_vol, "abs_net": abs_net, "net_flow": net_flow,
            "dir": "BUY" if is_buy else "SELL", "trades": bk["trades"],
            "peak_bp": ((h - o) if is_buy else (o - l)) / o * 10000.0,
            "perm_bp": ((c - o) if is_buy else (o - c)) / o * 10000.0,
            "range_bp": (h - l) / o * 10000.0,
            "ret_bp": (c - o) / o * 10000.0,
            "fallback_prior_ret_bp": bk.get("fallback_prior_ret_bp", 0.0),
            "fallback_prior_rng_bp": bk.get("fallback_prior_rng_bp", 20.0),
        }

    parsed = [parsed_map[k] for k in sorted(parsed_map.keys())]
    return parsed, len(bars_1d)


def fetch_hashkey_historical_bars(
    symbol: str = "BTCUSD",
    horizon_days: int = 365,
    high_freq_interval: str = "15m",
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Two-Stage Funnel for HashKey (`GET /quote/v1/klines` with `endTime` pagination):
      - Stage 1: Scans `horizon_days` of `1h` bars via `endTime`.
      - Stage 2: Drills down with `15m` bars across regular 20-day epochs spanning the full year.
    """
    sess = session or requests.Session()
    now_ms = int(time.time() * 1000)
    start_horizon_ms = now_ms - horizon_days * DAY_MS

    bars_1h_map: Dict[int, list] = {}
    end_cur = now_ms
    for _ in range(max(2, horizon_days * 24 // 1000 + 1)):
        r = sess.get(
            "https://api-pro.hashkey.com/quote/v1/klines",
            params={"symbol": symbol, "interval": "1h", "endTime": end_cur, "limit": 1000},
            timeout=10,
        ).json()
        if not isinstance(r, list) or not r:
            break
        for b in r:
            bars_1h_map[int(b[0])] = b
        end_cur = int(r[0][0]) - 1
        if end_cur < start_horizon_ms:
            break

    target_ends = [now_ms - d * DAY_MS for d in range(0, horizon_days, 20)]
    bars_hf_map: Dict[int, list] = {}

    def _fetch_hk_hf(en_ms: int) -> list:
        try:
            res = sess.get(
                "https://api-pro.hashkey.com/quote/v1/klines",
                params={"symbol": symbol, "interval": high_freq_interval, "endTime": en_ms, "limit": 1000},
                timeout=10,
            ).json()
            return res if isinstance(res, list) else []
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=5) as ex:
        for chunk in ex.map(_fetch_hk_hf, target_ends):
            for b in chunk:
                bars_hf_map[int(b[0])] = b

    raw_hf = [bars_hf_map[k] for k in sorted(bars_hf_map.keys())]
    parsed = []
    for b in raw_hf:
        ts = int(b[0])
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        vol = float(b[5])
        quote_vol = float(b[7]) if len(b) > 7 and float(b[7]) > 0 else vol * ((o + c) / 2.0)
        if o <= 0 or (h - l) <= 0 or quote_vol <= 0:
            continue
        clv = abs(c - o) / (h - l)
        est_imb = max(0.20, min(0.85, 0.22 + 0.63 * clv))
        abs_net = quote_vol * est_imb
        is_buy = c >= o
        net_flow = abs_net if is_buy else -abs_net
        parsed.append({
            "ts": ts, "dt": _ts_str(ts),
            "open": o, "high": h, "low": l, "close": c,
            "quote_vol": quote_vol, "abs_net": abs_net, "net_flow": net_flow,
            "dir": "BUY" if is_buy else "SELL", "trades": int(b[8]) if len(b) > 8 else 0,
            "peak_bp": ((h - o) if is_buy else (o - l)) / o * 10000.0,
            "perm_bp": ((c - o) if is_buy else (o - c)) / o * 10000.0,
            "range_bp": (h - l) / o * 10000.0,
            "ret_bp": (c - o) / o * 10000.0,
        })
    return parsed, len(bars_1h_map)


# ============================================================================
# LAYER 2: 4-Scenario Classifier & Order Impact Scaling Engine (High Cohesion)
# ============================================================================

def classify_bars_into_scenarios(
    bars: Sequence[Dict[str, Any]],
    min_net_usd: float,
    lookback: int = 8,
) -> Dict[str, Dict[str, Any]]:
    """
    Pure domain function: classifies institutional Taker bursts (`abs_net >= min_net_usd`)
    into 4 market regimes (`normal_calm`, `momentum_chase`, `extreme_stampede`, `contrarian_dip`)
    and extracts quantile impact benchmarks + representative real historical cases.
    """
    all_ranges = [b["range_bp"] for b in bars if b["range_bp"] > 0]
    p85_range = _pct(all_ranges, 0.85)

    scenarios: Dict[str, List[Dict[str, Any]]] = {
        "normal_calm": [],
        "momentum_chase": [],
        "extreme_stampede": [],
        "contrarian_dip": [],
    }

    for i in range(lookback, len(bars)):
        b = bars[i]
        if b["abs_net"] < min_net_usd:
            continue
        prev_window = bars[i - lookback : i]
        if (b["ts"] - prev_window[0]["ts"]) > lookback * 3600_000 * 4:
            if "fallback_prior_ret_bp" in b:
                prior_ret_bp = float(b["fallback_prior_ret_bp"])
                prior_avg_range = float(b.get("fallback_prior_rng_bp", 20.0))
            else:
                continue
        else:
            p_start = prev_window[0]["open"]
            p_end = prev_window[-1]["close"]
            prior_ret_bp = (p_end - p_start) / p_start * 10000.0 if p_start > 0 else 0.0
            prior_avg_range = statistics.mean(x["range_bp"] for x in prev_window)

        event = {
            **b,
            "prior_ret_bp": round(prior_ret_bp, 1),
            "prior_avg_range_bp": round(prior_avg_range, 1),
        }

        if prior_avg_range >= p85_range or abs(prior_ret_bp) >= 75.0 or b["peak_bp"] >= 80.0:
            scenarios["extreme_stampede"].append(event)
        elif (b["dir"] == "BUY" and prior_ret_bp >= 25.0) or (b["dir"] == "SELL" and prior_ret_bp <= -25.0):
            scenarios["momentum_chase"].append(event)
        elif (b["dir"] == "BUY" and prior_ret_bp <= -25.0) or (b["dir"] == "SELL" and prior_ret_bp >= 25.0):
            scenarios["contrarian_dip"].append(event)
        else:
            scenarios["normal_calm"].append(event)

    summary: Dict[str, Dict[str, Any]] = {}
    for s_key, evs in scenarios.items():
        if not evs:
            summary[s_key] = {"count": 0}
            continue
        peaks = [e["peak_bp"] for e in evs]
        perms = [e["perm_bp"] for e in evs]
        nets = [e["abs_net"] for e in evs]
        med_peak = statistics.median(peaks)
        med_perm = statistics.median(perms)
        retention = min(1.0, max(0.0, med_perm / med_peak)) if med_peak > 0 else 0.75

        sorted_evs = sorted(evs, key=lambda x: x["peak_bp"], reverse=True)
        top_cases: List[Dict[str, Any]] = []
        used_days = set()

        # Pass 1: Include top BUY cases (e.g. 上涨追高抢筹 / 恐慌轧空 / 正常大买单)
        for e in sorted_evs:
            day_str = e["dt"][:10]
            if e["dir"] == "BUY" and day_str not in used_days and e["peak_bp"] >= med_peak:
                used_days.add(day_str)
                top_cases.append({
                    "dt": e["dt"],
                    "dir": e["dir"],
                    "open": round(e["open"], 2),
                    "high": round(e["high"], 2),
                    "low": round(e["low"], 2),
                    "close": round(e["close"], 2),
                    "abs_net_usd": round(e["abs_net"], 0),
                    "quote_vol_usd": round(e["quote_vol"], 0),
                    "peak_bp": round(e["peak_bp"], 1),
                    "perm_bp": round(e["perm_bp"], 1),
                    "prior_ret_bp": e["prior_ret_bp"],
                })
            if len(top_cases) >= 3:
                break

        # Pass 2: Include top SELL cases (e.g. 顺势杀跌 / 连环清算砸盘 / 正常大卖单)
        for e in sorted_evs:
            day_str = e["dt"][:10]
            if e["dir"] == "SELL" and day_str not in used_days and e["peak_bp"] >= med_peak:
                used_days.add(day_str)
                top_cases.append({
                    "dt": e["dt"],
                    "dir": e["dir"],
                    "open": round(e["open"], 2),
                    "high": round(e["high"], 2),
                    "low": round(e["low"], 2),
                    "close": round(e["close"], 2),
                    "abs_net_usd": round(e["abs_net"], 0),
                    "quote_vol_usd": round(e["quote_vol"], 0),
                    "peak_bp": round(e["peak_bp"], 1),
                    "perm_bp": round(e["perm_bp"], 1),
                    "prior_ret_bp": e["prior_ret_bp"],
                })
            if len(top_cases) >= 6:
                break

        summary[s_key] = {
            "count": len(evs),
            "avg_net_flow_usd": round(statistics.mean(nets), 0),
            "median_peak_bp": round(med_peak, 1),
            "p75_peak_bp": round(_pct(peaks, 0.75), 1),
            "p90_peak_bp": round(_pct(peaks, 0.90), 1),
            "max_peak_bp": round(max(peaks), 1),
            "median_perm_bp": round(med_perm, 1),
            "perm_retention_ratio": round(retention, 2),
            "top_cases": top_cases,
        }
    return summary


# ============================================================================
# LAYER 3: Dataset Repository & Order Scenario Evaluator
# ============================================================================

def load_historical_dataset(dataset_path: str = DEFAULT_DATASET_PATH) -> Optional[Dict[str, Any]]:
    """Load the 365-day multi-exchange scenario dataset with in-memory caching."""
    global _MEMORY_DATASET_CACHE
    if _MEMORY_DATASET_CACHE is not None and dataset_path == DEFAULT_DATASET_PATH:
        return _MEMORY_DATASET_CACHE
    if not os.path.exists(dataset_path):
        return None
    try:
        with open(dataset_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if dataset_path == DEFAULT_DATASET_PATH:
            _MEMORY_DATASET_CACHE = data
        return data
    except Exception:
        return None


def refresh_historical_dataset(
    horizon_days: int = 365,
    output_path: str = DEFAULT_DATASET_PATH,
) -> Dict[str, Any]:
    """
    Re-fetch `horizon_days` (default 365 days = 1 full year) of historical bars & ticks
    across Binance, OKX, Coinbase, Kraken, and HashKey via public APIs and persist to JSON.
    """
    global _MEMORY_DATASET_CACHE
    session = requests.Session()
    session.headers.update({"User-Agent": "LiquidityAnalysis-HistoricalTracker/1.0"})
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - horizon_days * DAY_MS

    bn_bars, bn_s1 = fetch_binance_historical_bars(horizon_days=horizon_days, session=session)
    okx_bars, okx_s1 = fetch_okx_historical_bars(horizon_days=horizon_days, session=session)
    cb_bars, cb_s1 = fetch_coinbase_historical_bars(horizon_days=horizon_days, session=session)
    kr_bars, kr_s1 = fetch_kraken_historical_bars_and_ticks(horizon_days=horizon_days, session=session)
    hk_bars, hk_s1 = fetch_hashkey_historical_bars(horizon_days=horizon_days, session=session)

    dataset: Dict[str, Any] = {
        "generated_at_utc": _ts_str(now_ms),
        "horizon_days": horizon_days,
        "window_start_utc": _ts_str(start_ms),
        "window_end_utc": _ts_str(now_ms),
        "exchanges": {},
    }

    configs = [
        ("binance", "Binance (BTCUSDT)", bn_bars, bn_s1, 2_000_000, "5m", "REST /api/v3/klines (startTime..endTime, 365d)"),
        ("okx", "OKX (BTC-USDT)", okx_bars, okx_s1, 1_500_000, "15m", "REST /api/v5/market/history-candles (after pagination, 365d)"),
        ("coinbase", "Coinbase (BTC-USD)", cb_bars, cb_s1, 1_500_000, "15m", "REST /products/BTC-USD/candles (start..end ISO, 365d)"),
        ("kraken", "Kraken (XBTUSD)", kr_bars, kr_s1, 1_000_000, "15m + Tick", "REST /0/public/OHLC + /0/public/Trades?since=<ns> (365d)"),
        ("hashkey_global", "HashKey (BTCUSD)", hk_bars, hk_s1, 200_000, "15m", "REST /quote/v1/klines (endTime pagination, 365d)"),
    ]

    for key, label, bars, s1_cnt, min_usd, bar_interval, api_method in configs:
        scen = classify_bars_into_scenarios(bars, min_net_usd=min_usd)
        dataset["exchanges"][key] = {
            "label": label,
            "api_method": api_method,
            "stage1_macro_bars": s1_cnt,
            "stage2_high_freq_bars": len(bars),
            "high_freq_interval": bar_interval,
            "min_large_order_net_usd": min_usd,
            "span_start_utc": bars[0]["dt"] if bars else "",
            "span_end_utc": bars[-1]["dt"] if bars else "",
            "scenarios": scen,
        }

    dataset["exchanges"]["hashkey_hk"] = dict(dataset["exchanges"]["hashkey_global"])
    dataset["exchanges"]["hashkey_hk"]["label"] = "HashKey HK (BTCUSD)"

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    if output_path == DEFAULT_DATASET_PATH:
        _MEMORY_DATASET_CACHE = dataset
    return dataset


def evaluate_order_across_4_scenarios(
    exchange: str,
    side: str,
    order_usd: float,
    taker_fee_bp: float,
    fiat_usdt_friction_bp: float,
    otc_quote_bp: float = 30.0,
    dataset_path: str = DEFAULT_DATASET_PATH,
) -> Optional[HistoricalScenarioMatrix]:
    """
    Given an exchange and target order size `order_usd`, maps the 365-day empirical
    4-scenario benchmarks (`normal_calm`, `momentum_chase`, `extreme_stampede`, `contrarian_dip`)
    to the user's specific order size using Square-Root Impact scaling:
        Est_Impact(Q) = Hist_Peak_bp * (Q / Hist_Avg_Net_USD)^0.42
    (where 0.42 is the empirical dampened square-root exponent across multi-minute bursts),
    adds `taker_fee_bp + fiat_usdt_friction_bp`, and attaches real historical cases.
    """
    ds = load_historical_dataset(dataset_path)
    if not ds:
        return None
    ex_map = ds.get("exchanges", {})
    ex_key = exchange.lower().strip()
    if ex_key in ("hashkey", "hashkey_hkd"):
        ex_key = "hashkey_hk"
    ex_data = ex_map.get(ex_key) or ex_map.get("binance")
    if not ex_data:
        return None

    side_up = "BUY" if side.lower().strip() == "buy" else "SELL"
    eff_order_usd = max(50_000.0, float(order_usd or 1_000_000.0))
    fixed_friction_bp = taker_fee_bp + fiat_usdt_friction_bp

    scen_estimates: List[ScenarioExecutionEstimate] = []
    raw_scens = ex_data.get("scenarios", {})
    normal_base_net = max(
        100_000.0,
        float(raw_scens.get("normal_calm", {}).get("avg_net_flow_usd", 1_500_000.0)),
    )
    size_ratio = eff_order_usd / normal_base_net
    scale_factor = max(0.35, min(2.8, math.pow(size_ratio, 0.42)))
    vwap_to_peak_factor = 0.70

    for s_key, s_label in SCENARIO_DEFINITIONS:
        st = raw_scens.get(s_key, {})
        cnt = int(st.get("count", 0))
        if cnt <= 0:
            continue
        avg_net = max(100_000.0, float(st.get("avg_net_flow_usd", normal_base_net)))
        med_peak = float(st.get("median_peak_bp", 15.0))
        p75_peak = float(st.get("p75_peak_bp", 25.0))
        p90_peak = float(st.get("p90_peak_bp", 40.0))
        perm_ret = float(st.get("perm_retention_ratio", 0.65))

        est_imp_med = round(med_peak * scale_factor * vwap_to_peak_factor, 1)
        est_imp_p90 = round(p90_peak * scale_factor * vwap_to_peak_factor, 1)

        est_all_in_med = round(est_imp_med + fixed_friction_bp, 1)
        est_all_in_p90 = round(est_imp_p90 + fixed_friction_bp, 1)

        raw_cases = st.get("top_cases", [])
        # Prioritize cases matching the user's trade direction (BUY vs SELL)
        matching_cases = [c for c in raw_cases if c.get("dir") == side_up]
        other_cases = [c for c in raw_cases if c.get("dir") != side_up]
        ordered_cases = (matching_cases + other_cases)[:3]

        case_objs = [
            HistoricalCaseRecord(
                dt=str(c["dt"]),
                dir=str(c["dir"]),
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                abs_net_usd=float(c["abs_net_usd"]),
                quote_vol_usd=float(c["quote_vol_usd"]),
                peak_bp=float(c["peak_bp"]),
                perm_bp=float(c["perm_bp"]),
                prior_ret_bp=float(c["prior_ret_bp"]),
            )
            for c in ordered_cases
        ]

        scen_estimates.append(
            ScenarioExecutionEstimate(
                scenario_key=s_key,
                scenario_label=s_label,
                sample_count=cnt,
                hist_avg_net_usd=avg_net,
                hist_median_peak_bp=med_peak,
                hist_p75_peak_bp=p75_peak,
                hist_p90_peak_bp=p90_peak,
                hist_perm_retention=perm_ret,
                est_impact_median_bp=est_imp_med,
                est_impact_p90_bp=est_imp_p90,
                est_all_in_median_bp=est_all_in_med,
                est_all_in_p90_bp=est_all_in_p90,
                otc_savings_median_bp=round(est_all_in_med - otc_quote_bp, 1),
                otc_savings_p90_bp=round(est_all_in_p90 - otc_quote_bp, 1),
                representative_cases=case_objs,
            )
        )

    return HistoricalScenarioMatrix(
        exchange=ex_key,
        exchange_label=str(ex_data.get("label", exchange)),
        horizon_days=int(ds.get("horizon_days", 365)),
        span_start_utc=str(ex_data.get("span_start_utc", "")),
        span_end_utc=str(ex_data.get("span_end_utc", "")),
        stage1_macro_bars=int(ex_data.get("stage1_macro_bars", 0)),
        stage2_high_freq_bars=int(ex_data.get("stage2_high_freq_bars", 0)),
        high_freq_interval=str(ex_data.get("high_freq_interval", "15m")),
        api_method=str(ex_data.get("api_method", "")),
        scenarios=scen_estimates,
    )
