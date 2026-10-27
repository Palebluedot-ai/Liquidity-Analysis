"""Regression test for opening a new form without replacing the report."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.core.json import JSON
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from liquidity_analysis.lark_bot import EventReceipts, LiquidityBotHandlers
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


def test_query_forms_require_a_new_human_trigger_and_ack_before_sending(tmp_path, caplog):
    """Replay, bot messages and process restarts must not create unsolicited forms."""
    client = Mock()
    client.im.v1.message.create.return_value.success.return_value = True
    jobs = []
    database = tmp_path / "receipts.sqlite3"
    receipts = EventReceipts(database)
    handler = LiquidityBotHandlers(client, background=jobs.append, receipts=receipts)

    def message(message_id, sender_type="user", event_id="ev_message"):
        return P2ImMessageReceiveV1({
            "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_type": sender_type},
                "message": {
                    "message_id": message_id, "chat_id": "oc_test",
                    "message_type": "text", "content": '{"text":"private text"}',
                    "create_time": 1793084400000,
                },
            },
        })

    with caplog.at_level("INFO"):
        # No event means no work, even if the client is connected.
        assert jobs == []
        for sender in ("bot", "app", None):
            handler.on_message(message("om_non_user", sender))
        handler.on_message(message(None))
        assert jobs == []

        # The callback returns before any network call; each message gets one job.
        handler.on_message(message("om_first"))
        client.im.v1.message.create.assert_not_called()
        assert len(jobs) == 1
        handler.on_message(message("om_first", event_id="ev_redelivery"))
        assert len(jobs) == 1
        jobs.pop(0)()
        client.im.v1.message.create.assert_called_once()

        # Simultaneous deliveries cannot race past the receipt check.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: handler.on_message(message("om_concurrent")), range(8)))
        assert len(jobs) == 1
        jobs.pop(0)()
        assert client.im.v1.message.create.call_count == 2

        # Restarting still remembers prior message IDs.
        receipts.close()
        receipts = EventReceipts(database)
        handler = LiquidityBotHandlers(client, background=jobs.append, receipts=receipts)
        handler.on_message(message("om_first", event_id="ev_after_restart"))
        assert jobs == []
        handler.on_message(message("om_new_after_restart"))
        assert len(jobs) == 1
        jobs.pop(0)()
        assert client.im.v1.message.create.call_count == 3

        # An intentional reopen is allowed once; redelivery preserves the report.
        callback = P2CardActionTrigger({
            "header": {"event_id": "ev_reopen", "event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_test"},
                "action": {"form_value": {}, "value": {"action": ACTION_OPEN_QUERY_FORM}},
            },
        })
        for _ in range(2):
            response = json.loads(JSON.marshal(handler.on_card_action(callback)))
            assert "card" not in response
        assert len(jobs) == 1
        jobs.pop(0)()
        assert client.im.v1.message.create.call_count == 4
        receipts.close()
        receipts = EventReceipts(database)
        handler = LiquidityBotHandlers(client, background=jobs.append, receipts=receipts)
        handler.on_card_action(callback)
        assert jobs == []
        receipts.close()

    assert "忽略非用户消息" in caplog.text
    assert "忽略重复消息" in caplog.text
    assert "忽略重复卡片回调" in caplog.text
    assert "发送卡片成功" in caplog.text
    assert "private text" not in caplog.text
