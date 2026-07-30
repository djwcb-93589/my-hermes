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
import inspect
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Callable

from hermes.approval import build_approval_deferred
from hermes.config import MODEL_TIMEOUT_SECONDS, client as _default_client
from hermes.hooks import (
    AsyncHookRegistry,
    HookContext,
    HookControlDispatchResult,
    HookEvent,
    HookEventName,
    SyncHookRegistry,
    build_post_llm_call_payload,
    build_post_tool_call_payload,
    build_run_end_payload,
    build_sync_control_bridge,
)
from hermes.model_streaming import (
    ModelTurnResult,
    StreamAccumulator,
    StreamEvent,
)
from hermes.model_execution import (
    ModelExecutionCancelled,
    ModelExecutionTimedOut,
    consume_interruptible_stream,
    run_interruptible_call,
)
from hermes.redaction import redact_explicit_secrets
from hermes.steering import (
    SteerEntry,
    SteerMailbox,
    format_steer_guidance,
)
from hermes.tokens import estimate_tokens


logger = logging.getLogger(__name__)


_PARSED_TOOL_CALL_TOKEN = object()
"""仅由 parse_tool_call() 写入的内部验证凭证。"""


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
    tool_batches: int = 0
    tool_call_count: int = 0
    pending_steer: tuple[SteerEntry, ...] = ()


@dataclass
class InjectedSteer:
    """记录一次工具结果注入，供持久化失败时完整回滚。"""

    entries: tuple[SteerEntry, ...]
    tool_message: dict
    original_content: object


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
    *,
    parsed_arguments: object | None = None,
) -> dict | None:
    """从受信任 Tool Result 提取待审批请求，并绑定已解析参数。"""
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
    # 审批结果由已获授权的工具处理器生成；这里不维护工具名白名单，
    # 以便新的受控工具复用同一条 CLI / Gateway 恢复链路。具体操作身份
    # 仍在持久化前由审批指纹和绑定校验复核。
    if (
        not isinstance(request_id, str)
        or not request_id.startswith("approval_")
        or not isinstance(tool_name, str)
        or not tool_name
    ):
        return None
    call_name = AgentLoop._tool_call_name(tool_call)
    if call_name != tool_name:
        return None
    arguments = parsed_arguments
    if arguments is None:
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


def _build_cancelled_tool_output() -> str:
    """构造未启动工具的稳定取消结果，不携带输入或工具参数。"""
    return (
        "(cancelled: tool call was not started because "
        "the current run was cancelled)"
    )


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


@dataclass(frozen=True, slots=True)
class ParsedToolCall:
    """工具调用的一次性解析与安全边界检查结果。"""

    tool_name: str
    tool_call_id: str
    arguments: object | None
    argument_keys: tuple[str, ...]
    entry: object | None
    allowed: bool
    blocked: bool
    approval_mode: str
    risk_level: str
    durable_execution: bool
    error_output: str | None = None
    error_status: str | None = None
    error_detail: str | None = None
    _validation_token: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _validated_registry: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def is_dispatchable(self) -> bool:
        """只有解析有效且通过全部硬边界的调用才能进入控制 Hook。"""
        return self.error_output is None

    def is_verified_for(self, registry: object) -> bool:
        """验证调用只能在产生它的同一 Registry 上复用。"""
        return (
            self._validation_token is _PARSED_TOOL_CALL_TOKEN
            and self._validated_registry is registry
        )


def parse_tool_call(
    tool_call,
    registry,
    *,
    blocked_tools: set[str] | None = None,
    allowed_tool_names: set[str] | frozenset[str] | None = None,
    durable_execution: bool = False,
) -> ParsedToolCall:
    """一次性完成参数解析、会话边界和 Registry 条目查询。"""
    function = getattr(tool_call, "function", None)
    tool_name = str(getattr(function, "name", "<unknown>"))
    tool_call_id = str(getattr(tool_call, "id", ""))
    try:
        arguments = json.loads(getattr(function, "arguments", ""))
    except Exception as exc:
        short = _short_error(exc)
        return ParsedToolCall(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=None,
            argument_keys=(),
            entry=None,
            allowed=False,
            blocked=False,
            approval_mode="unknown",
            risk_level="unknown",
            durable_execution=durable_execution,
            error_output=(
                f"(error: invalid JSON arguments in {tool_name}: {short})"
            ),
            error_status="json",
            error_detail=f"invalid JSON in tool_call {tool_name!r}: {short}",
            _validation_token=_PARSED_TOOL_CALL_TOKEN,
            _validated_registry=registry,
        )

    argument_keys = (
        tuple(sorted(str(key) for key in arguments))
        if type(arguments) is dict
        else ()
    )
    blocked = bool(blocked_tools and tool_name in blocked_tools)
    allowed = (
        allowed_tool_names is None or tool_name in allowed_tool_names
    )
    get_entry = getattr(registry, "get_entry", None)
    entry = get_entry(tool_name) if callable(get_entry) else None
    approval_mode = getattr(getattr(entry, "approval_mode", None), "value", None)
    risk_level = getattr(getattr(entry, "risk_level", None), "value", None)
    common = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "arguments": arguments,
        "argument_keys": argument_keys,
        "entry": entry,
        "allowed": allowed,
        "blocked": blocked,
        "approval_mode": str(approval_mode or "unknown"),
        "risk_level": str(risk_level or "unknown"),
        "durable_execution": durable_execution,
        "_validation_token": _PARSED_TOOL_CALL_TOKEN,
        "_validated_registry": registry,
    }
    if blocked:
        return ParsedToolCall(
            **common,
            error_output=f"(error: '{tool_name}' is blocked)",
            error_status="blocked",
            error_detail=f"blocked tool invoked: {tool_name!r}",
        )
    if not allowed:
        return ParsedToolCall(
            **common,
            error_output=json.dumps(
                {
                    "ok": False,
                    "error_type": "tool_disabled",
                    "fatal": True,
                    "error": (
                        "Tool is not enabled in this session: "
                        f"{tool_name}"
                    ),
                },
                ensure_ascii=False,
            ),
            error_status="disabled",
            error_detail=f"disabled tool invoked: {tool_name!r}",
        )
    if entry is None:
        return ParsedToolCall(
            **common,
            error_output=json.dumps(
                {"error": f"Unknown tool: {tool_name}"},
                ensure_ascii=False,
            ),
            error_status="unknown_tool",
            error_detail=f"unknown tool invoked: {tool_name!r}",
        )
    return ParsedToolCall(**common)


