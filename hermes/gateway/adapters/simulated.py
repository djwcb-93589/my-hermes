"""SimulatedAdapter: replays a scripted message list. Used by --simulate mode."""

from __future__ import annotations

import asyncio

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.text_utils import MessageDeduplicator, TextBatcher
from hermes.gateway.types import MessageEvent, SendResult, SessionSource, build_session_key


class SimulatedAdapter(BasePlatformAdapter):
    """
    Replays a list of scripted messages, then stops.

    Useful for testing text batching, deduplication, and the full
    Gateway pipeline without any real platform connection.
    """

    def __init__(self, messages: list[dict] | None = None):
        super().__init__("simulated")
        self._dedup = MessageDeduplicator()
        self._batcher: TextBatcher | None = None
        self._replies: list[tuple[str, str]] = []  # (chat_id, content) log

        # 默认脚本：演示分片合并
        self._script = messages or [
            {"text": "你好，帮我查个东西", "user": "alice", "delay": 0},
            {"text": "这是一段很长的文本" + "。" * 500, "user": "bob", "delay": 1.0},
            {"text": "这是被拆开的第二部分", "user": "bob", "delay": 0.1},
            {"text": "你好", "user": "alice", "delay": 1.0, "msg_id": "dup_001"},
            {"text": "你好", "user": "alice", "delay": 0.05, "msg_id": "dup_001"},
        ]

    async def connect(self) -> bool:
        self._running = True
        self._batcher = TextBatcher(callback=self.handle_message)
        asyncio.create_task(self._replay_script())
        return True

    async def disconnect(self):
        self._running = False

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        self._replies.append((chat_id, content))
        print(f"\n[simulated] Reply to {chat_id}: {content[:120]}...\n")
        return SendResult(success=True)

    async def _replay_script(self):
        """Play scripted messages with specified delays."""
        for i, msg in enumerate(self._script):
            if not self._running:
                break
            await asyncio.sleep(msg.get("delay", 0.5))

            msg_id = msg.get("msg_id", f"sim_{i}")
            if self._dedup.is_duplicate(msg_id):
                print(f"  [simulated] dedup: skipped {msg_id}")
                continue

            event = MessageEvent(
                message_id=msg_id,
                text=msg["text"],
                source=SessionSource(
                    platform="simulated",
                    chat_id=msg.get("user", "user1"),
                    chat_type="dm",
                    user_id=msg.get("user", "user1"),
                    user_name=msg.get("user", "user1"),
                ),
            )

            session_key = build_session_key(event.source)
            await self._batcher.enqueue(session_key, event.text, event)

        await asyncio.sleep(3.0)
        self._running = False
