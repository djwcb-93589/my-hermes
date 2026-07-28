"""同步 Hook Registry 及同步、异步实现共享的注册语义。"""

from __future__ import annotations

import inspect
import logging
import math
from threading import RLock

from hermes.hooks.contracts import (
    HookCallback,
    HookContext,
    HookDispatchResult,
    HookEvent,
    HookInvocationResult,
    HookName,
    HookRegistration,
    HookRegistrationError,
    normalize_hook_name,
)
from hermes.hooks.controls import (
    AddContext,
    Block,
    DEFAULT_MAX_ADD_CONTEXT_CHARACTERS,
    DEFAULT_MAX_ADD_CONTEXT_ITEMS,
    HookControlDispatchResult,
    add_context_within_budget,
    build_control_dispatch_result,
    control_error_message,
    control_failure_reason,
    normalize_control_value,
    redact_added_context_results,
)


logger = logging.getLogger(__name__)


def _normalize_hook_id(value: object) -> str:
    """校验调用方提供的 Hook 标识。"""
    if not isinstance(value, str) or not value.strip():
        raise HookRegistrationError("hook_id must be a non-empty string")
    return value.strip()


def _default_hook_id(callback: HookCallback) -> str:
    """为未显式命名的回调生成可诊断的默认标识。"""
    callback_type = type(callback)
    module = getattr(callback, "__module__", callback_type.__module__)
    name = getattr(
        callback,
        "__qualname__",
        getattr(callback, "__name__", callback_type.__qualname__),
    )
    return f"{module}.{name}"


def _normalize_timeout(value: float | None) -> float | None:
    """校验可选的单 Hook 超时配置。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HookRegistrationError("timeout_seconds must be a positive number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise HookRegistrationError("timeout_seconds must be a positive number")
    return normalized


def _normalize_add_context_budget(value: object, *, name: str) -> int:
    """校验单次控制事件的 AddContext 累计预算。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


