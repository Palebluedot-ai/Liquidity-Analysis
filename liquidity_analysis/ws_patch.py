# -*- coding: utf-8 -*-
"""
Fix lark-oapi WebSocket client dropping CARD callback frames (SDK issue #126).

In `lark-oapi` 1.x, `Client._handle_data_frame` immediately returns on `MessageType.CARD`
without dispatching to `register_p2_card_action_trigger`, causing client-side 200340 timeout
after 3 seconds when a user clicks any card button or submits a form.

Rewriting the frame header `type` from `card` to `event` routes the payload through the
standard event dispatcher (which inspects `header.event_type == "p2.card.action.trigger"`)
and writes back the acknowledgment frame properly.
"""

from __future__ import annotations

import functools
import inspect
import logging

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_liquidity_bot_card_frame_patched"
_BUG_SIGNATURE = "elif message_type == MessageType.CARD:"


def apply_card_frame_patch() -> bool:
    """Patch `lark_oapi.ws.client.Client._handle_data_frame` so card callbacks work over WebSocket."""
    try:
        from lark_oapi.ws.client import Client
        from lark_oapi.ws.const import HEADER_TYPE
        from lark_oapi.ws.enum import MessageType
    except ImportError:
        logger.warning("未安装 lark-oapi，跳过 WebSocket CARD 帧补丁")
        return False

    if getattr(Client, _PATCH_FLAG, None) is not None:
        return True

    original = Client._handle_data_frame
    try:
        source = inspect.getsource(original)
        start = source.find(_BUG_SIGNATURE)
        if start != -1:
            branch_body = source[start + len(_BUG_SIGNATURE) :].lstrip()
            still_buggy = branch_body.startswith("return")
        else:
            still_buggy = False
    except (OSError, TypeError):
        still_buggy = True

    if not still_buggy:
        logger.info("lark-oapi 已支持 CARD 帧分发，无需补丁")
        return False

    @functools.wraps(original)
    async def _handle_data_frame_with_card_support(self, frame):
        for header in frame.headers:
            if header.key == HEADER_TYPE and header.value == MessageType.CARD.value:
                header.value = MessageType.EVENT.value
                break
        return await original(self, frame)

    Client._handle_data_frame = _handle_data_frame_with_card_support
    setattr(Client, _PATCH_FLAG, original)
    logger.info("已启用 lark-oapi WebSocket CARD 帧回调补丁")
    return True
