#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
estimate_slippage.py
====================
多交易所盘口流动性分析、做市商动态撤单退价模拟与 OTC Block Trade (30bp) 价值对标工具
"""

import argparse
import json
import sys
from typing import List, Optional

from liquidity_analysis.engine import (
    DEFAULT_OTC_QUOTE_BP,
    build_otc_pitch_lines,
    compare_exchanges,
    estimate_single,
    verify_orderbook,
)
from liquidity_analysis.exchanges import (
    DEFAULT_COMPARE_EXCHANGES,
    SUPPORTED_EXCHANGES,
)
from liquidity_analysis.historical_tracker import (
    incremental_rolling_sync,
    load_historical_dataset,
    refresh_historical_dataset,
)
from liquidity_analysis.lark_card import build_query_card, build_result_card
from liquidity_analysis.models import SlippageResult, VerificationReport
from liquidity_analysis.symbols import PRESET_ASSETS, parse_amount

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
    _print_comparison_table([r])


def _print_comparison_table(results: List[SlippageResult]) -> None:
    if not results:
        print("未获取到任何交易所盘口结果。", file=sys.stderr)
        return

    first = results[0]
    ccy = first.input_currency
    otc_bp = first.real_breakdown.otc_quote_bp if first.real_breakdown else DEFAULT_OTC_QUOTE_BP
    if first.target_notional_input is not None:
        prefix = "HK$" if ccy == "HKD" else "$"
        target_txt = (
            f"{prefix}{first.target_notional_input:,.2f} {ccy} "
            f"(≈ ${first.target_notional_usd:,.0f} USD / HK${first.target_notional_hkd:,.0f} HKD)"
        )
    else:
        target_txt = f"{first.target_qty:,.4f} {first.base_asset}"

    print("=" * 142)
    print(
        f"🔥 盘面真实全链路执行成本 vs OTC Block Trade ({otc_bp:.0f}bp 一口价) 深度对标 | "
        f"标的: {first.base_asset} | 方向: {first.side.upper()} | 目标: {target_txt}"
    )
    print("=" * 142)
    print(
        f"{'交易所':<16} {'交易对':<9} {'1档价(仅够%)':>18} {'①静态表层':>12} "
        f"{'②MM撤空退价':>12} {'③HFT抢跑':>10} {'④Taker费':>9} {'⑤USDT换汇':>10} "
        f"{'盘面真实总成本':>18} {'对比 OTC (' + str(int(otc_bp)) + 'bp)':>20}"
    )
    print("-" * 142)

    for r in results:
        if r.error or not r.real_breakdown:
            print(f"{r.exchange_label:<16} {r.symbol:<9} ❌ {r.error}")
            continue
        b = r.real_breakdown
        l1_txt = f"${r.top_price_usd:,.0f} ({r.level1_fill_pct:.1f}%)"
        s1_txt = f"{b.static_slippage_bp:.1f}bp({r.levels_used}档)"
        s2_txt = f"+{b.mm_retreat_bp:.1f}bp"
        s3_txt = f"+{b.hft_frontrun_bp:.1f}bp"
        s4_txt = f"+{b.taker_fee_bp:.1f}bp"
        s5_txt = f"+{b.fiat_usdt_friction_bp:.1f}bp"
        tot_txt = f"{b.total_orderbook_cost_bp:.1f}bp (${b.total_orderbook_cost_usd:,.0f})"
        if b.otc_savings_bp > 0:
            vs_txt = f"OTC省 {b.otc_savings_bp:.1f}bp (${b.otc_savings_usd:,.0f})"
        else:
            vs_txt = f"差 {b.otc_savings_bp:.1f}bp"
        print(
            f"{r.exchange_label:<16} {r.symbol:<9} {l1_txt:>18} {s1_txt:>12} "
            f"{s2_txt:>12} {s3_txt:>10} {s4_txt:>9} {s5_txt:>10} "
            f"{tot_txt:>18} {vs_txt:>20}"
        )

    valid_with_fade = [r for r in results if not r.error and r.fade_scenarios and r.real_breakdown]
    if valid_with_fade:
        print("-" * 142)
        print("⚡ 【不同做市商撤单/退价情景下的盘面穿透与每颗币额外加价（相对屏幕第 1 档）】")
        for r in valid_with_fade:
            s0 = r.fade_scenarios[0]
            s50 = r.fade_scenarios[1]
            s70 = r.fade_scenarios[2]
            b = r.real_breakdown
            assert b is not None
            print(
                f"  • {r.exchange_label:<12} | "
                f"静态理想(0%撤): 吃{s0.levels_used:>3}档, 贵+${s0.vwap_extra_per_coin_usd:>6,.2f}/{r.base_asset} ({s0.vwap_slippage_bp:>4.1f}bp)  ==>  "
                f"MM撤空退价: 吃{s50.levels_used:>3}档, 贵+${s50.vwap_extra_per_coin_usd:>6,.2f} ({s50.vwap_slippage_bp:>4.1f}bp)  ==>  "
                f"全链路落地均价: ${b.realistic_all_in_vwap_usd:,.2f} (比1档贵 +${b.realistic_extra_per_coin_usd:,.2f}/{r.base_asset}, 共 {b.total_orderbook_cost_bp:.1f}bp)"
            )

    print("-" * 142)
    pitch_lines = build_otc_pitch_lines(results)
    if pitch_lines:
        print("💡 【OTC Block Trade 客户对比分析结论（可直接复制发送）】")
        for line in pitch_lines:
            print(line)
    print("=" * 142)


def _print_verification_report(rep: VerificationReport) -> None:
    print("=" * 90)
    print(
        f"盘口 Maker 挂单真实性验证报告 | {rep.exchange_label} ({rep.symbol}) | "
        f"采样 {rep.samples_count} 次 (间隔 {rep.interval_sec}s)"
    )
    print("=" * 90)
    print("1. 盘口结构完整性:")
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
    print("2. 最新公开成交 (Public Trades) 交叉核验:")
    if rep.latest_trade_price is not None:
        print(
            f"   - 最新逐笔成交价: {rep.latest_trade_price:,.2f} "
            f"(距当前盘口中间价 {rep.trade_vs_mid_bp:.2f} bp, "
            f"吻合度: {'吻合 ✅' if rep.trade_within_inside_spread_or_10bp else '偏离 ⚠️'})"
        )
    else:
        print("   - 最新逐笔成交价: 暂未获取")
    print("3. 多帧盘口留存率与防闪单 (Anti-Spoofing) 检测:")
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
        description="多交易所现货盘口流动性、做市商动态撤单退价模拟与 OTC Block Trade 对比分析工具"
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
        help="目标名义金额 (支持数字或 1M / 500k / 100万 / 1000万), 与 --qty 二选一",
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
            "hashkey_global, bybit, 或 all (多所横向对比), 也支持逗号分隔如 binance,okx。默认 binance"
        ),
    )
    ap.add_argument(
        "--taker-fee-bp",
        type=float,
        default=None,
        help="手动指定盘面 Taker 吃单手续费率 (bp)，默认按各交易所真实费率 (Binance/OKX 10bp, Coinbase 15bp 等)",
    )
    ap.add_argument(
        "--otc-quote-bp",
        type=float,
        default=DEFAULT_OTC_QUOTE_BP,
        help=f"对标的 OTC Block Trade 一口价报价点数 (bp), 默认 {DEFAULT_OTC_QUOTE_BP} bp",
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
    ap.add_argument(
        "--backtest-1y",
        action="store_true",
        help="输出过去 365 天（1整年）五大交易所 4 大市场场景真实大单历史冲击与案例库",
    )
    ap.add_argument(
        "--refresh-1y",
        action="store_true",
        help="通过五大交易所公开 API 重新分页拉取过去 365 天历史 K 线与逐笔成交并更新本地 1 年期基准库",
    )
    ap.add_argument(
        "--rolling-sync",
        action="store_true",
        help="执行滚动增量同步 (Rolling Incremental Sync): 仅拉取最新增量 K 线并合并沉淀至本地滚动压缩库 (保留最长 730 天)",
    )

    args = ap.parse_args(argv)

    if args.rolling_sync or args.refresh_1y or args.backtest_1y:
        if args.refresh_1y:
            ds = refresh_historical_dataset(365)
        elif args.rolling_sync:
            ds = incremental_rolling_sync()
        else:
            ds = load_historical_dataset() or incremental_rolling_sync()
        if args.json:
            print(json.dumps(ds, indent=2, ensure_ascii=False))
            return 0
        print("=" * 110)
        print(
            f"📊 五大交易所 {ds.get('horizon_days', 365)} 天 ({ds.get('window_start_utc')} ~ {ds.get('window_end_utc')}) "
            f"真实大单 4 大场景历史冲击与取证库"
        )
        print("=" * 110)
        for ex_k, ex_v in ds.get("exchanges", {}).items():
            if ex_k == "hashkey_hk":
                continue
            print(
                f"\n>>> {ex_v['label']} | API: {ex_v['api_method']} | "
                f"Span: {ex_v['span_start_utc']} -> {ex_v['span_end_utc']} | "
                f"Stage2高频样本: {ex_v['stage2_high_freq_bars']} 根 ({ex_v['high_freq_interval']})"
            )
            for s_key, s_obj in ex_v.get("scenarios", {}).items():
                if s_obj.get("count", 0) <= 0:
                    continue
                print(
                    f"  • {s_key:<18} | N={s_obj['count']:<4} | 平均净大单=${s_obj['avg_net_flow_usd']/1e6:.2f}M | "
                    f"中位峰值冲击={s_obj['median_peak_bp']:>5.1f}bp | P75={s_obj['p75_peak_bp']:>5.1f}bp | "
                    f"P90={s_obj['p90_peak_bp']:>5.1f}bp | 永久留存={int(s_obj['perm_retention_ratio']*100)}%"
                )
                for c in s_obj.get("top_cases", [])[:2]:
                    print(
                        f"      - [{c['dt']}] {c['dir']} 净额=${c['abs_net_usd']/1e6:.2f}M (总成交=${c['quote_vol_usd']/1e6:.2f}M): "
                        f"${c['open']:,.1f} -> 瞬间冲击 {c['peak_bp']:.1f}bp / 收盘永久位移 {c['perm_bp']:.1f}bp (前序趋势={c['prior_ret_bp']:+.1f}bp)"
                    )
        return 0

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
            default_otc_bp=str(int(args.otc_quote_bp)),
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
            taker_fee_bp=args.taker_fee_bp,
            otc_quote_bp=args.otc_quote_bp,
        )
        if args.lark_card == "result":
            print(json.dumps(build_result_card(results), indent=2, ensure_ascii=False))
            return 0
        if args.json:
            print(
                json.dumps(
                    {
                        "results": [r.to_dict() for r in results],
                        "otc_pitch": build_otc_pitch_lines(results),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            _print_comparison_table(results)
        return 0

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
            taker_fee_bp=args.taker_fee_bp,
            otc_quote_bp=args.otc_quote_bp,
            allow_partial=False,
        )
    except RuntimeError as e:
        print(f"估算失败: {e}", file=sys.stderr)
        return 1

    if args.json:
        payload = r.to_dict()
        payload.update(
            {
                "target_usd": r.target_notional_usd,
                "vwap": r.vwap_quote,
                "last_price": r.last_price_quote,
                "vwap_vs_top_bp": r.vwap_slippage_bp,
                "last_vs_top_bp": r.last_slippage_bp,
                "otc_pitch": build_otc_pitch_lines([r]),
            }
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_single_result(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
