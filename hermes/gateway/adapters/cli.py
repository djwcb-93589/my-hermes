"""
CLIAdapter:本地终端入口,走 GatewayRunner 统一管线。

支持:
  - 普通文本 → MessageEvent
  - /new /stop /status /quit
  - Agent 运行期间 Ctrl+C → 取消当前任务(不退出)
  - 空闲时 Ctrl+C → 退出程序
"""

from __future__ import annotations

import asyncio
import uuid

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.types import MessageEvent, SendResult, SessionSource


class CLIAdapter(BasePlatformAdapter):
    """stdin/stdout adapter。"""

    PLATFORM = "cli"

    def __init__(self):
        super().__init__("cli")
        self._task: asyncio.Task | None = None
        self._should_quit = asyncio.Event()

    async def connect(self) -> bool:
        self._running = True
        self._task = asyncio.create_task(self._read_loop())
        return True

    async def disconnect(self):
        self._running = False
        self._should_quit.set()
        if self._task:
            self._task.cancel()

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        print(f"\n[cli] Assistant: {content}\n")
        return SendResult(success=True)

    async def _read_loop(self):
        """从 stdin 读行(stdin 在 executor 线程跑)。"""
        loop = asyncio.get_event_loop()
        print("[cli] Connected. Type /quit to exit.\n")
        while self._running:
            try:
                line = await loop.run_in_executor(
                    None, lambda: input("[cli] You: "),
                )
            except (EOFError, asyncio.CancelledError):
                break
            except KeyboardInterrupt:
                # 空闲时的 Ctrl+C → 退出
                print("\n[cli] Interrupted at idle, exiting.")
                self._should_quit.set()
                break

            line = (line or "").strip()
            if not line:
                continue

            low = line.lower()
            if low in ("/quit", "/exit"):
                self._should_quit.set()
                break

            event = MessageEvent(
                message_id=str(uuid.uuid4())[:8],
                text=line,
                source=SessionSource(
                    platform=self.PLATFORM,
                    account_id="cli",
                    chat_id="cli_dm",
                    chat_type="dm",
                    user_id="cli_user",
                    user_name="CLI User",
                ),
            )
            await self.handle_message(event)
