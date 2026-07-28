"""observations payload 与 PluginContext。"""

from __future__ import annotations

import pytest

from hermes.hooks import (
    AsyncHookRegistry, HookEventName, SyncHookRegistry,
    build_post_llm_call_payload, build_post_tool_call_payload, build_run_end_payload,
)
from hermes.hooks.events import normalize_hook_event_name, normalize_observation_event_name
from hermes.plugins import AsyncPluginContext, PluginContext, SyncPluginContext


# ===================== payload 构造 =====================

def test_build_post_llm_call_payload():
    p = build_post_llm_call_payload(
        finish_reason="stop", has_text=True, tool_call_count=2,
        token_usage={"prompt_tokens": 100, "total_tokens": 150}, duration_ms=42,
    )
    assert p["finish_reason"] == "stop"
    assert p["has_text"] is True
    assert p["tool_call_count"] == 2
    assert p["duration_ms"] == 42
    assert p["token_usage"]["total_tokens"] == 150


def test_build_post_llm_call_payload_finish_reason_none():
    p = build_post_llm_call_payload(
        finish_reason=None, has_text=False, tool_call_count=0,
        token_usage={}, duration_ms=0,
    )
    assert p["finish_reason"] is None
    assert "token_usage" not in p


def test_build_post_llm_call_payload_clamps_negative():
    p = build_post_llm_call_payload(
        finish_reason="stop", has_text=True, tool_call_count=-1,
        token_usage={}, duration_ms=-5,
    )
    assert p["tool_call_count"] == 0
    assert p["duration_ms"] == 0


def test_build_post_tool_call_payload():
    p = build_post_tool_call_payload(
        tool_name="terminal", tool_call_id="c1",
        status="succeeded", error_type=None, duration_ms=10,
    )
    assert p["tool_name"] == "terminal"
    assert p["status"] == "succeeded"
    assert p["success"] is True
    assert p["error_type"] is None


def test_build_post_tool_call_payload_failure_status():
    p = build_post_tool_call_payload(
        tool_name="file", tool_call_id="c1",
        status="failed", error_type="not_found", duration_ms=5,
    )
    assert p["success"] is False
    assert p["error_type"] == "not_found"


def test_build_run_end_payload_completed_with_summary():
    p = build_run_end_payload(
        status="completed", stop_reason="done",
        iterations=5, tool_call_count=10, summary="final answer",
    )
    assert p["has_final_reply"] is True
    assert p["iterations"] == 5


def test_build_run_end_payload_not_completed_no_reply():
    p = build_run_end_payload(
        status="max_iterations", stop_reason="limit",
        iterations=8, tool_call_count=3, summary="",
    )
    assert p["has_final_reply"] is False


def test_build_run_end_payload_completed_but_empty_summary():
    p = build_run_end_payload(
        status="completed", stop_reason="done",
        iterations=1, tool_call_count=0, summary="",
    )
    assert p["has_final_reply"] is False


# ===================== 事件名规范化 =====================

def test_normalize_hook_event_name_accepts_enum():
    assert normalize_hook_event_name(HookEventName.PRE_LLM_CALL) == "pre_llm_call"


def test_normalize_hook_event_name_accepts_string():
    assert normalize_hook_event_name("post_tool_call") == "post_tool_call"


def test_normalize_hook_event_name_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_hook_event_name("bogus_event")


def test_normalize_observation_event_name_alias():
    assert normalize_observation_event_name("run_end") == "run_end"
    with pytest.raises(ValueError):
        normalize_observation_event_name("unknown")


# ===================== PluginContext =====================

def test_plugin_context_accepts_sync_registrar():
    reg = SyncHookRegistry()
    # PluginContext 接受一个 registrar callable（事务/Registry.register 适配）
    ctx = PluginContext(lambda name, cb, hid, to: reg.register(name, cb, hook_id=hid, timeout_seconds=to))
    ctx.register_hook("post_tool_call", lambda c: None, hook_id="h")
    assert len(reg.registered_hooks("post_tool_call")) == 1


def test_plugin_context_rejects_non_callable_registrar():
    with pytest.raises(TypeError):
        PluginContext("not callable")  # type: ignore[arg-type]


def test_sync_plugin_context_accepts_registrar():
    """SyncPluginContext 接受任意 callable registrar（无类型校验，仅 callable 检查）。"""
    reg = SyncHookRegistry()
    ctx = SyncPluginContext(lambda name, cb, hid, to: reg.register(name, cb, hook_id=hid, timeout_seconds=to))
    ctx.register_hook("post_tool_call", lambda c: None, hook_id="h")
    assert len(reg.registered_hooks("post_tool_call")) == 1


def test_sync_plugin_context_rejects_non_callable():
    with pytest.raises(TypeError):
        SyncPluginContext("not callable")  # type: ignore[arg-type]


def test_async_plugin_context_accepts_registrar():
    reg = AsyncHookRegistry()
    ctx = AsyncPluginContext(lambda name, cb, hid, to: reg.register(name, cb, hook_id=hid, timeout_seconds=to))
    ctx.register_hook("post_tool_call", lambda c: None, hook_id="h")
    assert len(reg.registered_hooks("post_tool_call")) == 1


def test_async_plugin_context_rejects_non_callable():
    with pytest.raises(TypeError):
        AsyncPluginContext("not callable")  # type: ignore[arg-type]


def test_register_hook_normalizes_event_name():
    """PluginContext.register_hook 用固定事件集合校验，未知事件名注册时即报错。"""
    reg = SyncHookRegistry()
    ctx = PluginContext(lambda name, cb, hid, to: reg.register(name, cb, hook_id=hid, timeout_seconds=to))
    with pytest.raises(Exception):
        ctx.register_hook("bogus_event", lambda c: None, hook_id="h")
    ctx.register_hook(HookEventName.RUN_END, lambda c: None, hook_id="h1")
    assert len(reg.registered_hooks("run_end")) == 1
