"""
Platform adapter base class.

Subclasses implement connect / disconnect / send. _on_message is injected by
GatewayRunner at startup so adapters don't need to know about the runner.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from hermes.gateway.types import MessageEvent


class BasePlatformAdapter(ABC):
    """
    The contract every platform adapter must fulfil.

    Subclasses implement:
      - connect()      开始接收消息
      - disconnect()   停止
      - send()         把回复发回平台
    """

    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self._on_message: Callable | None = None  # injected by GatewayRunner
        self._running = False

    @abstractmethod
    async def connect(self) -> bool:
        """Start receiving messages. Return True if successful."""
        ...

    @abstractmethod
    async def disconnect(self):
        """Stop receiving messages and clean up."""
        ...

    @abstractmethod
    async def send(self, chat_id: str, content: str) -> bool:
        """Send a reply to the given chat. Return True if successful."""
        ...

    async def handle_message(self, event: MessageEvent):
        """Forward a translated event to the GatewayRunner callback."""
        if self._on_message:
            await self._on_message(event)
