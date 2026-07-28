"""平台无关的 steer 邮箱与文本格式化工具。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock


STEER_GUIDANCE_START = "[OUT_OF_BAND_USER_GUIDANCE]"
STEER_GUIDANCE_END = "[/OUT_OF_BAND_USER_GUIDANCE]"


@dataclass(frozen=True)
class SteerEntry:
    """一条与平台无关的 steer 条目。"""

    steer_id: str
    text: str
    sequence: int | None = None


class SteerMailbox:
    """在线程间传递运行中用户引导的短生命周期邮箱。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state = "new"
        self._pending: deque[SteerEntry] = deque()
        self._pending_ids: set[str] = set()

    def activate(self) -> None:
        """将新邮箱激活；已使用过的邮箱不能被静默复用。"""
        with self._lock:
            if self._state != "new":
                raise RuntimeError(
                    f"SteerMailbox cannot be activated from state {self._state!r}"
                )
            self._state = "active"

    def submit(self, entry: SteerEntry) -> bool:
        """提交一条 steer，并在邮箱未激活或已关闭时拒绝。"""
        if not isinstance(entry, SteerEntry):
            return False
        if (
            not isinstance(entry.steer_id, str)
            or not entry.steer_id.strip()
            or not isinstance(entry.text, str)
            or not entry.text.strip()
            or (
                entry.sequence is not None
                and (
                    isinstance(entry.sequence, bool)
                    or not isinstance(entry.sequence, int)
                    or entry.sequence <= 0
                )
            )
        ):
            return False
        normalized = SteerEntry(
            steer_id=entry.steer_id,
            text=entry.text.strip(),
            sequence=entry.sequence,
        )
        with self._lock:
            if (
                self._state != "active"
                or normalized.steer_id in self._pending_ids
            ):
                return False
            self._pending.append(normalized)
            self._pending_ids.add(normalized.steer_id)
            return True

    @property
    def is_active(self) -> bool:
        """返回 mailbox 当前是否仍接受 steer。"""
        with self._lock:
            return self._state == "active"

    def drain(self) -> tuple[SteerEntry, ...]:
        """原子取出并清空当前待处理的 steer。"""
        with self._lock:
            messages = tuple(self._pending)
            self._pending.clear()
            self._pending_ids.clear()
            return messages

    def close(self) -> None:
        """关闭邮箱但保留尚未消费的 steer。"""
        with self._lock:
            self._state = "closed"

    def restore_front(self, messages: tuple[SteerEntry, ...]) -> None:
        """将已取出的 steer 按原顺序放回队首。"""
        if not messages:
            return
        restored = tuple(messages)
        if any(not isinstance(message, SteerEntry) for message in restored):
            raise TypeError("restored steer messages must be SteerEntry values")
        if any(
            not isinstance(message.steer_id, str)
            or not message.steer_id.strip()
            or not isinstance(message.text, str)
            or not message.text.strip()
            or (
                message.sequence is not None
                and (
                    isinstance(message.sequence, bool)
                    or not isinstance(message.sequence, int)
                    or message.sequence <= 0
                )
            )
            for message in restored
        ):
            raise ValueError("restored steer entries are invalid")
        with self._lock:
            if self._state != "active":
                raise RuntimeError(
                    "SteerMailbox cannot restore messages after it is closed"
                )
            restored_ids = {message.steer_id for message in restored}
            if len(restored_ids) != len(restored) or restored_ids & self._pending_ids:
                raise ValueError("restored steer entries contain duplicate ids")
            self._pending.extendleft(reversed(restored))
            self._pending_ids.update(restored_ids)

    def close_and_drain(self) -> tuple[SteerEntry, ...]:
        """原子关闭邮箱并返回尚未消费的 steer。"""
        with self._lock:
            self._state = "closed"
            messages = tuple(self._pending)
            self._pending.clear()
            self._pending_ids.clear()
            return messages


def merge_steer_messages(messages: tuple[SteerEntry, ...]) -> str | None:
    """按到达顺序合并待重新排队的 steer。"""
    if not messages:
        return None
    return "\n\n".join(message.text for message in messages)


def format_steer_guidance(messages: tuple[SteerEntry, ...]) -> str:
    """将多条 steer 格式化为模型可识别的越界用户引导块。"""
    if not messages:
        return ""
    merged = "\n\n".join(message.text for message in messages)
    return (
        f"{STEER_GUIDANCE_START}\n"
        "The user sent the following guidance while you were working:\n"
        f"{merged}\n"
        f"{STEER_GUIDANCE_END}"
    )
