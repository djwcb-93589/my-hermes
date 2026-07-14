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
from typing import Callable

from hermes.agent_loop import AgentLoop
from hermes.backends import cleanup_backend
from hermes.config import MAX_CHILD_ITERATIONS, MODEL, client as _default_client
from hermes.delegate_jobs import get_delegate_job_manager
from hermes.tools import registry


# 子 agent 允许的 toolset 取值(用于 _validate_args 严格校验 toolsets 参数)。
# memory / delegate / cron 等持久副作用工具集整组禁掉。
_ALLOWED_CHILD_TOOLSETS = {"terminal", "file", "skill"}

# 子 agent 可用的具体工具名白名单(终极策略,按工具名而非 toolset)。
# terminal / file 单工具,自然全允许(它们自己已有安全边界);
# skill 只放行只读工具,**不**因为 toolsets=["skill"] 就放行整个 skill
# toolset —— 避免未来新增 skill 写工具(如 skill_create / skill_delete)
# 自动泄漏给子 agent。
_ALLOWED_CHILD_TOOLS = {
    "terminal",
    "file",
    "skill_view",
    "skills_list",
}

# 始终禁用的工具名(理论上白名单已覆盖,保留作第二层防御)。
DELEGATE_BLOCKED_TOOLS = {"delegate_task", "memory", "skill_manage", "cron"}

_DEFAULT_TOOLSETS = ["terminal", "file"]

DELEGATE_SYSTEM_PROMPT = """
You are a temporary delegated subagent in my-hermes.

You only work on the given child task. You do not have long-term memory and you must not behave like the main agent.

Rules:
- Use available tools when inspection or verification is needed.
- Do not call delegate_task, memory, skill_manage, or cron.
- Do not ask the user questions.
- Do not claim completion unless you have evidence.
- If you make assumptions, state them.
- If you change files, report exact file paths.
- If you run commands, report key commands and results.
- If something cannot be completed, say so clearly.

Final report must use this format:

Summary:
...

Findings:
...

Changes Made:
...

Verification:
...

Remaining Issues:
...
""".strip()


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
    """从 toolsets 里筛出子 agent 可用的 tool schema。

    策略:**工具名白名单**(不靠 toolset 黑名单)。即便 toolsets 含 "skill",
    也只会拿到 ``_ALLOWED_CHILD_TOOLS`` 列出的只读 skill 工具。
    未来 skill toolset 新增写工具不会自动放行。

    显式处理空列表:registry 现在同样把 ``[]`` 解释为“没工具”；这里保留
    前置拒绝作为 delegate 自己的参数边界，避免未来 registry 语义变化时
    子 agent 意外获得工具。
    """
    if not toolsets:
        return []
    defs = registry.get_definitions(toolsets)
    return [
        d for d in defs
        if d["function"]["name"] in _ALLOWED_CHILD_TOOLS
        and d["function"]["name"] not in DELEGATE_BLOCKED_TOOLS
    ]


def _build_child_prompt(context: str) -> str:
    """组装子 agent 的 system prompt。

    只写角色 / 约束 / 当前环境;具体 task 作为 user message 传入
    (``loop.run(goal)``),避免 goal 在 system / user 两处重复。
    """
    prompt = DELEGATE_SYSTEM_PROMPT + "\n\n"
    if context:
        prompt += f"# Context\n{context}\n\n"
    prompt += (
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Working directory: {os.getcwd()}"
    )
    return prompt


# ---------------------------------------------------------------------------
# DelegateAgentLoop —— 子 agent 专属策略(只继承公共骨架,不沾主会话能力)
# ---------------------------------------------------------------------------

class DelegateAgentLoop(AgentLoop):
    """子 agent 循环。

    只继承 AgentLoop 公共骨架:iteration loop / model call / assistant
    parse / tool_call dispatch / messages append / stop condition。

    明确不启用主会话能力:
      - 无 DB 持久化(不覆盖 on_assistant_message / on_tool_message 等)
      - 无 compression(不覆盖 pre_model_call)
      - 无 fallback / retry / continuation(不启用主会话策略)
      - 无 blocked tools 之外的工具限制变更

    handle_model_error 覆盖为返 ``"abort"``:模型 API 异常走
    ``status="model_error"`` 路径,而不是默认 ``"raise"`` 冒泡到
    handle_delegate 兜底被误标为 tool_error。

    构造参数 ``model_kwargs`` 透传给基类,用于 provider-specific 额外
    参数(extra_body / temperature 等),由调用方决定。
    """

    def handle_model_error(self, exc, messages) -> str:
        # 子 agent 不做 fallback / retry;模型异常直接 abort 出 model_error
        return "abort"


