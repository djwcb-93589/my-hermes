"""平台无关的 steer 邮箱与文本格式化工具。"""

from __future__ import annotations

from collections import deque
from threading import Lock


STEER_GUIDANCE_START = "[OUT_OF_BAND_USER_GUIDANCE]"
STEER_GUIDANCE_END = "[/OUT_OF_BAND_USER_GUIDANCE]"


class SteerMailbox:
    """在线程间传递运行中用户引导的短生命周期邮箱。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state = "new"
        self._pending: deque[str] = deque()

    def activate(self) -> None:
        """将新邮箱激活；已使用过的邮箱不能被静默复用。"""
        with self._lock:
            if self._state != "new":
                raise RuntimeError(
                    f"SteerMailbox cannot be activated from state {self._state!r}"
                )
            self._state = "active"

    def submit(self, text: str) -> bool:
        """提交一条 steer，并在邮箱未激活或已关闭时拒绝。"""
        if not isinstance(text, str):
            return False
        normalized = text.strip()
        if not normalized:
            return False
        with self._lock:
            if self._state != "active":
                return False
            self._pending.append(normalized)
            return True

    def drain(self) -> tuple[str, ...]:
        """原子取出并清空当前待处理的 steer。"""
        with self._lock:
            messages = tuple(self._pending)
            self._pending.clear()
            return messages

    def restore_front(self, messages: tuple[str, ...]) -> None:
        """将已取出的 steer 按原顺序放回队首。"""
        if not messages:
            return
        restored = tuple(messages)
        if any(not isinstance(message, str) for message in restored):
            raise TypeError("restored steer messages must be strings")
        with self._lock:
            if self._state != "active":
                raise RuntimeError(
                    "SteerMailbox cannot restore messages after it is closed"
                )
            self._pending.extendleft(reversed(restored))

    def close_and_drain(self) -> tuple[str, ...]:
        """原子关闭邮箱并返回尚未消费的 steer。"""
        with self._lock:
            self._state = "closed"
            messages = tuple(self._pending)
            self._pending.clear()
            return messages


def merge_steer_messages(messages: tuple[str, ...]) -> str | None:
    """按到达顺序合并待重新排队的 steer。"""
    if not messages:
        return None
    return "\n\n".join(messages)


def format_steer_guidance(messages: tuple[str, ...]) -> str:
    """将多条 steer 格式化为模型可识别的越界用户引导块。"""
    merged = merge_steer_messages(messages)
    if merged is None:
        return ""
    return (
        f"{STEER_GUIDANCE_START}\n"
        "The user sent the following guidance while you were working:\n"
        f"{merged}\n"
        f"{STEER_GUIDANCE_END}"
    )