def dispatch_parsed_tool_call(
    parsed_call: ParsedToolCall,
    registry,
    *,
    session_key: str | None = None,
    tool_context: dict | None = None,
    require_valid_durable_context: bool = False,
) -> tuple[str, str | None, str | None]:
    """复用已解析参数执行既有审批和 durable 工具分发流程。"""
    if not parsed_call.is_verified_for(registry) or not parsed_call.is_dispatchable:
        return (
            parsed_call.error_output or "(error: tool call rejected)",
            parsed_call.error_status or "dispatch",
            parsed_call.error_detail or "tool call was not internally validated",
        )
    dispatch_context = dict(tool_context or {})
    runtime_hook_registry = dispatch_context.get("hook_registry")
    try:
        durable_context = dispatch_context.pop("durable_tool_execution", None)
        # 普通 AgentLoop 不得把内部审批许可透传给工具。
        dispatch_context.pop("allow_sensitive", None)
        dispatch_context.pop("approval_grant", None)
        dispatch_context["session_key"] = session_key
        dispatch_entry = getattr(registry, "_dispatch_verified_entry", None)
        if durable_context is None:
            if callable(dispatch_entry):
                output = dispatch_entry(
                    parsed_call.entry,
                    parsed_call.arguments,
                    **dispatch_context,
                )
            else:
                output = registry.dispatch(
                    parsed_call.tool_name,
                    parsed_call.arguments,
                    **dispatch_context,
                )
        else:
            from hermes.durable_tool_dispatcher import (
                DurableToolDispatcher,
                DurableToolExecutionContext,
            )

            context = DurableToolExecutionContext.from_value(durable_context)
            if context is None:
                if require_valid_durable_context:
                    raise RuntimeError("durable tool execution context is invalid")
                if callable(dispatch_entry):
                    output = dispatch_entry(
                        parsed_call.entry,
                        parsed_call.arguments,
                        **dispatch_context,
                    )
                else:
                    output = registry.dispatch(
                        parsed_call.tool_name,
                        parsed_call.arguments,
                        **dispatch_context,
                    )
            else:
                output = DurableToolDispatcher(registry, context)._dispatch_verified(
                    parsed_call.tool_name,
                    parsed_call.arguments,
                    tool_call_id=parsed_call.tool_call_id,
                    entry=parsed_call.entry,
                    **dispatch_context,
                )
    except Exception as exc:
        short = _short_error(exc)
        return (
            f"(error: tool {parsed_call.tool_name} failed: {short})",
            "dispatch",
            (
                f"tool {parsed_call.tool_name!r} dispatch raised: "
                f"{type(exc).__name__}: {short}"
            ),
        )
    finally:
        # 同步桥接器只在 delegate 工具运行时存在；普通子任务返回后立即
        # 释放，后台任务则由 handler 显式转交给 worker 的 finally。
        close = getattr(runtime_hook_registry, "close", None)
        retained = getattr(
            runtime_hook_registry,
            "retained_for_background_delegate",
            False,
        )
        if callable(close) and not retained:
            close()
    return output, None, None