# ---------------------------------------------------------------------------
# 共享执行 helper(同步 handle_delegate 和后台 worker 都走这个)
# ---------------------------------------------------------------------------

def run_delegate_child(
    goal: str,
    context: str,
    toolsets: list[str],
    child_session_key: str,
    cancel_checker: Callable[[], bool] | None = None,
) -> dict:
    """跑一个子 agent 任务,返回 dict(不是 JSON)。

    同步 ``handle_delegate(background=False)`` 和后台 worker 都用这个
    helper,避免两套 subagent 执行逻辑。``cleanup_backend`` 在 finally
    里保证执行,无论成功 / 异常 / 取消 / max_iter。
    """
    try:
        tools = _filter_definitions(toolsets)
        if not tools:
            return {
                "ok": False, "status": "invalid_args",
                "summary": "", "iterations": 0, "tools_used": [],
                "error": (f"no usable tools after applying child restrictions; "
                          f"toolsets={toolsets!r}"),
            }

        loop = DelegateAgentLoop(
            model=MODEL,
            max_iterations=MAX_CHILD_ITERATIONS,
            tools=tools,
            system_prompt=_build_child_prompt(context),
            registry=registry,
            client=_default_client,
            session_key=child_session_key,
            blocked_tools=DELEGATE_BLOCKED_TOOLS,
            # provider-specific 参数留空,需要时由调用方注入
            model_kwargs=None,
            cancel_checker=cancel_checker,
        )
        # goal 作为 user message 传入;system prompt 只描述角色 / 约束
        result = loop.run(goal)
        return {
            "ok": result.ok,
            "status": result.status,
            "summary": result.summary,
            "iterations": result.iterations,
            "tools_used": list(result.tools_used),
            "error": result.error,
        }
    except Exception as exc:
        # DelegateAgentLoop.handle_model_error 已返 abort,模型异常不会冒泡
        # 到这里;真到这里说明是别的未预期异常,归到 tool_error 作兜底
        return {
            "ok": False, "status": "tool_error",
            "summary": "", "iterations": 0, "tools_used": [],
            "error": repr(exc),
        }
    finally:
        # 无论成功 / 失败 / max_iter / 取消,都释放 child 的 terminal backend
        cleanup_backend(child_session_key)


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------

def _json_dumps(obj: dict) -> str:
    """JSON 序列化 helper(关 ensure_ascii 以便中文直读)。"""
    return json.dumps(obj, ensure_ascii=False)


def handle_delegate(args, **kwargs) -> str:
    """handle_delegate:启动一个隔离的 leaf 子 agent。

    ``background=False``(默认):同步执行,等待子 agent 完成后返回结构化 JSON。
    ``background=True``:提交后台 job,立即返回 job_id,status="submitted"。
    """
    goal, context, toolsets, err = _validate_args(args)
    if err is not None:
        return err

    # 提前过滤:无可用工具直接返,不创建 backend / job
    if not _filter_definitions(toolsets):
        return _result(
            False, "invalid_args", "",
            error=(f"no usable tools after applying child restrictions; "
                   f"requested={args.get('toolsets')!r}"),
        )

    background = bool(args.get("background", False))
    parent_session_key = kwargs.get("session_key")
    child_session_key = f"child-{uuid.uuid4().hex[:12]}"

    if not background:
        # ---------- 同步模式 ----------
        print(f"  [delegate] sync child={child_session_key} goal={goal[:80]!r}")
        r = run_delegate_child(goal, context, toolsets, child_session_key)
        return _result(
            r["ok"], r["status"], r["summary"],
            iterations=r["iterations"],
            tools_used=r["tools_used"],
            child_session_key=child_session_key,
            error=r["error"],
        )

    # ---------- 后台模式 ----------
    manager = get_delegate_job_manager()

    def runner_factory(job):
        # 闭包绑定 cancel_checker:从 manager 查 job.cancel_requested
        job_id = job.job_id

        def runner() -> dict:
            return run_delegate_child(
                goal, context, toolsets, child_session_key,
                cancel_checker=lambda: manager.is_cancel_requested(job_id),
            )
        return runner

    submit_result = manager.submit(
        runner_factory=runner_factory,
        goal=goal,
        context=context,
        toolsets=toolsets,
        parent_session_key=parent_session_key,
        child_session_key=child_session_key,
    )

    if not submit_result["ok"]:
        # 并发上限拒绝:job 还没创建,但 child_session_key 已分配过,
        # 这里 cleanup 防止潜在泄漏(run_delegate_child 没被调用过,
        # backend 也没真正创建,但保险一道)。
        cleanup_backend(child_session_key)
        return _json_dumps(submit_result)

    print(f"  [delegate] background job={submit_result['job_id']} "
          f"child={child_session_key} goal={goal[:80]!r}")
    return _json_dumps(submit_result)


