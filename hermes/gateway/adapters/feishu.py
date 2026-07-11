"""
飞书 adapter:基于 ``lark-channel-sdk``,第一阶段仅支持文本。

特性:
  - 私聊 / 群聊 / 话题(PolicyConfig: dm_policy / group_policy / require_mention)
  - 群聊默认仅响应 @机器人
  - open_id + union_id
  - reply / thread 路由
  - SDK 内部发送重试(OutboundConfig: max_attempts / backoff / jitter)
  - SDK 结构化错误码(error.code / error.retryable)
  - 长文本分片(UTF-16 安全,不截断 Unicode)
  - allowed_users / allowed_chats / allow_all 白名单(adapter 自身 + SDK 双层)
  - 安全模式 compat / audit / strict(SecurityConfig)
  - MessageDeduplicator 二次去重(主依赖 SDK 去重)

不实现:图片、文件、语音、卡片、流式回复。
"""

from __future__ import annotations

import uuid

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.text_utils import MessageDeduplicator, utf16_len, truncate_utf16
from hermes.gateway.types import MessageEvent, MessageType, SendResult, SessionSource


FEISHU_UTF16_LIMIT = 30000  # 飞书单条文本 UTF-16 code units 上限

# 脱敏后允许暴露给上层 / 日志的错误码白名单
_SAFE_ERROR_CODES = frozenset({
    "rate_limited", "permission_denied", "send_timeout",
    "not_connected", "format_error",
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
        # send_max_retries 是 SDK 的"总尝试次数",不是"额外重试次数"。
        # 小于 1 时校正为 1(至少尝试一次)。
        self.send_max_retries = max(1, int(send_max_retries))
        # 退避基础时间不能为负
        self.send_retry_base_delay = max(0.0, float(send_retry_base_delay))
        self._channel = None
        self._dedup = MessageDeduplicator(max_size=5000)

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
            kwargs: dict = {
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }
            # 注入 SDK 配置:任何构造失败(TypeError / ImportError)直接抛,
            # connect 的 except 捕获后返回 False —— 不静默吞掉错误配置
            self._inject_policy_config(kwargs)
            self._inject_security_config(kwargs)
            self._inject_outbound_config(kwargs)

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
            # 脱敏:不输出 app_secret / 完整 traceback
            print(f"  [feishu] connect failed: {type(exc).__name__}")
            self._channel = None
            return False

    # ----- SDK 配置注入(失败时 raise,不静默吞) -----

    def _inject_policy_config(self, kwargs: dict) -> None:
        """用 SDK 当前字段构建 PolicyConfig。

        策略:
          - allow_all=True → dm_policy / group_policy = "open"
          - allow_all=False:
            - 有 allowed_users → dm_policy = "allowlist", allow_from = [...]
            - 无 allowed_users → dm_policy = "disabled"(不意外放开)
            - 群聊同理(group_policy / group_allowlist)
          - require_mention 透传

        构造失败(TypeError / ImportError)直接抛,不静默降级。
        Adapter 自身仍保留最终白名单校验(见 _is_allowed)。
        """
        from lark_channel import PolicyConfig

        if self.allow_all:
            dm_policy = "open"
            group_policy = "open"
        else:
            dm_policy = "allowlist" if self.allowed_users else "disabled"
            group_policy = "allowlist" if self.allowed_chats else "disabled"

        kwargs["policy"] = PolicyConfig(
            dm_policy=dm_policy,
            group_policy=group_policy,
            require_mention=self.require_mention,
            allow_from=list(self.allowed_users) if self.allowed_users else None,
            group_allowlist=list(self.allowed_chats) if self.allowed_chats else None,
        )

    def _inject_security_config(self, kwargs: dict) -> None:
        """注入安全模式(compat / audit / strict)。"""
        from lark_channel import SecurityConfig
        kwargs["security"] = SecurityConfig(mode=self.security_mode)

    def _inject_outbound_config(self, kwargs: dict) -> None:
        """注入 SDK 发送重试策略(总尝试次数 + 退避 + jitter)。

        SDK 内部负责重试,Adapter 不再在外层循环调 channel.send()。
        尝试 OutboundConfig,SDK 若改名则用 RetryConfig 兜底。
        """
        attempts = self.send_max_retries
        base = self.send_retry_base_delay
        max_delay = 60.0  # 合理上限,避免指数退避等太久

        try:
            from lark_channel import OutboundConfig
            kwargs["outbound"] = OutboundConfig(
                max_attempts=attempts,
                base_delay_seconds=base,
                max_delay_seconds=max_delay,
                jitter=True,
            )
        except ImportError:
            from lark_channel import RetryConfig
            kwargs["retry"] = RetryConfig(
                max_attempts=attempts,
                base_delay_seconds=base,
                max_delay_seconds=max_delay,
                jitter=True,
            )

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
        msg_id = getattr(msg, "message_id", "") or ""
        chat_id = getattr(msg, "chat_id", "") or ""
        sender_id = getattr(msg, "sender_id", "") or ""
        if not msg_id or not chat_id or not sender_id:
            return

        # 二次去重(主依赖 SDK;这里用 MessageDeduplicator 做有限容量保护)
        if self._dedup.is_duplicate(msg_id):
            return

        # 第一阶段:严格只处理文本消息
        raw_type = getattr(msg, "raw_content_type", "") or ""
        if raw_type != "text":
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

        # Adapter 自身保留最终白名单校验(不完全依赖 SDK PolicyConfig)
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

        chunks = self._split_text(content, FEISHU_UTF16_LIMIT)
        original_reply_to = reply_to_message_id
        last_result = SendResult(success=True)

        for i, chunk in enumerate(chunks):
            # 每个分片一个固定 UUID,SDK 内部重试时复用同一 UUID 做幂等
            chunk_uuid = str(uuid.uuid4())

            opts: dict = {
                "receive_id_type": "chat_id",
                "uuid": chunk_uuid,
            }
            # 第一段回复原始消息;后续分片不再 reply(避免机器人自回复链)
            # thread 场景下所有分片保持在同一线程(reply_in_thread=True)
            if i == 0 and original_reply_to:
                opts["reply_to"] = original_reply_to
            if thread_id:
                opts["reply_in_thread"] = True

            result = await self._send_once(chat_id, chunk, opts)
            if not result.success:
                return result
            last_result = result

        return last_result

    async def _send_once(
        self,
        chat_id: str,
        chunk: str,
        opts: dict,
    ) -> SendResult:
        """单次发送(SDK 内部已做重试,这里不再外层循环)。"""
        try:
            result = await self._channel.send(
                chat_id,
                {"text": chunk},
                opts,
            )
        except Exception:
            return SendResult(success=False, error="unknown")

        # SDK 返 None → 失败
        if result is None:
            return SendResult(success=False, error="unknown")

        if getattr(result, "success", True) is False:
            # 读取 SDK 结构化错误对象,转换为脱敏错误码
            error_obj = getattr(result, "error", None)
            code = self._sanitize_error_code(error_obj)
            return SendResult(success=False, error=code)

        mid = getattr(result, "message_id", None)
        return SendResult(success=True, message_id=mid)

    @staticmethod
    def _sanitize_error_code(error_obj) -> str:
        """把 SDK 错误对象转成脱敏错误码。

        只返回白名单内的 code(rate_limited / permission_denied / send_timeout
        / not_connected / format_error),不输出 hint / raw / 完整响应体。
        未知 code 统一返 "unknown"。
        """
        code = getattr(error_obj, "code", None) if error_obj else None
        if not code:
            return "unknown"
        code_lower = str(code).lower()
        return code_lower if code_lower in _SAFE_ERROR_CODES else "unknown"

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
