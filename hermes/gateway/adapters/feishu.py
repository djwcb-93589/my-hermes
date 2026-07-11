"""
飞书 adapter:基于 ``lark-channel-sdk``,第一阶段仅支持文本。

特性:
  - 私聊 / 群聊 / 话题
  - 群聊默认仅响应 @机器人(require_mention)
  - open_id + union_id
  - reply / thread 路由
  - SDK 消息去重 + 发送有限重试(指数退避)
  - 长文本分片(UTF-16 安全,不截断 Unicode)
  - allowed_users / allowed_chats / allow_all 白名单
  - 安全模式 compat / audit / strict(SecurityConfig)

不实现:图片、文件、语音、卡片、流式回复。
"""

from __future__ import annotations

import asyncio
import uuid

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.text_utils import utf16_len, truncate_utf16
from hermes.gateway.types import MessageEvent, MessageType, SendResult, SessionSource


FEISHU_UTF16_LIMIT = 30000  # 飞书单条文本 UTF-16 code units 上限

# 可恢复发送错误:有限重试 + 指数退避
_RETRYABLE_SEND_ERRORS = frozenset({
    "rate_limited", "send_timeout", "not_connected", "timeout", "network",
})


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
        security_mode: str = "compat",
        send_max_retries: int = 3,
        send_retry_base_delay: float = 1.0,
    ):
        super().__init__("feishu")
        self.app_id = app_id
        self.app_secret = app_secret
        self.require_mention = require_mention
        self.allow_all = allow_all
        self.allowed_users = set(allowed_users or [])
        self.allowed_chats = set(allowed_chats or [])
        self.security_mode = security_mode
        self.send_max_retries = send_max_retries
        self.send_retry_base_delay = send_retry_base_delay
        self._channel = None
        self._seen: set[str] = set()  # message_id 去重

    # ===================== 生命周期 =====================

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
            # 构建 SDK 策略 + 安全配置
            kwargs: dict = {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }
            self._inject_policy_config(kwargs)
            self._inject_security_config(kwargs)

            self._channel = FeishuChannel(**kwargs)

            # 注册 SDK 事件(日志只输出脱敏的状态和错误类别)
            self._channel.on("message", self._on_message)
            self._channel.on("error", self._on_error)
            self._channel.on("reconnecting", lambda *a: print("  [feishu] reconnecting"))
            self._channel.on("reconnected", lambda *a: print("  [feishu] reconnected"))

            await self._channel.connect_until_ready(timeout=30)
            self._running = True
            return True
        except Exception as exc:
            # 脱敏:不输出 app_secret / 完整异常
            print(f"  [feishu] connect failed: {type(exc).__name__}")
            self._channel = None
            return False

    def _inject_policy_config(self, kwargs: dict) -> None:
        """尝试把私聊 / 群聊访问策略 + require_mention 传给 SDK。"""
        try:
            from lark_channel import PolicyConfig
            kwargs["policy"] = PolicyConfig(
                dm_enabled=True,
                group_enabled=True,
                require_mention=self.require_mention,
            )
        except (ImportError, TypeError):
            # SDK 没有 PolicyConfig 或签名不同 → 跳过,靠 adapter 自身白名单兜底
            pass

    def _inject_security_config(self, kwargs: dict) -> None:
        """尝试把安全模式传给 SDK。"""
        try:
            from lark_channel import SecurityConfig
            kwargs["security"] = SecurityConfig(mode=self.security_mode)
        except (ImportError, TypeError):
            pass

    async def disconnect(self):
        self._running = False
        if self._channel:
            try:
                await self._channel.disconnect()
            except Exception:
                pass
            self._channel = None

    # ===================== 消息入站 =====================

    async def _on_message(self, msg):
        """SDK message 回调 → 翻译成 MessageEvent。"""
        # 基础字段校验:缺 message_id / chat_id / sender_id 直接忽略
        msg_id = getattr(msg, "message_id", "") or ""
        chat_id = getattr(msg, "chat_id", "") or ""
        sender_id = getattr(msg, "sender_id", "") or ""
        if not msg_id or not chat_id or not sender_id:
            return

        # 去重
        if msg_id in self._seen:
            return
        self._seen.add(msg_id)
        # 防止 set 无限增长(超过 5000 条时清一半)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[2500:])

        # 第一阶段只接受文本消息
        raw_type = getattr(msg, "raw_content_type", "") or ""
        if raw_type and raw_type != "text":
            return

        text = getattr(msg, "content_text", "") or ""
        if not text.strip():
            return

        chat_type_raw = getattr(msg, "chat_type", "p2p")
        chat_type = "dm" if chat_type_raw == "p2p" else chat_type_raw

        # 群聊 / topic 默认仅响应 @机器人
        if (
            chat_type in ("group", "topic")
            and self.require_mention
            and not getattr(msg, "mentioned_bot", False)
        ):
            return

        sender = getattr(msg, "sender", None)
        union_id = getattr(sender, "union_id", None) if sender else None

        # 白名单(adapter 自身保留最终校验,不完全依赖 SDK PolicyConfig)
        if not self._is_allowed(sender_id, chat_id):
            return

        thread_id = None
        conversation = getattr(msg, "conversation", None)
        if conversation:
            thread_id = getattr(conversation, "thread_id", None)

        event = MessageEvent(
            message_id=msg_id,
            text=text,
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=self.PLATFORM,
                account_id=self.app_id,
                chat_id=chat_id,
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
                "raw_content_type": raw_type,
            },
        )
        await self.handle_message(event)

    async def _on_error(self, err, *args):
        """SDK error 事件:只输出脱敏的错误类别。"""
        err_type = type(err).__name__ if err else "Unknown"
        print(f"  [feishu] SDK error: {err_type}")

    def _is_allowed(self, user_id: str, chat_id: str) -> bool:
        """白名单检查(adapter 自身保留,不完全依赖 SDK)。"""
        if self.allow_all:
            return True
        if user_id and user_id in self.allowed_users:
            return True
        if chat_id and chat_id in self.allowed_chats:
            return True
        return False

    # ===================== 消息出站 =====================

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        if not self._channel:
            return SendResult(success=False, error="not_connected")

        # 长文本分片
        chunks = self._split_text(content, FEISHU_UTF16_LIMIT)

        # 保存原始 reply_to:所有分片都回复原始消息(不复写到前一分片)
        original_reply_to = reply_to_message_id
        last_result = SendResult(success=True)

        for i, chunk in enumerate(chunks):
            # 每个分片生成唯一 uuid,重试时复用同一 uuid 防重复发送
            chunk_uuid = str(uuid.uuid4())

            # 第一段回复原始消息;后续分片不再 reply(避免串成链),
            # 仅在 thread 场景用 reply_in_thread 让后续分片落在同一线程。
            opts: dict = {
                "receive_id_type": "chat_id",
                "uuid": chunk_uuid,
            }
            if i == 0 and original_reply_to:
                opts["reply_to"] = original_reply_to
            if thread_id:
                opts["reply_in_thread"] = True

            result = await self._send_with_retry(chat_id, chunk, opts, chunk_uuid)
            if not result.success:
                return result
            last_result = result

        return last_result

    async def _send_with_retry(
        self,
        chat_id: str,
        chunk: str,
        opts: dict,
        chunk_uuid: str,
    ) -> SendResult:
        """单分片发送 + 有限重试(指数退避)。

        uuid 在所有重试轮次中保持不变,避免 SDK 侧重复投递。
        """
        last_error = "unknown"

        for attempt in range(self.send_max_retries + 1):
            # 每轮重试用同一个 uuid,SDK 据此做幂等
            opts["uuid"] = chunk_uuid

            try:
                result = await self._channel.send(
                    chat_id,
                    {"text": chunk},
                    opts,
                )
            except Exception as exc:
                last_error = type(exc).__name__
                # 异常类型判断是否可恢复
                if not self._is_retryable_error_str(last_error.lower()):
                    return SendResult(success=False, error=last_error)
            else:
                # SDK 返 None → 失败(不是成功)
                if result is None:
                    last_error = "sdk_returned_none"
                elif getattr(result, "success", True) is False:
                    err_str = str(getattr(result, "error", "") or "unknown")
                    last_error = err_str
                    if not self._is_retryable_error_str(err_str.lower()):
                        # 不可恢复错误(permission_denied / format_error 等)直接返
                        return SendResult(success=False, error=err_str)
                else:
                    mid = getattr(result, "message_id", None)
                    return SendResult(success=True, message_id=mid)

            # 退避后重试
            if attempt < self.send_max_retries:
                delay = self.send_retry_base_delay * (2 ** attempt)
                await asyncio.sleep(delay)

        return SendResult(success=False, error=f"max_retries_exceeded: {last_error}")

    @staticmethod
    def _is_retryable_error_str(err_lower: str) -> bool:
        """判断错误是否可恢复(值得重试)。"""
        for keyword in _RETRYABLE_SEND_ERRORS:
            if keyword in err_lower:
                return True
        return False

    @staticmethod
    def _split_text(text: str, max_units: int) -> list[str]:
        """按 UTF-16 code units 安全分片,不截断 Unicode 字符。"""
        if utf16_len(text) <= max_units:
            return [text]
        chunks: list[str] = []
        remaining = text
        while utf16_len(remaining) > max_units:
            chunk = truncate_utf16(remaining, max_units)
            chunks.append(chunk)
            remaining = remaining[len(chunk):]
        if remaining:
            chunks.append(remaining)
        return chunks or [text]
