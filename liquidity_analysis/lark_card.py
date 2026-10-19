# -*- coding: utf-8 -*-
"""
Lark (飞书) Interactive Card (Schema 2.0) builder and callback payload processor.

Includes full 5-component realistic Market Maker withdrawal + price retreat + HFT front-running
+ Taker fee + Fiat/USDT friction breakdown benchmarked directly against the OTC Block Trade quote (e.g. 30 bp).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .engine import (
    DEFAULT_OTC_QUOTE_BP,
    build_otc_pitch_lines,
    compare_exchanges,
)
from .exchanges import SUPPORTED_EXCHANGES
from .models import SlippageResult, VerificationReport
from .symbols import PRESET_ASSETS, parse_amount

ACTION_SUBMIT_QUERY = "submit_slippage_query"
ACTION_OPEN_QUERY_FORM = "open_query_form"
ACTION_VERIFY_MAKER = "verify_maker_orders"

F_PRESET_SYMBOL = "preset_symbol"
F_CUSTOM_SYMBOL = "custom_symbol"
F_SIDE = "side"
F_CURRENCY = "currency"
F_PRESET_NOTIONAL = "preset_notional"
F_CUSTOM_NOTIONAL = "custom_notional"
F_EXCHANGE = "exchange"
F_OTC_QUOTE_BP = "otc_quote_bp"

PRESET_NOTIONAL_OPTIONS: List[tuple[str, str]] = [
    ("500,000 (50万)", "500000"),
    ("1,000,000 (100万)", "1000000"),
    ("2,000,000 (200万)", "2000000"),
    ("5,000,000 (500万)", "5000000"),
    ("10,000,000 (1000万)", "10000000"),
    ("15,000,000 (1500万)", "15000000"),
]

OTC_QUOTE_BP_OPTIONS: List[tuple[str, str]] = [
    ("20 bp (0.20% VIP 大宗报价)", "20"),
    ("25 bp (0.25% 优惠报价)", "25"),
    ("30 bp (0.30% 标准 OTC 报价)", "30"),
    ("35 bp (0.35% 报价)", "35"),
    ("40 bp (0.40% 报价)", "40"),
]


def _text(content: str, size: str = "normal") -> Dict[str, Any]:
    return {"tag": "markdown", "content": content, "text_size": size}


def _select(
    name: str,
    placeholder: str,
    options: List[tuple[str, str]],
    initial_option: Optional[str] = None,
) -> Dict[str, Any]:
    el: Dict[str, Any] = {
        "tag": "select_static",
        "name": name,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "width": "fill",
        "options": [
            {"text": {"tag": "plain_text", "content": label}, "value": val}
            for label, val in options
        ],
        "margin": "0px 0px 8px 0px",
    }
    if initial_option is not None:
        el["initial_option"] = initial_option
    return el


def _input(
    name: str,
    label: str,
    placeholder: str,
    default_value: str = "",
    required: bool = False,
) -> Dict[str, Any]:
    el: Dict[str, Any] = {
        "tag": "input",
        "name": name,
        "label": {"tag": "plain_text", "content": label},
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "required": required,
        "width": "fill",
        "margin": "0px 0px 8px 0px",
    }
    if default_value:
        el["default_value"] = default_value
    return el


def _submit_button(name: str, action: str, text: str) -> Dict[str, Any]:
    return {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": text},
        "type": "primary",
        "width": "fill",
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {"action": action}}],
    }


def _callback_button(
    text: str, value: Dict[str, Any], *, primary: bool = False
) -> Dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": "primary" if primary else "default",
        "width": "fill",
        "margin": "4px 0px 4px 0px",
        "behaviors": [{"type": "callback", "value": value}],
    }


def notice_card(title: str, body: str, *, template: str = "blue") -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {"elements": [_text(body)]},
    }


def error_card(body: str) -> Dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 测算未能完成"},
            "template": "red",
        },
        "body": {
            "elements": [
                _text(body),
                _callback_button(
                    "🔄 返回重新选择",
                    {"action": ACTION_OPEN_QUERY_FORM},
                    primary=True,
                ),
            ]
        },
    }


def build_query_card(
    default_symbol: str = "BTC",
    default_side: str = "buy",
    default_currency: str = "USD",
    default_amount: str = "1000000",
    default_exchange: str = "binance,okx",
    default_otc_bp: str = "30",
) -> Dict[str, Any]:
    """Generate Lark Schema 2.0 interactive form card."""
    symbol_choices = [(item["label"], item["value"]) for item in PRESET_ASSETS]

    exchange_choices: List[tuple[str, str]] = [
        ("Binance + OKX (主流盘面对比)", "binance,okx"),
        ("Binance + OKX + HashKey HK", "binance,okx,hashkey"),
        (
            "全部对比 (Binance / OKX / Coinbase / Kraken / HashKey)",
            "all",
        ),
    ] + [(label, key) for key, label in SUPPORTED_EXCHANGES.items()]

    preset_values = {v for _, v in PRESET_NOTIONAL_OPTIONS}
    init_notional = default_amount if default_amount in preset_values else "1000000"
    init_custom_notional = "" if default_amount in preset_values else default_amount

    preset_sym_values = {item["value"] for item in PRESET_ASSETS}
    init_sym = default_symbol.upper() if default_symbol.upper() in preset_sym_values else "BTC"
    init_custom_sym = "" if default_symbol.upper() in preset_sym_values else default_symbol

    otc_vals = {v for _, v in OTC_QUOTE_BP_OPTIONS}
    init_otc = default_otc_bp if default_otc_bp in otc_vals else "30"

    return {
        "schema": "2.0",
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": "📊 OTC Block Trade vs 交易所盘口真实冲击测算",
            },
        },
        "body": {
            "elements": [
                _text(
                    "选择**币对**、**方向**、**美金/港币金额**与**OTC 报价点数（如 30bp）**，"
                    "实时还原**大单触发做市商毫秒级撤空退价（MM Retreat）+ HFT 抢跑 + Taker 费 + USDT 换汇损耗**，"
                    "用真实微观结构数据证明 **OTC 一口价锁死比盘面更划算**。"
                ),
                {
                    "tag": "form",
                    "name": "slippage_query_form",
                    "elements": [
                        _text("**1. 选择币对（下拉选择，或在下方输入框手动填写其他币对）**"),
                        _select(
                            F_PRESET_SYMBOL,
                            "选择主流币种 (BTC / ETH / SOL 等)",
                            symbol_choices,
                            initial_option=init_sym,
                        ),
                        _input(
                            F_CUSTOM_SYMBOL,
                            "自定义币对 (选填)",
                            "若填此项则优先使用，如 PEPE、Solana、BTCUSDT、BTCHKD",
                            default_value=init_custom_sym,
                        ),
                        _text("**2. 买卖方向**"),
                        _select(
                            F_SIDE,
                            "选择买卖方向",
                            [
                                ("BUY 买入 (吃卖盘 Asks — 看盘面比 1 档多花多少)", "buy"),
                                ("SELL 卖出 (吃买盘 Bids — 看盘面比 1 档少收多少)", "sell"),
                            ],
                            initial_option=default_side,
                        ),
                        _text("**3. 购买/卖出计价货币**"),
                        _select(
                            F_CURRENCY,
                            "选择美金或港币",
                            [
                                ("USD (美金 $)", "USD"),
                                ("HKD (港币 HK$)", "HKD"),
                            ],
                            initial_option=default_currency.upper(),
                        ),
                        _text("**4. 目标金额（可下拉快选常用大宗金额，也可在下方输入框填任意金额）**"),
                        _select(
                            F_PRESET_NOTIONAL,
                            "快捷选择金额档位",
                            PRESET_NOTIONAL_OPTIONS,
                            initial_option=init_notional,
                        ),
                        _input(
                            F_CUSTOM_NOTIONAL,
                            "自定义金额 (选填)",
                            "若填此项则优先使用，支持 1000000、1.5M、300万、800k 等",
                            default_value=init_custom_notional,
                        ),
                        _text("**5. 选择对比交易所**"),
                        _select(
                            F_EXCHANGE,
                            "选择交易所范围",
                            exchange_choices,
                            initial_option=default_exchange,
                        ),
                        _text("**6. 对标 OTC 报价点数 (默认 30 bp 全包一口价)**"),
                        _select(
                            F_OTC_QUOTE_BP,
                            "选择 OTC 报价点数",
                            OTC_QUOTE_BP_OPTIONS,
                            initial_option=init_otc,
                        ),
                        _submit_button(
                            "submit_slippage_btn",
                            ACTION_SUBMIT_QUERY,
                            "📊 立即测算 (还原做市商撤单退价 vs OTC 30bp 对标)",
                        ),
                    ],
                },
            ]
        },
    }


def build_result_card(results: List[SlippageResult]) -> Dict[str, Any]:
    """Render a list of `SlippageResult` into a Lark Schema 2.0 Card proving OTC Block Trade advantage."""
    if not results:
        return error_card("未获取到任何交易所盘口数据，请检查币对名称或重试。")

    first = results[0]
    side_cn = "买入 (BUY / 吃卖盘 Asks)" if first.side == "buy" else "卖出 (SELL / 吃买盘 Bids)"
    worse_label = "比1档多付" if first.side == "buy" else "比1档少收"
    ccy = first.input_currency
    ccy_prefix = "HK$" if ccy == "HKD" else "$"
    otc_bp = first.real_breakdown.otc_quote_bp if first.real_breakdown else DEFAULT_OTC_QUOTE_BP

    if first.target_notional_input is not None:
        target_str = (
            f"{ccy_prefix}{first.target_notional_input:,.2f} {ccy} "
            f"(≈ ${first.target_notional_usd:,.0f} USD / HK${first.target_notional_hkd:,.0f} HKD)"
        )
    else:
        target_str = f"{first.target_qty:,.4f} {first.base_asset}"

    lines: List[str] = [
        f"**标的**：`{first.base_asset}` ｜ **方向**：{side_cn}",
        f"**目标规模**：{target_str} ｜ **对标 OTC 锁价**：**`{otc_bp:.0f} bp` (全包净价)**",
        "\n---\n### 一、🔥 盘面五项真实全链路成本 vs OTC 锁价对标（核心结论）",
        (
            f"| 交易所 | ①静态表层 | ②做市商撤空退价 | ③HFT抢跑传染 | ④Taker费 | ⑤USDT换汇摩擦 | **盘面真实总成本** | **对比 OTC ({otc_bp:.0f}bp)** |\n"
            "|---|---|---|---|---|---|---|---|"
        ),
    ]

    for r in results:
        if r.error or not r.real_breakdown:
            lines.append(
                f"| **{r.exchange_label}** (`{r.symbol}`) | - | - | - | - | - | - | ❌ 失败 |"
            )
            continue
        b = r.real_breakdown
        save_str = (
            f"✅ **OTC 净省 {b.otc_savings_bp:.1f}bp (`${b.otc_savings_usd:,.0f}`)**"
            if b.otc_savings_bp > 0
            else f"⚠️ 接近 OTC (`{b.total_orderbook_cost_bp:.1f}bp`)"
        )
        lines.append(
            f"| **{r.exchange_label}** (`{r.symbol}`) | "
            f"`{b.static_slippage_bp:.1f}bp` (吃{r.levels_used}档) | "
            f"**`+{b.mm_retreat_bp:.1f}bp`** | "
            f"**`+{b.hft_frontrun_bp:.1f}bp`** | "
            f"`+{b.taker_fee_bp:.1f}bp` | "
            f"`+{b.fiat_usdt_friction_bp:.1f}bp` | "
            f"**`{b.total_orderbook_cost_bp:.1f} bp` (`${b.total_orderbook_cost_usd:,.0f}`)** | "
            f"{save_str} |"
        )

    lines.extend(
        [
            "\n---\n### 二、📊 静态表层 vs 做市商撤单退价后每颗币额外差价（vs 第 1 档屏幕价）",
            (
                f"| 交易所 | 第1档价 (仅能满足%) | 静态理想 (0%撤单) | 常规HFT撤单+退价 | 剧烈撤单+HFT抢跑 | 盘面真实全包均价 |\n"
                "|---|---|---|---|---|---|"
            ),
        ]
    )

    for r in results:
        if r.error or not r.fade_scenarios or not r.real_breakdown:
            continue
        s0 = r.fade_scenarios[0]
        s50 = r.fade_scenarios[1] if len(r.fade_scenarios) > 1 else s0
        s70 = r.fade_scenarios[2] if len(r.fade_scenarios) > 2 else s0
        b = r.real_breakdown
        lines.append(
            f"| **{r.exchange_label}** | "
            f"`${r.top_price_usd:,.2f}` ({r.level1_fill_pct:.1f}%) | "
            f"吃{s0.levels_used}档 / `+${s0.vwap_extra_per_coin_usd:,.2f}` ({s0.vwap_slippage_bp:.1f}bp) | "
            f"吃{s50.levels_used}档 / **`+${s50.vwap_extra_per_coin_usd:,.2f}`** ({s50.vwap_slippage_bp:.1f}bp) | "
            f"吃{s70.levels_used}档 / **`+${s70.vwap_extra_per_coin_usd:,.2f}`** ({s70.vwap_slippage_bp:.1f}bp) | "
            f"**`${b.realistic_all_in_vwap_usd:,.2f}`** ({worse_label} **`+${b.realistic_extra_per_coin_usd:,.2f}/{r.base_asset}`**) |"
        )

    pitch_lines = build_otc_pitch_lines(results)
    if pitch_lines:
        lines.append("\n---\n### 三、💡 OTC Block Trade 客户对比话术（可直接复制）")
        lines.extend(pitch_lines)

    ex_str = ",".join(r.exchange for r in results if not r.error) or "binance,okx"
    first_valid_ex = next((r.exchange for r in results if not r.error), "binance")
    amt_str = str(int(first.target_notional_input or 1_000_000))

    return {
        "schema": "2.0",
        "header": {
            "template": "turquoise",
            "title": {
                "tag": "plain_text",
                "content": f"📈 {first.base_asset} ({first.side.upper()}) 盘面真实全链路成本 vs OTC {otc_bp:.0f}bp 报告",
            },
        },
        "body": {
            "elements": [
                _text("\n".join(lines)),
                _callback_button(
                    "🔄 重新选择参数 / 测算其他币对与金额",
                    {
                        "action": ACTION_OPEN_QUERY_FORM,
                        "symbol": first.base_asset,
                        "side": first.side,
                        "currency": first.input_currency,
                        "notional": amt_str,
                        "exchange": ex_str,
                        "otc_quote_bp": str(int(otc_bp)),
                    },
                    primary=True,
                ),
                _callback_button(
                    f"🔍 验证 {first.base_asset} ({first_valid_ex.upper()}) 盘口 Maker 挂单真伪",
                    {
                        "action": ACTION_VERIFY_MAKER,
                        "exchange": first_valid_ex,
                        "symbol": first.base_asset,
                        "side": first.side,
                        "currency": first.input_currency,
                        "notional": amt_str,
                    },
                ),
            ]
        },
    }


def build_verification_card(rep: VerificationReport) -> Dict[str, Any]:
    """Render a `VerificationReport` into a Lark Schema 2.0 Card."""
    lines = [
        f"**交易所**：**{rep.exchange_label}** (`{rep.symbol}`) ｜ **连续采样**：`{rep.samples_count}` 次 (间隔 `{rep.interval_sec}s`)",
        "\n#### 1. 盘口结构完整性校验",
        f"- **买一 / 卖一**：`{rep.best_bid:,.2f}` / `{rep.best_ask:,.2f}` (Spread `{rep.spread_bp:.2f} bp`，是否倒挂：{'否 ✅' if not rep.is_crossed else '是 ❌'})",
        f"- **档位严格排序**：买盘严格递减={'是 ✅' if rep.bids_strictly_descending else '否 ❌'}，卖盘严格递增={'是 ✅' if rep.asks_strictly_ascending else '否 ❌'}",
        f"- **序列号单调递增**：`{rep.sequence_ids}` ({'正常 ✅' if rep.sequence_monotonic else '异常 ❌'})",
        "\n#### 2. 最新公开逐笔成交 (Public Trades) 交叉核验",
    ]
    if rep.latest_trade_price is not None:
        lines.append(
            f"- **最新真实成交价**：`{rep.latest_trade_price:,.2f}` "
            f"(距当前盘口中间价 `{rep.trade_vs_mid_bp:.2f} bp`，吻合度：{'吻合 ✅' if rep.trade_within_inside_spread_or_10bp else '偏离 ⚠️'})"
        )
    else:
        lines.append("- **最新真实成交价**：暂未获取")

    lines.extend(
        [
            "\n#### 3. 多帧盘口留存率与防闪单 (Anti-Spoofing) 检测",
            f"- **前 50 档价格留存率**：**`{rep.level_retention_ratio * 100:.1f}%`** (连续快照间相同价格档位的存活率)",
            f"- **10bp 内挂单深度波动率 (CV)**：`{rep.depth_10bp_cv_pct:.2f}%` ｜ 样本(USD): `{[round(x, 0) for x in rep.depth_10bp_usd_samples]}`",
            f"- **25bp 内挂单深度波动率 (CV)**：`{rep.depth_25bp_cv_pct:.2f}%` ｜ 样本(USD): `{[round(x, 0) for x in rep.depth_25bp_usd_samples]}`",
        ]
    )
    if rep.simulated_slippage_bp_samples:
        lines.append(
            f"- **多帧模拟滑点波动区间**：`min={rep.simulated_slippage_bp_min:.2f} bp` / "
            f"`mean={rep.simulated_slippage_bp_mean:.2f} bp` / "
            f"`max={rep.simulated_slippage_bp_max:.2f} bp`"
        )
    lines.append(f"\n---\n**综合结论**：**{rep.verdict}**")
    for w in rep.warnings:
        lines.append(f"- ⚠️ {w}")

    return {
        "schema": "2.0",
        "header": {
            "template": "green" if not rep.warnings else "orange",
            "title": {
                "tag": "plain_text",
                "content": f"🔍 {rep.exchange_label} ({rep.symbol}) Maker 挂单真实性验证报告",
            },
        },
        "body": {
            "elements": [
                _text("\n".join(lines)),
                _callback_button(
                    "🔄 返回测算面板",
                    {"action": ACTION_OPEN_QUERY_FORM, "symbol": rep.symbol},
                    primary=True,
                ),
            ]
        },
    }


def _select_val(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("value") or "")
    return "" if raw is None else str(raw)


def parse_lark_form_params(form_value: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalized query parameters from Lark form submission `form_value`."""
    custom_symbol = _select_val(form_value.get(F_CUSTOM_SYMBOL)).strip()
    preset_symbol = _select_val(form_value.get(F_PRESET_SYMBOL)).strip() or "BTC"
    symbol = custom_symbol if custom_symbol else preset_symbol

    side = (_select_val(form_value.get(F_SIDE)).strip() or "buy").lower()
    currency = (_select_val(form_value.get(F_CURRENCY)).strip() or "USD").upper()

    custom_notional = _select_val(form_value.get(F_CUSTOM_NOTIONAL)).strip()
    preset_notional = _select_val(form_value.get(F_PRESET_NOTIONAL)).strip()
    legacy_notional = _select_val(form_value.get("notional")).strip()
    raw_notional = custom_notional or legacy_notional or preset_notional or "1000000"
    notional = parse_amount(raw_notional)

    exchange_sel = (_select_val(form_value.get(F_EXCHANGE)).strip() or "binance,okx").lower()
    exchanges = (
        None
        if exchange_sel == "all"
        else [x.strip() for x in exchange_sel.split(",") if x.strip()]
    )

    otc_raw = _select_val(form_value.get(F_OTC_QUOTE_BP)).strip()
    otc_quote_bp = float(otc_raw) if otc_raw else DEFAULT_OTC_QUOTE_BP

    return {
        "symbol": symbol,
        "side": side,
        "currency": currency,
        "notional": notional,
        "exchanges": exchanges,
        "exchange_sel": exchange_sel,
        "otc_quote_bp": otc_quote_bp,
    }


def handle_lark_form_submit(form_value: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous helper to process `action.form_value` and return a rendered result card."""
    params = parse_lark_form_params(form_value)
    results = compare_exchanges(
        symbol=params["symbol"],
        side=params["side"],
        notional=params["notional"],
        currency=params["currency"],
        exchanges=params["exchanges"],
        otc_quote_bp=params["otc_quote_bp"],
    )
    return build_result_card(results)
