# -*- coding: utf-8 -*-
"""
Lark (飞书) Bot WebSocket Long-Connection Application.

Usage:
    cp .env.example .env   # Fill in LARK_APP_ID and LARK_APP_SECRET
    uv run python -m liquidity_analysis.lark_bot

How users interact in Lark:
  1. Send any message to the bot (e.g. "hi", "测算", "btc") -> Bot replies with the
     Schema 2.0 Interactive Selection Card (`build_query_card`).
  2. In the card, select:
     - Coin pair from dropdown (BTC, ETH, Solana, etc.) or type a custom pair
     - Direction (BUY / SELL)
     - Currency (USD 美金 / HKD 港币)
     - Amount from preset dropdown (50万 / 100万 / 200万 / 500万 / 1000万 / 1500万) or custom input
     - Exchanges (Binance + OKX, All 5 exchanges, or single exchange)
  3. Click "📊 立即测算 (生成 OTC vs 盘面对比报告)" -> Bot immediately acknowledges within
     the 3-second callback window and pushes the full OTC vs Order Book comparison card.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Dict

from .engine import DEFAULT_TAKER_FEE_BP, compare_exchanges, verify_orderbook
from .lark_card import (
    ACTION_OPEN_QUERY_FORM,
    ACTION_SUBMIT_QUERY,
    ACTION_VERIFY_MAKER,
    build_query_card,
    build_result_card,
    build_verification_card,
    error_card,
    notice_card,
    parse_lark_form_params,
)
from .ws_patch import apply_card_frame_patch

logger = logging.getLogger(__name__)


def _load_dotenv_if_present() -> None:
    """Lightweight .env loader (no external dependency required, also works with python-dotenv)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def _run_in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class EventReceipts:
    """Atomically claim events, optionally retaining receipts across restarts."""

    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            str(path) if path is not None else ":memory:",
            timeout=1.0,
            check_same_thread=False,
        )
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS received_events "
                "(kind TEXT NOT NULL, event_key TEXT NOT NULL, "
                "PRIMARY KEY (kind, event_key))"
            )

    def claim(self, kind: str, event_key: str) -> bool:
        with self._lock, self._db:
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO received_events VALUES (?, ?)",
                (kind, event_key),
            )
            return inserted.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


