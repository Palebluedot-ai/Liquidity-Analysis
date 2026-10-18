# -*- coding: utf-8 -*-
"""
Lark (飞书) Interactive Card builder and form callback handler.

Provides:
  - `build_query_card()`: Interactive form card with:
      1. Preset symbol dropdown (BTC, ETH, Solana, etc.) + Custom symbol input box
      2. Buy / Sell direction selector
      3. Currency selector (USD 美金 / HKD 港币) + Notional amount input
      4. Exchange selector (All comparison or individual exchanges: Binance, OKX, Coinbase, Kraken, HashKey, etc.)
  - `build_result_card(results)`: Rich Lark result card comparing execution VWAP, slippage (bp),
      and order book depth across exchanges.
  - `handle_lark_form_submit(form_value)`: End-to-end callback handler from Lark card submission to result card.
"""

from typing import Any, Dict, List, Optional

from .engine import compare_exchanges
from .exchanges import SUPPORTED_EXCHANGES
from .models import SlippageResult
from .symbols import PRESET_ASSETS, parse_amount


def build_query_card(
    default_symbol: str = "BTC",
    default_side: str = "buy",
    default_currency: str = "USD",
    default_amount: str = "1000000",
    default_exchange: str = "all",
) -> Dict[str, Any]:
    """Generate a Lark interactive form card for liquidity & slippage analysis."""
    symbol_options = [
        {"text": {"tag": "plain_text", "content": item["label"]}, "value": item["value"]}
        for item in PRESET_ASSETS
    ]

    exchange_options = [
        {
            "text": {
                "tag": "plain_text",
                "content": "全部对比 (Binance / OKX / Coinbase / Kraken / HashKey)",
            },
            "value": "all",
        }
    ] + [
        {"text": {"tag": "plain_text", "content": label}, "value": key}
        for key, label in SUPPORTED_EXCHANGES.items()
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": "📊 盘口流动性与滑点测算 (Liquidity & Slippage Analyzer)",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "支持从下拉菜单选择主流币种或手动输入任意币对，实时拉取各交易所 L2 盘口深度，"
                    "模拟大额市价单逐档吃单并计算 **VWAP 滑点 (bp)** 与 **最后一口滑点 (bp)**。"
                ),
            },
            {
                "tag": "form",
                "name": "slippage_query_form",
                "elements": [
                    {
                        "tag": "select_static",
                        "name": "preset_symbol",
                        "placeholder": {"tag": "plain_text", "content": "选择常用币种"},
                        "initial_option": default_symbol,
                        "options": symbol_options,
                    },
                    {
                        "tag": "input",
                        "name": "custom_symbol",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "选填：自定义币种或币对（如 Solana、PEPE、BTCUSDT、BTCHKD），填写后优先使用",
                        },
                        "default_value": "",
                    },
                    {
                        "tag": "select_static",
                        "name": "side",
                        "placeholder": {"tag": "plain_text", "content": "交易方向"},
                        "initial_option": default_side,
                        "options": [
                            {
                                "text": {
                                    "tag": "plain_text",
                                    "content": "BUY 买入 (吃卖盘 Asks)",
                                },
                                "value": "buy",
                            },
                            {
                                "text": {
                                    "tag": "plain_text",
                                    "content": "SELL 卖出 (吃买盘 Bids)",
                                },
                                "value": "sell",
                            },
                        ],
                    },
                    {
                        "tag": "select_static",
                        "name": "currency",
                        "placeholder": {"tag": "plain_text", "content": "计价货币"},
                        "initial_option": default_currency,
                        "options": [
                            {
                                "text": {"tag": "plain_text", "content": "USD (美金 $)"},
                                "value": "USD",
                            },
                            {
                                "text": {"tag": "plain_text", "content": "HKD (港币 HK$)"},
                                "value": "HKD",
                            },
                        ],
                    },
                    {
                        "tag": "input",
                        "name": "notional",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "输入购买/卖出金额（支持 15000000、15M、500k、1000万）",
                        },
                        "default_value": str(default_amount),
                    },
                    {
                        "tag": "select_static",
                        "name": "exchange",
                        "placeholder": {"tag": "plain_text", "content": "选择交易所"},
                        "initial_option": default_exchange,
                        "options": exchange_options,
                    },
                    {
                        "tag": "button",
                        "name": "submit_btn",
                        "text": {"tag": "plain_text", "content": "立即测算滑点"},
                        "type": "primary",
                        "action_type": "form_submit",
                    },
                ],
            },
        ],
    }


