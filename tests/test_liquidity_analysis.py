# -*- coding: utf-8 -*-
"""Unit tests for Liquidity Analysis engine, OTC vs Order Book metrics, and Lark Schema 2.0 cards."""

import unittest

from estimate_slippage import demo_screenshot
from liquidity_analysis.engine import (
    adverse_slippage_bp,
    build_otc_pitch_lines,
    evaluate_snapshot,
    walk_book,
)
from liquidity_analysis.fx import convert_notional_to_quote, quote_to_usd_and_hkd
from liquidity_analysis.lark_card import (
    F_CURRENCY,
    F_CUSTOM_NOTIONAL,
    F_CUSTOM_SYMBOL,
    F_EXCHANGE,
    F_PRESET_NOTIONAL,
    F_PRESET_SYMBOL,
    F_SIDE,
    build_query_card,
    build_result_card,
    parse_lark_form_params,
)
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
        q_usd = convert_notional_to_quote(7_800_000.0, "HKD", "USDT", rate)
        self.assertAlmostEqual(q_usd, 1_000_000.0)

        q_hkd = convert_notional_to_quote(7_800_000.0, "HKD", "HKD", rate)
        self.assertAlmostEqual(q_hkd, 7_800_000.0)

        q_usd_to_hkd = convert_notional_to_quote(1_000_000.0, "USD", "HKD", rate)
        self.assertAlmostEqual(q_usd_to_hkd, 7_800_000.0)

        usd_val, hkd_val = quote_to_usd_and_hkd(624_000.0, "HKD", rate)
        self.assertAlmostEqual(usd_val, 80_000.0)
        self.assertAlmostEqual(hkd_val, 624_000.0)

    def test_walk_book_and_adverse_slippage(self):
        asks = [(100.0, 10.0), (101.0, 10.0), (102.0, 10.0)]
        res_buy = walk_book(asks, target_qty=15.0)
        self.assertAlmostEqual(res_buy["filled_qty"], 15.0)
        self.assertAlmostEqual(res_buy["cost_usd"], 100.0 * 10 + 101.0 * 5)
        self.assertAlmostEqual(res_buy["vwap"], 1505.0 / 15.0)
        self.assertEqual(res_buy["levels_used"], 2)
        self.assertFalse(res_buy["depth_exhausted"])

        slip_buy = adverse_slippage_bp(100.0, res_buy["vwap"], "buy")
        self.assertGreater(slip_buy, 0.0)

        partial = walk_book(asks, target_qty=50.0, allow_partial=True)
        self.assertTrue(partial["depth_exhausted"])
        self.assertAlmostEqual(partial["filled_qty"], 30.0)
        self.assertAlmostEqual(partial["fill_ratio"], 30.0 / 50.0)

    def test_evaluate_snapshot_otc_metrics_and_lark_schema_2_cards(self):
        snap = OrderBookSnapshot(
            exchange="okx",
            exchange_label="OKX",
            symbol="BTC-USDT",
            base_asset="BTC",
            quote_asset="USDT",
            bids=[(80000.0, 1.0), (79900.0, 10.0), (79800.0, 20.0)],
            asks=[(80000.0, 1.0), (80100.0, 10.0), (80200.0, 20.0)],
            sequence_id="12345",
        )
        # Buy $1,000,000 USD: Level 1 only has 1 BTC = $80,000 (8% of $1,000,000)
        res = evaluate_snapshot(
            snap,
            side="buy",
            input_currency="USD",
            notional=1_000_000.0,
            fx_usd_hkd=7.80,
            taker_fee_bp=5.0,
        )
        self.assertFalse(res.depth_exhausted)
        self.assertEqual(res.levels_used, 3)
        self.assertAlmostEqual(res.level1_notional_usd, 80_000.0)
        self.assertAlmostEqual(res.level1_fill_pct, 8.0)
        self.assertGreater(res.vwap_extra_per_coin_usd, 0.0)
        self.assertAlmostEqual(res.last_extra_per_coin_usd, 200.0)
        self.assertGreater(res.slippage_loss_usd, 0.0)
        self.assertAlmostEqual(res.taker_fee_usd, 500.0)

        pitch = build_otc_pitch_lines([res])
        self.assertGreaterEqual(len(pitch), 3)

        # Verify Lark Card Schema 2.0
        q_card = build_query_card()
        self.assertEqual(q_card["schema"], "2.0")
        self.assertIn("body", q_card)

        r_card = build_result_card([res])
        self.assertEqual(r_card["schema"], "2.0")
        self.assertIn("body", r_card)

        # Verify form parsing with dropdown + custom override
        params = parse_lark_form_params(
            {
                F_PRESET_SYMBOL: {"value": "BTC"},
                F_CUSTOM_SYMBOL: "",
                F_SIDE: {"value": "buy"},
                F_CURRENCY: {"value": "USD"},
                F_PRESET_NOTIONAL: {"value": "1000000"},
                F_CUSTOM_NOTIONAL: "",
                F_EXCHANGE: {"value": "binance,okx"},
            }
        )
        self.assertEqual(params["symbol"], "BTC")
        self.assertEqual(params["notional"], 1_000_000.0)
        self.assertEqual(params["exchanges"], ["binance", "okx"])

    def test_historical_tracker_4_scenarios(self):
        from liquidity_analysis.historical_tracker import (
            evaluate_order_across_4_scenarios,
            load_historical_dataset,
        )

        ds = load_historical_dataset()
        self.assertIsNotNone(ds)
        self.assertEqual(ds["horizon_days"], 365)
        for ex in ("binance", "okx", "coinbase", "kraken", "hashkey_global"):
            self.assertIn(ex, ds["exchanges"])
            mat = evaluate_order_across_4_scenarios(
                exchange=ex,
                side="buy",
                order_usd=2_000_000.0,
                taker_fee_bp=10.0,
                fiat_usdt_friction_bp=11.0,
                otc_quote_bp=30.0,
            )
            self.assertIsNotNone(mat)
            self.assertEqual(len(mat.scenarios), 4)
            scen_keys = [s.scenario_key for s in mat.scenarios]
            self.assertEqual(
                scen_keys,
                ["normal_calm", "momentum_chase", "extreme_stampede", "contrarian_dip"],
            )
            # Verify that momentum_chase and extreme_stampede have higher impact than normal_calm
            normal_bp = mat.scenarios[0].est_all_in_median_bp
            chase_bp = mat.scenarios[1].est_all_in_median_bp
            stampede_bp = mat.scenarios[2].est_all_in_median_bp
            self.assertGreaterEqual(chase_bp, normal_bp)
            self.assertGreater(stampede_bp, normal_bp)
            self.assertGreater(len(mat.scenarios[1].representative_cases), 0)


if __name__ == "__main__":
    unittest.main()
