"""
平台 adapter 基类。

Adapter 生命周期分为 initialize / restore_pending / start_receiving / disconnect。
旧 Adapter 的 start_receiving 默认复用 connect,保持向后兼容。send 统一返回
``SendResult``,让上层(GatewayRunner)能判断发送是否成功并决定是否重试。
消息回调和持久状态查询由 GatewayRunner 启动时注入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from hermes.gateway.types import MessageEvent, SendResult


class BasePlatformAdapter(ABC):
    """所有平台 adapter 必须满足的契约。

    分阶段契约:
      - ``initialize()``       初始化发送端和本地资源,不得接收外部新事件
      - ``restore_pending()``  恢复平台自己的 Inbox
      - ``start_receiving()``  开始接收外部新事件
      - ``disconnect()``       停止 + 清理

    旧子类只需继续实现 ``connect()``；默认 ``start_receiving()`` 会调用它。
    """

    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self._on_message: Callable | None = None  # GatewayRunner 注入
        self._message_state_lookup: Callable | None = None
        self._running = False

    async def initialize(self) -> bool:
        """初始化资源但不开始接收；旧 Adapter 默认无需单独初始化。"""
        return True

    async def restore_pending(self) -> None:
        """恢复平台 Inbox；没有独立 Inbox 的 Adapter 默认无操作。"""

    async def start_receiving(self) -> bool:
        """开始接收外部事件；旧 Adapter 兼容调用原 ``connect()``。"""
        return await self.connect()

    @abstractmethod
    async def connect(self) -> bool:
        ...

    @abstractmethod
    async def disconnect(self):
        ...

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        ...

    def prepare_outbound(
        self,
        content: str,
        *,
        delivery_id: str,
    ) -> list[dict]:
        """把回复转换成可持久化的确定分片。

        普通 adapter 默认只生成一个纯文本分片。平台有自己的消息格式或
        长度限制时覆盖该方法,但不能在这里执行网络请求。
        """
        return [{"content": content}]

    async def send_prepared(
        self,
        chat_id: str,
        payload: dict,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        """发送一个已准备分片;默认复用现有 ``send`` 实现。"""
        return await self.send(
            chat_id,
            str(payload.get("content", "")),
            reply_to_message_id=reply_to_message_id,
            thread_id=thread_id,
        )

    async def handle_message(self, event: MessageEvent):
        """把翻译好的 event 转发给 GatewayRunner 回调。"""
        if self._on_message:
            await self._on_message(event)

    def persisted_message_state(self, event: MessageEvent) -> dict | None:
        """查询该平台消息是否已经进入 Gateway queue / Outbox。"""
        if self._message_state_lookup is None:
            return None
        return self._message_state_lookup(event)
