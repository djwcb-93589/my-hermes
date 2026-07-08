"""
delegate 工具:同步、可隔离的 leaf subagent。

每次 delegate 调用生成唯一 ``child_session_key``,子 agent 调用 terminal /
file 等工具时把该 key 透传过去,实现 cwd / 文件状态的会话级隔离。无论
成功 / 异常 / max_iterations,都通过 finally 清理对应 backend。

子 agent 是 leaf:不允许调 delegate_task / memory / skill_manage / cron
等会产生跨轮持久副作用的工具。返回值统一为结构化 JSON。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from hermes.backends import cleanup_backend
from hermes.config import MAX_CHILD_ITERATIONS, MODEL, client
from hermes.tools import registry


# 子 agent 允许的 toolset 白名单:memory / delegate / cron 等持久副作用
# 工具集整组禁掉,即使在 args["toolsets"] 里传了也会被过滤。
_ALLOWED_CHILD_TOOLSETS = {"terminal", "file", "skill"}

# 白名单 toolset 内仍然禁用的具体工具名(skill_manage 会改文件)。
# 加上白名单外的关键工具名作为第二层防御,确保即使 schema 漏过也拦得住。
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
# 参数校验 + 工具过滤
# ---------------------------------------------------------------------------

def _validate_args(args: dict) -> tuple[str | None, str, list[str], str | None]:
    """校验 handle_delegate 入参。

    返回 (goal, context, safe_toolsets, error_json)。
    error_json 非空表示拒绝;goal 此时为 None。
    """
    goal = args.get("goal", "")
    if not isinstance(goal, str) or not goal.strip():
        return None, "", [], _result(
            False, "invalid_args", "",
            error="goal is required and must be a non-empty string",
        )

    context = args.get("context", "")
    if not isinstance(context, str):
        context = ""

    requested = args.get("toolsets")
    if requested is None:
        requested = _DEFAULT_TOOLSETS
    elif not isinstance(requested, list) or not all(isinstance(t, str) for t in requested):
        return None, "", [], _result(
            False, "invalid_args", "",
            error="toolsets must be a list of strings",
        )

    # 跟白名单取交集;用户传 ["cron"] / ["memory"] 都会被过滤为 []
    safe = [t for t in requested if t in _ALLOWED_CHILD_TOOLSETS]
    return goal, context, safe, None


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
# 子 agent 循环
# ---------------------------------------------------------------------------

def _last_assistant_text(messages: list[dict]) -> str:
    """取最后一段 assistant 文本作为 summary(用于异常 / max_iter 路径)。"""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"]
    return ""


def _run_child(
    goal: str,
    context: str,
    tools: list[dict],
    child_session_key: str,
) -> tuple[str, str, int, list[str], str | None]:
    """跑子 agent 循环。返回 (status, summary, iterations, tools_used, error)。

    status ∈ {"completed", "max_iterations", "tool_error", "model_error"}
    """
    messages: list[dict] = [{"role": "user", "content": goal}]
    system_prompt = _build_child_prompt(goal, context)
    tools_used: list[str] = []
    iterations = 0

    for iteration in range(MAX_CHILD_ITERATIONS):
        iterations = iteration + 1
        api_messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                tools=tools if tools else None,
            )
        except Exception as exc:
            return ("model_error", _last_assistant_text(messages),
                    iterations, tools_used, repr(exc))

        assistant_msg = response.choices[0].message
        msg_dict: dict = {"role": "assistant", "content": assistant_msg.content or ""}
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in assistant_msg.tool_calls
            ]
        messages.append(msg_dict)

        # 模型不再调工具 → 任务完成
        if not assistant_msg.tool_calls:
            return ("completed", assistant_msg.content or "",
                    iterations, tools_used, None)

        # 处理本轮所有 tool_call
        for tc in assistant_msg.tool_calls:
            tool_name = tc.function.name

            # 双层防御:schema 已过滤 blocked tools,这里再拦一道
            if tool_name in DELEGATE_BLOCKED_TOOLS:
                return ("tool_error", _last_assistant_text(messages),
                        iterations, tools_used,
                        f"blocked tool invoked by child: {tool_name!r}")

            # 参数必须是合法 JSON
            try:
                tool_args = json.loads(tc.function.arguments)
            except Exception as exc:
                return ("tool_error", _last_assistant_text(messages),
                        iterations, tools_used,
                        f"invalid JSON in tool_call {tool_name!r}: {exc}")

            # 关键:把 child_session_key 透传给所有下游工具(terminal/file)
            try:
                output = registry.dispatch(
                    tool_name, tool_args,
                    session_key=child_session_key,
                )
            except Exception as exc:
                return ("tool_error", _last_assistant_text(messages),
                        iterations, tools_used,
                        f"tool {tool_name!r} raised: {exc}")

            if tool_name not in tools_used:
                tools_used.append(tool_name)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            })

    # 走完 MAX_CHILD_ITERATIONS 仍未完成
    return ("max_iterations", _last_assistant_text(messages),
            iterations, tools_used, None)


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
        status, summary, iterations, tools_used, error = _run_child(
            goal, context, tools, child_session_key,
        )
    except Exception as exc:
        # _run_child 内部已捕获大部分异常;此处兜底防漏
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
                            "Requested toolsets; intersected with the child "
                            "whitelist {terminal, file, skill}. Default "
                            "['terminal', 'file']. Memory / delegate / cron "
                            "are always excluded."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
        handler=handle_delegate,
    )