# ---------------------------------------------------------------------------
# 后台 job 查询 / 取消工具
# ---------------------------------------------------------------------------

def handle_delegate_status(args, **kwargs) -> str:
    """查询后台 job 当前状态(轻量视图,不含 summary)。

    返回字段: ok / job_id / status / child_status / cancel_requested /
    created_at / started_at / finished_at / iterations / tools_used / error。
    job_id 不存在返 not_found。
    """
    job_id = args.get("job_id", "")
    if not job_id:
        return _json_dumps({"ok": False, "error_type": "invalid_args",
                            "error": "job_id is required"})
    view = get_delegate_job_manager().status_view(job_id)
    if view is None:
        return _json_dumps({"ok": False, "error_type": "not_found",
                            "error": f"unknown job_id: {job_id}"})
    return _json_dumps(view)


def handle_delegate_result(args, **kwargs) -> str:
    """查询后台 job 结果(完整视图,非阻塞)。

    - queued / running:ok=False, error="Job is still running", summary=""
    - completed / failed / cancelled:ok 视 job.status 而定(completed→True),
      返完整 summary / iterations / tools_used / child_session_key / error /
      child_status
    """
    job_id = args.get("job_id", "")
    if not job_id:
        return _json_dumps({"ok": False, "error_type": "invalid_args",
                            "error": "job_id is required"})
    view = get_delegate_job_manager().result_view(job_id)
    if view is None:
        return _json_dumps({"ok": False, "error_type": "not_found",
                            "error": f"unknown job_id: {job_id}"})
    return _json_dumps(view)


def handle_delegate_cancel(args, **kwargs) -> str:
    """协作式取消后台 job:标记 cancel_requested,worker 下一轮检查退出。"""
    job_id = args.get("job_id", "")
    if not job_id:
        return _json_dumps({"ok": False, "error_type": "invalid_args",
                            "error": "job_id is required"})
    res = get_delegate_job_manager().cancel(job_id)
    return _json_dumps(res)


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
                "context. background=false (default) blocks until the child "
                "finishes and returns {ok, status, summary, iterations, "
                "tools_used, child_session_key, error}. background=true "
                "submits a background job and immediately returns "
                "{ok, status='submitted', job_id, child_session_key}; poll "
                "with delegate_status / delegate_result / delegate_cancel."
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
                    "background": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "false (default): synchronous — block until the "
                            "child finishes. true: submit a background job "
                            "and return immediately with job_id."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
        handler=handle_delegate,
    )
    registry.register(
        name="delegate_status",
        toolset="delegate",
        schema={
            "name": "delegate_status",
            "description": (
                "Lightweight status probe for a background delegate job. "
                "Returns {ok, job_id, status ∈ queued|running|completed|"
                "failed|cancelled, child_status, cancel_requested, "
                "iterations, tools_used, error, timestamps}. Does NOT "
                "include summary (use delegate_result for that). Use this "
                "to check whether the job is still running or how it ended."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                },
                "required": ["job_id"],
            },
        },
        handler=handle_delegate_status,
    )
    registry.register(
        name="delegate_result",
        toolset="delegate",
        schema={
            "name": "delegate_result",
            "description": (
                "Fetch the full result of a background delegate job. "
                "Non-blocking: if still queued/running, returns ok=false "
                "with error='Job is still running' and empty summary. If "
                "completed/failed/cancelled, returns ok (true only when "
                "status=completed), summary, iterations, tools_used, "
                "child_session_key, child_status, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                },
                "required": ["job_id"],
            },
        },
        handler=handle_delegate_result,
    )
    registry.register(
        name="delegate_cancel",
        toolset="delegate",
        schema={
            "name": "delegate_cancel",
            "description": (
                "Cooperatively cancel a background delegate job. Sets the "
                "cancel_requested flag; the worker checks it at the next "
                "iteration boundary and exits with status=cancelled. Does "
                "not forcibly kill the worker thread."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                },
                "required": ["job_id"],
            },
        },
        handler=handle_delegate_cancel,
    )