def dispatch_tool_call(
    tool_call,
    registry,
    *,
    session_key: str | None = None,
    blocked_tools: set[str] | None = None,
    tool_context: dict | None = None,
    parsed_call: ParsedToolCall | None = None,
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
    parsed = parsed_call or parse_tool_call(
        tool_call,
        registry,
        blocked_tools=blocked_tools,
        allowed_tool_names=(tool_context or {}).get("allowed_tool_names"),
        durable_execution=(
            (tool_context or {}).get("durable_tool_execution") is not None
        ),
    )
    return dispatch_parsed_tool_call(
        parsed,
        registry,
        session_key=session_key,
        tool_context=tool_context,
    )


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
        stream_sink: "Callable[[StreamEvent], object] | None" = None,
        steer_mailbox: SteerMailbox | None = None,
        hook_registry: SyncHookRegistry | None = None,
        parent_run_id: str | None = None,
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
        self.stream_sink = stream_sink
        self.steer_mailbox = steer_mailbox
        # 未进入 run 前邮箱保持 new；每次 run 结束后标记为已关闭。
        self._steer_mailbox_closed = True
        self._unconfirmed_steer: list[SteerEntry] = []
        if hook_registry is not None and not isinstance(
            hook_registry,
            SyncHookRegistry,
        ):
            raise TypeError("hook_registry must be a SyncHookRegistry")
        self.hook_registry = hook_registry
        if parent_run_id is not None and (
            not isinstance(parent_run_id, str) or not parent_run_id
        ):
            raise TypeError("parent_run_id must be a non-empty string or None")
        # 仅用于将子运行与父运行的观察事件关联，不使用会话或用户标识。
        self.parent_run_id = parent_run_id
        self.run_id: str | None = None
        # 运行期状态(每次 run() 重置)
        self.iterations = 0
        self.tools_used: list[str] = []
        self.tool_batches = 0
        self.tool_call_count = 0
        self._tool_observations: list[dict[str, object]] = []
        # 工具错误计数:按 (tool_name, error_type) 累计连续失败次数。
        # 工具成功调用后清掉该 tool_name 的所有计数,避免历史错误干扰。
        self._tool_error_counts: dict[tuple[str, str], int] = {}

    # --- 取消检查(后台 delegate 用) ---

    def _is_cancelled(self) -> bool:
        return self.cancel_checker is not None and bool(self.cancel_checker())

    def steer(self, entry: SteerEntry) -> bool:
        """向当前运行提交一条 steer；未配置邮箱时返回 False。"""
        if self.steer_mailbox is None:
            return False
        return self.steer_mailbox.submit(entry)

    def _activate_steer_mailbox(self) -> None:
        """在正式进入循环前激活邮箱，拒绝重复运行复用。"""
        if self.steer_mailbox is None:
            return
        self._unconfirmed_steer.clear()
        self.steer_mailbox.activate()
        self._steer_mailbox_closed = False

    def _close_steer_mailbox(self, *, drain: bool) -> tuple[SteerEntry, ...]:
        """关闭邮箱；取消路径保留 pending，正常结果路径才消费。"""
        if self.steer_mailbox is None or self._steer_mailbox_closed:
            pending = tuple(self._unconfirmed_steer)
            self._unconfirmed_steer.clear()
            return pending
        if drain:
            pending = self.steer_mailbox.close_and_drain()
        else:
            self.steer_mailbox.close()
            pending = ()
        self._steer_mailbox_closed = True
        unconfirmed = tuple(self._unconfirmed_steer)
        self._unconfirmed_steer.clear()
        return (*unconfirmed, *pending)

    def _finalize_steer_result(self, result: "AgentLoopResult") -> "AgentLoopResult":
        """在结构化结果返回前关闭邮箱并附加未消费 steer。"""
        result.pending_steer = self._close_steer_mailbox(drain=True)
        return result

    def _invalid_steer_mailbox_result(self, error) -> "AgentLoopResult":
        """构造不暴露邮箱内部状态的稳定错误结果。"""
        logger.warning(
            "SteerMailbox activation failed: %s",
            type(error).__name__,
        )
        return self._result(
            ok=False,
            status="error",
            summary="",
            messages=[],
            error="invalid steer mailbox state",
            error_type="invalid_steer_mailbox_state",
            fatal=True,
            retryable=False,
        )

    def _inject_pending_steer(
        self,
        tool_messages: list[dict],
        tool_error: "AgentLoopResult | None",
        *,
        has_next_iteration: bool,
    ) -> InjectedSteer | None:
        """仅在完整且可继续的工具批次后把 steer 追加到最后结果。"""
        if (
            self.steer_mailbox is None
            or tool_error is not None
            or not tool_messages
            or not has_next_iteration
        ):
            return None
        pending = self.steer_mailbox.drain()
        if not pending:
            return None
        last_tool_message = tool_messages[-1]
        original_content = last_tool_message.get("content")
        content_text = "" if original_content is None else str(original_content)
        guidance = format_steer_guidance(pending)
        last_tool_message["content"] = (
            f"{content_text}\n\n{guidance}"
            if content_text
            else guidance
        )
        return InjectedSteer(
            entries=pending,
            tool_message=last_tool_message,
            original_content=original_content,
        )

    def _restore_injected_steer(
        self,
        injected: InjectedSteer | None,
    ) -> None:
        """工具批次持久化失败时恢复已取出的 steer。"""
        if injected is None:
            return
        injected.tool_message["content"] = injected.original_content
        if self.steer_mailbox is not None:
            try:
                self.steer_mailbox.restore_front(injected.entries)
            except Exception:
                # 邮箱已经失效时不能假装恢复成功，交给上层按原消息 ID 处理。
                self._unconfirmed_steer.extend(injected.entries)

    def _record_tool_batch(self, tool_calls) -> None:
        self.tool_batches += 1
        self.tool_call_count += len(tool_calls)

    def _record_tool_observation(
        self,
        tool_call,
        *,
        status: str,
        error_type: str | None,
        duration_ms: int,
    ) -> None:
        """保存持久化完成后才会分发的工具观察摘要。"""
        tool_name = self._tool_call_name(tool_call)
        self._tool_observations.append(
            build_post_tool_call_payload(
                tool_name=tool_name,
                tool_call_id=str(getattr(tool_call, "id", "")),
                status=status,
                error_type=error_type,
                duration_ms=duration_ms,
            )
        )

    def _hook_context(
        self,
        *,
        invocation_suffix: str,
        payload: dict[str, object],
    ) -> HookContext:
        """构造只包含安全运行关联信息的 Hook 上下文。"""
        run_id = self.run_id
        if run_id is None:
            # 所有正式分发都从 run() 内发生；此防御分支避免意外使用会话标识。
            raise RuntimeError("run_id is not initialized")
        metadata: dict[str, object] = {"run_id": run_id}
        if self.parent_run_id is not None:
            metadata["parent_run_id"] = self.parent_run_id
        return HookContext(
            invocation_id=f"{run_id}:{invocation_suffix}",
            metadata=metadata,
            payload=payload,
        )

    def _pre_llm_hook_context(self, messages: list[dict]) -> HookContext:
        """构造不含消息正文和系统提示词的模型调用控制上下文。"""
        allowed_tool_names = getattr(self, "allowed_tool_names", None)
        return self._hook_context(
            invocation_suffix=f"pre_llm:{self.iterations}",
            payload={
                "iteration": self.iterations,
                "model_role": self._model_role(),
                "message_count": len(messages),
                "estimated_tokens": estimate_tokens(messages),
                "has_tool_definitions": bool(self.tools),
                "allowed_tool_count": (
                    len(allowed_tool_names)
                    if allowed_tool_names is not None
                    else len(self.tools)
                ),
            },
        )

    def _parse_tool_call(self, tool_call) -> ParsedToolCall:
        """为控制 Hook 与实际工具分发创建同一份已解析调用。"""
        return parse_tool_call(
            tool_call,
            self.registry,
            blocked_tools=self.blocked_tools,
            allowed_tool_names=getattr(self, "allowed_tool_names", None),
            durable_execution=(
                self.tool_context.get("durable_tool_execution") is not None
            ),
        )

    def _pre_tool_hook_context(self, parsed_call: ParsedToolCall) -> HookContext:
        """由已验证调用构造不含参数值的控制上下文。"""
        return self._hook_context(
            invocation_suffix=f"pre_tool:{parsed_call.tool_call_id}",
            payload={
                "tool_name": parsed_call.tool_name,
                "tool_call_id": parsed_call.tool_call_id,
                "argument_keys": list(parsed_call.argument_keys),
                "argument_count": len(parsed_call.argument_keys),
                "approval_mode": parsed_call.approval_mode,
                "risk_level": parsed_call.risk_level,
                "durable_execution": parsed_call.durable_execution,
            },
        )

    @staticmethod
    def _temporary_context_messages(contexts: tuple[str, ...]) -> list[dict]:
        """构造仅供当前模型请求使用、绝不持久化的临时系统消息。"""
        return [
            {
                "role": "system",
                "content": f"[PLUGIN_TEMPORARY_CONTEXT]\n{text}",
            }
            for text in contexts
        ]

    def _dispatch_pre_llm_control(
        self,
        messages: list[dict],
    ) -> HookControlDispatchResult | None:
        """同步分发模型调用控制 Hook。"""
        registry = self.hook_registry
        if not isinstance(registry, SyncHookRegistry):
            return None
        return registry.emit_control(
            HookEvent(
                name=HookEventName.PRE_LLM_CALL.value,
                context=self._pre_llm_hook_context(messages),
            )
        )

    def _dispatch_pre_tool_control(
        self,
        parsed_call: ParsedToolCall,
    ) -> HookControlDispatchResult | None:
        """同步分发工具调用控制 Hook，未通过既有边界时不触发。"""
        registry = self.hook_registry
        if not isinstance(registry, SyncHookRegistry):
            return None
        return registry.emit_control(
            HookEvent(
                name=HookEventName.PRE_TOOL_CALL.value,
                context=self._pre_tool_hook_context(parsed_call),
            )
        )

    def _delegate_hook_registry(self) -> SyncHookRegistry | None:
        """为同步 Delegate 提供显式同步控制接口，绝不跨线程调用异步 Registry。"""
        registry = self.hook_registry
        if isinstance(registry, SyncHookRegistry):
            return registry
        if isinstance(registry, AsyncHookRegistry):
            return build_sync_control_bridge(registry)
        return None

    @staticmethod
    def _hook_token_usage(response) -> dict[str, int]:
        """提取可安全暴露的模型 token 统计，不保留原始响应对象。"""
        usage = _object_value(response, "usage")
        values = {
            "prompt_tokens": _optional_int(_object_value(usage, "prompt_tokens")),
            "completion_tokens": _optional_int(
                _object_value(usage, "completion_tokens")
            ),
            "total_tokens": _optional_int(_object_value(usage, "total_tokens")),
        }
        return {
            name: value
            for name, value in values.items()
            if value is not None
        }

    def _emit_post_llm_call(
        self,
        *,
        response,
        assistant_msg,
        finish_reason: str | None,
        duration_ms: int,
    ) -> None:
        """在 assistant 消息完成既有持久化后分发只读模型观察事件。"""
        registry = self.hook_registry
        if not isinstance(registry, SyncHookRegistry):
            return
        payload = build_post_llm_call_payload(
            finish_reason=finish_reason,
            has_text=bool(_object_value(assistant_msg, "content")),
            tool_call_count=len(_object_value(assistant_msg, "tool_calls") or ()),
            token_usage=self._hook_token_usage(response),
            duration_ms=duration_ms,
        )
        self._emit_sync_hook(
            HookEventName.POST_LLM_CALL,
            self._hook_context(
                invocation_suffix=f"llm:{self.iterations}",
                payload=payload,
            ),
        )

    def _emit_post_tool_calls(self) -> None:
        """在整批工具消息持久化后按原工具顺序分发观察事件。"""
        registry = self.hook_registry
        if not isinstance(registry, SyncHookRegistry):
            self._tool_observations = []
            return
        observations = self._tool_observations
        self._tool_observations = []
        for observation in observations:
            self._emit_sync_hook(
                HookEventName.POST_TOOL_CALL,
                self._hook_context(
                    invocation_suffix=(
                        f"tool:{observation['tool_call_id']}"
                    ),
                    payload=observation,
                ),
            )

    def _emit_run_end(self, result: "AgentLoopResult") -> None:
        """在结果生成后、返回调用方前分发只读运行结束事件。"""
        registry = self.hook_registry
        if not isinstance(registry, SyncHookRegistry):
            return
        self._emit_sync_hook(
            HookEventName.RUN_END,
            self._hook_context(
                invocation_suffix="run_end",
                payload=build_run_end_payload(
                    status=result.status,
                    stop_reason=result.error_type or result.status,
                    iterations=result.iterations,
                    tool_call_count=result.tool_call_count,
                    summary=result.summary,
                ),
            ),
        )

    def _emit_sync_hook(
        self,
        event_name: HookEventName,
        context: HookContext,
    ) -> None:
        """隔离观察 Hook 基础设施自身的意外错误。"""
        registry = self.hook_registry
        if not isinstance(registry, SyncHookRegistry):
            return
        try:
            # Registry 已自行隔离单个 Plugin 回调异常与超时结果。
            registry.emit(HookEvent(name=event_name.value, context=context))
        except Exception:
            logger.exception("Hook dispatch setup failed: event=%s", event_name.value)

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
        self.run_id = uuid.uuid4().hex
        self.iterations = 0
        self.tools_used = []
        self.tool_batches = 0
        self.tool_call_count = 0
        self._tool_observations = []
        try:
            self._activate_steer_mailbox()
        except Exception as exc:
            result = self._invalid_steer_mailbox_result(exc)
            self._emit_run_end(result)
            return result
        try:
            try:
                result = self._run_inner(user_message)
            except Exception as exc:
                # 内部 _run_inner 已经处理了 model / persistence / tool 等已知
                # 异常,真到这里说明是未预期 bug,统一标 internal_error
                result = self._internal_error_result(
                    messages=[], error=f"unhandled exception: {exc!r}",
                )
            result = self._finalize_steer_result(result)
            self._emit_run_end(result)
            return result
        finally:
            self._close_steer_mailbox(drain=False)

    def _run_inner(self, user_message: str) -> AgentLoopResult:
        messages = self.init_messages(user_message)
        self.iterations = 0
        self.tools_used = []
        self.tool_batches = 0
        self.tool_call_count = 0
        self._tool_observations = []

        for iteration in range(self.max_iterations):
            # 1) iteration 开始前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)

            self.iterations = iteration + 1
            messages = self.pre_model_call(messages)

            # 2) 模型调用前检查取消
            if self._is_cancelled():
                return self._cancel_result(messages)

            pre_llm_control = self._dispatch_pre_llm_control(messages)
            if pre_llm_control is not None and pre_llm_control.blocked:
                return self._result(
                    ok=False,
                    status="hook_blocked",
                    summary=self.last_assistant_text(messages),
                    messages=messages,
                    error=pre_llm_control.block_reason,
                    error_type="hook_blocked",
                    fatal=True,
                    retryable=False,
                )
            if self._is_cancelled():
                return self._cancel_result(messages)
            request_messages = (
                [
                    *messages,
                    *self._temporary_context_messages(
                        pre_llm_control.added_context
                    ),
                ]
                if pre_llm_control is not None
                and pre_llm_control.added_context
                else messages
            )

            # 模型调用 —— 走 handle_model_error 决定后续动作
            call_model = str(self.model)
            call_model_role = self._model_role()
            call_started = time.perf_counter()
            try:
                response = self.call_model(request_messages)
            except ModelExecutionCancelled:
                return self._cancel_result(messages)
            except ModelExecutionTimedOut:
                if self._is_cancelled():
                    return self._cancel_result(messages)
                exc = TimeoutError("model request timed out")
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
                    return self._model_error_result(messages, repr(exc))
                raise exc
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

            model_turn = self._complete_model_turn(response)
            assistant_msg = model_turn.assistant_message
            finish_reason = model_turn.finish_reason
            has_output = bool(assistant_msg.content or assistant_msg.tool_calls)
            outcome = "success"
            if not has_output:
                outcome = (
                    "output_length_exhausted"
                    if finish_reason == "length"
                    else "empty_model_response"
                )
            model_duration_ms = max(
                0,
                int((time.perf_counter() - call_started) * 1000),
            )
            self._emit_model_call_event(
                _model_call_event(
                    iteration=self.iterations,
                    model=call_model,
                    model_role=call_model_role,
                    latency_ms=model_duration_ms,
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
                        self._emit_post_llm_call(
                            response=response,
                            assistant_msg=assistant_msg,
                            finish_reason=finish_reason,
                            duration_ms=model_duration_ms,
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
                self._record_tool_batch(assistant_msg.tool_calls)
                try:
                    tool_messages, tool_error = self.process_tool_calls(
                        assistant_msg.tool_calls, messages
                    )
                except Exception as exc:
                    # 工具分发过程中的持久化 / 结构异常
                    return self._persistence_error_result(messages, repr(exc))
                if self._is_cancelled():
                    tool_error = self._cancel_result(messages)
                injected_steer = self._inject_pending_steer(
                    tool_messages,
                    tool_error,
                    has_next_iteration=(iteration + 1 < self.max_iterations),
                )
                if self._is_cancelled():
                    self._restore_injected_steer(injected_steer)
                    injected_steer = None
                    tool_error = self._cancel_result(messages)
                try:
                    self.on_tool_messages_batch(
                        msg_dict,
                        tool_messages,
                        response,
                        steer_ids=(
                            tuple(entry.steer_id for entry in injected_steer.entries)
                            if injected_steer is not None
                            else ()
                        ),
                    )
                except Exception as exc:
                    # DB 写入失败:assistant + tool_messages 整组未落盘,停止 loop
                    self._restore_injected_steer(injected_steer)
                    return self._persistence_error_result(messages, repr(exc))
                self._emit_post_llm_call(
                    response=response,
                    assistant_msg=assistant_msg,
                    finish_reason=finish_reason,
                    duration_ms=model_duration_ms,
                )
                self._emit_post_tool_calls()
                if tool_error is not None:
                    return tool_error
                if self._is_cancelled():
                    return self._cancel_result(messages)
                continue

            # continuation hook(主会话:finish_reason == "length")
            if self.should_continue(finish_reason, messages):
                try:
                    self.on_assistant_message(msg_dict, response)
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                self._emit_post_llm_call(
                    response=response,
                    assistant_msg=assistant_msg,
                    finish_reason=finish_reason,
                    duration_ms=model_duration_ms,
                )
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
            self._emit_post_llm_call(
                response=response,
                assistant_msg=assistant_msg,
                finish_reason=finish_reason,
                duration_ms=model_duration_ms,
            )
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
        cancelled_batch = False
        for tc in tool_calls:
            tool_started = time.perf_counter()
            tc_name = self._tool_call_name(tc)
            parsed_call: ParsedToolCall | None = None
            cancelled_tool = cancelled_batch or self._is_cancelled()
            if cancelled_tool:
                cancelled_batch = True
            skipped_due_to_failure = (
                not cancelled_tool and fatal_detail is not None
            )
            deferred_for_approval = (
                not cancelled_tool and approval_request is not None
            )
            hook_blocked = False
            if cancelled_tool:
                output = _build_cancelled_tool_output()
                err_status = "cancelled"
                err_detail = "tool call was not started because the run was cancelled"
            elif skipped_due_to_failure:
                # 前序致命错误后不再执行后续调用，但保留完整 batch 供既有流程持久化。
                output = "(error: skipped because an earlier tool call failed)"
                err_status = None
                err_detail = None
            elif deferred_for_approval:
                output = build_approval_deferred()
                err_status = None
                err_detail = None
            else:
                parsed_call = self._parse_tool_call(tc)
                if self._is_cancelled():
                    cancelled_batch = True
                    cancelled_tool = True
                    output = _build_cancelled_tool_output()
                    err_status = "cancelled"
                    err_detail = "tool call was not started because the run was cancelled"
                elif not parsed_call.is_dispatchable:
                    output = parsed_call.error_output or "(error: tool call rejected)"
                    err_status = parsed_call.error_status
                    err_detail = parsed_call.error_detail
                else:
                    pre_tool_control = (
                        None
                        if self._is_cancelled()
                        else self._dispatch_pre_tool_control(parsed_call)
                    )
                    if self._is_cancelled():
                        cancelled_batch = True
                        cancelled_tool = True
                        output = _build_cancelled_tool_output()
                        err_status = "cancelled"
                        err_detail = "tool call was not started because the run was cancelled"
                    elif pre_tool_control is not None and pre_tool_control.blocked:
                        hook_blocked = True
                        output = (
                            f"(error: tool {tc_name} blocked by Hook: "
                            f"{pre_tool_control.block_reason})"
                        )
                        err_status = "hook_blocked"
                        err_detail = "tool call was blocked by a control Hook"
                    else:
                        try:
                            output, err_status, err_detail = self.dispatch_one(
                                tc,
                                parsed_call,
                            )
                        except Exception as exc:
                            # dispatch_one 自身出 bug(不是工具返错,是分发机制炸了)
                            short = _short_error(exc)
                            output = f"(error: tool {tc_name} failed: {short})"
                            err_status = "dispatch"
                            err_detail = f"tool {tc_name!r} dispatch raised: {short}"

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

            if cancelled_tool:
                self._record_tool_observation(
                    tc,
                    status="skipped",
                    error_type="cancelled",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                continue

            if skipped_due_to_failure:
                self._record_tool_observation(
                    tc,
                    status="skipped",
                    error_type="prior_tool_failure",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                continue

            if hook_blocked:
                self._record_tool_observation(
                    tc,
                    status="blocked",
                    error_type="hook_blocked",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                continue

            pending = (
                approval_request
                if deferred_for_approval
                else _extract_approval_request(
                    output,
                    tc,
                    parsed_arguments=(
                        parsed_call.arguments if parsed_call is not None else None
                    ),
                )
            )
            if pending is not None:
                self._record_tool_observation(
                    tc,
                    status="awaiting_approval",
                    error_type="approval_required",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                approval_request = pending
                continue

            fatal, err_type = self._classify_tool_error(output, err_status)
            self._record_tool_observation(
                tc,
                status=(
                    "succeeded" if not err_type and not err_status else "failed"
                ),
                error_type=(err_type or err_status or None),
                duration_ms=(time.perf_counter() - tool_started) * 1000,
            )
            if err_type == "cancelled" or err_status == "cancelled":
                cancelled_batch = True
                continue
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

        if cancelled_batch or self._is_cancelled():
            return tool_messages, self._cancel_result(messages)

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
        if self.stream_sink is not None:
            return self._call_model_stream(api_messages)
        return run_interruptible_call(
            lambda: self.client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                tools=self.tools if self.tools else None,
                **self.model_kwargs,
            ),
            cancel_checker=self._is_cancelled,
            timeout_seconds=MODEL_TIMEOUT_SECONDS,
        )

    def _call_model_stream(self, api_messages: list[dict]) -> ModelTurnResult:
        """同步消费流，并在完成前不向循环暴露不完整的模型消息。"""
        attempt_id = uuid.uuid4().hex
        stream_kwargs = dict(self.model_kwargs)
        stream_kwargs["stream"] = True
        stream_options = dict(stream_kwargs.get("stream_options") or {})
        stream_options["include_usage"] = True
        stream_kwargs["stream_options"] = stream_options
        accumulator = StreamAccumulator(attempt_id=attempt_id)
        self._emit_stream_event(StreamEvent("model_turn_started", attempt_id))
        try:
            def on_chunk(chunk) -> None:
                if self._is_cancelled():
                    raise ModelExecutionCancelled
                content_delta, reasoning_delta = accumulator.add_chunk(chunk)
                if content_delta:
                    if self._is_cancelled():
                        raise ModelExecutionCancelled
                    self._emit_stream_event(
                        StreamEvent("text_delta", attempt_id, content_delta)
                    )
                if reasoning_delta:
                    if self._is_cancelled():
                        raise ModelExecutionCancelled
                    self._emit_stream_event(
                        StreamEvent("reasoning_delta", attempt_id, reasoning_delta)
                    )

            consume_interruptible_stream(
                lambda: self.client.chat.completions.create(
                    model=self.model,
                    messages=api_messages,
                    tools=self.tools if self.tools else None,
                    **stream_kwargs,
                ),
                cancel_checker=self._is_cancelled,
                timeout_seconds=MODEL_TIMEOUT_SECONDS,
                on_chunk=on_chunk,
            )
            if self._is_cancelled():
                raise ModelExecutionCancelled
            result = accumulator.result()
            if self._is_cancelled():
                raise ModelExecutionCancelled
        except ModelExecutionCancelled:
            self._emit_stream_event(StreamEvent("model_turn_interrupted", attempt_id))
            raise
        except ModelExecutionTimedOut:
            self._emit_stream_event(StreamEvent("model_turn_interrupted", attempt_id))
            raise
        except BaseException:
            self._emit_stream_event(StreamEvent("model_turn_interrupted", attempt_id))
            raise
        self._emit_stream_event(StreamEvent("model_turn_completed", attempt_id))
        return result

    def _emit_stream_event(self, event: StreamEvent) -> None:
        """尽力发送展示事件，展示层异常不能影响模型调用。"""
        sink = self.stream_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            pass

    @staticmethod
    def _complete_model_turn(response) -> ModelTurnResult:
        """把非流式或已累加的响应归一为完整模型回合。"""
        if isinstance(response, ModelTurnResult):
            return response
        choice = response.choices[0]
        return ModelTurnResult(
            assistant_message=choice.message,
            finish_reason=choice.finish_reason,
            usage=getattr(response, "usage", None),
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

    def dispatch_one(
        self,
        tool_call,
        parsed_call: ParsedToolCall | None = None,
    ) -> tuple[str, str | None, str | None]:
        """处理单个 tool_call。默认走 dispatch_tool_call helper。

        返回值里的 error_status 表示工具执行失败,但调用方仍会生成
        合法 tool message,再由 batch hook 原子持久化。
        """
        tool_context = dict(self.tool_context)
        allowed_tool_names = getattr(self, "allowed_tool_names", None)
        if allowed_tool_names is not None:
            tool_context["allowed_tool_names"] = allowed_tool_names
        if self.cancel_checker is not None:
            tool_context["cancel_checker"] = self.cancel_checker
        if self._tool_call_name(tool_call) == "delegate_task":
            # 仅供同步 delegate 工具处理器转交子运行，不属于模型可见参数。
            delegate_registry = self._delegate_hook_registry()
            if delegate_registry is not None:
                tool_context["hook_registry"] = delegate_registry
            if self.run_id is not None:
                tool_context["parent_run_id"] = self.run_id
        return dispatch_tool_call(
            tool_call, self.registry,
            session_key=self.session_key,
            blocked_tools=self.blocked_tools,
            tool_context=tool_context,
            parsed_call=parsed_call,
        )

    def on_tool_message(self, tool_call, tool_msg: dict, output: str) -> None:
        """单条 tool msg 追加后调用。默认空。"""
        pass

    def on_tool_messages_batch(
        self,
        assistant_msg: dict,
        tool_messages: list[dict],
        response,
        *,
        steer_ids: tuple[str, ...] = (),
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
            tool_batches=self.tool_batches,
            tool_call_count=self.tool_call_count,
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
        stream_sink: "Callable[[StreamEvent], object] | None" = None,
        steer_mailbox: SteerMailbox | None = None,
        hook_registry: AsyncHookRegistry | None = None,
        parent_run_id: str | None = None,
    ):
        if hook_registry is not None and not isinstance(
            hook_registry,
            AsyncHookRegistry,
        ):
            raise TypeError("hook_registry must be an AsyncHookRegistry")
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
            stream_sink=stream_sink,
            steer_mailbox=steer_mailbox,
            hook_registry=None,
            parent_run_id=parent_run_id,
        )
        self.hook_registry = hook_registry

    async def _dispatch_pre_llm_control_async(
        self,
        messages: list[dict],
    ) -> HookControlDispatchResult | None:
        """异步分发模型调用控制 Hook，并保留外部取消语义。"""
        registry = self.hook_registry
        if not isinstance(registry, AsyncHookRegistry):
            return None
        return await registry.emit_control(
            HookEvent(
                name=HookEventName.PRE_LLM_CALL.value,
                context=self._pre_llm_hook_context(messages),
            )
        )

    async def _dispatch_pre_tool_control_async(
        self,
        parsed_call: ParsedToolCall,
    ) -> HookControlDispatchResult | None:
        """异步分发工具调用控制 Hook，未通过既有边界时不触发。"""
        registry = self.hook_registry
        if not isinstance(registry, AsyncHookRegistry):
            return None
        return await registry.emit_control(
            HookEvent(
                name=HookEventName.PRE_TOOL_CALL.value,
                context=self._pre_tool_hook_context(parsed_call),
            )
        )

    async def run(self, user_message: str) -> AgentLoopResult:
        """异步跑一次完整循环,Task 取消必须原样向上传播。"""
        self.run_id = uuid.uuid4().hex
        self.iterations = 0
        self.tools_used = []
        self.tool_batches = 0
        self.tool_call_count = 0
        self._tool_observations = []
        try:
            self._activate_steer_mailbox()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = self._invalid_steer_mailbox_result(exc)
            return await self._finalize_run_result(result)
        try:
            try:
                result = await self._run_inner(user_message)
            except asyncio.CancelledError:
                # 真正取消模型 HTTP 请求依赖 CancelledError 继续传到 Runner。
                raise
            except Exception as exc:
                result = self._internal_error_result(
                    messages=[], error=f"unhandled exception: {exc!r}",
                )
            result = await self._finalize_run_result(result)
            return self._finalize_steer_result(result)
        finally:
            self._close_steer_mailbox(drain=False)

    async def _finalize_run_result(
        self,
        result: AgentLoopResult,
    ) -> AgentLoopResult:
        """在异步结果即将返回时分发 run_end 观察事件。"""
        await self._emit_run_end_async(result)
        return result

    async def _emit_post_llm_call_async(
        self,
        *,
        response,
        assistant_msg,
        finish_reason: str | None,
        duration_ms: int,
    ) -> None:
        """在 assistant 消息完成既有持久化后分发异步模型观察事件。"""
        registry = self.hook_registry
        if not isinstance(registry, AsyncHookRegistry):
            return
        payload = build_post_llm_call_payload(
            finish_reason=finish_reason,
            has_text=bool(_object_value(assistant_msg, "content")),
            tool_call_count=len(_object_value(assistant_msg, "tool_calls") or ()),
            token_usage=self._hook_token_usage(response),
            duration_ms=duration_ms,
        )
        await self._emit_async_hook(
            HookEventName.POST_LLM_CALL,
            self._hook_context(
                invocation_suffix=f"llm:{self.iterations}",
                payload=payload,
            ),
        )

    async def _emit_post_tool_calls_async(self) -> None:
        """在整批工具消息持久化后按原工具顺序分发异步观察事件。"""
        registry = self.hook_registry
        if not isinstance(registry, AsyncHookRegistry):
            self._tool_observations = []
            return
        observations = self._tool_observations
        self._tool_observations = []
        for observation in observations:
            await self._emit_async_hook(
                HookEventName.POST_TOOL_CALL,
                self._hook_context(
                    invocation_suffix=(
                        f"tool:{observation['tool_call_id']}"
                    ),
                    payload=observation,
                ),
            )

    async def _emit_run_end_async(self, result: AgentLoopResult) -> None:
        """在异步结果生成后、返回调用方前分发 run_end 事件。"""
        registry = self.hook_registry
        if not isinstance(registry, AsyncHookRegistry):
            return
        await self._emit_async_hook(
            HookEventName.RUN_END,
            self._hook_context(
                invocation_suffix="run_end",
                payload=build_run_end_payload(
                    status=result.status,
                    stop_reason=result.error_type or result.status,
                    iterations=result.iterations,
                    tool_call_count=result.tool_call_count,
                    summary=result.summary,
                ),
            ),
        )

    async def _emit_async_hook(
        self,
        event_name: HookEventName,
        context: HookContext,
    ) -> None:
        """隔离观察回调失败，同时保留 AgentLoop 外部取消语义。"""
        registry = self.hook_registry
        if not isinstance(registry, AsyncHookRegistry):
            return
        try:
            await registry.emit(HookEvent(name=event_name.value, context=context))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Hook dispatch failed: event=%s", event_name.value)

    async def _run_inner(self, user_message: str) -> AgentLoopResult:
        messages = self.init_messages(user_message)
        self.iterations = 0
        self.tools_used = []
        self.tool_batches = 0
        self.tool_call_count = 0
        self._tool_observations = []

        for iteration in range(self.max_iterations):
            if self._is_cancelled():
                return self._cancel_result(messages)

            self.iterations = iteration + 1
            messages = await self.pre_model_call(messages)

            if self._is_cancelled():
                return self._cancel_result(messages)

            pre_llm_control = await self._dispatch_pre_llm_control_async(messages)
            if pre_llm_control is not None and pre_llm_control.blocked:
                return self._result(
                    ok=False,
                    status="hook_blocked",
                    summary=self.last_assistant_text(messages),
                    messages=messages,
                    error=pre_llm_control.block_reason,
                    error_type="hook_blocked",
                    fatal=True,
                    retryable=False,
                )
            if self._is_cancelled():
                return self._cancel_result(messages)
            request_messages = (
                [
                    *messages,
                    *self._temporary_context_messages(
                        pre_llm_control.added_context
                    ),
                ]
                if pre_llm_control is not None
                and pre_llm_control.added_context
                else messages
            )

            call_model = str(self.model)
            call_model_role = self._model_role()
            call_started = time.perf_counter()
            try:
                response = await self.call_model(request_messages)
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

            model_turn = self._complete_model_turn(response)
            assistant_msg = model_turn.assistant_message
            finish_reason = model_turn.finish_reason
            has_output = bool(assistant_msg.content or assistant_msg.tool_calls)
            outcome = "success"
            if not has_output:
                outcome = (
                    "output_length_exhausted"
                    if finish_reason == "length"
                    else "empty_model_response"
                )
            model_duration_ms = max(
                0,
                int((time.perf_counter() - call_started) * 1000),
            )
            await self._emit_model_call_event_async(
                _model_call_event(
                    iteration=self.iterations,
                    model=call_model,
                    model_role=call_model_role,
                    latency_ms=model_duration_ms,
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
                        await self._emit_post_llm_call_async(
                            response=response,
                            assistant_msg=assistant_msg,
                            finish_reason=finish_reason,
                            duration_ms=model_duration_ms,
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
                self._record_tool_batch(assistant_msg.tool_calls)
                try:
                    tool_messages, tool_error = await self.process_tool_calls(
                        assistant_msg.tool_calls, messages,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                if self._is_cancelled():
                    tool_error = self._cancel_result(messages)
                injected_steer = self._inject_pending_steer(
                    tool_messages,
                    tool_error,
                    has_next_iteration=(iteration + 1 < self.max_iterations),
                )
                if self._is_cancelled():
                    self._restore_injected_steer(injected_steer)
                    injected_steer = None
                    tool_error = self._cancel_result(messages)
                try:
                    await self.on_tool_messages_batch(
                        msg_dict,
                        tool_messages,
                        response,
                        steer_ids=(
                            tuple(entry.steer_id for entry in injected_steer.entries)
                            if injected_steer is not None
                            else ()
                        ),
                    )
                except asyncio.CancelledError:
                    self._restore_injected_steer(injected_steer)
                    raise
                except Exception as exc:
                    self._restore_injected_steer(injected_steer)
                    return self._persistence_error_result(messages, repr(exc))
                await self._emit_post_llm_call_async(
                    response=response,
                    assistant_msg=assistant_msg,
                    finish_reason=finish_reason,
                    duration_ms=model_duration_ms,
                )
                await self._emit_post_tool_calls_async()
                if tool_error is not None:
                    return tool_error
                if self._is_cancelled():
                    return self._cancel_result(messages)
                continue

            if self.should_continue(finish_reason, messages):
                try:
                    await self.on_assistant_message(msg_dict, response)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._persistence_error_result(messages, repr(exc))
                await self._emit_post_llm_call_async(
                    response=response,
                    assistant_msg=assistant_msg,
                    finish_reason=finish_reason,
                    duration_ms=model_duration_ms,
                )
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
            await self._emit_post_llm_call_async(
                response=response,
                assistant_msg=assistant_msg,
                finish_reason=finish_reason,
                duration_ms=model_duration_ms,
            )
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
        cancelled_batch = False
        for tc in tool_calls:
            tool_started = time.perf_counter()
            tc_name = self._tool_call_name(tc)
            parsed_call: ParsedToolCall | None = None
            cancelled_tool = cancelled_batch or self._is_cancelled()
            if cancelled_tool:
                cancelled_batch = True
            skipped_due_to_failure = (
                not cancelled_tool and fatal_detail is not None
            )
            deferred_for_approval = (
                not cancelled_tool and approval_request is not None
            )
            hook_blocked = False
            if cancelled_tool:
                output = _build_cancelled_tool_output()
                err_status = "cancelled"
                err_detail = "tool call was not started because the run was cancelled"
            elif skipped_due_to_failure:
                # 前序致命错误后不再执行后续调用，但保留完整 batch 供既有流程持久化。
                output = "(error: skipped because an earlier tool call failed)"
                err_status = None
                err_detail = None
            elif deferred_for_approval:
                output = build_approval_deferred()
                err_status = None
                err_detail = None
            else:
                parsed_call = self._parse_tool_call(tc)
                if self._is_cancelled():
                    cancelled_batch = True
                    cancelled_tool = True
                    output = _build_cancelled_tool_output()
                    err_status = "cancelled"
                    err_detail = "tool call was not started because the run was cancelled"
                elif not parsed_call.is_dispatchable:
                    output = parsed_call.error_output or "(error: tool call rejected)"
                    err_status = parsed_call.error_status
                    err_detail = parsed_call.error_detail
                else:
                    pre_tool_control = None
                    if not self._is_cancelled():
                        pre_tool_control = await self._dispatch_pre_tool_control_async(
                            parsed_call
                        )
                    if self._is_cancelled():
                        cancelled_batch = True
                        cancelled_tool = True
                        output = _build_cancelled_tool_output()
                        err_status = "cancelled"
                        err_detail = "tool call was not started because the run was cancelled"
                    elif pre_tool_control is not None and pre_tool_control.blocked:
                        hook_blocked = True
                        output = (
                            f"(error: tool {tc_name} blocked by Hook: "
                            f"{pre_tool_control.block_reason})"
                        )
                        err_status = "hook_blocked"
                        err_detail = "tool call was blocked by a control Hook"
                    else:
                        try:
                            output, err_status, err_detail = await self.dispatch_one(
                                tc,
                                parsed_call,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            short = _short_error(exc)
                            output = f"(error: tool {tc_name} failed: {short})"
                            err_status = "dispatch"
                            err_detail = f"tool {tc_name!r} dispatch raised: {short}"

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

            if cancelled_tool:
                self._record_tool_observation(
                    tc,
                    status="skipped",
                    error_type="cancelled",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                continue

            if skipped_due_to_failure:
                self._record_tool_observation(
                    tc,
                    status="skipped",
                    error_type="prior_tool_failure",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                continue

            if hook_blocked:
                self._record_tool_observation(
                    tc,
                    status="blocked",
                    error_type="hook_blocked",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                continue

            pending = (
                approval_request
                if deferred_for_approval
                else _extract_approval_request(
                    output,
                    tc,
                    parsed_arguments=(
                        parsed_call.arguments if parsed_call is not None else None
                    ),
                )
            )
            if pending is not None:
                self._record_tool_observation(
                    tc,
                    status="awaiting_approval",
                    error_type="approval_required",
                    duration_ms=(time.perf_counter() - tool_started) * 1000,
                )
                approval_request = pending
                continue

            fatal, err_type = self._classify_tool_error(output, err_status)
            self._record_tool_observation(
                tc,
                status=(
                    "succeeded" if not err_type and not err_status else "failed"
                ),
                error_type=(err_type or err_status or None),
                duration_ms=(time.perf_counter() - tool_started) * 1000,
            )
            if err_type == "cancelled" or err_status == "cancelled":
                cancelled_batch = True
                continue
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

        if cancelled_batch or self._is_cancelled():
            return tool_messages, self._cancel_result(messages)

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
        if self.stream_sink is not None:
            return await self._call_model_stream_async(api_messages)
        return await self.client.chat.completions.create(
            model=self.model,
            messages=api_messages,
            tools=self.tools if self.tools else None,
            **self.model_kwargs,
        )

    async def _call_model_stream_async(
        self,
        api_messages: list[dict],
    ) -> ModelTurnResult:
        """异步消费模型流，完成后才向循环返回完整模型回合。"""
        attempt_id = uuid.uuid4().hex
        stream_kwargs = dict(self.model_kwargs)
        stream_kwargs["stream"] = True
        stream_options = dict(stream_kwargs.get("stream_options") or {})
        stream_options["include_usage"] = True
        stream_kwargs["stream_options"] = stream_options
        accumulator = StreamAccumulator(attempt_id=attempt_id)
        stream = None
        try:
            await self._emit_stream_event_async(
                StreamEvent("model_turn_started", attempt_id)
            )
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                tools=self.tools if self.tools else None,
                **stream_kwargs,
            )
            if inspect.isawaitable(stream):
                stream = await stream
            async for chunk in stream:
                content_delta, reasoning_delta = accumulator.add_chunk(chunk)
                if content_delta:
                    await self._emit_stream_event_async(
                        StreamEvent("text_delta", attempt_id, content_delta)
                    )
                if reasoning_delta:
                    await self._emit_stream_event_async(
                        StreamEvent(
                            "reasoning_delta",
                            attempt_id,
                            reasoning_delta,
                        )
                    )
            result = accumulator.result()
        except asyncio.CancelledError:
            await self._emit_stream_event_async(
                StreamEvent("model_turn_interrupted", attempt_id)
            )
            raise
        except BaseException:
            await self._emit_stream_event_async(
                StreamEvent("model_turn_interrupted", attempt_id)
            )
            raise
        finally:
            await self._close_model_stream_async(stream)
        await self._emit_stream_event_async(
            StreamEvent("model_turn_completed", attempt_id)
        )
        return result

    async def _close_model_stream_async(self, stream) -> None:
        """尽力关闭异步模型流，清理异常不覆盖原始模型结果。"""
        if stream is None:
            return
        close = getattr(stream, "aclose", None)
        if not callable(close):
            close = getattr(stream, "close", None)
        if not callable(close):
            return
        try:
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Async model stream close failed")

    async def _emit_stream_event_async(self, event: StreamEvent) -> None:
        """隔离 sink 异常，但保留异步任务取消语义。"""
        sink = self.stream_sink
        if sink is None:
            return
        try:
            result = sink(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Stream sink failed: event=%s",
                event.event_type,
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
        parsed_call: ParsedToolCall | None = None,
    ) -> tuple[str, str | None, str | None]:
        """在线程池运行同步工具，并在取消时等待真实调用完成收口。"""

        tool_context = dict(self.tool_context)
        allowed_tool_names = getattr(self, "allowed_tool_names", None)
        if allowed_tool_names is not None:
            tool_context["allowed_tool_names"] = allowed_tool_names
        if self.cancel_checker is not None:
            tool_context["cancel_checker"] = self.cancel_checker
        if self._tool_call_name(tool_call) == "delegate_task":
            delegate_registry = self._delegate_hook_registry()
            if delegate_registry is not None:
                tool_context["hook_registry"] = delegate_registry
            if self.run_id is not None:
                tool_context["parent_run_id"] = self.run_id

        dispatch_task = asyncio.create_task(
            asyncio.to_thread(
                dispatch_tool_call,
                tool_call,
                self.registry,
                session_key=self.session_key,
                blocked_tools=self.blocked_tools,
                tool_context=tool_context,
                parsed_call=parsed_call,
            ),
            name="hermes-tool-dispatch",
        )
        cancelled = False
        while not dispatch_task.done():
            try:
                await asyncio.shield(dispatch_task)
            except asyncio.CancelledError:
                # to_thread 已运行后不能被 asyncio 取消；等待它观察同一个
                # cancel_checker 并完成，避免 session cleanup 漏过迟到 spawn。
                cancelled = True
        if cancelled:
            try:
                dispatch_task.result()
            except BaseException:
                pass
            raise asyncio.CancelledError
        return dispatch_task.result()

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
        *,
        steer_ids: tuple[str, ...] = (),
    ) -> None:
        """assistant tool_call 与 tool results 生成后的异步 hook。默认空。"""
        pass
