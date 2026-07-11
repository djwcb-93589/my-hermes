"""
飞书 adapter:基于 ``lark-channel-sdk``,第一阶段仅支持文本。

特性:
  - 私聊 / 群聊 / 话题
  - 群聊默认仅响应 @机器人
  - open_id + union_id
  - reply / thread 路由
  - SDK 消息去重 + 发送重试
  - 长文本分片(TextBatcher)
  - allowed_users / allowed_chats / allow_all 白名单

不实现:图片、文件、语音、卡片、流式回复。
"""

from __future__ import annotations

import asyncio
import uuid

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.text_utils import MessageDeduplicator, utf16_len, truncate_utf16
from hermes.gateway.types import MessageEvent, MessageType, SendResult, SessionSource


FEISHU_UTF16_LIMIT = 30000  # 飞书单条文本 UTF-16 code units 上限


class FeishuAdapter(BasePlatformAdapter):
    """飞书文本 adapter。"""

    PLATFORM = "feishu"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        require_mention: bool = True,
        allow_all: bool = False,
        allowed_users: list[str] | None = None,
        allowed_chats: list[str] | None = None,
    ):
        super().__init__("feishu")
        self.app_id = app_id
        self.app_secret = app_secret
        self.require_mention = require_mention
        self.allow_all = allow_all
        self.allowed_users = set(allowed_users or [])
        self.allowed_chats = set(allowed_chats or [])
        self._channel = None
        self._dedup = MessageDeduplicator(max_size=5000)

    async def connect(self) -> bool:
        try:
            from lark_channel import FeishuChannel
        except ImportError:
            print("  [feishu] lark-channel-sdk not installed, skipping")
            return False

        if not self.app_id or not self.app_secret:
            print("  [feishu] missing app_id / app_secret")
            return False

        try:
            self._channel = FeishuChannel(
                app_id=self.app_id,
                app_secret=self.app_secret,
            )
            self._channel.on("message", self._on_message)
            await self._channel.connect_until_ready(timeout=30)
            self._running = True
            return True
        except Exception as exc:
            print(f"  [feishu] connect failed: {exc!r}")
            self._channel = None
            return False

    async def disconnect(self):
        self._running = False
        if self._channel:
            try:
                await self._channel.disconnect()
            except Exception:
                pass
            self._channel = None

    # ----- 消息入站 -----

    async def _on_message(self, msg):
        """SDK message 回调 → 翻译成 MessageEvent。"""
        # 去重
        msg_id = getattr(msg, "message_id", "")
        if msg_id and self._dedup.is_duplicate(msg_id):
            return

        chat_type_raw = getattr(msg, "chat_type", "p2p")
        chat_type = "dm" if chat_type_raw == "p2p" else chat_type_raw

        # 群聊默认仅响应 @机器人
        if (
            chat_type in ("group", "topic")
            and self.require_mention
            and not getattr(msg, "mentioned_bot", False)
        ):
            return

        sender = getattr(msg, "sender", None)
        sender_id = getattr(msg, "sender_id", "") or ""
        union_id = getattr(sender, "union_id", None) if sender else None

        # 白名单
        if not self._is_allowed(sender_id, getattr(msg, "chat_id", "")):
            return

        thread_id = None
        conversation = getattr(msg, "conversation", None)
        if conversation:
            thread_id = getattr(conversation, "thread_id", None)

        event = MessageEvent(
            message_id=msg_id,
            text=getattr(msg, "content_text", "") or "",
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=self.PLATFORM,
                account_id=self.app_id,
                chat_id=getattr(msg, "chat_id", "") or "",
                chat_type=chat_type,
                user_id=sender_id,
                user_id_alt=union_id or "",
                user_name=getattr(msg, "sender_name", "") or "",
                thread_id=thread_id,
            ),
            reply_to_message_id=getattr(msg, "reply_to_message_id", None),
            metadata={
                "mentioned_bot": getattr(msg, "mentioned_bot", False),
                "mentioned_all": getattr(msg, "mentioned_all", False),
                "raw_content_type": getattr(msg, "raw_content_type", ""),
            },
        )
        await self.handle_message(event)

    def _is_allowed(self, user_id: str, chat_id: str) -> bool:
        """白名单检查。"""
        if self.allow_all:
            return True
        if user_id and user_id in self.allowed_users:
            return True
        if chat_id and chat_id in self.allowed_chats:
            return True
        return False

    # ----- 消息出站 -----

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        if not self._channel:
            return SendResult(success=False, error="not connected")

        # 长文本分片:飞书单条 UTF-16 上限约 30000
        chunks = self._split_text(content, FEISHU_UTF16_LIMIT)
        last_result = SendResult(success=True)

        for chunk in chunks:
            opts = {
                "receive_id_type": "chat_id",
                "uuid": str(uuid.uuid4()),
            }
            if reply_to_message_id:
                opts["reply_to"] = reply_to_message_id
            if thread_id:
                opts["reply_in_thread"] = True

            try:
                result = await self._channel.send(
                    chat_id,
                    {"text": chunk},
                    opts,
                )
                # SDK 可能返带 .success 的对象,也可能抛异常
                if result is None:
                    last_result = SendResult(success=True, message_id=None)
                elif getattr(result, "success", True) is False:
                    err = getattr(result, "error", "unknown send error")
                    return SendResult(success=False, error=str(err))
                else:
                    mid = getattr(result, "message_id", None)
                    last_result = SendResult(success=True, message_id=mid)
                    reply_to_message_id = mid  # 后续分片 reply 到前一条
            except Exception as exc:
                return SendResult(success=False, error=str(exc))

        return last_result

    @staticmethod
    def _split_text(text: str, max_units: int) -> list[str]:
        """按 UTF-16 code units 分片。"""
        if utf16_len(text) <= max_units:
            return [text]
        chunks: list[str] = []
        remaining = text
        while utf16_len(remaining) > max_units:
            chunk = truncate_utf16(remaining, max_units)
            chunks.append(chunk)
            # 按 codepoint 偏移截断
            remaining = remaining[len(chunk):]
        if remaining:
            chunks.append(remaining)
        return chunks or [text]
