# -*- coding: utf-8 -*-
"""Unit tests for Liquidity Analysis engine, symbol normalization, FX conversion, and Lark cards."""

import unittest

from estimate_slippage import SCREENSHOT_BIDS, demo_screenshot
from liquidity_analysis.engine import (
    adverse_slippage_bp,
    compute_depth_bands,
    evaluate_snapshot,
    walk_book,
)
from liquidity_analysis.fx import convert_notional_to_quote, quote_to_usd_and_hkd
from liquidity_analysis.lark_card import build_query_card, build_result_card
from liquidity_analysis.models import OrderBookSnapshot
from liquidity_analysis.symbols import (
    parse_amount,
    parse_user_symbol,
    resolve_exchange_symbol,
)


class TestLiquidityAnalysis(unittest.TestCase):
    def test_demo_screenshot_matches_reference(self):
        r = demo_screenshot()
        self.assertEqual(r["levels"], 33)
        self.assertAlmostEqual(r["total_qty"], 3.44714, places=5)
        self.assertAlmostEqual(r["total_usd"], 296629.23, places=2)

    def test_symbol_parsing_and_resolution(self):
        self.assertEqual(parse_user_symbol("BTC"), ("BTC", None))
        self.assertEqual(parse_user_symbol("Solana"), ("SOL", None))
        self.assertEqual(parse_user_symbol("SOL (Solana)"), ("SOL", None))
        self.assertEqual(parse_user_symbol("BTCUSDT"), ("BTC", "USDT"))
        self.assertEqual(parse_user_symbol("ETH/HKD"), ("ETH", "HKD"))
        self.assertEqual(parse_user_symbol("BTC-USD"), ("BTC", "USD"))

        bn_cands = resolve_exchange_symbol("Solana", "binance")
        self.assertEqual(bn_cands[0], ("SOLUSDT", "SOL", "USDT"))

        okx_cands = resolve_exchange_symbol("ETH", "okx")
        self.assertEqual(okx_cands[0], ("ETH-USDT", "ETH", "USDT"))

        cb_cands = resolve_exchange_symbol("BTC", "coinbase")
        self.assertEqual(cb_cands[0], ("BTC-USD", "BTC", "USD"))

        hk_cands = resolve_exchange_symbol("BTC", "hashkey_hkd", input_currency="HKD")
        self.assertEqual(hk_cands[0], ("BTCHKD", "BTC", "HKD"))

    def test_parse_amount_shorthands(self):
        self.assertAlmostEqual(parse_amount("15,000,000"), 15_000_000.0)
        self.assertAlmostEqual(parse_amount("15M"), 15_000_000.0)
        self.assertAlmostEqual(parse_amount("500k"), 500_000.0)
        self.assertAlmostEqual(parse_amount("1000万"), 10_000_000.0)
        self.assertAlmostEqual(parse_amount("HK$7,800,000"), 7_800_000.0)

    def test_fx_conversion_and_hkd_support(self):
        rate = 7.80
        # 7.8M HKD on a USD/USDT order book -> 1M USD
        q_usd = convert_notional_to_quote(7_800_000.0, "HKD", "USDT", rate)
        self.assertAlmostEqual(q_usd, 1_000_000.0)

        # 7.8M HKD on a native HKD order book -> 7.8M HKD
        q_hkd = convert_notional_to_quote(7_800_000.0, "HKD", "HKD", rate)
        self.assertAlmostEqual(q_hkd, 7_800_000.0)

        # 1M USD on a native HKD order book -> 7.8M HKD
        q_usd_to_hkd = convert_notional_to_quote(1_000_000.0, "USD", "HKD", rate)
        self.assertAlmostEqual(q_usd_to_hkd, 7_800_000.0)

        usd_val, hkd_val = quote_to_usd_and_hkd(624_000.0, "HKD", rate)
        self.assertAlmostEqual(usd_val, 80_000.0)
        self.assertAlmostEqual(hkd_val, 624_000.0)

    def test_walk_book_and_adverse_slippage(self):
        # Simple 3-level ask book for buying
        asks = [(100.0, 10.0), (101.0, 10.0), (102.0, 10.0)]
        res_buy = walk_book(asks, target_qty=15.0)
        self.assertAlmostEqual(res_buy["filled_qty"], 15.0)
        self.assertAlmostEqual(res_buy["cost_usd"], 100.0 * 10 + 101.0 * 5)
        self.assertAlmostEqual(res_buy["vwap"], 1505.0 / 15.0)
        self.assertEqual(res_buy["levels_used"], 2)
        self.assertFalse(res_buy["depth_exhausted"])

        # Adverse slippage for buy: vwap > top_ask -> positive bp
        slip_buy = adverse_slippage_bp(100.0, res_buy["vwap"], "buy")
        self.assertGreater(slip_buy, 0.0)
        self.assertAlmostEqual(slip_buy, (100.33333333333333 - 100.0) / 100.0 * 1e4, places=4)

        # Partial fill mode when book depth is exceeded
        partial = walk_book(asks, target_qty=50.0, allow_partial=True)
        self.assertTrue(partial["depth_exhausted"])
        self.assertAlmostEqual(partial["filled_qty"], 30.0)
        self.assertAlmostEqual(partial["fill_ratio"], 30.0 / 50.0)

    def test_evaluate_snapshot_and_lark_cards(self):
        snap = OrderBookSnapshot(
            exchange="binance",
            exchange_label="Binance",
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            bids=[(80000.0, 5.0), (79900.0, 10.0), (79800.0, 20.0)],
            asks=[(80010.0, 5.0), (80100.0, 10.0), (80200.0, 20.0)],
            sequence_id="12345",
        )
        res = evaluate_snapshot(
            snap,
            side="buy",
            input_currency="HKD",
            notional=7_800_000.0,  # = $1,000,000 USD at 7.80
            fx_usd_hkd=7.80,
        )
        self.assertFalse(res.depth_exhausted)
        self.assertAlmostEqual(res.cost_usd, 1_000_000.0, places=2)
        self.assertAlmostEqual(res.cost_hkd, 7_800_000.0, places=2)
        self.assertGreater(res.vwap_slippage_bp, 0.0)

        q_card = build_query_card()
        self.assertIn("elements", q_card)
        r_card = build_result_card([res])
        self.assertIn("elements", r_card)


if __name__ == "__main__":
    unittest.main()
