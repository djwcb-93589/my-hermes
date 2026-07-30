"""把既有安全 HookContext 转换为旁路 Observation Sink 事件。"""

from __future__ import annotations

from collections.abc import Mapping

from hermes.hooks import (
    AsyncHookRegistry,
    HookContext,
    HookEventName,
    HookRegistration,
    SyncHookRegistry,
)
from hermes.observability.contracts import (
    ModelCallObservation,
    ObservationSink,
    RunObservation,
    ToolCallObservation,
)


def register_observation_sink(
    hook_registry: SyncHookRegistry | AsyncHookRegistry,
    sink: ObservationSink,
    *,
    hook_id_prefix: str = "observability",
) -> tuple[HookRegistration, ...]:
    """通过公开 Hook 注册接口接入不改变业务结果的旁路观察 Sink。"""
    if not isinstance(hook_registry, (SyncHookRegistry, AsyncHookRegistry)):
        raise TypeError("hook_registry must be a HookRegistry")
    for method_name in (
        "record_tool_call",
        "record_model_call",
        "record_run_end",
    ):
        if not callable(getattr(sink, method_name, None)):
            raise TypeError("sink does not implement ObservationSink")
    if not isinstance(hook_id_prefix, str) or not hook_id_prefix.strip():
        raise ValueError("hook_id_prefix must be a non-empty string")
    prefix = hook_id_prefix.strip()

    def on_tool_call(context: HookContext) -> None:
        sink.record_tool_call(_tool_call_observation(context))

    def on_model_call(context: HookContext) -> None:
        sink.record_model_call(_model_call_observation(context))

    def on_run_end(context: HookContext) -> None:
        sink.record_run_end(_run_observation(context))

    return (
        hook_registry.register(
            HookEventName.POST_TOOL_CALL.value,
            on_tool_call,
            hook_id=f"{prefix}:post_tool_call",
        ),
        hook_registry.register(
            HookEventName.POST_LLM_CALL.value,
            on_model_call,
            hook_id=f"{prefix}:post_llm_call",
        ),
        hook_registry.register(
            HookEventName.RUN_END.value,
            on_run_end,
            hook_id=f"{prefix}:run_end",
        ),
    )


def _context_identity(context: HookContext) -> tuple[str, str, str | None]:
    """提取既有 HookContext 的运行关联字段，不读取会话或消息内容。"""
    if not isinstance(context, HookContext):
        raise TypeError("context must be a HookContext")
    observation_id = _required_text(context.invocation_id, "invocation_id")
    metadata = context.metadata
    run_id = _required_text(metadata.get("run_id"), "run_id")
    parent_run_id = _optional_text(metadata.get("parent_run_id"), "parent_run_id")
    return observation_id, run_id, parent_run_id


def _tool_call_observation(context: HookContext) -> ToolCallObservation:
    """仅映射 POST_TOOL_CALL 已公开的摘要字段。"""
    observation_id, run_id, parent_run_id = _context_identity(context)
    payload = _payload(context)
    return ToolCallObservation(
        observation_id=observation_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        tool_call_id=_required_text(payload.get("tool_call_id"), "tool_call_id"),
        tool_name=_required_text(payload.get("tool_name"), "tool_name"),
        status=_required_text(payload.get("status"), "status"),
        success=_required_bool(payload.get("success"), "success"),
        error_type=_optional_text(payload.get("error_type"), "error_type"),
        duration_ms=_nonnegative_int(payload.get("duration_ms"), "duration_ms"),
    )


def _model_call_observation(context: HookContext) -> ModelCallObservation:
    """仅映射 POST_LLM_CALL 的 token 统计与完成摘要。"""
    observation_id, run_id, parent_run_id = _context_identity(context)
    payload = _payload(context)
    token_usage = payload.get("token_usage", {})
    if not isinstance(token_usage, Mapping):
        raise TypeError("token_usage must be a mapping")
    return ModelCallObservation(
        observation_id=observation_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        finish_reason=_optional_text(
            payload.get("finish_reason"),
            "finish_reason",
        ),
        has_text=_required_bool(payload.get("has_text"), "has_text"),
        tool_call_count=_nonnegative_int(
            payload.get("tool_call_count"),
            "tool_call_count",
        ),
        prompt_tokens=_optional_nonnegative_int(
            token_usage.get("prompt_tokens"),
            "prompt_tokens",
        ),
        completion_tokens=_optional_nonnegative_int(
            token_usage.get("completion_tokens"),
            "completion_tokens",
        ),
        total_tokens=_optional_nonnegative_int(
            token_usage.get("total_tokens"),
            "total_tokens",
        ),
        duration_ms=_nonnegative_int(payload.get("duration_ms"), "duration_ms"),
    )


def _run_observation(context: HookContext) -> RunObservation:
    """仅映射 RUN_END 的运行状态和计数，不读取 summary 正文。"""
    observation_id, run_id, parent_run_id = _context_identity(context)
    payload = _payload(context)
    return RunObservation(
        observation_id=observation_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        status=_required_text(payload.get("status"), "status"),
        stop_reason=_required_text(payload.get("stop_reason"), "stop_reason"),
        iterations=_nonnegative_int(payload.get("iterations"), "iterations"),
        tool_call_count=_nonnegative_int(
            payload.get("tool_call_count"),
            "tool_call_count",
        ),
        has_final_reply=_required_bool(
            payload.get("has_final_reply"),
            "has_final_reply",
        ),
    )


def _payload(context: HookContext) -> Mapping[str, object]:
    """取得 Hook 提供的冻结 payload，不读取未声明字段。"""
    if not isinstance(context.payload, Mapping):
        raise TypeError("payload must be a mapping")
    return context.payload


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _required_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field_name)
