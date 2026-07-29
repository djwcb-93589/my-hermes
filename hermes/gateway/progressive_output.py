"""Gateway 单次运行的渐进式回复缓冲与节流控制。"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Callable

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.types import MessageEvent, SendResult
from hermes.model_streaming import StreamEvent


@dataclass(frozen=True)
class ProgressiveOutputConfig:
    """渐进式输出配置；默认关闭以保持原有一次性发送行为。"""

    enabled: bool = False
    initial_delay_seconds: float = 0.3
    min_update_interval_seconds: float = 1.0
    min_chars_delta: int = 24
    max_intermediate_edits: int = 18


def _load_nonnegative_number(
    config: dict,
    name: str,
    default: float,
) -> float:
    value = config.get(name, default)
    if isinstance(value, bool):
        raise ValueError(
            f"gateway.progressive_output.{name} must be a non-negative number"
        )
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"gateway.progressive_output.{name} must be a non-negative number"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(
            f"gateway.progressive_output.{name} must be a non-negative number"
        )
    return parsed


def _load_nonnegative_integer(
    config: dict,
    name: str,
    default: int,
    *,
    maximum: int | None = None,
) -> int:
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"gateway.progressive_output.{name} must be a non-negative integer"
        )
    if value < 0 or (maximum is not None and value > maximum):
        suffix = (
            f" no greater than {maximum}"
            if maximum is not None
            else ""
        )
        raise ValueError(
            f"gateway.progressive_output.{name} must be a non-negative "
            f"integer{suffix}"
        )
    return value


def load_progressive_output_config(
    gateway_config: dict,
) -> ProgressiveOutputConfig:
    """读取并严格校验 Gateway 渐进式输出配置。"""
    if not isinstance(gateway_config, dict):
        raise ValueError("gateway must be a mapping")
    raw = gateway_config.get("progressive_output", {})
    if not isinstance(raw, dict):
        raise ValueError("gateway.progressive_output must be a mapping")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            "gateway.progressive_output.enabled must be a boolean"
        )
    return ProgressiveOutputConfig(
        enabled=enabled,
        initial_delay_seconds=_load_nonnegative_number(
            raw,
            "initial_delay_seconds",
            0.3,
        ),
        min_update_interval_seconds=_load_nonnegative_number(
            raw,
            "min_update_interval_seconds",
            1.0,
        ),
        min_chars_delta=_load_nonnegative_integer(
            raw,
            "min_chars_delta",
            24,
        ),
        # 飞书单条消息最多编辑 20 次；保留最终编辑和平台余量。
        max_intermediate_edits=_load_nonnegative_integer(
            raw,
            "max_intermediate_edits",
            18,
            maximum=18,
        ),
    )


class ProgressiveReplyController:
    """合并一个 Agent run 的正文事件，并串行维护一条平台草稿。"""

    def __init__(
        self,
        *,
        route_key: str,
        generation: int,
        event: MessageEvent,
        adapter: BasePlatformAdapter,
        config: ProgressiveOutputConfig,
        generation_is_valid: Callable[[], bool],
    ):
        self.route_key = route_key
        self.generation = generation
        self.event = event
        self.adapter = adapter
        self.config = config
        self._generation_is_valid = generation_is_valid

        self._attempt_id: str | None = None
        self._attempt_text = ""
        self._attempt_accepting = False
        self._first_text_at: float | None = None
        self._draft_message_id: str | None = None
        self._last_sent_text = ""
        self._last_sent_attempt_id: str | None = None
        self._last_update_at: float | None = None
        self._edit_count = 0
        self._wakeup = asyncio.Event()
        self._closed = False
        self._aborted = False
        self._finalized = False
        self._update_task = asyncio.create_task(self._run_updates())

    @property
    def draft_message_id(self) -> str | None:
        if self._aborted:
            return None
        return self._draft_message_id

    @property
    def has_draft(self) -> bool:
        return self.draft_message_id is not None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def aborted(self) -> bool:
        return self._aborted

    def _generation_valid(self) -> bool:
        try:
            return bool(self._generation_is_valid())
        except Exception:
            return False

    def feed(self, event: StreamEvent) -> None:
        """只更新内存并唤醒后台任务，不执行任何平台网络请求。"""
        if self._closed or self._aborted:
            return
        if not self._generation_valid():
            self._abort_locally(cancel_task=True)
            return
        if not isinstance(event, StreamEvent):
            return

        event_type = event.event_type
        attempt_id = str(event.attempt_id or "")
        if event_type == "reasoning_delta":
            return
        if event_type == "model_turn_started":
            if not attempt_id:
                return
            self._attempt_id = attempt_id
            self._attempt_text = ""
            self._attempt_accepting = True
            self._first_text_at = None
            return
        if event_type == "text_delta":
            if (
                not attempt_id
                or attempt_id != self._attempt_id
                or not self._attempt_accepting
                or not isinstance(event.delta, str)
                or not event.delta
            ):
                return
            if self._first_text_at is None:
                self._first_text_at = time.monotonic()
            self._attempt_text += event.delta
            self._wakeup.set()
            return
        if event_type == "model_turn_interrupted":
            if attempt_id == self._attempt_id:
                self._attempt_id = None
                self._attempt_text = ""
                self._attempt_accepting = False
                self._first_text_at = None
                self._wakeup.set()
            return
        if event_type == "model_turn_completed":
            # 最终内容只能由持久化 Outbox 提供，此处不做收口。
            if attempt_id == self._attempt_id:
                self._attempt_accepting = False
            return

    def _abort_locally(self, *, cancel_task: bool) -> None:
        self._aborted = True
        self._closed = True
        self._wakeup.set()
        message_id = self._draft_message_id
        self._draft_message_id = None
        if message_id:
            try:
                self.adapter.release_progressive_reply(message_id)
            except Exception:
                pass
        task = self._update_task
        if cancel_task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _update_plan(
        self,
    ) -> tuple[str, str, bool, float] | None:
        attempt_id = self._attempt_id
        text = self._attempt_text
        first_text_at = self._first_text_at
        if not attempt_id or not text or first_text_at is None:
            return None
        if (
            attempt_id == self._last_sent_attempt_id
            and text == self._last_sent_text
        ):
            return None

        now = time.monotonic()
        if self._draft_message_id is None:
            due_at = first_text_at + self.config.initial_delay_seconds
            return attempt_id, text, True, max(0.0, due_at - now)

        if self._edit_count >= self.config.max_intermediate_edits:
            return None
        replaces_attempt = attempt_id != self._last_sent_attempt_id
        extends_preview = (
            attempt_id == self._last_sent_attempt_id
            and text.startswith(self._last_sent_text)
        )
        added_chars = (
            len(text) - len(self._last_sent_text)
            if extends_preview
            else len(text)
        )
        if (
            not replaces_attempt
            and added_chars < self.config.min_chars_delta
        ):
            return None
        due_at = (
            (self._last_update_at or now)
            + self.config.min_update_interval_seconds
        )
        return attempt_id, text, False, max(0.0, due_at - now)

    async def _wait_until_due(self, delay: float) -> bool:
        if delay <= 0:
            return True
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return True
        self._wakeup.clear()
        return False

    async def _run_updates(self) -> None:
        try:
            while not self._closed:
                await self._wakeup.wait()
                self._wakeup.clear()
                while not self._closed:
                    if not self._generation_valid():
                        self._abort_locally(cancel_task=False)
                        return
                    plan = self._update_plan()
                    if plan is None:
                        break
                    attempt_id, text, create, delay = plan
                    if not await self._wait_until_due(delay):
                        continue
                    if not self._generation_valid():
                        self._abort_locally(cancel_task=False)
                        return

                    if create:
                        result = await self.adapter.start_progressive_reply(
                            self.event,
                            text,
                        )
                    else:
                        message_id = self._draft_message_id
                        if not message_id:
                            continue
                        result = await self.adapter.update_progressive_reply(
                            message_id,
                            text,
                        )

                    if not result.success:
                        self._abort_locally(cancel_task=False)
                        return
                    if create:
                        if not result.message_id:
                            self._abort_locally(cancel_task=False)
                            return
                        self._draft_message_id = result.message_id
                    else:
                        self._edit_count += 1
                    self._last_sent_attempt_id = attempt_id
                    self._last_sent_text = text
                    self._last_update_at = time.monotonic()
                    if self._wakeup.is_set():
                        self._wakeup.clear()
                        continue
        except asyncio.CancelledError:
            raise
        except Exception:
            self._abort_locally(cancel_task=False)

    async def close(self) -> None:
        """停止接收和中间更新，但保留草稿供最终 Outbox 收口。"""
        self._closed = True
        self._wakeup.set()
        task = self._update_task
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            self._abort_locally(cancel_task=False)

    async def abort(self) -> None:
        """停止本次渐进式输出并释放 Adapter 内的草稿所有权。"""
        self._aborted = True
        await self.close()
        message_id = self._draft_message_id
        self._draft_message_id = None
        if message_id:
            try:
                self.adapter.release_progressive_reply(message_id)
            except Exception:
                pass

    async def finalize(self, prepared_payload: dict) -> SendResult:
        """用持久化 Outbox 的第一分片精确完成现有草稿。"""
        await self.close()
        message_id = self._draft_message_id
        if (
            self._aborted
            or self._finalized
            or not message_id
            or not self._generation_valid()
        ):
            await self.abort()
            return SendResult(
                success=False,
                error="progressive_reply_unavailable",
                retryable=False,
            )
        try:
            result = await self.adapter.finalize_progressive_reply(
                message_id,
                prepared_payload,
            )
        except asyncio.CancelledError:
            await self.abort()
            raise
        except Exception:
            result = SendResult(
                success=False,
                error="progressive_reply_finalize_failed",
                retryable=False,
            )
        if not result.success:
            await self.abort()
            return result
        if not result.message_id:
            result.message_id = message_id
        self._finalized = True
        self._draft_message_id = None
        return result
