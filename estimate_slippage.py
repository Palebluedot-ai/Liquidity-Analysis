#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
estimate_slippage.py
====================
多交易所盘口流动性分析与滑点估算工具 (Liquidity & Slippage Analyzer)

核心扩展功能:
  1. 币对支持:
     - 预设下拉列表币种: BTC, ETH, SOL (Solana), BNB, XRP, DOGE, SUI, AVAX, LINK, HSK 等
     - 自定义输入: 支持任意币种简称/全称或交易对 (如 Solana, PEPE, BTCUSDT, BTC/USD, BTCHKD)
  2. 购买/卖出金额支持美金 (USD) 或港币 (HKD):
     - 自动拉取实时 USD/HKD 汇率换算，同时输出 USD 与 HKD 计价的 VWAP 与成本
     - 支持 15000000、15M、500k、1000万 等人性化金额输入
  3. 多交易所实时 L2 盘口支持:
     - Binance, OKX, Coinbase, Kraken, HashKey HK (USD & HKD 盘口), HashKey Global, Bybit
     - 支持单所测算或多所并发横向对比 (--exchange all)
  4. 盘口 Maker 单真实性验证 (--verify):
     - 盘口结构与序列号单调性校验 + 最新公开逐笔成交价交叉比对 + 多帧采样深度留存率/防闪单检测
