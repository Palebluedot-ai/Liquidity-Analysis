import json
from dataclasses import replace

import pytest

from liquidity_analysis.engine import evaluate_snapshot
from liquidity_analysis.historical_tracker import evaluate_order_across_4_scenarios
from liquidity_analysis.lark_card import build_result_card
from liquidity_analysis.models import OrderBookSnapshot


def make_result(exchange='binance', side='buy'):
    snapshot = OrderBookSnapshot(
        exchange=exchange, exchange_label=exchange.title(), symbol='BTC-USDT',
        base_asset='BTC', quote_asset='USDT',
        bids=[(80000., 1.), (79900., 10.), (79800., 20.)],
        asks=[(80000., 1.), (80100., 10.), (80200., 20.)],
    )
    result = evaluate_snapshot(snapshot, side=side, input_currency='USD',
                               notional=1_000_000., fx_usd_hkd=7.8, taker_fee_bp=10.)
    result.real_breakdown.historical_matrix = evaluate_order_across_4_scenarios(
        exchange=exchange, side=side, order_usd=1_000_000., taker_fee_bp=10.,
        fiat_usdt_friction_bp=11., otc_quote_bp=30.)
    return result


def test_all_exchanges_retained_and_details_collapsed():
    results = [make_result(ex) for ex in ('binance', 'okx', 'coinbase', 'kraken', 'hashkey_global')]
    card = build_result_card(results)
    elements = card['body']['elements']
    tables = [e for e in elements if e['tag'] == 'table']
    assert len(tables) == 1
    assert len(tables[0]['columns']) == 2
    assert [row['exchange'] for row in tables[0]['rows']] == [r.exchange_label for r in results]
    for result, row in zip(results, tables[0]['rows']):
        assert row['cost'] == f'{result.real_breakdown.total_orderbook_cost_bp:.1f}'
    text = '\n'.join(e['content'] for e in elements if e['tag'] == 'markdown')
    assert '|---' not in text
    for result in results:
        b = result.real_breakdown
        assert f'**{result.exchange_label} · {result.symbol}**' in text
        for value in (b.static_slippage_bp, b.mm_retreat_bp, b.hft_frontrun_bp,
                      b.taker_fee_bp, b.fiat_usdt_friction_bp):
            assert f'{value:.1f} bp' in text
        assert f'{b.strategy_optimal_bp:.1f} bp（{b.optimal_exec_mins:.1f}' in text
        for scenario in b.historical_matrix.scenarios:
            assert f'{scenario.est_all_in_median_bp:.1f} / {scenario.est_all_in_p90_bp:.1f}' in text
    panels = [e for e in elements if e['tag'] == 'collapsible_panel']
    assert len(panels) == 21
    assert all(p['expanded'] is False for p in panels)
    assert all(e['tag'] == 'markdown' for p in panels for e in p['elements'])
    history_panels = [p for p in panels if '样本与历史案例' in p['header']['title']['content']]
    for result, panel in zip(results, history_panels):
        detail = panel['elements'][0]['content']
        matrix = result.real_breakdown.historical_matrix
        assert matrix.span_start_utc[:10] in detail
        for scenario in matrix.scenarios:
            assert f'N={scenario.sample_count:,}' in detail
            if scenario.scenario_key in ('momentum_chase', 'extreme_stampede') and scenario.representative_cases:
                assert scenario.representative_cases[0].dt in detail
    # Match the application message envelope, including escaped card JSON.
    payload = {'receive_id': 'ou_test', 'msg_type': 'interactive',
               'content': json.dumps(card, ensure_ascii=False)}
    assert len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) < 30_000


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('savings', [-5., 0., 5.])
def test_single_exchange_missing_history_and_signed_copy(side, savings):
    result = make_result(side=side)
    result.real_breakdown.historical_matrix = None
    result.real_breakdown.otc_savings_bp = savings
    result.real_breakdown.otc_savings_usd = savings * 100
    failed = replace(result, exchange='failed', exchange_label='Failed', error='unavailable')
    elements = build_result_card([failed, result])['body']['elements']
    tables = [e for e in elements if e['tag'] == 'table']
    assert len(tables) == 1
    assert len(tables[0]['rows']) == 2
    assert tables[0]['rows'][0]['cost'] == '测算失败'
    text = '\n'.join(e['content'] for e in elements if e['tag'] == 'markdown')
    assert '正常震荡：—' in text
    assert 'unavailable' in text
    panels = [e for e in elements if e['tag'] == 'collapsible_panel']
    detail = panels[0]['elements'][0]['content']
    assert ('多付' if side == 'buy' else '少收') in detail
    copy = panels[-2]['elements'][0]['content']
    assert ('OTC 估算节省' if savings > 0 else '盘面估算成本比 OTC 低' if savings < 0 else '持平') in copy
    assert '~' not in copy


def test_empty_and_failed_results_have_no_pitch_tables():
    assert build_result_card([])['header']['template'] == 'red'
    failed = replace(make_result(), error='unavailable')
    elements = build_result_card([failed])['body']['elements']
    tables = [e for e in elements if e['tag'] == 'table']
    assert len(tables) == 1
    assert tables[0]['rows'][0]['cost'] == '测算失败'
    assert not any(e['tag'] == 'collapsible_panel' for e in elements)
