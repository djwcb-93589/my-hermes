"""
delegate 工具:同步、可隔离的 leaf subagent。

每次 delegate 调用生成唯一 ``child_session_key``,通过 AgentLoop 跑子
agent 循环,所有工具调用透传 child_session_key 实现 cwd / 文件状态隔离。
无论成功 / 异常 / max_iter,都通过 finally 清理对应 backend。

子 agent 是 leaf agent:
  - toolsets 严格校验,只允许 ``_ALLOWED_CHILD_TOOLSETS`` 内的项;未知
    或不允许的 toolset 立即返 ``invalid_args``,不静默丢弃。
  - 即使 schema 已过滤 blocked tools,``_run`` 内还有第二层防御。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from hermes.agent_loop import AgentLoop
from hermes.backends import cleanup_backend
from hermes.config import MAX_CHILD_ITERATIONS, MODEL
from hermes.tools import registry


# 子 agent 允许的 toolset 白名单:memory / delegate / cron 等持久副作用
# 工具集整组禁掉。
_ALLOWED_CHILD_TOOLSETS = {"terminal", "file", "skill"}

# 白名单 toolset 内仍然禁用的具体工具名(skill_manage 会改文件)。
# 也覆盖白名单外的关键工具名,作为第二层防御。
DELEGATE_BLOCKED_TOOLS = {"delegate_task", "memory", "skill_manage", "cron"}

_DEFAULT_TOOLSETS = ["terminal", "file"]


# ---------------------------------------------------------------------------
# 结构化返回 helper
# ---------------------------------------------------------------------------

def _result(
    ok: bool,
    status: str,
    summary: str,
    *,
    iterations: int = 0,
    tools_used: list[str] | None = None,
    child_session_key: str = "",
    error: str | None = None,
) -> str:
    """构造统一 JSON 返回。"""
    return json.dumps({
        "ok": ok,
        "status": status,
        "summary": summary,
        "iterations": iterations,
        "tools_used": tools_used or [],
        "child_session_key": child_session_key,
        "error": error,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 参数校验(严格)
# ---------------------------------------------------------------------------

def _validate_args(args: dict) -> tuple[str | None, str, list[str] | None, str | None]:
    """严格校验 handle_delegate 入参。

    返回 ``(goal, context, toolsets, error_json)``。error_json 非空表示
    拒绝;此时 goal / toolsets 为 None。

    toolsets 策略:每个 toolset 必须在 ``_ALLOWED_CHILD_TOOLSETS`` 内,
    出现未知 / 不允许的项(如 ``memory`` / ``delegate`` / ``cron``)立即
    返 ``invalid_args``,**不**静默丢弃。
    """
    goal = args.get("goal", "")
    if not isinstance(goal, str) or not goal.strip():
        return None, "", None, _result(
            False, "invalid_args", "",
            error="goal is required and must be a non-empty string",
        )

    context = args.get("context", "")
    if not isinstance(context, str):
        context = ""

    requested = args.get("toolsets")
    if requested is None:
        requested = list(_DEFAULT_TOOLSETS)
    elif not isinstance(requested, list) or not all(isinstance(t, str) for t in requested):
        return None, "", None, _result(
            False, "invalid_args", "",
            error="toolsets must be a list of strings",
        )

    # 严格校验:出现未知 / 不允许的 toolset 直接拒绝,不静默过滤
    invalid = [t for t in requested if t not in _ALLOWED_CHILD_TOOLSETS]
    if invalid:
        return None, "", None, _result(
            False, "invalid_args", "",
            error=(
                f"unsupported toolsets: {invalid}; "
                f"allowed: {sorted(_ALLOWED_CHILD_TOOLSETS)}"
            ),
        )

    if not requested:
        return None, "", None, _result(
            False, "invalid_args", "",
            error="toolsets is empty",
        )

    return goal, context, requested, None


def _filter_definitions(toolsets: list[str]) -> list[dict]:
    """从 toolsets 里筛掉 DELEGATE_BLOCKED_TOOLS,返回剩余 tool schema。

    显式处理空列表:registry.get_definitions 把空 enabled_toolsets 视作
    "不过滤",delegate 这里必须当成"没工具",否则 toolsets=[] 会泄漏
    所有工具给子 agent。
    """
    if not toolsets:
        return []
    defs = registry.get_definitions(toolsets)
    return [
        d for d in defs
        if d["function"]["name"] not in DELEGATE_BLOCKED_TOOLS
    ]


def _build_child_prompt(goal: str, context: str) -> str:
    """组装子 agent 的 system prompt。"""
    prompt = (
        "You are a focused sub-agent. "
        "Complete the task and report results.\n"
        f"# Task\n{goal}\n\n"
    )
    if context:
        prompt += f"# Context\n{context}\n\n"
    prompt += (
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Working directory: {os.getcwd()}"
    )
    return prompt


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def handle_delegate(args, **kwargs) -> str:
    """handle_delegate:启动一个隔离的 leaf 子 agent。"""
    goal, context, toolsets, err = _validate_args(args)
    if err is not None:
        return err

    tools = _filter_definitions(toolsets)
    if not tools:
        return _result(
            False, "invalid_args", "",
            error=(f"no usable tools after applying child restrictions; "
                   f"requested={args.get('toolsets')!r}"),
        )

    child_session_key = f"child-{uuid.uuid4().hex[:12]}"
    print(f"  [delegate] child session={child_session_key} goal={goal[:80]!r}")

    try:
        loop = AgentLoop(
            model=MODEL,
            max_iterations=MAX_CHILD_ITERATIONS,
            tools=tools,
            system_prompt=_build_child_prompt(goal, context),
            registry=registry,
            session_key=child_session_key,
            blocked_tools=DELEGATE_BLOCKED_TOOLS,
        )
        result = loop.run(goal)
        status = result.status
        summary = result.summary
        iterations = result.iterations
        tools_used = result.tools_used
        error = result.error
    except Exception as exc:
        # AgentLoop 内部已捕获大部分异常;此处兜底防漏
        status = "tool_error"
        summary = ""
        iterations = 0
        tools_used = []
        error = repr(exc)
    finally:
        # 无论成功 / 失败 / max_iter,都释放 child 的 terminal backend
        cleanup_backend(child_session_key)

    ok = (status == "completed")
    return _result(
        ok, status, summary,
        iterations=iterations, tools_used=tools_used,
        child_session_key=child_session_key,
        error=error,
    )


def register(registry):
    registry.register(
        name="delegate_task",
        toolset="delegate",
        schema={
            "name": "delegate_task",
            "description": (
                "Delegate a leaf sub-task to an isolated child agent. The "
                "child gets its own session_key (terminal / file backend "
                "isolated from the parent), cannot call delegate / memory / "
                "skill_manage / cron (no recursion, no persistent side "
                "effects across rounds), and only sees the goal + optional "
                "context. Returns structured JSON: "
                "{ok, status, summary, iterations, tools_used, "
                "child_session_key, error}. "
                "status ∈ {completed, max_iterations, tool_error, "
                "model_error, invalid_args}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Task description for the child agent.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to inject into the child system prompt.",
                    },
                    "toolsets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Allowed child toolsets. Each item must be one "
                            "of {terminal, file, skill}. Unknown or "
                            "disallowed values (memory / delegate / cron) "
                            "cause invalid_args. Default "
                            "['terminal', 'file']."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
        handler=handle_delegate,
    )
