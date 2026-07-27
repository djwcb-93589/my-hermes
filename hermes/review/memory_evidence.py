"""将 Memory Review 的固定消息窗口整理为带来源标记的确定性证据。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from hermes.redaction import redact_explicit_secrets


_MAX_REVIEW_EVIDENCE_TEXT = 16_000
_MAX_USER_TEXT = 1_600
_MAX_ASSISTANT_TEXT = 900
_MAX_TOOL_ARGUMENTS_TEXT = 600
_MAX_TOOL_RESULT_TEXT = 700
_MAX_ENTRY_TEXT = 1_500
_TRUNCATED_MARKER = "[truncated]"
_OMITTED_ARGUMENT_KEYS = frozenset({
    "body",
    "content",
    "expression",
    "html",
    "javascript",
    "password",
    "script",
    "secret",
    "token",
})
_PRESERVED_RESULT_KEYS = (
    "ok",
    "status",
    "error_type",
    "error",
    "fatal",
    "retryable",
    "name",
    "action",
    "title",
    "url",
    "message",
)
_OBSERVATION_RESULT_KEYS = (
    "snapshot",
    "analysis",
    "result",
    "output",
)


class _EvidenceSource(str, Enum):
    USER_MESSAGE = "USER_MESSAGE"
    TOOL_OBSERVATION = "TOOL_OBSERVATION"
    TOOL_ERROR = "TOOL_ERROR"
    ASSISTANT_DECISION = "ASSISTANT_DECISION — UNVERIFIED"
    ASSISTANT_REPORT = "ASSISTANT_REPORT — UNVERIFIED"


@dataclass(frozen=True)
class _EvidenceEntry:
    """一条带任务位置、来源和选择优先级的审视证据。"""

    order: int
    task_index: int
    source: _EvidenceSource
    text: str
    priority: int


@dataclass
class _ToolEvent:
    """一次工具调用及其对应的可观察结果。"""

    order: int
    task_index: int
    name: str
    arguments: str
    call_id: str
    result: str = ""
    error: bool = False


@dataclass
class _ForegroundTaskEvidence:
    """由一条用户消息开始的前台任务证据。"""

    task_index: int
    entries: list[_EvidenceEntry] = field(default_factory=list)
    tool_events: list[_ToolEvent] = field(default_factory=list)


def _truncate_text(text: str, *, limit: int) -> str:
    """按字符上限裁剪文本，并显式说明证据不完整。"""
    if len(text) <= limit:
        return text
    marker = f"\n{_TRUNCATED_MARKER}"
    if limit <= len(marker):
        return marker[:limit]
    return f"{text[:limit - len(marker)].rstrip()}{marker}"


def _normalize_text(value: object, *, limit: int) -> str:
    """把证据转成脱敏文本，省略二进制并限制单条长度。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    if "\x00" in text:
        return "[binary content omitted]"
    text = redact_explicit_secrets(text).strip()
    return _truncate_text(text, limit=limit)


def _is_explicit_tool_error(content: str) -> bool:
    """只把工具明确返回的错误状态归为工具错误。"""
    if content.lstrip().lower().startswith("(error:"):
        return True
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and (
        payload.get("ok") is False
        or "error" in payload
        or bool(payload.get("error_type"))
    )