"""

import argparse
import json
import sys
from typing import List, Optional

from liquidity_analysis.engine import (
    compare_exchanges,
    estimate_single,
    verify_orderbook,
)
from liquidity_analysis.exchanges import (
    DEFAULT_COMPARE_EXCHANGES,
    SUPPORTED_EXCHANGES,
)
from liquidity_analysis.lark_card import build_query_card, build_result_card
from liquidity_analysis.models import SlippageResult, VerificationReport
from liquidity_analysis.symbols import PRESET_ASSETS, parse_amount

# 2026-10-05 BTC/USDT 买盘截图(从上到下): (数量, 价格) — 保留原版静态复核样本
SCREENSHOT_BIDS = [
    (1.23548, 86055.48), (0.00066, 86055.47), (0.00014, 86054.55),
    (0.03618, 86054.54), (0.11866, 86054.53), (0.00007, 86054.29),
    (0.00006, 86053.75), (0.00009, 86053.00), (0.00599, 86052.98),
    (0.00012, 86052.00), (0.00999, 86051.60), (0.00007, 86051.35),
    (0.00006, 86051.06), (0.00014, 86050.90), (0.10734, 86050.89),
    (0.01199, 86050.22), (0.00006, 86050.00), (0.00006, 86049.49),
    (0.00006, 86049.46), (0.01199, 86048.84), (0.00047, 86048.77),
    (0.00020, 86048.54), (0.31393, 86048.53), (0.00007, 86048.49),
    (0.03496, 86048.48), (0.03486, 86048.47), (0.00069, 86048.43),
    (0.00160, 86048.27), (0.00012, 86047.74), (0.00550, 86047.60),
    (0.00400, 86047.26), (0.50363, 86047.25), (1.00790, 86047.24),
]


def demo_screenshot():
    qty = sum(q for q, _ in SCREENSHOT_BIDS)
    usd = sum(q * p for q, p in SCREENSHOT_BIDS)
    return {
        "levels": len(SCREENSHOT_BIDS),
        "total_qty": qty,
        "total_usd": usd,
        "avg_price": usd / qty,
        "top_bid": SCREENSHOT_BIDS[0][1],
    }


def _print_single_result(r: SlippageResult) -> None:
    ccy = r.input_currency
    if r.target_notional_input is not None:
        prefix = "HK$" if ccy == "HKD" else "$"
        target_txt = (
            f"{prefix}{r.target_notional_input:,.2f} {ccy} "
            f"(≈ ${r.target_notional_usd:,.2f} USD / HK${r.target_notional_hkd:,.2f} HKD)"
        )
    else:
        target_txt = f"{r.target_qty:,.4f} {r.base_asset}"

    print(
        f"交易所: {r.exchange_label} ({r.exchange})  交易对: {r.symbol}  "
        f"方向: {r.side.upper()}  目标: {target_txt}"
    )
    if ccy == "HKD" or r.quote_asset == "HKD":
        print(f"参考汇率: 1 USD = {r.fx_usd_hkd:.4f} HKD")
    print(
        f"盘口最优价(top): {r.top_price:,.2f} {r.quote_asset} "
        f"(买一 {r.top_bid:,.2f} / 卖一 {r.top_ask:,.2f}, 价差 {r.spread_bp:.2f} bp)"
    )
    print(
        f"同侧盘口总额: ${r.book_side_total_usd:,.0f} USD / HK${r.book_side_total_hkd:,.0f} HKD "
        f"({r.book_levels} 档, 共 {r.book_side_total_qty:,.4f} {r.base_asset})"
    )
    status_str = (
        "100% 全部成交"
        if not r.depth_exhausted
        else f"⚠️ 盘口深度耗尽 (仅成交 {r.fill_ratio * 100:.2f}%)"
    )
    print(f"吃掉档位数: {r.levels_used} ({status_str})")
    print(
        f"实际成交: {r.filled_qty:,.4f} {r.base_asset} / "
        f"${r.cost_usd:,.2f} USD (HK${r.cost_hkd:,.2f} HKD)"
    )
    print(
        f"VWAP: ${r.vwap_usd:,.2f} USD (HK${r.vwap_hkd:,.2f} HKD)  "
        f"→ 不利滑点 {r.vwap_slippage_bp:.2f} bp (相对 mid {r.vwap_vs_mid_bp:.2f} bp)"
    )
    print(
        f"最后一口: ${r.last_price_usd:,.2f} USD (HK${r.last_price_hkd:,.2f} HKD)  "
        f"→ 不利滑点 {r.last_slippage_bp:.2f} bp"
    )
    if r.depth_bands:
        bands_str = " | ".join(
            f"±{int(b.bp)}bp: ${b.cum_usd:,.0f} ({b.cum_qty:,.2f} {r.base_asset})"
            for b in r.depth_bands
        )
        print(f"盘口深度阶梯: {bands_str}")
    print(f"注意: {r.note}")


def _print_comparison_table(results: List[SlippageResult]) -> None:
    if not results:
        print("未获取到任何交易所盘口结果。", file=sys.stderr)
        return

    first = results[0]
    ccy = first.input_currency
    if first.target_notional_input is not None:
        prefix = "HK$" if ccy == "HKD" else "$"
        target_txt = (
            f"{prefix}{first.target_notional_input:,.2f} {ccy} "
            f"(≈ ${first.target_notional_usd:,.0f} USD / HK${first.target_notional_hkd:,.0f} HKD)"
        )
    else:
        target_txt = f"{first.target_qty:,.4f} {first.base_asset}"

    print("=" * 112)
    print(
        f"跨交易所流动性与滑点对比 | 标的: {first.base_asset} | 方向: {first.side.upper()} | "
        f"目标: {target_txt} | 汇率: {first.fx_usd_hkd:.4f} HKD/USD"
    )
    print("=" * 112)
    header = (
        f"{'交易所':<22} {'交易对':<10} {'最优价(USD)':>13} {'VWAP(USD)':>13} "
        f"{'VWAP(HKD)':>14} {'VWAP滑点':>10} {'尾档滑点':>10} {'同侧总深(USD)':>15} {'状态':<12}"
    )
    print(header)
    print("-" * 112)

    for r in results:
        if r.error:
            print(
                f"{r.exchange_label:<22} {r.symbol:<10} {'-':>13} {'-':>13} "
                f"{'-':>14} {'-':>10} {'-':>10} {'-':>15} ❌ {r.error[:30]}"
            )
            continue
        top_usd = r.top_price / r.fx_usd_hkd if r.quote_asset == "HKD" else r.top_price
        status = (
            f"100% ({r.levels_used}档)"
            if not r.depth_exhausted
            else f"⚠️ {r.fill_ratio * 100:.1f}% (耗尽)"
        )
        print(
            f"{r.exchange_label:<22} {r.symbol:<10} "
            f"${top_usd:>12,.2f} ${r.vwap_usd:>12,.2f} "
            f"HK${r.vwap_hkd:>11,.2f} {r.vwap_slippage_bp:>7.2f} bp "
            f"{r.last_slippage_bp:>7.2f} bp ${r.book_side_total_usd:>14,.0f} {status:<12}"
        )

    print("-" * 112)
    full_fills = [r for r in results if not r.error and not r.depth_exhausted]
    if full_fills:
        best_slip = min(full_fills, key=lambda x: x.vwap_slippage_bp)
        best_px = (
            min(full_fills, key=lambda x: x.vwap_usd)
            if first.side == "buy"
            else max(full_fills, key=lambda x: x.vwap_usd)
        )
        print(
            f"🏆 最小盘口冲击: {best_slip.exchange_label} ({best_slip.symbol}) — "
            f"VWAP 滑点 {best_slip.vwap_slippage_bp:.2f} bp "
            f"(均价 ${best_slip.vwap_usd:,.2f} USD / HK${best_slip.vwap_hkd:,.2f} HKD)"
        )
        print(
            f"💰 最优绝对均价: {best_px.exchange_label} ({best_px.symbol}) — "
            f"成交均价 ${best_px.vwap_usd:,.2f} USD / HK${best_px.vwap_hkd:,.2f} HKD "
            f"(盘口滑点 {best_px.vwap_slippage_bp:.2f} bp)"
        )
    print(f"注: {first.note}")


def _print_verification_report(rep: VerificationReport) -> None:
    print("=" * 90)
    print(
        f"盘口 Maker 挂单真实性验证报告 | {rep.exchange_label} ({rep.symbol}) | "
        f"采样 {rep.samples_count} 次 (间隔 {rep.interval_sec}s)"
    )
    print("=" * 90)
    print(f"1. 盘口结构完整性:")
    print(
        f"   - 买一 / 卖一 : {rep.best_bid:,.2f} / {rep.best_ask:,.2f} "
        f"(Spread: {rep.spread_bp:.2f} bp, 是否倒挂: {'否 ✅' if not rep.is_crossed else '是 ❌'})"
    )
    print(
        f"   - 价格严格排序: 买盘递减={'是 ✅' if rep.bids_strictly_descending else '否 ❌'}, "
        f"卖盘递增={'是 ✅' if rep.asks_strictly_ascending else '否 ❌'}"
    )
    print(
        f"   - 序列号单调性: {rep.sequence_ids} ({'单调递增/正常 ✅' if rep.sequence_monotonic else '异常 ❌'})"
    )
    print(f"2. 最新公开成交 (Public Trades) 交叉核验:")
    if rep.latest_trade_price is not None:
        print(
            f"   - 最新逐笔成交价: {rep.latest_trade_price:,.2f} "
            f"(距当前盘口中间价 {rep.trade_vs_mid_bp:.2f} bp, "
            f"吻合度: {'吻合 ✅' if rep.trade_within_inside_spread_or_10bp else '偏离 ⚠️'})"
        )
    else:
        print("   - 最新逐笔成交价: 暂未获取")
    print(f"3. 多帧盘口留存率与防闪单 (Anti-Spoofing) 检测:")
    print(
        f"   - 前 50 档价格留存率: {rep.level_retention_ratio * 100:.1f}% "
        f"(连续两次快照之间相同价格档位的保留比例)"
    )
    print(
        f"   - 10bp 内挂单深度波动率 (CV): {rep.depth_10bp_cv_pct:.2f}% | "
        f"样本(USD): {[round(x, 0) for x in rep.depth_10bp_usd_samples]}"
    )
    print(
        f"   - 25bp 内挂单深度波动率 (CV): {rep.depth_25bp_cv_pct:.2f}% | "
        f"样本(USD): {[round(x, 0) for x in rep.depth_25bp_usd_samples]}"
    )
    if rep.simulated_slippage_bp_samples:
        print(
            f"   - 多帧模拟滑点区间: min={rep.simulated_slippage_bp_min:.2f} bp, "
            f"mean={rep.simulated_slippage_bp_mean:.2f} bp, "
            f"max={rep.simulated_slippage_bp_max:.2f} bp"
        )
    print("-" * 90)
    print(f"结论: {rep.verdict}")
    for w in rep.warnings:
        print(f"  ⚠️ {w}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="多交易所现货盘口流动性与滑点估算工具 (支持 USD/HKD、预设与自定义币对、多所对比、盘口真伪验证)"
    )
    ap.add_argument(
        "--symbol",
        default="BTC",
        help="币种或交易对 (如 BTC, ETH, Solana, SOL, PEPE, BTCUSDT, BTCHKD), 默认 BTC",
    )
    ap.add_argument(
        "--side",
        choices=["sell", "buy"],
        default="sell",
        help="sell=吃买盘(砸盘), buy=吃卖盘(扫货), 默认 sell",
    )
    ap.add_argument(
        "--notional",
        type=str,
        default=None,
        help="目标名义金额 (支持数字或 15M / 500k / 1000万), 与 --qty 二选一",
    )
    ap.add_argument(
        "--qty",
        type=str,
        default=None,
        help="目标数量 (币本位, 如 10 或 100), 与 --notional 二选一",
    )
    ap.add_argument(
        "--currency",
        choices=["USD", "HKD", "usd", "hkd"],
        default="USD",
        help="金额计价币种: USD (美金) 或 HKD (港币), 默认 USD",
    )
    ap.add_argument(
        "--exchange",
        default="binance",
        help=(
            "交易所: binance, okx, coinbase, kraken, hashkey, hashkey_hkd, "
            "hashkey_global, bybit, 或 all (多所横向对比), 也支持逗号分隔如 binance,okx,hashkey。默认 binance"
        ),
    )
    ap.add_argument(
        "--fx-rate",
        type=float,
        default=None,
        help="手动指定 USD/HKD 汇率 (默认自动获取实时汇率)",
    )
    ap.add_argument("--limit", type=int, default=5000, help="深度档数上限, 默认 5000")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument(
        "--demo-screenshot",
        action="store_true",
        help="仅复算截图里 33 档买盘的总额, 不抓盘口",
    )
    ap.add_argument(
        "--list-presets",
        action="store_true",
        help="列出下拉菜单预设币对与支持的交易所列表",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="对目标交易所盘口执行 Maker 单真实性与多帧留存率验证",
    )
    ap.add_argument(
        "--verify-samples",
        type=int,
        default=3,
        help="--verify 模式下的连续采样次数, 默认 3",
    )
    ap.add_argument(
        "--lark-card",
        choices=["query", "result"],
        default=None,
        help="输出飞书 (Lark) 交互卡片 JSON (query=查询表单卡片, result=测算结果卡片)",
    )

    args = ap.parse_args(argv)

    if args.list_presets:
        info = {
            "preset_assets": PRESET_ASSETS,
            "supported_exchanges": SUPPORTED_EXCHANGES,
            "default_compare_exchanges": DEFAULT_COMPARE_EXCHANGES,
        }
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    if args.demo_screenshot:
        r = demo_screenshot()
        if args.json:
            print(json.dumps(r, indent=2))
        else:
            print(
                f"截图买盘: {r['levels']} 档, {r['total_qty']:.5f} BTC, "
                f"合计 ${r['total_usd']:,.2f}, 加权均价 {r['avg_price']:,.2f}"
            )
        return 0

    if args.lark_card == "query":
        card = build_query_card(
            default_symbol=args.symbol.upper(),
            default_side=args.side,
            default_currency=args.currency.upper(),
            default_amount=args.notional or "1000000",
        )
        print(json.dumps(card, indent=2, ensure_ascii=False))
        return 0

    notional_val: Optional[float] = (
        parse_amount(args.notional) if args.notional is not None else None
    )
    qty_val: Optional[float] = parse_amount(args.qty) if args.qty is not None else None

    if args.verify:
        ex_target = args.exchange.split(",")[0].strip().lower()
        if ex_target == "all":
            ex_target = "binance"
        rep = verify_orderbook(
            exchange=ex_target,
            symbol=args.symbol,
            side=args.side,
            notional=notional_val if (notional_val is not None or qty_val is not None) else 1_000_000.0,
            qty=qty_val,
            currency=args.currency.upper(),
            samples=args.verify_samples,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(rep.to_dict(), indent=2, ensure_ascii=False))
        else:
            _print_verification_report(rep)
        return 0

    if (notional_val is None) == (qty_val is None):
        ap.error("--notional 与 --qty 必须且只能指定一个")

    ex_raw = args.exchange.strip().lower()
    is_multi = ex_raw == "all" or "," in ex_raw

    if is_multi or args.lark_card == "result":
        ex_list = (
            None
            if ex_raw == "all"
            else [x.strip().lower() for x in ex_raw.split(",") if x.strip()]
        )
        results = compare_exchanges(
            symbol=args.symbol,
            side=args.side,
            notional=notional_val,
            qty=qty_val,
            currency=args.currency.upper(),
            exchanges=ex_list,
            limit=args.limit,
            fx_rate=args.fx_rate,
        )
        if args.lark_card == "result":
            print(json.dumps(build_result_card(results), indent=2, ensure_ascii=False))
            return 0
        if args.json:
            print(
                json.dumps(
                    [r.to_dict() for r in results], indent=2, ensure_ascii=False
                )
            )
        else:
            _print_comparison_table(results)
        return 0

    # Single-exchange mode
    try:
        r = estimate_single(
            exchange=ex_raw,
            symbol=args.symbol,
            side=args.side,
            notional=notional_val,
            qty=qty_val,
            currency=args.currency.upper(),
            limit=args.limit,
            fx_rate=args.fx_rate,
            allow_partial=False,
        )
    except RuntimeError as e:
        print(f"估算失败: {e}", file=sys.stderr)
        return 1

    if args.json:
        # Include legacy keys (`target_usd`, `cost_usd`, `vwap`, `last_price`, `vwap_vs_top_bp`, `last_vs_top_bp`)
        # alongside the expanded multi-currency fields
        payload = r.to_dict()
        payload.update(
            {
                "target_usd": r.target_notional_usd,
                "vwap": r.vwap_quote,
                "last_price": r.last_price_quote,
                "vwap_vs_top_bp": r.vwap_slippage_bp,
                "last_vs_top_bp": r.last_slippage_bp,
            }
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_single_result(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