def build_result_card(results: List[SlippageResult]) -> Dict[str, Any]:
    """Render a list of `SlippageResult` into a Lark Interactive Card."""
    if not results:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red",
                "title": {"tag": "plain_text", "content": "⚠️ 未获取到任何盘口数据"},
            },
            "elements": [{"tag": "markdown", "content": "请检查币对名称或网络连接。"}],
        }

    first = results[0]
    side_cn = "买入 (BUY / 吃卖盘 Asks)" if first.side == "buy" else "卖出 (SELL / 吃买盘 Bids)"
    ccy = first.input_currency
    ccy_prefix = "HK$" if ccy == "HKD" else "$"

    if first.target_notional_input is not None:
        target_str = (
            f"{ccy_prefix}{first.target_notional_input:,.2f} {ccy} "
            f"(≈ ${first.target_notional_usd:,.0f} USD / HK${first.target_notional_hkd:,.0f} HKD)"
        )
    else:
        target_str = f"{first.target_qty:,.4f} {first.base_asset}"

    # Pick best venue among fully filled exchanges
    full_fills = [r for r in results if not r.error and not r.depth_exhausted]
    lines: List[str] = [
        f"**标的**：`{first.base_asset}` ｜ **方向**：{side_cn}",
        f"**目标规模**：{target_str} ｜ **参考汇率**：`1 USD = {first.fx_usd_hkd:.4f} HKD`",
    ]
    if full_fills:
        best_slip = min(full_fills, key=lambda r: r.vwap_slippage_bp)
        best_px = (
            min(full_fills, key=lambda r: r.vwap_usd)
            if first.side == "buy"
            else max(full_fills, key=lambda r: r.vwap_usd)
        )
        lines.append(
            f"🏆 **最小盘口滑点**：**{best_slip.exchange_label}** (`{best_slip.symbol}`) — "
            f"VWAP 滑点 **{best_slip.vwap_slippage_bp:.2f} bp** "
            f"(均价 `${best_slip.vwap_usd:,.2f}` / `HK${best_slip.vwap_hkd:,.2f}`)"
        )
        lines.append(
            f"💰 **最优绝对均价**：**{best_px.exchange_label}** (`{best_px.symbol}`) — "
            f"成交均价 **`${best_px.vwap_usd:,.2f}`** (`HK${best_px.vwap_hkd:,.2f}`) "
            f"｜ 盘口滑点 {best_px.vwap_slippage_bp:.2f} bp"
        )

    lines.append("\n---\n### 各交易所盘口滑点对比")
    lines.append(
        "| 交易所 | 交易对 | 盘口最优价 (USD) | 成交均价 VWAP | VWAP 滑点 | 最后一口滑点 | 同侧盘口总深 (USD) | 状态 |\n"
        "|---|---|---|---|---|---|---|---|"
    )

    for r in results:
        if r.error:
            lines.append(
                f"| **{r.exchange_label}** | `{r.symbol}` | - | - | - | - | - | ❌ 不支持/失败 |"
            )
            continue
        top_usd = (
            r.top_price / r.fx_usd_hkd if r.quote_asset == "HKD" else r.top_price
        )
        status = (
            "✅ 100% 成交"
            if not r.depth_exhausted
            else f"⚠️ 深度不足 ({r.fill_ratio * 100:.1f}%)"
        )
        lines.append(
            f"| **{r.exchange_label}** | `{r.symbol}` | `${top_usd:,.2f}` | "
            f"`${r.vwap_usd:,.2f}` (`HK${r.vwap_hkd:,.0f}`) | "
            f"**{r.vwap_slippage_bp:.2f} bp** | {r.last_slippage_bp:.2f} bp | "
            f"`${r.book_side_total_usd:,.0f}` ({r.book_levels}档) | {status} |"
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {
                "tag": "plain_text",
                "content": f"📈 {first.base_asset} ({first.side.upper()}) 跨所流动性与滑点报告",
            },
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "注：静态盘口估算，未含手续费；真实大额成交时做市商可能动态撤单，实际滑点通常更大。",
                    }
                ],
            },
        ],
    }


def handle_lark_form_submit(form_value: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process `action.form_value` from a Lark card callback and return a rendered result card.
    """
    custom_symbol = str(form_value.get("custom_symbol") or "").strip()
    preset_symbol = str(form_value.get("preset_symbol") or "BTC").strip()
    symbol = custom_symbol if custom_symbol else preset_symbol

    side = str(form_value.get("side") or "buy").strip().lower()
    currency = str(form_value.get("currency") or "USD").strip().upper()
    notional_raw = form_value.get("notional") or "1000000"
    notional = parse_amount(notional_raw)

    exchange_sel = str(form_value.get("exchange") or "all").strip().lower()
    exchanges = None if exchange_sel == "all" else [exchange_sel]

    results = compare_exchanges(
        symbol=symbol,
        side=side,
        notional=notional,
        currency=currency,
        exchanges=exchanges,
    )
    return build_result_card(results)
