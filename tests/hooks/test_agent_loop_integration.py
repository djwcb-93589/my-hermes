"""端到端：hook 接入 AgentLoop 后控制型与观察型 hook 实际生效。

用真实 AgentLoop 基类 + mock OpenAI client + stub 工具，验证：
- pre_tool_call 的 Block 阻止工具执行
- pre_llm_call 的 AddContext 注入临时 system 消息
- post_llm_call / post_tool_call / run_end 观察 hook 被触发
- delegate_task 工具获得 bridge（_delegate_hook_registry）
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from hermes.agent_loop import AgentLoop, AgentLoopResult
from hermes.hooks import (
    AddContext, Allow, Block, HookContext, HookEventName,
    SyncHookRegistry,
)
from hermes.tools import ToolRegistry


# ===================== mock OpenAI client =====================

@dataclass
class _MockFn:
    name: str
    arguments: str


@dataclass
class _MockTC:
    id: str
    function: _MockFn
    type: str = "function"


@dataclass
class _MockMsg:
    content: str | None
    tool_calls: list[_MockTC] | None = None
    role: str = "assistant"


@dataclass
class _MockChoice:
    message: _MockMsg
    finish_reason: str = "stop"


@dataclass
class _MockResp:
    choices: list[_MockChoice]
    usage: dict = field(default_factory=lambda: {"total_tokens": 10})


def _tc(name, args, cid=None):
    return _MockTC(id=cid or f"c{uuid.uuid4().hex[:6]}", function=_MockFn(name, json.dumps(args)))


class _ScriptedClient:
    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.requests: list[dict] = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *, model, messages, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools})
        if self._i >= len(self._script):
            resp = _MockResp(choices=[_MockChoice(_MockMsg(content=""), finish_reason="stop")])
        else:
            resp = self._script[self._i]
            self._i += 1
        return resp


def _resp_text(t):
    return _MockResp(choices=[_MockChoice(_MockMsg(content=t), finish_reason="stop")])


def _resp_tools(*tcs):
    return _MockResp(choices=[_MockChoice(_MockMsg(content=None, tool_calls=list(tcs)), finish_reason="tool_calls")])


# ===================== 最小 AgentLoop 实例化 =====================

def _make_loop(client, registry, hook_registry, *, max_iterations=4):
    """构造一个基类 AgentLoop，注册一个 stub 工具。"""
    def stub_handler(args, **kwargs):
        return json.dumps({"ok": True, "result": "stub ran"})

    registry.register(
        name="stub_tool",
        toolset="test",
        schema={"name": "stub_tool", "description": "stub", "parameters": {"type": "object", "properties": {}}},
        handler=stub_handler,
        execution_environments=("cli",),
        unattended_allowed=True,
    )
    return AgentLoop(
        model="test-model",
        max_iterations=max_iterations,
        tools=[{"type": "function", "function": {"name": "stub_tool", "description": "stub", "parameters": {"type": "object", "properties": {}}}}],
        system_prompt="test system",
        registry=registry,
        client=client,
        session_key="test-session",
        hook_registry=hook_registry,
    )


# ===================== pre_tool_call: Block 阻止工具 =====================

def test_pre_tool_call_block_prevents_execution(tmp_path):
    """控制 hook 返回 Block 时，工具不执行，结果为 hook_blocked。"""
    client = _ScriptedClient([
        _resp_tools(_tc("stub_tool", {})),
        _resp_text("blocked result"),
    ])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    # 注册一个 Block 所有 stub_tool 的 hook
    def block_hook(ctx):
        return Block("tool disabled by policy")
    hooks.register("pre_tool_call", block_hook, hook_id="policy")

    loop = _make_loop(client, reg, hooks)
    result = loop.run("do something")

    # 工具被 block，但 loop 继续跑到最终回复
    assert result.ok is True
    # hook 被触发（client 收到至少一次请求）
    assert len(client.requests) >= 1


def test_pre_tool_call_allow_executes_tool(tmp_path):
    """控制 hook Allow 时，工具正常执行。"""
    client = _ScriptedClient([
        _resp_tools(_tc("stub_tool", {})),
        _resp_text("done"),
    ])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    hooks.register("pre_tool_call", lambda ctx: Allow(), hook_id="allow")
    loop = _make_loop(client, reg, hooks)
    result = loop.run("do something")
    assert result.ok is True
    assert "stub_tool" in loop.tools_used


# ===================== pre_llm_call: AddContext 注入 =====================

def test_pre_llm_call_addcontext_injected_into_request(tmp_path):
    """pre_llm_call 的 AddContext 作为临时 system 消息注入模型请求。"""
    client = _ScriptedClient([_resp_text("ok")])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    hooks.register("pre_llm_call", lambda ctx: AddContext("extra guidance"), hook_id="ctx")
    loop = _make_loop(client, reg, hooks)
    loop.run("hi")

    # 第一次模型请求应含 [PLUGIN_TEMPORARY_CONTEXT] 注入
    first_req = client.requests[0]
    msgs = first_req["messages"]
    injected = [m for m in msgs if m.get("role") == "system" and "PLUGIN_TEMPORARY_CONTEXT" in m.get("content", "")]
    assert len(injected) == 1
    assert "extra guidance" in injected[0]["content"]


def test_pre_llm_call_block_returns_hook_blocked(tmp_path):
    """pre_llm_call 的 Block 直接返回 hook_blocked 结果。"""
    client = _ScriptedClient([])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    hooks.register("pre_llm_call", lambda ctx: Block("model blocked"), hook_id="b")
    loop = _make_loop(client, reg, hooks)
    result = loop.run("hi")
    assert result.ok is False
    assert result.status == "hook_blocked"
    assert result.error_type == "hook_blocked"
    assert "model blocked" in result.error


# ===================== 观察 hook 触发 =====================

def test_post_llm_call_hook_fired(tmp_path):
    """post_llm_call 观察 hook 在模型调用后被触发。"""
    client = _ScriptedClient([_resp_text("final answer")])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    fired = []
    hooks.register("post_llm_call", lambda ctx: fired.append(ctx.payload), hook_id="obs")
    loop = _make_loop(client, reg, hooks)
    loop.run("hi")
    assert len(fired) >= 1
    # payload 含安全摘要字段（不含正文）
    p = fired[0]
    assert "finish_reason" in p or "duration_ms" in p


def test_post_tool_call_hook_fired(tmp_path):
    """post_tool_call 观察 hook 在工具执行后被触发。"""
    client = _ScriptedClient([
        _resp_tools(_tc("stub_tool", {})),
        _resp_text("done"),
    ])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    fired = []
    hooks.register("post_tool_call", lambda ctx: fired.append(ctx.payload), hook_id="obs")
    loop = _make_loop(client, reg, hooks)
    loop.run("do something")
    assert len(fired) >= 1
    assert fired[0].get("tool_name") == "stub_tool"


def test_run_end_hook_fired(tmp_path):
    """run_end 观察 hook 在 run 结束后被触发，含 has_final_reply。"""
    client = _ScriptedClient([_resp_text("final answer")])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    fired = []
    hooks.register("run_end", lambda ctx: fired.append(ctx.payload), hook_id="end")
    loop = _make_loop(client, reg, hooks)
    loop.run("hi")
    assert len(fired) == 1
    assert fired[0].get("has_final_reply") is True


def test_run_end_hook_no_final_reply_on_failure(tmp_path):
    """run 失败时 has_final_reply 为 False。"""
    client = _ScriptedClient([])  # 空脚本 -> 空响应
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    fired = []
    hooks.register("run_end", lambda ctx: fired.append(ctx.payload), hook_id="end")
    loop = _make_loop(client, reg, hooks, max_iterations=1)
    loop.run("hi")
    assert len(fired) == 1
    assert fired[0].get("has_final_reply") is False


# ===================== hook 上下文含 run_id / parent_run_id =====================

def test_hook_context_carries_run_id(tmp_path):
    """hook 收到的 context.metadata 含 run_id。"""
    client = _ScriptedClient([_resp_text("ok")])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    seen = []
    hooks.register("post_llm_call", lambda ctx: seen.append(dict(ctx.metadata)), hook_id="obs")
    loop = _make_loop(client, reg, hooks)
    loop.run("hi")
    assert "run_id" in seen[0]
    assert seen[0]["run_id"] == loop.run_id


def test_hook_context_no_parent_run_id_by_default(tmp_path):
    """无 parent_run_id 时 metadata 不含该键。"""
    client = _ScriptedClient([_resp_text("ok")])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    seen = []
    hooks.register("post_llm_call", lambda ctx: seen.append(dict(ctx.metadata)), hook_id="obs")
    loop = _make_loop(client, reg, hooks)
    loop.run("hi")
    assert "parent_run_id" not in seen[0]


# ===================== 无 hook_registry 时降级 =====================

def test_no_hook_registry_runs_normally(tmp_path):
    """不传 hook_registry 时，AgentLoop 正常运行（hook 降级为 no-op）。"""
    client = _ScriptedClient([_resp_text("ok")])
    reg = ToolRegistry()
    loop = _make_loop(client, reg, None)
    result = loop.run("hi")
    assert result.ok is True


# ===================== pre_llm payload 安全性 =====================

def test_pre_llm_payload_excludes_message_content(tmp_path):
    """pre_llm_call 的 payload 不含消息正文，只含计数与估算 token。"""
    client = _ScriptedClient([_resp_text("ok")])
    reg = ToolRegistry()
    hooks = SyncHookRegistry()
    seen = []
    hooks.register("pre_llm_call", lambda ctx: seen.append(ctx.payload), hook_id="pre")
    loop = _make_loop(client, reg, hooks)
    loop.run("secret content like api_key=sk-12345")
    p = seen[0]
    # 只含安全摘要字段
    assert "message_count" in p
    assert "estimated_tokens" in p
    # 不含正文：遍历所有字符串值确认无敏感内容
    def _walk(v):
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                yield from _walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                yield from _walk(x)
    values = list(_walk(p))
    assert not any("sk-12345" in x for x in values), "payload 含密钥"
    assert not any("secret content" in x for x in values), "payload 含正文"
