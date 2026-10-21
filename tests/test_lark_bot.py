"""Regression test for opening a new form without replacing the report."""

import json
from unittest.mock import Mock

from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from liquidity_analysis.lark_bot import LiquidityBotHandlers
from liquidity_analysis.lark_card import ACTION_OPEN_QUERY_FORM, build_query_card


def test_reopen_preserves_report_and_sends_new_form():
    jobs = []
    client = Mock()
    client.im.v1.message.create.return_value.success.return_value = True
    handler = LiquidityBotHandlers(client, background=jobs.append)
    callback = P2CardActionTrigger({"event": {
        "operator": {"open_id": "ou_test"},
        "action": {"form_value": {}, "value": {
            "action": ACTION_OPEN_QUERY_FORM, "symbol": "ETH", "side": "sell",
            "currency": "HKD", "notional": "2000000", "exchange": "okx",
            "otc_quote_bp": "25",
        }},
    }})

    response = json.loads(JSON.marshal(handler.on_card_action(callback)))
    assert "card" not in response  # Lark must not replace the clicked report.
    client.im.v1.message.create.assert_not_called()

    assert len(jobs) == 1
    jobs[0]()
    client.im.v1.message.create.assert_called_once()
    request = client.im.v1.message.create.call_args.args[0]
    assert request.receive_id_type == "open_id"
    assert request.request_body.receive_id == "ou_test"
    assert json.loads(request.request_body.content) == build_query_card(
        default_symbol="ETH", default_side="sell", default_currency="HKD",
        default_amount="2000000", default_exchange="okx", default_otc_bp="25",
    )
