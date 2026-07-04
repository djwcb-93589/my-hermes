"""ConsoleAdapter: stdin/stdout adapter for testing the full Gateway pipeline."""

from __future__ import annotations

import asyncio
import uuid

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.types import MessageEvent, SessionSource


class ConsoleAdapter(BasePlatformAdapter):
    """
    A trivial adapter that reads from stdin.

    This lets you test the full Gateway pipeline without any external platform.
    Every line you type becomes a MessageEvent from user 'console_user'.
    """

    def __init__(self):
        super().__init__("console")
        self._task: asyncio.Task | None = None

    async def connect(self) -> bool:
        self._running = True
        self._task = asyncio.create_task(self._read_loop())
        return True

    async def disconnect(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def send(self, chat_id: str, content: str) -> bool:
        print(f"\n[{self.platform_name}] Assistant: {content}\n")
        return True

    async def _read_loop(self):
        """Read lines from stdin in a thread, post as MessageEvents."""
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(
                    None, lambda: input("[console] You: ")
                )
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                self._running = False
                break

            event = MessageEvent(
                message_id=str(uuid.uuid4())[:8],
                text=line,
                source=SessionSource(
                    platform="console",
                    chat_id="console_user",
                    chat_type="dm",
                    user_id="console_user",
                    user_name="Console User",
                ),
            )
            await self.handle_message(event)
