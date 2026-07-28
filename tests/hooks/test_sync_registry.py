"""SyncHookRegistry：注册、emit、emit_control。"""

from __future__ import annotations

import pytest

from hermes.hooks import (
    AddContext, Allow, Block, HookContext, HookEvent,
    HookRegistrationError, SyncHookRegistry,
)


# ===================== 注册语义 =====================

def test_register_returns_registration():
    reg = SyncHookRegistry()
    r = reg.register("post_tool_call", lambda ctx: None, hook_id="h1")
    assert r.event_name == "post_tool_call"
    assert r.hook_id == "h1"
    assert r.timeout_seconds is None


def test_register_default_hook_id_from_callback():
    reg = SyncHookRegistry()
    def my_hook(ctx):
        return None
    r = reg.register("post_tool_call", my_hook)
    assert "my_hook" in r.hook_id


def test_register_rejects_duplicate_callback():
    reg = SyncHookRegistry()
    cb = lambda ctx: None
    reg.register("post_tool_call", cb, hook_id="h1")
    with pytest.raises(HookRegistrationError):
        reg.register("post_tool_call", cb, hook_id="h2")


def test_register_rejects_duplicate_hook_id():
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: None, hook_id="dup")
    with pytest.raises(HookRegistrationError):
        reg.register("post_tool_call", lambda ctx: None, hook_id="dup")


def test_register_rejects_non_callable():
    reg = SyncHookRegistry()
    with pytest.raises(HookRegistrationError):
        reg.register("post_tool_call", "not callable")  # type: ignore[arg-type]


def test_register_rejects_empty_event_name():
    reg = SyncHookRegistry()
    with pytest.raises(HookRegistrationError):
        reg.register("   ", lambda ctx: None, hook_id="h1")


def test_register_rejects_timeout_seconds():
    reg = SyncHookRegistry()
    with pytest.raises(HookRegistrationError):
        reg.register("post_tool_call", lambda ctx: None, timeout_seconds=1.0)


def test_register_rejects_async_callback():
    reg = SyncHookRegistry()
    async def cb(ctx):
        return None
    with pytest.raises(HookRegistrationError):
        reg.register("post_tool_call", cb)


def test_registered_hooks_returns_snapshot():
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: None, hook_id="h1")
    reg.register("post_tool_call", lambda ctx: None, hook_id="h2")
    reg.register("post_llm_call", lambda ctx: None, hook_id="h3")
    assert [h.hook_id for h in reg.registered_hooks("post_tool_call")] == ["h1", "h2"]


def test_registered_hooks_empty_for_unknown_event():
    assert SyncHookRegistry().registered_hooks("bogus") == ()


# ===================== _commit_registrations（plugin 事务提交）=====================

def test_commit_registrations_atomic():
    reg = SyncHookRegistry()
    staging = SyncHookRegistry()
    staging.register("post_tool_call", lambda ctx: 1, hook_id="p:h1")
    staging.register("post_llm_call", lambda ctx: 2, hook_id="p:h2")
    items = tuple(
        r for name in ("post_tool_call", "post_llm_call", "pre_llm_call", "pre_tool_call", "run_end")
        for r in staging.registered_hooks(name)
    )
    committed = reg._commit_registrations(items)
    assert len(committed) == 2
    assert len(reg.registered_hooks("post_tool_call")) == 1
    assert len(reg.registered_hooks("post_llm_call")) == 1


def test_commit_rejects_conflict():
    """commit 时发现重复 hook_id 整组拒绝（原子性）。"""
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: 1, hook_id="p:h1")
    staging = SyncHookRegistry()
    staging.register("post_tool_call", lambda ctx: 2, hook_id="p:h1")  # 冲突 id
    items = tuple(r for r in staging.registered_hooks("post_tool_call"))
    with pytest.raises(HookRegistrationError):
        reg._commit_registrations(items)
    # 原有注册不受影响
    assert len(reg.registered_hooks("post_tool_call")) == 1


def test_commit_empty_returns_empty():
    assert SyncHookRegistry()._commit_registrations(()) == ()


# ===================== emit（观察型）=====================

def test_emit_returns_empty_for_no_hooks():
    result = SyncHookRegistry().emit(HookEvent(name="post_tool_call", context=HookContext()))
    assert result.results == ()