def _compact_value(
    value: object,
    *,
    key: str = "",
    depth: int = 0,
) -> object:
    """压缩嵌套参数，避免脚本、正文和大型集合进入 Memory 证据。"""
    normalized_key = key.strip().lower()
    if normalized_key in _OMITTED_ARGUMENT_KEYS:
        return "[omitted]"
    if depth >= 2:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        compact: dict[str, object] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= 12:
                compact[_TRUNCATED_MARKER] = True
                break
            compact[str(child_key)] = _compact_value(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
        return compact
    if isinstance(value, (list, tuple)):
        compact_items = [
            _compact_value(item, depth=depth + 1)
            for item in value[:6]
        ]
        if len(value) > 6:
            compact_items.append(_TRUNCATED_MARKER)
        return compact_items
    if isinstance(value, str):
        return _truncate_text(
            redact_explicit_secrets(value),
            limit=240,
        )
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _truncate_text(str(value), limit=240)


def _compact_tool_arguments(tool_name: str, arguments: object) -> str:
    """保留工具目标和动作，省略输入框原值及大段可执行内容。"""
    if isinstance(arguments, str):
        try:
            payload = json.loads(arguments)
        except (TypeError, ValueError):
            return _normalize_text(
                arguments,
                limit=_MAX_TOOL_ARGUMENTS_TEXT,
            )
    else:
        payload = arguments
    if not isinstance(payload, Mapping):
        return _normalize_text(payload, limit=_MAX_TOOL_ARGUMENTS_TEXT)

    compact_value = _compact_value(payload)
    if not isinstance(compact_value, Mapping):
        return _normalize_text(
            compact_value,
            limit=_MAX_TOOL_ARGUMENTS_TEXT,
        )
    compact = dict(compact_value)
    if tool_name == "browser_type" and "text" in compact:
        compact["text"] = "[input omitted]"
    return _normalize_text(compact, limit=_MAX_TOOL_ARGUMENTS_TEXT)


def _compact_tool_result(content: object) -> str:
    """优先保留状态、错误和短观察结果，不传递完整工具输出。"""
    if content is None:
        return ""
    if isinstance(content, str):
        raw = content
    else:
        try:
            raw = json.dumps(content, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            raw = str(content)
    if "\x00" in raw:
        return "[binary content omitted]"
    raw = redact_explicit_secrets(raw).strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return _normalize_text(raw, limit=_MAX_TOOL_RESULT_TEXT)
    if not isinstance(payload, Mapping):
        return _normalize_text(payload, limit=_MAX_TOOL_RESULT_TEXT)

    compact: dict[str, object] = {}
    for key in _PRESERVED_RESULT_KEYS:
        if key in payload:
            compact[key] = _compact_value(payload[key], key=key)
    for key in _OBSERVATION_RESULT_KEYS:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        compact[key] = _normalize_text(value, limit=420)
        break
    if not compact:
        return _normalize_text(payload, limit=_MAX_TOOL_RESULT_TEXT)
    return _normalize_text(compact, limit=_MAX_TOOL_RESULT_TEXT)


def _tool_call_details(tool_call: object) -> tuple[str, object, str]:
    """从持久化 tool call 中读取工具名、参数和调用标识。"""
    if not isinstance(tool_call, Mapping):
        return "unknown", "", ""
    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        return "unknown", "", str(tool_call.get("id", ""))
    name = function.get("name")
    return (
        name if isinstance(name, str) and name else "unknown",
        function.get("arguments", ""),
        str(tool_call.get("id", "")),
    )


def _spread_indices(size: int) -> list[int]:
    """按首、尾、中间递归覆盖证据，避免只保留窗口前部。"""
    if size <= 0:
        return []
    pending: list[tuple[int, int]] = [(0, size - 1)]
    selected: list[int] = []
    seen: set[int] = set()
    while pending:
        start, end = pending.pop(0)
        if start > end:
            continue
        middle = (start + end + 1) // 2
        for index in (start, end, middle):
            if index not in seen:
                seen.add(index)
                selected.append(index)
        if start + 1 <= middle - 1:
            pending.append((start + 1, middle - 1))
        if middle + 1 <= end - 1:
            pending.append((middle + 1, end - 1))
    return selected


def _selection_order(entries: list[_EvidenceEntry]) -> list[_EvidenceEntry]:
    """按可信优先级选择，并在多个前台任务之间轮转。"""
    unique: list[_EvidenceEntry] = []
    seen: set[tuple[_EvidenceSource, str]] = set()
    for entry in entries:
        identity = (entry.source, entry.text)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(entry)

    ordered: list[_EvidenceEntry] = []
    for priority in sorted({entry.priority for entry in unique}, reverse=True):
        by_task: dict[int, list[_EvidenceEntry]] = {}
        for entry in unique:
            if entry.priority == priority:
                by_task.setdefault(entry.task_index, []).append(entry)
        queues = {
            task_index: [
                task_entries[index]
                for index in _spread_indices(len(task_entries))
            ]
            for task_index, task_entries in by_task.items()
        }
        while any(queues.values()):
            for task_index in sorted(queues):
                if queues[task_index]:
                    ordered.append(queues[task_index].pop(0))
    return ordered


def _format_tool_event(event: _ToolEvent) -> str:
    """将工具动作与结果组合成一条可核对、但不解释原因的观察。"""
    parts = [f"Tool: {event.name}"]
    if event.arguments:
        parts.append(f"Parameters: {event.arguments}")
    if event.result:
        parts.append(f"Observed result: {event.result}")
    return _truncate_text("\n".join(parts), limit=_MAX_ENTRY_TEXT)


def _parse_tasks(messages: list[dict]) -> list[_ForegroundTaskEvidence]:
    """按用户消息切分任务，并关联工具调用与结果。"""
    tasks: list[_ForegroundTaskEvidence] = []
    current_task: _ForegroundTaskEvidence | None = None
    events_by_call_id: dict[str, _ToolEvent] = {}
    order = 0

    def ensure_task() -> _ForegroundTaskEvidence:
        nonlocal current_task
        if current_task is None:
            current_task = _ForegroundTaskEvidence(task_index=len(tasks))
            tasks.append(current_task)
        return current_task

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "user":
            current_task = _ForegroundTaskEvidence(task_index=len(tasks))
            tasks.append(current_task)
            content = _normalize_text(
                message.get("content", ""),
                limit=_MAX_USER_TEXT,
            )
            if content:
                current_task.entries.append(
                    _EvidenceEntry(
                        order=order,
                        task_index=current_task.task_index,
                        source=_EvidenceSource.USER_MESSAGE,
                        text=content,
                        priority=100,
                    )
                )
                order += 1
            continue

        task = ensure_task()
        if role == "assistant":
            content = _normalize_text(
                message.get("content", ""),
                limit=_MAX_ASSISTANT_TEXT,
            )
            tool_calls = message.get("tool_calls")
            if content:
                task.entries.append(
                    _EvidenceEntry(
                        order=order,
                        task_index=task.task_index,
                        source=(
                            _EvidenceSource.ASSISTANT_DECISION
                            if tool_calls
                            else _EvidenceSource.ASSISTANT_REPORT
                        ),
                        text=content,
                        priority=30 if tool_calls else 40,
                    )
                )
                order += 1
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    name, arguments, call_id = _tool_call_details(tool_call)
                    event = _ToolEvent(
                        order=order,
                        task_index=task.task_index,
                        name=name,
                        arguments=_compact_tool_arguments(name, arguments),
                        call_id=call_id,
                    )
                    task.tool_events.append(event)
                    if call_id:
                        events_by_call_id[call_id] = event
                    order += 1
            continue

        if role != "tool":
            continue
        call_id = str(message.get("tool_call_id", ""))
        result = _compact_tool_result(message.get("content", ""))
        if not result:
            continue
        event = events_by_call_id.get(call_id)
        if event is None:
            event = _ToolEvent(
                order=order,
                task_index=task.task_index,
                name="unknown",
                arguments="",
                call_id=call_id,
            )
            task.tool_events.append(event)
            order += 1
        event.result = result
        event.error = _is_explicit_tool_error(
            str(message.get("content", ""))
        )

    return tasks


def _collect_entries(
    tasks: list[_ForegroundTaskEvidence],
) -> list[_EvidenceEntry]:
    """把任务文本和工具观察转换为统一的选择单位。"""
    entries: list[_EvidenceEntry] = []
    for task in tasks:
        entries.extend(task.entries)
        for event in task.tool_events:
            entries.append(
                _EvidenceEntry(
                    order=event.order,
                    task_index=event.task_index,
                    source=(
                        _EvidenceSource.TOOL_ERROR
                        if event.error
                        else _EvidenceSource.TOOL_OBSERVATION
                    ),
                    text=_format_tool_event(event),
                    priority=90 if event.error else 70,
                )
            )
    return entries


def _render_evidence(
    tasks: list[_ForegroundTaskEvidence],
    entries: list[_EvidenceEntry],
) -> str:
    """在总预算内选择证据，并按原任务和消息顺序呈现。"""
    prefix = (
        "Fixed-window Memory Review evidence.\n\n"
        "Evidence rules:\n"
        "- USER_MESSAGE was supplied directly by the user.\n"
        "- TOOL_OBSERVATION records what a tool returned, not why it happened.\n"
        "- TOOL_ERROR records an explicit tool failure.\n"
        "- ASSISTANT_DECISION and ASSISTANT_REPORT are unverified.\n"
        "- Unverified assistant claims cannot independently justify a memory write.\n"
        "- Reusable procedures and troubleshooting methods belong to Skills, "
        "not Memory.\n"
    )
    heading_reserve = sum(
        len(f"\nForeground task {task.task_index + 1}:") + 2
        for task in tasks
    )
    marker_reserve = len(_TRUNCATED_MARKER) + 2
    remaining = max(
        0,
        _MAX_REVIEW_EVIDENCE_TEXT
        - len(prefix)
        - heading_reserve
        - marker_reserve,
    )
    candidates = _selection_order(entries)
    chosen: list[_EvidenceEntry] = []
    omitted = False
    for entry in candidates:
        rendered_length = len(entry.source.value) + len(entry.text) + 12
        if rendered_length <= remaining:
            chosen.append(entry)
            remaining -= rendered_length
        else:
            omitted = True

    chosen_by_task: dict[int, list[_EvidenceEntry]] = {}
    for entry in chosen:
        chosen_by_task.setdefault(entry.task_index, []).append(entry)

    parts = [prefix.rstrip()]
    for task in tasks:
        task_entries = chosen_by_task.get(task.task_index)
        if not task_entries:
            continue
        parts.append(f"\nForeground task {task.task_index + 1}:")
        for entry in sorted(task_entries, key=lambda item: item.order):
            parts.append(f"\n[{entry.source.value}]\n{entry.text}")
    if omitted:
        parts.append(f"\n{_TRUNCATED_MARKER}")
    return _truncate_text(
        "\n".join(parts),
        limit=_MAX_REVIEW_EVIDENCE_TEXT,
    )


def build_memory_review_messages(messages: list[dict]) -> list[dict]:
    """把原始固定窗口转换为单条带来源和可信等级的审视输入。"""
    tasks = _parse_tasks(messages)
    entries = _collect_entries(tasks)
    evidence = _render_evidence(tasks, entries)
    return [{"role": "user", "content": evidence}]


__all__ = ["build_memory_review_messages"]
