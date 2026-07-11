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

    async def handle_message(self, event: MessageEvent):
        """把翻译好的 event 转发给 GatewayRunner 回调。"""
        if self._on_message:
            await self._on_message(event)
