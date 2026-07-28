"""SyncControlBridge：跨线程控制桥接。"""

from __future__ import annotations

import pytest

from hermes.hooks import (
    AddContext, Allow, AsyncHookRegistry, Block, HookContext, HookEvent,
    HookRegistrationError, SyncControlBridge, build_sync_control_bridge,
)
from tests.hooks.conftest import run_on_loop


async def _make_bridge(reg):
    return build_sync_control_bridge(reg)


# ===================== 构造校验 =====================

def test_must_be_created_on_running_loop():
    reg = AsyncHookRegistry()
    closed = __import__("asyncio").new_event_loop()
    closed.close()
    with pytest.raises(RuntimeError):
        SyncControlBridge(reg, closed)


def test_rejects_non_async_registry():
    with pytest.raises(TypeError):
        SyncControlBridge("not a registry", None)  # type: ignore[arg-type]


def test_register_is_forbidden(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        bridge = run_on_loop(loop, _make_bridge(reg))
        with pytest.raises(HookRegistrationError):
            bridge.register("pre_tool_call", lambda ctx: None)
    finally:
        stop()


# ===================== 跨线程控制分发 =====================

def test_dispatches_block_from_worker_thread(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        reg.register("pre_tool_call", lambda ctx: Block("denied by async hook"), hook_id="h1")
        bridge = run_on_loop(loop, _make_bridge(reg))
        result = bridge.emit_control(
            HookEvent(name="pre_tool_call", context=HookContext(invocation_id="i", payload={}))
        )
        assert result.blocked is True
        assert result.block_reason == "denied by async hook"
    finally:
        stop()


def test_dispatches_allow_from_worker_thread(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        reg.register("pre_tool_call", lambda ctx: Allow(), hook_id="h1")
        bridge = run_on_loop(loop, _make_bridge(reg))
        result = bridge.emit_control(
            HookEvent(name="pre_tool_call", context=HookContext(invocation_id="i", payload={}))
        )
        assert result.blocked is False
    finally:
        stop()


def test_empty_hooks_not_blocked(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        bridge = run_on_loop(loop, _make_bridge(reg))
        result = bridge.emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
        assert result.blocked is False
        assert result.results == ()
    finally:
        stop()


def test_failure_on_closed_bridge(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        reg.register("pre_tool_call", lambda ctx: Allow(), hook_id="h1")
        bridge = run_on_loop(loop, _make_bridge(reg))
        bridge.close()
        result = bridge.emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
        assert result.blocked is True
        assert result.results[0].hook_id == "control_bridge"
        assert result.results[0].success is False
    finally:
        stop()


def test_non_control_event_fails(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        bridge = run_on_loop(loop, _make_bridge(reg))
        result = bridge.emit_control(HookEvent(name="post_tool_call", context=HookContext()))
        assert result.blocked is True
    finally:
        stop()


def test_bridge_addcontext_passes_through(loop_thread):
    """bridge 正确传递 AddContext 累积结果。"""
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        reg.register("pre_llm_call", lambda ctx: AddContext("ctx-from-async"), hook_id="h1")
        bridge = run_on_loop(loop, _make_bridge(reg))
        result = bridge.emit_control(HookEvent(name="pre_llm_call", context=HookContext()))
        assert result.added_context == ("ctx-from-async",)
        assert result.blocked is False
    finally:
        stop()


# ===================== retain / close 生命周期 =====================

def test_retain_for_background_delegate(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        bridge = run_on_loop(loop, _make_bridge(reg))
        assert bridge.retained_for_background_delegate is False
        bridge.retain_for_background_delegate()
        assert bridge.retained_for_background_delegate is True
    finally:
        stop()


def test_retain_after_close_raises(loop_thread):
    loop, stop = loop_thread
    try:
        reg = AsyncHookRegistry()
        bridge = run_on_loop(loop, _make_bridge(reg))
        bridge.close()
        with pytest.raises(RuntimeError):
            bridge.retain_for_background_delegate()
    finally:
        stop()
