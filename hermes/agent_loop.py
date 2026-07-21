"""
AgentLoop:parent agent 与 sub agent 共用的循环骨架(模板方法模式)。

``run()`` 是公共骨架:iteration loop → model call → assistant parse →
tool_call dispatch → messages append → stop condition。所有"主会话
专有"行为(DB 持久化、压缩、fallback、continuation 等)通过覆盖下方
hooks 注入,AgentLoop 本身不依赖 conn / session_id / add_messages。

默认实现是一份无副作用的最小循环,delegate 子 agent 直接使用;
主会话通过 ``ConversationAgentLoop``(定义在 conversation.py)覆盖
hooks 注入压缩 / fallback / DB 持久化等行为。

``AsyncAgentLoop`` 保留同一结果和错误策略,供 Gateway 直接等待并取消
异步模型 HTTP 请求;同步 ``AgentLoop`` 继续服务 CLI / delegate。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable

from hermes.approval import build_approval_deferred
from hermes.config import client as _default_client
from hermes.redaction import redact_explicit_secrets


@dataclass
class AgentLoopResult:
    """AgentLoop.run 的返回。"""
    ok: bool
    status: str  # completed | awaiting_approval | max_iterations | tool_error | model_error | error | cancelled
    summary: str
    messages: list[dict]
    iterations: int
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None
    # 错误分类字段(只在 ok=False 时有意义):
    #   error_type: 具体类型(model_error / persistence_error / tool_error /
    #               internal_error / cancelled / 具体工具 error_type)
    #   fatal: True 表示调用方不应盲目重试整个 agent
    #   retryable: True 表示瞬时可重试(模型临时不可用等)
    error_type: str | None = None
    fatal: bool = False
    retryable: bool = True
    approval_request: dict | None = None


# ---------------------------------------------------------------------------
# 共享 helper(也可独立使用)
# ---------------------------------------------------------------------------

# fatal marker:普通字符串工具错误命中这些关键字时直接终止 loop,
# 即使工具没返结构化 JSON error_type 也能识别。
_FATAL_MARKERS = (
    "safety_blocked",
    "forbidden",
    "permission_denied",
    "path_escape",
    "persistence_error",
    "cancelled",
)

# Windows 绝对路径:带引号时允许路径包含空格;未加引号时以空白为边界,
# 同时覆盖 UNC 路径。该正则只用于工作区路径规范化,不隐藏外部路径。
_WINDOWS_ABS_PATH_RE = re.compile(
    r"(?:\"(?:[A-Za-z]:[\\/]|\\\\)[^\"\r\n]+\""
    r"|'(?:[A-Za-z]:[\\/]|\\\\)[^'\r\n]+'"
    r"|(?:[A-Za-z]:[\\/]|\\\\)[^\s\"']+)"
)
_WINDOWS_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]")
_MSYS_DRIVE_PATH_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")
# Unix 绝对路径:要求 / 前面不是字母数字或路径字符,避免把已经规范化的
# 相对路径再次当成绝对路径处理。
_UNIX_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9._\-/<>])(/[A-Za-z0-9._\-/]+)")
_TRACEBACK_FRAME_RE = re.compile(
    r"^\s*File\s+['\"].+['\"],\s+line\s+\d+(?:,\s+in\s+.*)?$"
)
_ERROR_CONTEXT_RE = re.compile(
    r"(?i)\b(?:command|working directory|cwd|path|file|target|operation|tool|"
    r"request[_ -]?id|http status|status(?: code)?|errno|database|table|column|"
    r"field|constraint|stderr)\s*[:=]"
)
_ERROR_REASON_RE = re.compile(
    r"(?i)(?:\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)\b|\berrno\b|"
    r"access denied|permission denied|not found|timed? out|unavailable|"
    r"\bfailed\b|\bfailure\b)"
)
_PATH_HINT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:[^/\s]+/)*[^/\s]+|"
    r"(?:^|\s)(?:\.{0,2}/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)"
)


def _normalize_msys_path(path_text: str, workspace_root: str | None) -> str:
    """Windows 工作区下把 Git Bash 的 ``/d/...`` 转成 ``D:\\...``。"""
    if not workspace_root or not _WINDOWS_DRIVE_ROOT_RE.match(str(workspace_root)):
        return path_text
    match = _MSYS_DRIVE_PATH_RE.fullmatch(path_text)
    if not match:
        return path_text
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{match.group(1).upper()}:\\{rest}"


def _normalize_path_text(path_text: str, workspace_root: str | None = None) -> str:
    """把工作区内绝对路径规范化为相对路径,外部路径保持原样。"""
    original = path_text.strip().strip("'\"")
    normalized = _normalize_msys_path(original, workspace_root)

    if not workspace_root:
        return original

    root_text = str(workspace_root).strip().strip("'\"")

    # 使用纯词法路径判断,不访问文件系统,也不受当前运行平台影响。
    if normalized.startswith("\\\\") or _WINDOWS_DRIVE_ROOT_RE.match(normalized):
        path = PureWindowsPath(normalized)
        if root_text.startswith("\\\\") or _WINDOWS_DRIVE_ROOT_RE.match(root_text):
            try:
                rel = path.relative_to(PureWindowsPath(root_text))
                if ".." not in rel.parts:
                    return rel.as_posix()
            except ValueError:
                pass
        return original

    if normalized.startswith("/") and root_text.startswith("/"):
        try:
            rel = PurePosixPath(normalized).relative_to(PurePosixPath(root_text))
            if ".." not in rel.parts:
                return rel.as_posix()
        except ValueError:
            pass
    return original


def _extract_error_summary(text: str, max_lines: int = 3) -> str:
    """折叠 traceback 栈帧,保留错误原因和最多三行关键上下文。"""
    lines: list[str] = []
    skip_frame_source = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("Traceback (most recent call last):"):
            skip_frame_source = False
            continue
        if _TRACEBACK_FRAME_RE.match(raw_line):
            skip_frame_source = True
            continue
        if skip_frame_source:
            skip_frame_source = False
            if raw_line[:1].isspace() or stripped.startswith("^"):
                continue
        if stripped in {
            "During handling of the above exception, another exception occurred:",
            "The above exception was the direct cause of the following exception:",
        }:
            continue
        if not lines or lines[-1] != stripped:
            lines.append(stripped)

    if not lines:
        return ""
    if len(lines) <= max_lines:
        return "; ".join(lines)

    final_index = len(lines) - 1
    scored: list[tuple[int, int]] = []
    for index, line in enumerate(lines[:-1]):
        score = 0
        if _ERROR_CONTEXT_RE.search(line):
            score += 4
        if _PATH_HINT_RE.search(line):
            score += 3
        if _ERROR_REASON_RE.search(line):
            score += 2
        if score:
            scored.append((score, index))

    selected = {final_index}
    for _score, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        selected.add(index)
        if len(selected) >= max_lines:
            break

    # 没有足够的显式标签时,补充最靠近错误开头的上下文。
    if len(selected) < max_lines:
        for index in range(final_index):
            selected.add(index)
            if len(selected) >= max_lines:
                break

    return "; ".join(lines[index] for index in sorted(selected))


def _truncate_error_part(text: str, max_len: int) -> str:
    """截断单段错误文本时同时保留开头类型和末尾原因。"""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    available = max_len - 3
    head_len = (available + 1) // 2
    tail_len = available - head_len
    if tail_len <= 0:
        return f"{text[:head_len]}..."
    return f"{text[:head_len].rstrip()}...{text[-tail_len:].lstrip()}"


def _limit_error_summary(text: str, max_len: int) -> str:
    """限长时分别保留上下文目标和末尾异常类型、原因。"""
    if len(text) <= max_len:
        return text
    if max_len <= 0:
        return ""

    # 摘要末段通常是异常类型和原因,单独分配空间可避免被长上下文挤掉。
    if "; " in text and max_len >= 24:
        context, reason = text.rsplit("; ", 1)
        available = max_len - 2
        context_len = available // 2
        reason_len = available - context_len
        return (
            f"{_truncate_error_part(context, context_len)}; "
            f"{_truncate_error_part(reason, reason_len)}"
        )
    return _truncate_error_part(text, max_len)


def _sanitize_error_message(
    exc,
    max_len: int = 300,
    workspace_root: str | None = None,
) -> str:
    """把底层异常转成可给模型看的短错误信息。

    做四件事:
      1. 折叠 traceback 栈帧,保留一至三行错误原因和关键上下文
      2. 只替换明确凭证值,保留字段名
      3. 工作区内绝对路径规范化为相对路径,外部路径保持原样
      4. 从首尾共同限长,保留操作目标和末尾错误原因

    workspace_root 默认用 os.getcwd() 兜底,不阻塞修复。
    """
    if workspace_root is None:
        try:
            workspace_root = os.getcwd()
        except Exception:
            workspace_root = None

    if isinstance(exc, str):
        text = exc
    else:
        detail = str(exc)
        error_name = type(exc).__name__
        text = f"{error_name}: {detail}" if detail else error_name

    text = _extract_error_summary(text)
    text = redact_explicit_secrets(text)

    # 路径只做工作区相对化,不再按关键词或工作区边界隐藏。
    text = _WINDOWS_ABS_PATH_RE.sub(
        lambda match: _normalize_path_text(match.group(0), workspace_root),
        text,
    )
    text = _UNIX_ABS_PATH_RE.sub(
        lambda match: _normalize_path_text(match.group(0), workspace_root),
        text,
    )

    text = text or "Tool execution failed."
    return _limit_error_summary(text, max_len)


def _short_error(exc) -> str:
    """旧 API 保留:脱敏 + 截断到 200 字符的简短错误描述。

    内部调 ``_sanitize_error_message``,所有调用点自动获得脱敏能力。
    """
    return _sanitize_error_message(exc, max_len=200)


def _detect_fatal_marker(text: str) -> str | None:
    """扫描普通字符串错误,命中 fatal 关键字返具体 marker 名。

    用于工具返非 JSON 字符串(或 JSON 解析失败)时仍能识别致命错误,
    避免安全 / 权限 / 路径逃逸类错误因没结构化 error_type 而被当成
    可恢复错误继续 loop。
    """
    if not text:
        return None
    lower = text.lower()
    for marker in _FATAL_MARKERS:
        if marker in lower:
            return marker
    return None


def _extract_approval_request(
    output: str,
    tool_call,
) -> dict | None:
    """从受信任 Tool Result 提取待审批请求，并绑定原始 tool_call 参数。"""
    if not isinstance(output, str):
        return None
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("approval_required") is not True:
        return None
    request = payload.get("approval_request")
    if not isinstance(request, dict):
        return None

    request_id = request.get("id")
    tool_name = request.get("tool_name")
    if (
        not isinstance(request_id, str)
        or not request_id.startswith("approval_")
        or tool_name not in {"file", "terminal", "gateway_send_file", "cron"}
    ):
        return None
    call_name = AgentLoop._tool_call_name(tool_call)
    if call_name != tool_name:
        return None
    try:
        arguments = json.loads(tool_call.function.arguments)
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(arguments, dict):
        return None

    details = request.get("details")
    return {
        "id": request_id,
        "tool_name": tool_name,
        "tool_call_id": str(getattr(tool_call, "id", "")),
        "arguments": arguments,
        "summary": str(request.get("summary", "需要批准的工具操作")),
        "details": details if isinstance(details, dict) else {},
    }


def build_assistant_msg_dict(
    assistant_msg,
    *,
    preserve_reasoning: bool = False,
) -> dict:
    """把 SDK 的 assistant message 对象转成可序列化 dict。"""
    msg_dict: dict = {
        "role": "assistant",
        "content": assistant_msg.content or "",
    }
    reasoning_content = getattr(
        assistant_msg,
        "reasoning_content",
        None,
    )
    if preserve_reasoning and reasoning_content is not None:
        # 仅为截断续写保存协议状态，普通最终回复不额外持久化隐藏推理。
        msg_dict["reasoning_content"] = str(reasoning_content)
    if assistant_msg.tool_calls:
        # 思考模型要求后续工具轮完整回传该字段；普通模型缺少该字段时
        # 补空串，使同一份历史也能被要求该协议字段的 fallback 接受。
        msg_dict["reasoning_content"] = (
            "" if reasoning_content is None else str(reasoning_content)
        )
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
    return msg_dict


def _object_value(obj, name: str, default=None):
    """兼容 SDK 对象与普通 dict 的只读字段访问。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _optional_int(value) -> int | None:
    """把 usage / HTTP 状态中的整数安全规范化。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _content_char_count(value) -> int:
    """只统计正文长度，不复制或持久化正文。"""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(value)
    except TypeError:
        return 0


def _model_error_category(exc) -> tuple[int | None, str]:
    """从状态码和异常类型生成不包含响应正文的稳定错误分类。"""
    status = _optional_int(getattr(exc, "status_code", None))
    if status is None:
        response = getattr(exc, "response", None)
        status = _optional_int(getattr(response, "status_code", None))

    # 这里只用异常文本做内存内分类，事件中不保存原文或响应 body。
    error_text = str(exc).lower()
    exception_name = type(exc).__name__.lower()
    if status == 400 and "context" in error_text:
        return status, "context_overflow"
    if status == 400:
        return status, "bad_request"
    if status in (401, 403):
        return status, "auth"
    if status == 404:
        return status, "model_not_found"
    if status == 408 or "timeout" in exception_name or "timed out" in error_text:
        return status, "timeout"
    if status == 429:
        return status, "rate_limit"
    if status is not None and status >= 500:
        return status, "server_error"
    if "connection" in exception_name or "connection" in error_text:
        return status, "network_error"
    if status is not None:
        return status, f"http_{status}"
    return None, "unknown"


def _model_call_event(
    *,
    iteration: int,
    model: str,
    model_role: str,
    latency_ms: int,
    outcome: str,
    response=None,
    assistant_msg=None,
    finish_reason=None,
    error=None,
) -> dict:
    """构造不含 prompt、回答、推理正文和异常正文的模型调用诊断。"""
    content = _object_value(assistant_msg, "content")
    reasoning = _object_value(assistant_msg, "reasoning_content")
    tool_calls = _object_value(assistant_msg, "tool_calls") or []
    usage = _object_value(response, "usage")
    completion_details = _object_value(usage, "completion_tokens_details")
    prompt_details = _object_value(usage, "prompt_tokens_details")

    http_status = None
    error_category = None
    exception_type = None
    if error is not None:
        http_status, error_category = _model_error_category(error)
        exception_type = type(error).__name__
    elif outcome != "success":
        error_category = outcome

    return {
        "iteration": iteration,
        "model": str(model),
        "model_role": model_role,
        "outcome": outcome,
        "finish_reason": (
            None if finish_reason is None else str(finish_reason)
        ),
        "latency_ms": max(0, int(latency_ms)),
        "has_content": bool(content),
        "content_chars": _content_char_count(content),
        "has_reasoning": bool(reasoning),
        "reasoning_chars": _content_char_count(reasoning),
        "tool_call_count": len(tool_calls),
        "prompt_tokens": _optional_int(_object_value(usage, "prompt_tokens")),
        "completion_tokens": _optional_int(
            _object_value(usage, "completion_tokens")
        ),
        "total_tokens": _optional_int(_object_value(usage, "total_tokens")),
        "reasoning_tokens": _optional_int(
            _object_value(completion_details, "reasoning_tokens")
            or _object_value(usage, "reasoning_tokens")
        ),
        "cached_tokens": _optional_int(
            _object_value(prompt_details, "cached_tokens")
            or _object_value(usage, "cached_tokens")
        ),
        "http_status": http_status,
        "error_category": error_category,
        "exception_type": exception_type,
    }


def dispatch_tool_call(
    tool_call,
    registry,
    *,
    session_key: str | None = None,
    blocked_tools: set[str] | None = None,
    tool_context: dict | None = None,
) -> tuple[str, str | None, str | None]:
    """处理单个 tool_call。

    返回 ``(tool_message_content, error_status, error_detail)``:
      - 成功: ``(output, None, None)``
      - blocked 工具: ``("(error: ...)", "blocked", "blocked tool invoked: <name>")``
      - JSON 参数解析失败: ``("(error: ...)", "json", "invalid JSON in <name>: <exc>")``
      - dispatch 抛异常: ``("(error: ...)", "dispatch", "tool <name> raised: <exc>")``

    AgentLoop 只统一摘要参数解析与 dispatch 异常。正常 output 由具体
    工具边界负责；Terminal/File 处理各自成功出口中的明确凭证值，
    这里不对所有正常 Tool Result 做全局扫描。
    """
    tool_name = tool_call.function.name

    if blocked_tools and tool_name in blocked_tools:
        return (
            f"(error: '{tool_name}' is blocked)",
            "blocked",
            f"blocked tool invoked: {tool_name!r}",
        )

    try:
        tool_args = json.loads(tool_call.function.arguments)
    except Exception as exc:
        short = _short_error(exc)
        return (
            f"(error: invalid JSON arguments in {tool_name}: {short})",
            "json",
            f"invalid JSON in tool_call {tool_name!r}: {short}",
        )

    try:
        dispatch_context = dict(tool_context or {})
        durable_context = dispatch_context.pop("durable_tool_execution", None)
        # 普通 AgentLoop 不得把内部审批许可透传给工具。
        dispatch_context.pop("allow_sensitive", None)
        dispatch_context.pop("approval_grant", None)
        dispatch_context["session_key"] = session_key
        if durable_context is None:
            output = registry.dispatch(tool_name, tool_args, **dispatch_context)
        else:
            from hermes.durable_tool_dispatcher import (
                DurableToolDispatcher,
                DurableToolExecutionContext,
            )

            context = DurableToolExecutionContext.from_value(durable_context)
            if context is None:
                output = registry.dispatch(tool_name, tool_args, **dispatch_context)
            else:
                output = DurableToolDispatcher(
                    registry,
                    context,
                ).dispatch(
                    tool_name,
                    tool_args,
                    tool_call_id=tool_call.id,
                    **dispatch_context,
                )
    except Exception as exc:
        short = _short_error(exc)
        return (
            f"(error: tool {tool_name} failed: {short})",
            "dispatch",
            f"tool {tool_name!r} raised: {type(exc).__name__}: {short}",
        )

    return output, None, None


# ---------------------------------------------------------------------------
# AgentLoop —— 模板方法基类
# ---------------------------------------------------------------------------

class AgentLoop:
    """公共循环骨架。

    子类通过覆盖下列 hook 注入主会话行为:
      - ``init_messages``               构造初始 messages(默认单条 user)
      - ``pre_model_call``              模型调用前(主会话用来做 compression)
      - ``call_model``                  实际 API 调用
      - ``handle_model_error``          模型异常处理,返回 "retry"/"abort"/"raise"
      - ``on_assistant_message``        普通 assistant msg 追加后
      - ``should_continue``             是否触发 continuation
      - ``continuation_message``        续写 prompt
      - ``on_continuation_message``     continuation 追加后(主会话 add_messages)
      - ``on_tool_dispatch_start``      即将处理 tool_calls(主会话重置 continuation_count)
      - ``dispatch_one``                处理单个 tool_call
      - ``on_tool_message``             单条 tool msg 追加后
      - ``on_tool_messages_batch``      assistant tool_call + tool results 完成后
    """

    # 同一 (tool_name, error_type) 连续错误上限。超过即升级为 fatal:
    # 避免模型卡在反复传错参数的死循环里,浪费 token / iteration。
    TOOL_ERROR_LIMIT = 3

    def __init__(
        self,
        *,
        model: str,
        max_iterations: int,
        tools: list[dict],
        system_prompt: str,
        registry,
        client=_default_client,
        session_key: str | None = None,
        blocked_tools: set[str] | None = None,
        model_kwargs: dict | None = None,
        cancel_checker: "Callable[[], bool] | None" = None,
        tool_context: dict | None = None,
    ):
        self.model = model
        self.max_iterations = max_iterations
        self.tools = tools
        self.system_prompt = system_prompt
        self.registry = registry
        self.client = client
        self.session_key = session_key
        self.blocked_tools = set(blocked_tools) if blocked_tools else set()
        # 调用方可显式传递平台能力上下文，工具处理器只读取所需字段。
        self.tool_context = dict(tool_context) if tool_context else {}
        # provider-specific 额外参数(如 extra_body / temperature 等)。
        # AgentLoop 只透传,不理解内容;由 ConversationAgentLoop /
        # DelegateAgentLoop 的调用方决定。
        self.model_kwargs = dict(model_kwargs) if model_kwargs else {}
        # 协作式取消检查器:返回 True 表示外部已请求取消,循环应尽快退出。
        # 默认 None = 不检查。后台 delegate 用它实现 cancel。
        self.cancel_checker = cancel_checker
        # 运行期状态(每次 run() 重置)
        self.iterations = 0
        self.tools_used: list[str] = []
        # 工具错误计数:按 (tool_name, error_type) 累计连续失败次数。
        # 工具成功调用后清掉该 tool_name 的所有计数,避免历史错误干扰。
        self._tool_error_counts: dict[tuple[str, str], int] = {}

    # --- 取消检查(后台 delegate 用) ---

    def _is_cancelled(self) -> bool:
        return self.cancel_checker is not None and bool(self.cancel_checker())

    def _cancel_result(self, messages: list[dict]) -> "AgentLoopResult":
        return self._result(
            ok=False, status="cancelled",
            summary=self.last_assistant_text(messages),
            messages=messages, error="cancel requested",
            error_type="cancelled", fatal=True, retryable=False,
        )

    # --- 边界错误结果 helper(让 run() 调用点统一) ---

    def _model_error_result(self, messages, error):
        """模型最终失败:不可继续 loop,但调用方可能下次能重试整个 agent。"""
        return self._result(
            ok=False, status="model_error",
            summary=self.last_assistant_text(messages),
            messages=messages, error=error,
            error_type="model_error", fatal=True, retryable=True,
        )

    def _persistence_error_result(self, messages, error):
        """DB 持久化失败:数据完整性问题,不重试。"""
        return self._result(
            ok=False, status="error",
            summary=self.last_assistant_text(messages),
            messages=messages, error=error,
            error_type="persistence_error", fatal=True, retryable=False,
        )

    def _internal_error_result(self, messages, error):
        """未预期异常:兜底,避免原始异常冒到最外层。"""
        return self._result(
            ok=False, status="error",
            summary=self.last_assistant_text(messages),
            messages=messages, error=error,
            error_type="internal_error", fatal=True, retryable=False,
        )

    # --- 工具错误致命判定 ---

    # 致命工具错误集合:模型即使看到错误也无法修正,继续 loop 只会无限循环。
    # 安全 / 权限 / 路径逃逸 / DB / 取消 都属于这一类。
    _FATAL_TOOL_ERROR_TYPES = frozenset({
        "forbidden",
        "permission_denied",
        "path_escape",
        "safety_blocked",
        "cancelled",
        "persistence_error",
        "internal_error",
    })

    def _classify_tool_error(
        self,
        output: str,
        err_status: str | None,
    ) -> tuple[bool, str]:
        """判断工具错误是否致命。返回 (fatal, error_type)。

        判定顺序:
          1. err_status == "blocked":致命(模型调黑名单工具)
          2. 先确认 output 是否为错误:err_status 非空、顶层 JSON 明确包含
             error / error_type / fatal / ok=false,或文本以 ``(error:`` 开头
          3. 仅对已确认的错误扫描 fatal marker:
             safety_blocked / forbidden / permission_denied / path_escape /
             persistence_error / cancelled → 致命。即使 err_status 是
             "json" / "dispatch",只要异常文本里含 fatal marker 就立即终止,
             避免 dispatch 抛出含 "permission_denied" 的异常被误判为可恢复。
          4. err_status in {"json", "dispatch"}:非致命(模型参数 / 调用问题,可修正)
          5. output 是 JSON 含 error_type 字段:
             - fatal=True 或 error_type 在致命集合 → 致命
             - 其它 error_type → 非致命
             - 有 error 但无 error_type → unknown_error(非致命)
          6. 其它:非致命(默认让模型继续)

        为什么允许非致命错误继续 loop:模型可能传错参数 / 调不存在文件,
        看到错误后能调整。直接终止会让简单工具错误升级成整个 agent 失败。
        """
        if err_status == "blocked":
            return True, "blocked"

        obj = None
        if isinstance(output, str):
            try:
                obj = json.loads(output)
            except (ValueError, TypeError):
                pass

        structured_error = isinstance(obj, dict) and (
            obj.get("ok") is False
            or "error" in obj
            or bool(obj.get("error_type"))
            or obj.get("fatal") is True
        )
        explicit_text_error = (
            isinstance(output, str)
            and output.lstrip().lower().startswith("(error:")
        )
        confirmed_error = bool(err_status) or structured_error or explicit_text_error

        # 只扫描已确认的错误,避免正常文件内容 / terminal 输出中的 fatal
        # 关键字被误判。dispatch 异常仍可通过 marker 立即升级为致命错误。
        if confirmed_error and isinstance(output, str):
            marker = _detect_fatal_marker(output)
            if marker:
                return True, marker

        if err_status in ("json", "dispatch"):
            # 调用层错误(参数 JSON 非法 / 工具抛异常):非致命,让模型修正
            return False, err_status

        if isinstance(obj, dict):
            err_type = obj.get("error_type")
            if obj.get("fatal") is True:
                return True, err_type or "fatal_flagged"
            if err_type in self._FATAL_TOOL_ERROR_TYPES:
                return True, err_type
            if err_type:
                return False, err_type
            # 有 error 字段但没 error_type(如 registry 的 "Unknown tool"
            # 返回 {"error": "..."}):归类为 unknown_error,让计数逻辑
            # 能把它和真正的成功调用(无 error 字段)区分开。
            if "error" in obj or obj.get("ok") is False:
                return False, "unknown_error"

        return False, ""

    # ===================== 模板方法 =====================

    def run(self, user_message: str) -> AgentLoopResult:
        """跑一次完整循环。从单条 user_message 开始。

        顶层 try/except 兜底:任何未预期异常都包装成 internal_error,
        不让原始异常(openai client / sqlite3 / json)冒到最外层。
        """
        try:
            return self._run_inner(user_message)
        except Exception as exc:
            # 内部 _run_inner 已经处理了 model / persistence / tool 等已知
            # 异常,真到这里说明是未预期 bug,统一标 internal_error
            return self._internal_error_result(
                messages=[], error=f"unhandled exception: {exc!r}",
            )

    def _run_inner(self, user_message: str) -> AgentLoopResult:
        messages = self.init_messages(user_message)
        self.iterations = 0
        self.tools_used = []

        for iteration in range(self.max_iterations):
            # 1) iteration 开始前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)

            self.iterations = iteration + 1
            messages = self.pre_model_call(messages)

            # 2) 模型调用前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)

            # 模型调用 —— 走 handle_model_error 决定后续动作
            call_model = str(self.model)
            call_model_role = self._model_role()
            call_started = time.perf_counter()
            try:
                response = self.call_model(messages)
            except Exception as exc:
                self._emit_model_call_event(
                    _model_call_event(
                        iteration=self.iterations,
                        model=call_model,
                        model_role=call_model_role,
                        latency_ms=(time.perf_counter() - call_started) * 1000,
                        outcome="error",
                        error=exc,
                    )
                )
                decision = self.handle_model_error(exc, messages)
                if decision == "retry":
                    continue
                if decision == "abort":
                    # 模型最终失败:返回结构化 model_error,不抛异常
                    return self._model_error_result(messages, repr(exc))
                # "raise" 或任何未知返回值都重新抛,但被顶层兜底 catch
                raise

            assistant_msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason
            has_output = bool(assistant_msg.content or assistant_msg.tool_calls)
            outcome = "success"
            if not has_output:
                outcome = (
                    "output_length_exhausted"
                    if finish_reason == "length"
                    else "empty_model_response"
                )
            self._emit_model_call_event(
                _model_call_event(
                    iteration=self.iterations,
                    model=call_model,
                    model_role=call_model_role,
                    latency_ms=(time.perf_counter() - call_started) * 1000,
                    outcome=outcome,
                    response=response,
                    assistant_msg=assistant_msg,
                    finish_reason=finish_reason,
                )
            )

            # 模型请求期间可能收到 /stop 或后续消息。响应返回后必须
            # 再检查一次,避免把已经过时的内容写入历史或继续发送。
            if self._is_cancelled():
                return self._cancel_result(messages)

            # reasoning-only 与完全空响应都不是合法 assistant 输出，必须在
            # append 前处理，避免污染同轮重试或 fallback 的请求历史。
            if not has_output:
                reasoning_content = getattr(
                    assistant_msg,
                    "reasoning_content",
                    None,
                )
                if finish_reason == "length" and reasoning_content:
                    continuation_msg = build_assistant_msg_dict(
                        assistant_msg,
                        preserve_reasoning=True,
                    )
                    messages.append(continuation_msg)
                    if self.should_continue(finish_reason, messages):
                        try:
                            self.on_assistant_message(
                                continuation_msg,
                                response,
                            )
                        except Exception as exc:
                            return self._persistence_error_result(
                                messages,
                                repr(exc),
                            )
                        cont_msg = self.continuation_message()
                        messages.append(cont_msg)
                        try:
                            self.on_continuation_message(cont_msg)
                        except Exception as exc:
                            return self._persistence_error_result(
                                messages,
                                repr(exc),
                            )
                        continue
                    messages.pop()
                decision = self.handle_model_error(
                    RuntimeError("model returned empty content with no tool_calls"),
                    messages,
                )
                if decision == "retry":
                    continue
                return self._model_error_result(
                    messages,
                    "model returned empty content with no tool_calls",
                )

            msg_dict = build_assistant_msg_dict(
                assistant_msg,
                preserve_reasoning=finish_reason == "length",
            )
            messages.append(msg_dict)

            if assistant_msg.tool_calls:
                # assistant tool_call 必须等对应 tool result 生成后一起持久化,
                # 避免数据库里出现只有 tool_call 没有 tool result 的半截历史。
                self.on_tool_dispatch_start()
                # 3) tool 调用前检查取消
                if self._is_cancelled():
                    return self._cancel_result(messages)
                try:
                    tool_messages, tool_error = self.process_tool_calls(
                        assistant_msg.tool_calls, messages
                    )
                except Exception as exc:
                    # 工具分发过程中的持久化 / 结构异常
                    return self._persistence_error_result(messages, repr(exc))
                try:
                    self.on_tool_messages_batch(msg_dict, tool_messages, response)
                except Exception as exc:
                    # DB 写入失败:assistant + tool_messages 整组未落盘,停止 loop
                    return self._persistence_error_result(messages, repr(exc))
                if tool_error is not None:
                    return tool_error
                continue

            # continuation hook(主会话:finish_reason == "length")
            if self.should_continue(finish_reason, messages):
                try:
                    self.on_assistant_message(msg_dict, response)
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                cont_msg = self.continuation_message()
                messages.append(cont_msg)
                try:
                    self.on_continuation_message(cont_msg)
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                continue

            try:
                self.on_final_assistant_message(msg_dict, response)
            except Exception as exc:
                return self._persistence_error_result(messages, repr(exc))
            return self._result(
                ok=True, status="completed",
                summary=assistant_msg.content or "",
                messages=messages,
            )

        # 跑满 max_iterations 仍未完成
        return self._result(
            ok=False, status="max_iterations",
            summary=self.last_assistant_text(messages),
            messages=messages,
        )

    def process_tool_calls(
        self,
        tool_calls,
        messages,
    ) -> tuple[list[dict], AgentLoopResult | None]:
        """处理本轮所有 tool_calls,返回生成的 tool messages 和可选错误结果。

        错误分类策略:
          - 致命错误(safety_blocked / forbidden / permission_denied /
            path_escape / cancelled / persistence_error):立即终止 loop。
            这类错误模型即使看到也无法修正,继续只会无限循环或越界。
          - 非致命错误(invalid_json / unknown_tool / file_not_found /
            ambiguous_match / 普通工具异常):包装成合法 tool message
            追加到上下文,让模型下一轮有机会修正参数或换做法。
          - 同一 (tool_name, error_type) 连续失败次数达到
            ``TOOL_ERROR_LIMIT``:升级为 fatal,终止 loop。
            避免模型卡在反复传错参数的死循环里。
          - 工具成功调用:清掉该 tool_name 的所有错误计数。
        """
        tool_messages: list[dict] = []
        fatal_detail: str | None = None
        fatal_error_type: str | None = None
        approval_request: dict | None = None
        for tc in tool_calls:
            if approval_request is not None:
                output = build_approval_deferred()
                err_status = None
                err_detail = None
            else:
                try:
                    output, err_status, err_detail = self.dispatch_one(tc)
                except Exception as exc:
                    # dispatch_one 自身出 bug(不是工具返错,是分发机制炸了)
                    tool_name = self._tool_call_name(tc)
                    short = _short_error(exc)
                    output = f"(error: tool {tool_name} failed: {short})"
                    err_status = "dispatch"
                    err_detail = f"tool {tool_name!r} dispatch raised: {short}"

            tc_name = self._tool_call_name(tc)
            if tc_name not in self.tools_used:
                self.tools_used.append(tc_name)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            }
            messages.append(tool_msg)
            tool_messages.append(tool_msg)
            self.on_tool_message(tc, tool_msg, output)

            # 已经决定终止,后续 tool_call 仍生成 tool_msg 让 batch 持久化完整
            if fatal_detail is not None:
                continue

            pending = _extract_approval_request(output, tc)
            if pending is not None:
                approval_request = pending
                continue

            fatal, err_type = self._classify_tool_error(output, err_status)

            if fatal:
                # safety / 权限 / 路径逃逸 / cancelled / persistence:必须终止
                fatal_detail = (
                    err_detail
                    or f"fatal tool error ({err_type}) in {tc_name!r}"
                )
                fatal_error_type = err_type or "tool_error"
                continue

            # 非致命:计数。工具成功(err_status falsy 且无 error_type)清计数
            # 注意 _classify_tool_error 成功时返 err_type="",不是 None。
            if not err_type and not err_status:
                self._clear_tool_error_counts(tc_name)
            else:
                # 计数 key 按 (tool_name, error_type) 隔离:工具 A 的错误
                # 不应被工具 B 的成功清掉,也不应被工具 B 的错误累计影响。
                display_type = err_type or err_status or "unknown"
                key = (tc_name, display_type)
                self._tool_error_counts[key] = (
                    self._tool_error_counts.get(key, 0) + 1
                )
                if self._tool_error_counts[key] >= self.TOOL_ERROR_LIMIT:
                    # 同类错误连续超上限:升级 fatal,防模型死循环
                    fatal_detail = (
                        f"tool {tc_name!r} repeated "
                        f"{display_type} "
                        f"{self._tool_error_counts[key]} times; aborting"
                    )
                    fatal_error_type = display_type

        if approval_request is not None:
            return tool_messages, self._result(
                ok=False,
                status="awaiting_approval",
                summary="",
                messages=messages,
                error="tool operation is awaiting remote approval",
                error_type="approval_required",
                fatal=False,
                retryable=False,
                approval_request=approval_request,
            )
        if fatal_detail is not None:
            return tool_messages, self._result(
                ok=False, status="tool_error",
                summary=self.last_assistant_text(messages),
                messages=messages, error=fatal_detail,
                error_type=fatal_error_type or "tool_error",
                fatal=True, retryable=False,
            )
        return tool_messages, None

    def _clear_tool_error_counts(self, tool_name: str) -> None:
        """工具成功调用后清掉该 tool 的所有错误计数。

        避免历史错误累积影响后续正常流程:模型修正参数后应重新获得
        完整 TOOL_ERROR_LIMIT 次重试机会。
        """
        keys = [k for k in self._tool_error_counts if k[0] == tool_name]
        for k in keys:
            del self._tool_error_counts[k]

    # ===================== 可覆盖 hooks =====================

    def init_messages(self, user_message: str) -> list[dict]:
        """构造初始 messages。默认单条 user message。"""
        return [{"role": "user", "content": user_message}]

    def pre_model_call(self, messages: list[dict]) -> list[dict]:
        """模型调用前的 hook。返回(可能修改后的)messages。默认无操作。"""
        return messages

    def call_model(self, messages: list[dict]):
        """实际 API 调用。``model_kwargs`` 原样透传给 provider SDK。"""
        api_messages = (
            [{"role": "system", "content": self.system_prompt}] + messages
        )
        return self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            tools=self.tools if self.tools else None,
            **self.model_kwargs,
        )

    def _model_role(self) -> str:
        """诊断中只区分主模型与 fallback，不记录供应商凭证或地址。"""
        return "fallback" if bool(getattr(self, "_using_fallback", False)) else "primary"

    def _emit_model_call_event(self, event: dict) -> None:
        """诊断是 best effort，记录失败不得改变 AgentLoop 结果。"""
        try:
            self.on_model_call_event(event)
        except Exception:
            pass

    def on_model_call_event(self, event: dict) -> None:
        """模型调用诊断 hook；默认不持久化。"""
        pass

    def handle_model_error(self, exc, messages) -> str:
        """模型调用异常时调用。返回:
          - "retry": 跳过本轮 tool_calls,进下一轮 iteration
          - "abort": 作为 model_error 返回
          - "raise": 重新抛异常(默认)
        """
        return "raise"

    def on_assistant_message(self, msg_dict: dict, response) -> None:
        """普通 assistant msg 追加后调用。默认空。"""
        pass

    def on_final_assistant_message(self, msg_dict: dict, response) -> None:
        """最终 assistant 消息 hook;默认保持旧持久化行为。"""
        self.on_assistant_message(msg_dict, response)

    def should_continue(self, finish_reason: str, messages: list[dict]) -> bool:
        """是否触发 continuation(默认不触发)。"""
        return False

    def continuation_message(self) -> dict:
        """continuation 时塞回的 prompt。"""
        return {"role": "user", "content": "Please continue from where you left off."}

    def on_continuation_message(self, cont_msg: dict) -> None:
        """continuation msg 追加后(主会话 add_messages)。默认空。"""
        pass

    def on_tool_dispatch_start(self) -> None:
        """即将进入 tool_call 处理。主会话用来重置 continuation_count。默认空。"""
        pass

    def dispatch_one(self, tool_call) -> tuple[str, str | None, str | None]:
        """处理单个 tool_call。默认走 dispatch_tool_call helper。

        返回值里的 error_status 表示工具执行失败,但调用方仍会生成
        合法 tool message,再由 batch hook 原子持久化。
        """
        tool_context = dict(self.tool_context)
        if (
            self.cancel_checker is not None
            and self._tool_call_name(tool_call) == "terminal"
        ):
            tool_context["cancel_checker"] = self.cancel_checker
        return dispatch_tool_call(
            tool_call, self.registry,
            session_key=self.session_key,
            blocked_tools=self.blocked_tools,
            tool_context=tool_context,
        )

    def on_tool_message(self, tool_call, tool_msg: dict, output: str) -> None:
        """单条 tool msg 追加后调用。默认空。"""
        pass

    def on_tool_messages_batch(
        self,
        assistant_msg: dict,
        tool_messages: list[dict],
        response,
    ) -> None:
        """assistant tool_call 与对应 tool results 全部生成后调用。默认空。"""
        pass

    # ===================== 辅助 =====================

    @staticmethod
    def _tool_call_name(tool_call) -> str:
        function = getattr(tool_call, "function", None)
        return getattr(function, "name", "<unknown>")

    @staticmethod
    def last_assistant_text(messages: list[dict]) -> str:
        """取最后一段 assistant 文本(用于异常 / max_iter 路径的 summary)。"""
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                return m["content"]
        return ""

    def _result(
        self,
        *,
        ok: bool,
        status: str,
        summary: str,
        messages: list[dict],
        error: str | None = None,
        error_type: str | None = None,
        fatal: bool = False,
        retryable: bool = True,
        approval_request: dict | None = None,
    ) -> AgentLoopResult:
        """统一构造结果对象。"""
        return AgentLoopResult(
            ok=ok, status=status, summary=summary,
            messages=messages, iterations=self.iterations,
            tools_used=list(self.tools_used),
            error=error, error_type=error_type,
            fatal=fatal, retryable=retryable,
            approval_request=approval_request,
        )


# ---------------------------------------------------------------------------
# AsyncAgentLoop —— Gateway 专用异步循环骨架
# ---------------------------------------------------------------------------

class AsyncAgentLoop(AgentLoop):
    """异步 Agent 循环。

    保留 ``AgentLoop`` 的结果类型、错误判定和纯函数 hooks,只把模型请求、
    持久化 hooks 与工具分发改为可等待调用。同步 ``AgentLoop`` 不受影响。
    """

    def __init__(
        self,
        *,
        model: str,
        max_iterations: int,
        tools: list[dict],
        system_prompt: str,
        registry,
        client,
        session_key: str | None = None,
        blocked_tools: set[str] | None = None,
        model_kwargs: dict | None = None,
        cancel_checker: "Callable[[], bool] | None" = None,
        tool_context: dict | None = None,
    ):
        super().__init__(
            model=model,
            max_iterations=max_iterations,
            tools=tools,
            system_prompt=system_prompt,
            registry=registry,
            client=client,
            session_key=session_key,
            blocked_tools=blocked_tools,
            model_kwargs=model_kwargs,
            cancel_checker=cancel_checker,
            tool_context=tool_context,
        )

    async def run(self, user_message: str) -> AgentLoopResult:
        """异步跑一次完整循环,Task 取消必须原样向上传播。"""
        try:
            return await self._run_inner(user_message)
        except asyncio.CancelledError:
            # 真正取消模型 HTTP 请求依赖 CancelledError 继续传到 Runner。
            raise
        except Exception as exc:
            return self._internal_error_result(
                messages=[], error=f"unhandled exception: {exc!r}",
            )

    async def _run_inner(self, user_message: str) -> AgentLoopResult:
        messages = self.init_messages(user_message)
        self.iterations = 0
        self.tools_used = []

        for iteration in range(self.max_iterations):
            if self._is_cancelled():
                return self._cancel_result(messages)

            self.iterations = iteration + 1
            messages = await self.pre_model_call(messages)

            if self._is_cancelled():
                return self._cancel_result(messages)

            call_model = str(self.model)
            call_model_role = self._model_role()
            call_started = time.perf_counter()
            try:
                response = await self.call_model(messages)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_model_call_event_async(
                    _model_call_event(
                        iteration=self.iterations,
                        model=call_model,
                        model_role=call_model_role,
                        latency_ms=(time.perf_counter() - call_started) * 1000,
                        outcome="error",
                        error=exc,
                    )
                )
                decision = await self.handle_model_error(exc, messages)
                if decision == "retry":
                    continue
                if decision == "abort":
                    return self._model_error_result(messages, repr(exc))
                raise

            assistant_msg = response.choices[0].message
            finish_reason = response.choices[0].finish_reason
            has_output = bool(assistant_msg.content or assistant_msg.tool_calls)
            outcome = "success"
            if not has_output:
                outcome = (
                    "output_length_exhausted"
                    if finish_reason == "length"
                    else "empty_model_response"
                )
            await self._emit_model_call_event_async(
                _model_call_event(
                    iteration=self.iterations,
                    model=call_model,
                    model_role=call_model_role,
                    latency_ms=(time.perf_counter() - call_started) * 1000,
                    outcome=outcome,
                    response=response,
                    assistant_msg=assistant_msg,
                    finish_reason=finish_reason,
                )
            )

            # 保留协作式取消检查,处理未通过 Task.cancel() 触发的旧调用方。
            if self._is_cancelled():
                return self._cancel_result(messages)

            # reasoning-only 与完全空响应都不能进入下一次模型请求的历史。
            if not has_output:
                reasoning_content = getattr(
                    assistant_msg,
                    "reasoning_content",
                    None,
                )
                if finish_reason == "length" and reasoning_content:
                    continuation_msg = build_assistant_msg_dict(
                        assistant_msg,
                        preserve_reasoning=True,
                    )
                    messages.append(continuation_msg)
                    if self.should_continue(finish_reason, messages):
                        try:
                            await self.on_assistant_message(
                                continuation_msg,
                                response,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            return self._persistence_error_result(
                                messages,
                                repr(exc),
                            )
                        cont_msg = self.continuation_message()
                        messages.append(cont_msg)
                        try:
                            await self.on_continuation_message(cont_msg)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            return self._persistence_error_result(
                                messages,
                                repr(exc),
                            )
                        continue
                    messages.pop()
                decision = await self.handle_model_error(
                    RuntimeError("model returned empty content with no tool_calls"),
                    messages,
                )
                if decision == "retry":
                    continue
                return self._model_error_result(
                    messages,
                    "model returned empty content with no tool_calls",
                )

            msg_dict = build_assistant_msg_dict(
                assistant_msg,
                preserve_reasoning=finish_reason == "length",
            )
            messages.append(msg_dict)

            if assistant_msg.tool_calls:
                self.on_tool_dispatch_start()
                if self._is_cancelled():
                    return self._cancel_result(messages)
                try:
                    tool_messages, tool_error = await self.process_tool_calls(
                        assistant_msg.tool_calls, messages,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                try:
                    await self.on_tool_messages_batch(
                        msg_dict, tool_messages, response,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                if tool_error is not None:
                    return tool_error
                continue

            if self.should_continue(finish_reason, messages):
                try:
                    await self.on_assistant_message(msg_dict, response)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                cont_msg = self.continuation_message()
                messages.append(cont_msg)
                try:
                    await self.on_continuation_message(cont_msg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                continue

            try:
                await self.on_final_assistant_message(msg_dict, response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._persistence_error_result(messages, repr(exc))
            return self._result(
                ok=True, status="completed",
                summary=assistant_msg.content or "",
                messages=messages,
            )

        return self._result(
            ok=False, status="max_iterations",
            summary=self.last_assistant_text(messages),
            messages=messages,
        )

    async def process_tool_calls(
        self,
        tool_calls,
        messages,
    ) -> tuple[list[dict], AgentLoopResult | None]:
        """异步处理工具调用,错误分类与同步循环保持一致。"""
        tool_messages: list[dict] = []
        fatal_detail: str | None = None
        fatal_error_type: str | None = None
        approval_request: dict | None = None
        for tc in tool_calls:
            if approval_request is not None:
                output = build_approval_deferred()
                err_status = None
                err_detail = None
            else:
                try:
                    output, err_status, err_detail = await self.dispatch_one(tc)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tool_name = self._tool_call_name(tc)
                    short = _short_error(exc)
                    output = f"(error: tool {tool_name} failed: {short})"
                    err_status = "dispatch"
                    err_detail = f"tool {tool_name!r} dispatch raised: {short}"

            tc_name = self._tool_call_name(tc)
            if tc_name not in self.tools_used:
                self.tools_used.append(tc_name)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            }
            messages.append(tool_msg)
            tool_messages.append(tool_msg)
            await self.on_tool_message(tc, tool_msg, output)

            if fatal_detail is not None:
                continue

            pending = _extract_approval_request(output, tc)
            if pending is not None:
                approval_request = pending
                continue

            fatal, err_type = self._classify_tool_error(output, err_status)
            if fatal:
                fatal_detail = (
                    err_detail
                    or f"fatal tool error ({err_type}) in {tc_name!r}"
                )
                fatal_error_type = err_type or "tool_error"
                continue

            if not err_type and not err_status:
                self._clear_tool_error_counts(tc_name)
            else:
                display_type = err_type or err_status or "unknown"
                key = (tc_name, display_type)
                self._tool_error_counts[key] = (
                    self._tool_error_counts.get(key, 0) + 1
                )
                if self._tool_error_counts[key] >= self.TOOL_ERROR_LIMIT:
                    fatal_detail = (
                        f"tool {tc_name!r} repeated "
                        f"{display_type} "
                        f"{self._tool_error_counts[key]} times; aborting"
                    )
                    fatal_error_type = display_type

        if approval_request is not None:
            return tool_messages, self._result(
                ok=False,
                status="awaiting_approval",
                summary="",
                messages=messages,
                error="tool operation is awaiting remote approval",
                error_type="approval_required",
                fatal=False,
                retryable=False,
                approval_request=approval_request,
            )
        if fatal_detail is not None:
            return tool_messages, self._result(
                ok=False, status="tool_error",
                summary=self.last_assistant_text(messages),
                messages=messages, error=fatal_detail,
                error_type=fatal_error_type or "tool_error",
                fatal=True, retryable=False,
            )
        return tool_messages, None

    # ===================== 异步 hooks =====================

    async def pre_model_call(self, messages: list[dict]) -> list[dict]:
        """模型调用前的异步 hook。默认无操作。"""
        return messages

    async def call_model(self, messages: list[dict]):
        """异步模型调用。``model_kwargs`` 原样透传给 provider SDK。"""
        api_messages = (
            [{"role": "system", "content": self.system_prompt}] + messages
        )
        return await self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            tools=self.tools if self.tools else None,
            **self.model_kwargs,
        )

    async def _emit_model_call_event_async(self, event: dict) -> None:
        """异步诊断同样 best effort，不得让记录失败中断会话。"""
        try:
            await self.on_model_call_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def on_model_call_event(self, event: dict) -> None:
        """异步模型调用诊断 hook；默认不持久化。"""
        pass

    async def handle_model_error(self, exc, messages) -> str:
        """模型调用异常时调用。默认重新抛出。"""
        return "raise"

    async def on_assistant_message(self, msg_dict: dict, response) -> None:
        """普通 assistant msg 追加后的异步 hook。默认空。"""
        pass

    async def on_final_assistant_message(
        self,
        msg_dict: dict,
        response,
    ) -> None:
        """最终 assistant 消息 hook;默认保持旧持久化行为。"""
        await self.on_assistant_message(msg_dict, response)

    async def on_continuation_message(self, cont_msg: dict) -> None:
        """continuation msg 追加后的异步 hook。默认空。"""
        pass

    async def dispatch_one(
        self,
        tool_call,
    ) -> tuple[str, str | None, str | None]:
        """在线程池运行现有同步工具,避免阻塞 Gateway 事件循环。"""
        tool_context = dict(self.tool_context)
        if (
            self.cancel_checker is not None
            and self._tool_call_name(tool_call) == "terminal"
        ):
            tool_context["cancel_checker"] = self.cancel_checker
        return await asyncio.to_thread(
            dispatch_tool_call,
            tool_call,
            self.registry,
            session_key=self.session_key,
            blocked_tools=self.blocked_tools,
            tool_context=tool_context,
        )

    async def on_tool_message(
        self,
        tool_call,
        tool_msg: dict,
        output: str,
    ) -> None:
        """单条 tool msg 追加后的异步 hook。默认空。"""
        pass

    async def on_tool_messages_batch(
        self,
        assistant_msg: dict,
        tool_messages: list[dict],
        response,
    ) -> None:
        """assistant tool_call 与 tool results 生成后的异步 hook。默认空。"""
        pass