def test_emit_runs_callbacks_in_order():
    reg = SyncHookRegistry()
    order = []
    reg.register("post_tool_call", lambda ctx: order.append("a"), hook_id="a")
    reg.register("post_tool_call", lambda ctx: order.append("b"), hook_id="b")
    reg.emit(HookEvent(name="post_tool_call", context=HookContext()))
    assert order == ["a", "b"]


def test_emit_collects_values():
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: 42, hook_id="a")
    reg.register("post_tool_call", lambda ctx: "x", hook_id="b")
    result = reg.emit(HookEvent(name="post_tool_call", context=HookContext()))
    assert result.results[0].value == 42
    assert result.results[1].value == "x"


def test_emit_isolates_failure():
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")), hook_id="fail")
    reg.register("post_tool_call", lambda ctx: "ok", hook_id="ok")
    result = reg.emit(HookEvent(name="post_tool_call", context=HookContext()))
    assert result.results[0].success is False
    assert result.results[0].error_type == "RuntimeError"
    assert result.results[1].success is True


def test_emit_rejects_non_event():
    with pytest.raises(TypeError):
        SyncHookRegistry().emit("not an event")  # type: ignore[arg-type]


def test_dispatch_convenience_entry():
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: 1, hook_id="h")
    assert reg.dispatch("post_tool_call", HookContext()).results[0].value == 1


# ===================== emit_control（控制型）=====================

def test_emit_control_allow_continues():
    reg = SyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: Allow(), hook_id="h")
    result = reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
    assert result.blocked is False
    assert result.block_reason is None


def test_emit_control_block_short_circuits():
    reg = SyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: Block("forbidden"), hook_id="b1")
    ran = []
    reg.register("pre_tool_call", lambda ctx: ran.append("after"), hook_id="b2")
    result = reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
    assert result.blocked is True
    assert result.block_reason == "forbidden"
    assert ran == []


def test_emit_control_none_means_allow():
    reg = SyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: None, hook_id="h")
    assert reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())).blocked is False


def test_emit_control_addcontext_collected():
    reg = SyncHookRegistry()
    reg.register("pre_llm_call", lambda ctx: AddContext("extra info"), hook_id="h")
    result = reg.emit_control(HookEvent(name="pre_llm_call", context=HookContext()))
    assert result.added_context == ("extra info",)


def test_emit_control_addcontext_only_for_pre_llm():
    reg = SyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: AddContext("x"), hook_id="h")
    assert reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())).blocked is True


def test_emit_control_multiple_addcontext_accumulate():
    reg = SyncHookRegistry()
    reg.register("pre_llm_call", lambda ctx: AddContext("first"), hook_id="h1")
    reg.register("pre_llm_call", lambda ctx: AddContext("second"), hook_id="h2")
    result = reg.emit_control(HookEvent(name="pre_llm_call", context=HookContext()))
    assert result.added_context == ("first", "second")


def test_emit_control_failure_blocks():
    reg = SyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: (_ for _ in ()).throw(ValueError("bad")), hook_id="fail")
    ran = []
    reg.register("pre_tool_call", lambda ctx: ran.append("x"), hook_id="after")
    result = reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
    assert result.blocked is True
    assert ran == []


def test_emit_control_invalid_value_blocks():
    reg = SyncHookRegistry()
    reg.register("pre_tool_call", lambda ctx: "invalid", hook_id="h")
    assert reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())).blocked is True


def test_emit_control_empty_hooks_not_blocked():
    result = SyncHookRegistry().emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
    assert result.blocked is False
    assert result.results == ()


def test_emit_control_non_control_event_with_hook_blocks():
    reg = SyncHookRegistry()
    reg.register("post_tool_call", lambda ctx: Allow(), hook_id="h")
    result = reg.emit_control(HookEvent(name="post_tool_call", context=HookContext()))
    assert result.blocked is True


def test_emit_control_non_control_event_empty_not_blocked():
    result = SyncHookRegistry().emit_control(HookEvent(name="post_tool_call", context=HookContext()))
    assert result.blocked is False


def test_emit_control_preserves_order_with_allow():
    reg = SyncHookRegistry()
    calls = []
    reg.register("pre_tool_call", lambda ctx: (calls.append("a"), Allow())[1], hook_id="a")
    reg.register("pre_tool_call", lambda ctx: (calls.append("b"), Allow())[1], hook_id="b")
    reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext()))
    assert calls == ["a", "b"]
