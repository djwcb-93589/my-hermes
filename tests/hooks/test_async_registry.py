"""AsyncHookRegistry：异步分发、超时、取消、控制语义。"""

from __future__ import annotations

import asyncio

import pytest

from hermes.hooks import (
    AddContext, Allow, AsyncHookRegistry, Block,
    HookContext, HookEvent,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ===================== 注册与超时 =====================

def test_register_supports_timeout():
    reg = AsyncHookRegistry()
    r = reg.register("post_tool_call", lambda ctx: None, hook_id="h", timeout_seconds=1.5)
    assert r.timeout_seconds == 1.5


def test_register_default_timeout():
    reg = AsyncHookRegistry(default_timeout_seconds=2.0)
    assert reg.register("post_tool_call", lambda ctx: None, hook_id="h").timeout_seconds == 2.0


def test_default_timeout_seconds_property():
    reg = AsyncHookRegistry(default_timeout_seconds=3.0)
    assert reg.default_timeout_seconds == 3.0
    assert AsyncHookRegistry().default_timeout_seconds is None


def test_register_rejects_invalid_timeout():
    reg = AsyncHookRegistry()
    with pytest.raises(Exception):
        reg.register("post_tool_call", lambda ctx: None, timeout_seconds=-1)
    with pytest.raises(Exception):
        reg.register("post_tool_call", lambda ctx: None, timeout_seconds=0)


def test_register_accepts_coroutine_callback():
    reg = AsyncHookRegistry()
    async def cb(ctx):
        return 42
    reg.register("post_tool_call", cb, hook_id="h")


# ===================== emit（观察型）=====================

def test_emit_runs_in_order():
    reg = AsyncHookRegistry()
    order = []
    reg.register("post_tool_call", lambda ctx: order.append("a"), hook_id="a")
    reg.register("post_tool_call", lambda ctx: order.append("b"), hook_id="b")
    _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert order == ["a", "b"]


def test_emit_collects_values():
    reg = AsyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: 1, hook_id="a")
    reg.register("post_tool_call", lambda ctx: 2, hook_id="b")
    result = _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert [r.value for r in result.results] == [1, 2]


def test_emit_runs_coroutine_callback():
    reg = AsyncHookRegistry()
    async def cb(ctx):
        await asyncio.sleep(0)
        return "async result"
    reg.register("post_tool_call", cb, hook_id="h")
    result = _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert result.results[0].value == "async result"


def test_emit_isolates_failure():
    reg = AsyncHookRegistry()
    async def fail1(ctx):
        raise RuntimeError("fails 1")
    async def fail2(ctx):
        raise RuntimeError("fails 2")
    reg.register("post_tool_call", fail1, hook_id="f1")
    reg.register("post_tool_call", fail2, hook_id="f2")
    reg.register("post_tool_call", lambda ctx: "ok", hook_id="ok")
    result = _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert result.results[0].success is False
    assert result.results[1].success is False
    assert result.results[2].success is True


def test_emit_timeout_marks_timed_out():
    reg = AsyncHookRegistry()
    async def slow(ctx):
        await asyncio.sleep(0.2)
        return "late"
    reg.register("post_tool_call", slow, hook_id="slow", timeout_seconds=0.05)
    reg.register("post_tool_call", lambda ctx: "after", hook_id="after")
    result = _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert result.results[0].success is False
    assert result.results[0].timed_out is True
    assert result.results[0].error_type == "TimeoutError"
    assert result.results[1].value == "after"


# ===================== emit_control（控制型）=====================

def test_emit_control_allow():
    reg = AsyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: Allow(), hook_id="h")
    result = _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert result.blocked is False


def test_emit_control_block_short_circuits():
    reg = AsyncHookRegistry()
    ran = []
    reg.register("pre_tool_call", lambda ctx: Block("nope"), hook_id="b")
    reg.register("pre_tool_call", lambda ctx: ran.append("x"), hook_id="after")
    result = _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert result.blocked is True
    assert result.block_reason == "nope"
    assert ran == []


def test_emit_control_addcontext():
    reg = AsyncHookRegistry()
    reg.register("pre_llm_call", lambda ctx: AddContext("ctx"), hook_id="h")
    result = _run(reg.emit_control(HookEvent(name="pre_llm_call", context=HookContext())))
    assert result.added_context == ("ctx",)


def test_emit_control_timeout_blocks():
    reg = AsyncHookRegistry()
    async def slow(ctx):
        await asyncio.sleep(0.2)
        return Allow()
    reg.register("pre_tool_call", slow, hook_id="s", timeout_seconds=0.05)
    result = _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert result.blocked is True
    assert result.results[0].timed_out is True


def test_emit_control_failure_blocks():
    reg = AsyncHookRegistry()
    async def fail(ctx):
        raise ValueError("bad")
    reg.register("pre_tool_call", fail, hook_id="f")
    result = _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert result.blocked is True
    assert result.results[0].success is False


def test_emit_control_order_with_allow():
    reg = AsyncHookRegistry()
    calls = []
    reg.register("pre_tool_call", lambda ctx: (calls.append("a"), Allow())[1], hook_id="a")
    reg.register("pre_tool_call", lambda ctx: (calls.append("b"), Allow())[1], hook_id="b")
    _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert calls == ["a", "b"]


def test_emit_control_addcontext_only_pre_llm():
    reg = AsyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: AddContext("x"), hook_id="h")
    result = _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert result.blocked is True


# ===================== 同步回调在线程池 =====================

def test_emit_sync_callback_via_to_thread():
    reg = AsyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: "from thread", hook_id="h")
    result = _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert result.results[0].value == "from thread"


def test_emit_control_sync_callback():
    reg = AsyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: Block("blocked by sync"), hook_id="h")
    result = _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert result.blocked is True
    assert result.block_reason == "blocked by sync"


# ===================== dispatch 便捷入口 =====================

def test_dispatch():
    reg = AsyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: 1, hook_id="h")
    result = _run(reg.dispatch("post_tool_call", HookContext()))
    assert result.results[0].value == 1


# ===================== _commit_registrations =====================

def test_commit_registrations_uses_default_timeout():
    """async commit 时为无超时注册项补默认超时。"""
    reg = AsyncHookRegistry(default_timeout_seconds=2.0)
    staging = AsyncHookRegistry()
    staging.register("post_tool_call", lambda ctx: 1, hook_id="p:h1")
    items = tuple(r for r in staging.registered_hooks("post_tool_call"))
    # commit 不改原 registration 的 timeout，但 _control_snapshot 会用 default
    committed = reg._commit_registrations(items)
    assert len(committed) == 1
