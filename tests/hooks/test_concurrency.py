"""并发隔离、取消与延迟释放边界。"""

from __future__ import annotations

import asyncio

import pytest

from hermes.hooks import (
    AddContext, Allow, AsyncHookRegistry, Block,
    HookContext, HookEvent,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ===================== execution_lock：同一回调不并发 =====================

def test_control_hook_concurrent_calls_serialized():
    """两个 emit_control 并发请求同一 hook，回调不会重叠执行。

    execution_lock 保证：第二次回调必须等第一次释放锁后才能进入。
    """
    reg = AsyncHookRegistry()
    in_flight = []
    max_concurrent = [0]
    lock = asyncio.Lock()

    async def cb(ctx):
        async with lock:
            in_flight.append(1)
            max_concurrent[0] = max(max_concurrent[0], len(in_flight))
        await asyncio.sleep(0.05)  # 故意让两次调用有机会重叠
        async with lock:
            in_flight.pop()
        return Allow()

    reg.register("pre_tool_call", cb, hook_id="h", timeout_seconds=2.0)

    # 并发跑两个 emit_control（gather 在 _run 的 loop 内创建）
    async def _both():
        return await asyncio.gather(
            reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())),
            reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())),
        )
    _run(_both())
    assert max_concurrent[0] == 1, "同一回调被并发执行了"


def test_control_hook_lock_released_after_complete():
    """回调完成后锁释放，后续调用可正常进入。"""
    reg = AsyncHookRegistry()
    count = [0]

    async def cb(ctx):
        count[0] += 1
        return Allow()

    reg.register("pre_tool_call", cb, hook_id="h", timeout_seconds=2.0)
    _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    _run(reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())))
    assert count[0] == 2


def test_different_hooks_run_independently():
    """不同 hook_id 的回调互不阻塞（各自独立 execution_lock）。"""
    reg = AsyncHookRegistry()
    started = []

    async def cb_a(ctx):
        started.append("a")
        await asyncio.sleep(0.05)
        return Allow()

    def cb_b(ctx):
        started.append("b")
        return Allow()

    reg.register("pre_tool_call", cb_a, hook_id="a", timeout_seconds=2.0)
    reg.register("pre_tool_call", cb_b, hook_id="b", timeout_seconds=2.0)
    async def _both():
        return await asyncio.gather(
            reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())),
            reg.emit_control(HookEvent(name="pre_tool_call", context=HookContext())),
        )
    _run(_both())
    # 两个都执行了
    assert "a" in started and "b" in started


# ===================== 观察型失败隔离 =====================

def test_observation_hook_consecutive_failures_isolated():
    """观察型 hook 连续失败不影响后续。"""
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
    assert result.results[2].value == "ok"


# ===================== AddContext 跨 hook 累积与 Block 保留 =====================

def test_addcontext_accumulates_across_hooks():
    """多个控制 hook 的 AddContext 顺序累积。"""
    reg = AsyncHookRegistry()
    reg.register("pre_llm_call", lambda ctx: AddContext("first"), hook_id="h1")
    reg.register("pre_llm_call", lambda ctx: AddContext("second"), hook_id="h2")
    reg.register("pre_llm_call", lambda ctx: Allow(), hook_id="h3")
    result = _run(reg.emit_control(HookEvent(name="pre_llm_call", context=HookContext())))
    assert result.added_context == ("first", "second")
    assert result.blocked is False


def test_block_keeps_prior_addcontext():
    """Block 短路前已累积的 AddContext 仍保留在结果里。"""
    reg = AsyncHookRegistry()
    reg.register("pre_llm_call", lambda ctx: AddContext("kept"), hook_id="h1")
    reg.register("pre_llm_call", lambda ctx: Block("stop"), hook_id="h2")
    result = _run(reg.emit_control(HookEvent(name="pre_llm_call", context=HookContext())))
    assert result.blocked is True
    assert result.block_reason == "stop"
    assert result.added_context == ("kept",)


# ===================== 超时后脱离 Task 被消费 =====================

def test_timed_out_task_result_consumed_no_warning():
    """超时的 hook Task 被取消后，其结果被 _observe_detached_task 消费，不抛未观察异常。"""
    reg = AsyncHookRegistry()

    async def slow(ctx):
        await asyncio.sleep(0.2)
        return "late"

    reg.register("post_tool_call", slow, hook_id="s", timeout_seconds=0.05)
    # 不应抛异常（含 RecursionError 之外的未观察异常会被 done_callback 消费）
    result = _run(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
    assert result.results[0].timed_out is True
    assert result.results[0].success is False


# ===================== 取消传播 =====================

def test_emit_propagates_cancel():
    """外部取消 emit 时，当前 hook 的 Task 被取消并向上传播 CancelledError。"""
    reg = AsyncHookRegistry()

    async def slow(ctx):
        await asyncio.sleep(0.5)
        return "late"

    reg.register("post_tool_call", slow, hook_id="s", timeout_seconds=2.0)

    async def runner():
        task = asyncio.create_task(reg.emit(HookEvent(name="post_tool_call", context=HookContext())))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        _run(runner())
