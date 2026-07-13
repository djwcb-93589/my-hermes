"""
平台 adapter 基类。

子类实现 connect / disconnect / send。send 统一返回 ``SendResult``,
让上层(GatewayRunner)能判断发送是否成功并决定是否重试。
_on_message 由 GatewayRunner 启动时注入。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from hermes.gateway.types import MessageEvent, SendResult


class BasePlatformAdapter(ABC):
    """所有平台 adapter 必须满足的契约。

    子类实现:
      - ``connect()``     开始接收消息,返 True/False
      - ``disconnect()``  停止 + 清理
      - ``send(...)``     发回复,返 ``SendResult``
    """

    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self._on_message: Callable | None = None  # GatewayRunner 注入
        self._running = False

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