class LiquidityBotHandlers:
    """Lark message and interactive card callback handlers."""

    def __init__(
        self,
        client: Any,
        background: Callable[[Callable[[], None]], None] = _run_in_thread,
        receipts: EventReceipts | None = None,
    ) -> None:
        self._client = client
        self._background = background
        self._receipts = receipts if receipts is not None else EventReceipts()

    def on_message(self, data: Any) -> None:
        """Acknowledge each human message promptly and send its form once."""
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender_type = getattr(getattr(event, "sender", None), "sender_type", None)
        message_id = getattr(message, "message_id", None)
        chat_id = getattr(message, "chat_id", None)
        event_id = getattr(getattr(data, "header", None), "event_id", None)
        logger.info(
            "收到消息事件 event_id=%s message_id=%s sender_type=%s message_type=%s create_time=%s",
            event_id, message_id, sender_type,
            getattr(message, "message_type", None), getattr(message, "create_time", None),
        )
        if sender_type != "user":
            logger.info("忽略非用户消息 message_id=%s sender_type=%s", message_id, sender_type)
            return
        if not message_id or not chat_id:
            logger.warning("忽略缺少 message_id/chat_id 的消息 event_id=%s", event_id)
            return
        # Lark message redeliveries can have a different event_id: dedupe by message_id.
        if not self._receipts.claim("message", message_id):
            logger.info("忽略重复消息 message_id=%s event_id=%s", message_id, event_id)
            return

        def _message_worker() -> None:
            logger.info("用户消息触发查询卡片 message_id=%s", message_id)
            try:
                self._send_to_chat(chat_id, build_query_card())
            except Exception:
                logger.exception("回复查询卡片失败 message_id=%s", message_id)

        # Network I/O must not delay the SDK's event acknowledgment.
        self._background(_message_worker)

    def on_card_action(self, data: Any) -> Any:
        """
        Handle Lark Card Schema 2.0 form submissions and button clicks.
        Strictly returns within the 3-second Lark callback window.
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        event_id = getattr(getattr(data, "header", None), "event_id", None)
        action_value = data.event.action.value
        action = action_value.get("action", "") if isinstance(action_value, dict) else ""
        logger.info(
            "收到卡片回调 event_id=%s action=%s",
            event_id, action,
        )
        if event_id and not self._receipts.claim("card", event_id):
            logger.info("忽略重复卡片回调 event_id=%s", event_id)
            return self._card_response(
                None, P2CardActionTriggerResponse, toast="操作已接收，无需重复提交",
            )

        form = data.event.action.form_value or {}
        open_id = data.event.operator.open_id

        try:
            if action == ACTION_OPEN_QUERY_FORM:
                av = action_value if isinstance(action_value, dict) else {}
                card = build_query_card(
                    default_symbol=str(av.get("symbol") or "BTC"),
                    default_side=str(av.get("side") or "buy"),
                    default_currency=str(av.get("currency") or "USD"),
                    default_amount=str(av.get("notional") or "1000000"),
                    default_exchange=str(av.get("exchange") or "binance,okx"),
                    default_otc_bp=str(av.get("otc_quote_bp") or "30"),
                )

                def _open_worker() -> None:
                    logger.info("卡片按钮触发新查询表单 event_id=%s", event_id)
                    try:
                        self._send_to_user(open_id, card)
                    except Exception:
                        logger.exception("发送新测算表单失败")

                # Returning a card here would replace the report being clicked.
                self._background(_open_worker)
                return self._card_response(
                    None,
                    P2CardActionTriggerResponse,
                    toast="正在发送新测算表单，原报告已保留",
                )

            if action == ACTION_SUBMIT_QUERY or form:
                params = parse_lark_form_params(form)

                def _worker() -> None:
                    logger.info("卡片提交触发测算 event_id=%s", event_id)
                    try:
                        results = compare_exchanges(
                            symbol=params["symbol"],
                            side=params["side"],
                            notional=params["notional"],
                            currency=params["currency"],
                            exchanges=params["exchanges"],
                            otc_quote_bp=params["otc_quote_bp"],
                        )
                        res_card = build_result_card(results)
                        self._send_to_user(open_id, res_card)
                    except Exception as exc:
                        logger.exception("异步盘口测算失败")
                        self._send_to_user(open_id, error_card(f"盘口测算失败: {exc}"))

                self._background(_worker)

                ccy_prefix = "HK$" if params["currency"] == "HKD" else "$"
                return self._card_response(
                    notice_card(
                        "⏳ 正在实时抓取各所盘口深度…",
                        (
                            f"已提交：**{params['side'].upper()} {ccy_prefix}{params['notional']:,.0f} "
                            f"{params['currency']} {params['symbol'].upper()}** "
                            f"（对比范围：`{params['exchange_sel']}`）。\n"
                            "正在并发拉取 5,000 档实时 L2 盘口并计算吃穿档位与额外差价，报告将在 1~3 秒内推送。"
                        ),
                        template="blue",
                    ),
                    P2CardActionTriggerResponse,
                    toast="已提交，正在实时抓取盘口…",
                )

            if action == ACTION_VERIFY_MAKER:
                av = action_value if isinstance(action_value, dict) else {}
                ex = str(av.get("exchange") or "binance")
                sym = str(av.get("symbol") or "BTC")
                side = str(av.get("side") or "buy")
                ccy = str(av.get("currency") or "USD")
                notional = float(av.get("notional") or 1_000_000)

                def _verify_worker() -> None:
                    logger.info("卡片按钮触发挂单验证 event_id=%s", event_id)
                    try:
                        self._send_to_user(
                            open_id,
                            notice_card(
                                "🔍 正在执行多帧盘口采样与真伪验证…",
                                f"正在对 **{ex.upper()} ({sym})** 连续采样 3 次盘口并比对最新逐笔成交，约需 3~4 秒…",
                                template="blue",
                            ),
                        )
                        rep = verify_orderbook(
                            exchange=ex,
                            symbol=sym,
                            side=side,
                            notional=notional,
                            currency=ccy,
                            samples=3,
                            interval_sec=1.2,
                        )
                        self._send_to_user(open_id, build_verification_card(rep))
                    except Exception as exc:
                        logger.exception("盘口真伪验证失败")
                        self._send_to_user(open_id, error_card(f"盘口验证失败: {exc}"))

                self._background(_verify_worker)
                return self._card_response(
                    None,
                    P2CardActionTriggerResponse,
                    toast="正在采样验证 Maker 挂单…",
                )

            return self._card_response(build_query_card(), P2CardActionTriggerResponse)

        except Exception as exc:
            logger.exception("处理卡片交互异常")
            return self._card_response(
                error_card(f"参数错误或系统异常: {exc}"),
                P2CardActionTriggerResponse,
            )

    @staticmethod
    def _card_response(
        card: Dict[str, Any] | None, resp_cls: Any, *, toast: str | None = None
    ) -> Any:
        # Omit `card` to acknowledge an interaction without replacing its message.
        payload: Dict[str, Any] = {}
        if card is not None:
            payload["card"] = {"type": "raw", "data": card}
        if toast:
            payload["toast"] = {"type": "success", "content": toast}
        return resp_cls(payload)

    def _send_to_chat(self, chat_id: str, card: Dict[str, Any]) -> None:
        self._send_card(receive_id=chat_id, receive_id_type="chat_id", card=card)

    def _send_to_user(self, open_id: str, card: Dict[str, Any]) -> None:
        self._send_card(receive_id=open_id, receive_id_type="open_id", card=card)

    def _send_card(
        self, *, receive_id: str, receive_id_type: str, card: Dict[str, Any]
    ) -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        logger.info("开始发送卡片 receive_id_type=%s receive_id=%s", receive_id_type, receive_id)
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error("发送飞书卡片失败: %s %s", response.code, response.msg)
        else:
            logger.info(
                "发送卡片成功 receive_id_type=%s receive_id=%s message_id=%s",
                receive_id_type, receive_id,
                getattr(getattr(response, "data", None), "message_id", None),
            )


def main() -> None:
    _load_dotenv_if_present()
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]: %(message)s",
    )

    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    domain = os.environ.get("LARK_DOMAIN", "https://open.feishu.cn").strip()

    if not app_id or not app_secret or app_id == "cli_xxxxxxxxxxxxxxxx":
        raise SystemExit(
            "未配置 LARK_APP_ID / LARK_APP_SECRET。\n"
            "请先执行 `cp .env.example .env` 并填入飞书/Lark 自建应用的凭证。"
        )

    try:
        import lark_oapi as lark
    except ImportError as e:
        raise SystemExit(
            "未安装 lark-oapi。请运行 `uv sync` 或 `pip install 'lark-oapi>=1.7.3,<2.0'`。"
        ) from e

    # 必须在建立长连接前启用 CARD 帧补丁，否则点击卡片提交按钮会报 200340
    apply_card_frame_patch()

    rest_client = (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(domain)
        .log_level(
            lark.LogLevel.DEBUG if log_level == "DEBUG" else lark.LogLevel.INFO
        )
        .build()
    )
    state_dir = Path(os.environ.get(
        "LARK_STATE_DIR", str(Path(__file__).resolve().parent.parent / ".runtime" / "lark"),
    ))
    handlers = LiquidityBotHandlers(
        client=rest_client, receipts=EventReceipts(state_dir / "received-events.sqlite3"),
    )

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handlers.on_message)
        .register_p2_card_action_trigger(handlers.on_card_action)
        .build()
    )

    logger.info("正在建立飞书/Lark WebSocket 长连接 (domain=%s)…", domain)
    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        domain=domain,
        log_level=(
            lark.LogLevel.DEBUG if log_level == "DEBUG" else lark.LogLevel.INFO
        ),
    )
    ws_client.start()


if __name__ == "__main__":
    main()