class _HookRegistryBase:
    """集中同步和异步 Registry 共用的注册、快照和校验逻辑。"""

    def __init__(
        self,
        *,
        max_add_context_items: int = DEFAULT_MAX_ADD_CONTEXT_ITEMS,
        max_add_context_characters: int = DEFAULT_MAX_ADD_CONTEXT_CHARACTERS,
    ) -> None:
        self._registrations: dict[HookName, list[HookRegistration]] = {}
        self._lock = RLock()
        self._max_add_context_items = _normalize_add_context_budget(
            max_add_context_items,
            name="max_add_context_items",
        )
        self._max_add_context_characters = _normalize_add_context_budget(
            max_add_context_characters,
            name="max_add_context_characters",
        )

    def register(
        self,
        event_name: HookName,
        callback: HookCallback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """注册一个 Hook；同一事件中禁止重复回调或重复标识。"""
        try:
            normalized_event_name = normalize_hook_name(event_name)
        except (TypeError, ValueError) as exc:
            raise HookRegistrationError(str(exc)) from exc
        if not callable(callback):
            raise HookRegistrationError("hook callback must be callable")
        normalized_hook_id = _normalize_hook_id(
            hook_id if hook_id is not None else _default_hook_id(callback)
        )
        normalized_timeout = _normalize_timeout(timeout_seconds)
        registration = HookRegistration(
            event_name=normalized_event_name,
            hook_id=normalized_hook_id,
            callback=callback,
            timeout_seconds=normalized_timeout,
        )

        with self._lock:
            registered = self._registrations.setdefault(normalized_event_name, [])
            if any(item.callback is callback for item in registered):
                raise HookRegistrationError(
                    "hook callback is already registered for event: "
                    f"{normalized_event_name}"
                )
            if any(item.hook_id == normalized_hook_id for item in registered):
                raise HookRegistrationError(
                    "hook_id is already registered for event: "
                    f"{normalized_event_name}: {normalized_hook_id}"
                )
            registered.append(registration)
        return registration

    def registered_hooks(
        self,
        event_name: HookName,
    ) -> tuple[HookRegistration, ...]:
        """返回指定事件按注册顺序排列的不可变注册快照。"""
        normalized_event_name = normalize_hook_name(event_name)
        with self._lock:
            return tuple(self._registrations.get(normalized_event_name, ()))

    def _registrations_for(
        self,
        event: HookEvent,
    ) -> tuple[HookRegistration, ...]:
        with self._lock:
            return tuple(self._registrations.get(event.name, ()))


class SyncHookRegistry(_HookRegistryBase):
    """按注册顺序同步执行回调，并隔离单个 Hook 的失败。"""

    def register(
        self,
        event_name: HookName,
        callback: HookCallback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """注册同步 Hook；同步执行不提供不可可靠终止的超时机制。"""
        if timeout_seconds is not None:
            raise HookRegistrationError(
                "SyncHookRegistry does not support timeout_seconds"
            )
        if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)
        ):
            raise HookRegistrationError(
                "SyncHookRegistry does not support async hook callbacks"
            )
        return super().register(
            event_name,
            callback,
            hook_id=hook_id,
        )

    def emit(self, event: HookEvent) -> HookDispatchResult:
        """分发事件；未注册回调时返回空的结构化结果。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        results: list[HookInvocationResult] = []
        for registration in self._registrations_for(event):
            try:
                value = registration.callback(event.context)
            except Exception as exc:
                logger.exception(
                    "Hook failed: event=%s hook_id=%s",
                    event.name,
                    registration.hook_id,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
            else:
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=True,
                        value=value,
                    )
                )
        return HookDispatchResult(event=event, results=tuple(results))

    def emit_control(self, event: HookEvent) -> HookControlDispatchResult:
        """按顺序分发控制 Hook；失败默认阻止，显式 Block 立即短路。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        results: list[HookInvocationResult] = []
        added_context: list[str] = []
        for registration in self._registrations_for(event):
            try:
                value = registration.callback(event.context)
                control_value = normalize_control_value(event, value)
            except Exception as exc:
                logger.exception(
                    "Control Hook failed: event=%s hook_id=%s",
                    event.name,
                    registration.hook_id,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type=type(exc).__name__,
                        error_message=control_error_message(exc),
                    )
                )
                return build_control_dispatch_result(
                    event,
                    results,
                    block_reason=control_failure_reason(),
                    added_context=added_context,
                )

            if isinstance(control_value, Block):
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=True,
                        value=control_value,
                    )
                )
                return build_control_dispatch_result(
                    event,
                    results,
                    block_reason=control_value.reason,
                    added_context=added_context,
                )
            if isinstance(control_value, AddContext):
                if not add_context_within_budget(
                    added_context,
                    control_value.text,
                    max_items=self._max_add_context_items,
                    max_characters=self._max_add_context_characters,
                ):
                    results.append(
                        HookInvocationResult(
                            hook_id=registration.hook_id,
                            success=False,
                            error_type="HookControlBudgetError",
                            error_message="AddContext budget exceeded",
                        )
                    )
                    return build_control_dispatch_result(
                        event,
                        redact_added_context_results(results),
                        block_reason=control_failure_reason(),
                        added_context=[],
                    )
                added_context.append(control_value.text)
            results.append(
                HookInvocationResult(
                    hook_id=registration.hook_id,
                    success=True,
                    value=control_value,
                )
            )
        return build_control_dispatch_result(
            event,
            results,
            added_context=added_context,
        )

    def dispatch(
        self,
        event_name: HookName,
        context: HookContext,
    ) -> HookDispatchResult:
        """使用事件名称和上下文分发的便捷入口。"""
        return self.emit(HookEvent(name=event_name, context=context))


HookRegistry = SyncHookRegistry
"""同步 Registry 的简洁公开名称。"""
