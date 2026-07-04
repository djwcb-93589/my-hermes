"""
Platform text utilities: UTF-16 counting, dedup, batching.

Three concerns shared across real platform adapters:
- utf16_len / truncate_utf16  — Telegram-style length limits (surrogate pairs)
- MessageDeduplicator         — protect against RESUME / network-replay dupes
- TextBatcher                 — merge rapid message fragments before forwarding
"""

from __future__ import annotations

import asyncio
from typing import Callable

from hermes.gateway.types import MessageEvent


def utf16_len(text: str) -> int:
    """
    Count UTF-16 code units (what Telegram uses for length limits).

    大部分字符 = 1 unit，但很多 emoji = 2 units (surrogate pair)。
    Python 的 len() 返回 code points，不是 code units。
    """
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int) -> str:
    """Truncate text to fit within max_units UTF-16 code units."""
    if utf16_len(text) <= max_units:
        return text
    result = []
    total = 0
    for ch in text:
        ch_units = len(ch.encode("utf-16-le")) // 2
        if total + ch_units > max_units:
            break
        result.append(ch)
        total += ch_units
    return "".join(result)


class MessageDeduplicator:
    """
    Prevents processing the same message twice.

    Discord RESUME 和网络抖动都可能重推消息。
    用 message_id 做去重，FIFO 淘汰旧记录。
    """

    def __init__(self, max_size: int = 1000):
        self._seen: set[str] = set()
        self._order: list[str] = []
        self._max_size = max_size

    def is_duplicate(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen.add(message_id)
        self._order.append(message_id)
        if len(self._order) > self._max_size:
            old_id = self._order.pop(0)
            self._seen.discard(old_id)
        return False


class TextBatcher:
    """
    Merges rapid text chunks from the same session into one message.

    当平台客户端拆分长文本时，多条消息在毫秒级内到达。
    TextBatcher 缓冲文本片段，等安静期过后合并成一条 MessageEvent。
    """

    def __init__(self, callback: Callable):
        self._callback = callback  # async def callback(event: MessageEvent)
        self._buffers: dict[str, list[str]] = {}
        self._events: dict[str, MessageEvent] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def enqueue(
        self,
        session_key: str,
        text: str,
        event: MessageEvent,
        split_threshold: int = 3900,
    ):
        """Buffer a text chunk. Flush after quiet period."""
        if session_key not in self._buffers:
            self._buffers[session_key] = []
        self._buffers[session_key].append(text)
        self._events[session_key] = event  # keep latest event metadata

        old_task = self._tasks.get(session_key)
        if old_task and not old_task.done():
            old_task.cancel()

        delay = 2.0 if len(text) >= split_threshold else 0.6

        self._tasks[session_key] = asyncio.create_task(
            self._flush_after(session_key, delay)
        )

    async def _flush_after(self, session_key: str, delay: float):
        await asyncio.sleep(delay)

        chunks = self._buffers.pop(session_key, [])
        event = self._events.pop(session_key, None)
        self._tasks.pop(session_key, None)

        if chunks and event:
            event.text = "".join(chunks)
            await self._callback(event)
