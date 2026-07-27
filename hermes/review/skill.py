"""尚未注册的 Skill Review Driver。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from hermes.redaction import redact_explicit_secrets
from hermes.review.contracts import (
    ForegroundReviewEvent,
    ReviewClaim,
    ReviewKind,
    ReviewRunSpec,
)
from hermes.review.skill_store import SkillReviewStore
from hermes.tools import (
    ApprovalMode,
    ExecutionEnvironment,
    ToolPolicy,
    ToolRiskLevel,
)


logger = logging.getLogger(__name__)


SKILL_REVIEW_SYSTEM_PROMPT = "You are a background skill review agent."

SKILL_REVIEW_INSTRUCTION = (
    "Review only the supplied fixed evidence window. First decide whether this "
    "work produced a reusable method. Prefer improving a Skill actually loaded "
    "in this window, then another relevant Skill you are permitted to curate; "
    "create a new Skill only when no suitable Skill exists. Do not store one-off "
    "facts, user preferences, project state, or temporary task progress in a "
    "Skill. Before changing an existing Skill, call skill_view, read its revision "
    "and governance_revision, then call skill_manage with both expected_revision "
    "and expected_governance_revision. For a new support file, use write_file "
    "without expected_revision but with expected_governance_revision. To update or "
    "remove a support file, first call skill_view with its relative_path, then pass "
    "that support file revision as expected_revision together with "
    "expected_governance_revision. Never overwrite a user-managed, system, "
    "external, or pinned Skill. You may only create, edit, patch, write_file, or "
    "remove_file. A tool result with ok=true only proves that the tool operation "
    "completed; it does not prove that the attempted strategy achieved the user's "
    "goal. When an assistant final report conflicts with recorded tool calls, tool "
    "results, or later execution decisions, treat the verifiable tool trace as "
    "authoritative. If this evidence has insufficient reusable value, reply "
    "exactly: Nothing to improve."
)


_MAX_EVIDENCE_TEXT = 1_600
_MAX_TOOL_RESULT_TEXT = 1_000
_MAX_TOOL_EVENT_TEXT = 850
_MAX_EXECUTION_NOTE_TEXT = 500
_MAX_REVIEW_EVIDENCE_TEXT = 20_000
_SECTION_BUDGETS = {
    "user": 2_400,
    "errors": 2_800,
    "decisions": 3_200,
    "skill_view": 2_000,
    "final": 2_800,
    "calls": 3_200,
    "success": 2_000,
}


@dataclass(frozen=True)
class _EvidenceEntry:
    """带任务位置和选择优先级的确定性证据条目。"""

    task_index: int
    order: int
    text: str
    priority: int = 0


@dataclass
class _ToolEvidence:
    """把一次工具调用与当时的判断及对应结果绑定在一起。"""

    task_index: int
    order: int
    name: str
    arguments: str
    assistant_note: str
    call_id: str
    result: str = ""


def _summarize_text(value: object, *, limit: int) -> str:
    """脱敏并限制证据文本，避免把大输出或二进制内容交给审视模型。"""
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
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n[truncated]"


def _tool_call_details(tool_call: object) -> tuple[str, str, str]:
    """从已持久化工具调用中提取名称、参数和调用标识。"""
    if not isinstance(tool_call, Mapping):
        return "unknown", "", ""
    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        return "unknown", "", str(tool_call.get("id", ""))
    name = function.get("name")
    arguments = function.get("arguments")
    return (
        name if isinstance(name, str) and name else "unknown",
        _summarize_text(arguments, limit=_MAX_EVIDENCE_TEXT),
        str(tool_call.get("id", "")),
    )


def _is_tool_error(content: str) -> bool:
    """识别需要保留的结构化或文本工具失败。"""
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


def _unique_entries(entries: list[_EvidenceEntry]) -> list[_EvidenceEntry]:
    """保留首次出现的证据，裁剪重复日志。"""
    unique: list[_EvidenceEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.text in seen:
            continue
        seen.add(entry.text)
        unique.append(entry)
    return unique


def _truncate_evidence(text: str, *, limit: int) -> str:
    """按字符预算确定性截断证据，并保留明确标记。"""
    if len(text) <= limit:
        return text
    marker = "\n[truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return f"{text[:limit - len(marker)].rstrip()}{marker}"


def _spread_indices(size: int) -> list[int]:
    """按首、尾、中间的顺序覆盖一组证据，避免只保留最早内容。"""
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
    """先分散保留高价值证据，再在各前台任务之间轮转选择。"""
    unique = _unique_entries(entries)
    selected: list[_EvidenceEntry] = []
    selected_orders: set[int] = set()

    priorities = sorted(
        {entry.priority for entry in unique if entry.priority > 0},
        reverse=True,
    )
    for priority in priorities:
        candidates = [entry for entry in unique if entry.priority == priority]
        for index in _spread_indices(len(candidates)):
            entry = candidates[index]
            selected.append(entry)
            selected_orders.add(entry.order)

    by_task: dict[int, list[_EvidenceEntry]] = {}
    for entry in unique:
        if entry.order in selected_orders:
            continue
        by_task.setdefault(entry.task_index, []).append(entry)
    task_queues = {
        task_index: [entries[index] for index in _spread_indices(len(entries))]
        for task_index, entries in by_task.items()
    }
    while any(task_queues.values()):
        for task_index in sorted(task_queues):
            queue = task_queues[task_index]
            if queue:
                selected.append(queue.pop(0))
    return selected


def _format_evidence_section(
    title: str,
    entries: list[_EvidenceEntry],
    *,
    budget: int,
    entry_limit: int = _MAX_EVIDENCE_TEXT,
) -> str:
    """在固定预算内跨任务分散选择证据，并保留明确裁剪标记。"""
    if not entries:
        return ""
    candidates = _selection_order(entries)
    header = f"\n{title}:\n"
    marker = "- [truncated]"
    rendered = [
        _truncate_evidence(entry.text, limit=entry_limit)
        for entry in candidates
    ]
    full_length = len(header) + sum(len(text) + 3 for text in rendered)
    remaining = budget - len(header)
    if remaining <= 0:
        return _truncate_evidence(header, limit=budget)
    truncated = full_length > budget
    if truncated:
        remaining -= len(marker) + 1

    chosen: list[tuple[_EvidenceEntry, str]] = []
    for entry, text in zip(candidates, rendered):
        cost = len(text) + 3
        if cost <= remaining:
            chosen.append((entry, text))
            remaining -= cost
    chosen.sort(key=lambda item: item[0].order)
    lines = [f"- {text}" for _, text in chosen]
    if truncated:
        lines.append(marker)
    return f"{header}{chr(10).join(lines)}"


def _tool_event_priority(event: _ToolEvidence) -> int:
    """优先保留会改变访问目标或包含明确资源位置的调用。"""
    arguments = event.arguments.lower()
    if '"url"' in arguments or "http://" in arguments or "https://" in arguments:
        return 2
    return 0


def _format_tool_event(event: _ToolEvidence) -> str:
    """把工具前判断、调用和结果组合成可核对的证据链。"""
    parts: list[str] = []
    if event.assistant_note:
        parts.append(f"Assistant decision: {event.assistant_note}")
    parts.append(f"Tool call: {event.name}")
    if event.arguments:
        parts.append(f"Parameters: {event.arguments}")
    if event.result:
        parts.append(f"Result: {event.result}")
    return "\n".join(parts)


def _build_review_messages(messages: list[dict]) -> list[dict]:
    """把固定窗口按角色和工具证据整理为单个确定性审视输入。"""
    user_entries: list[_EvidenceEntry] = []
    final_results: list[_EvidenceEntry] = []
    execution_notes: list[_EvidenceEntry] = []
    skill_view_entries: list[_EvidenceEntry] = []
    key_tool_calls: list[_EvidenceEntry] = []
    successful_tool_results: list[_EvidenceEntry] = []
    tool_errors: list[_EvidenceEntry] = []
    loaded_skill_names: set[str] = set()
    tool_events: list[_ToolEvidence] = []
    events_by_call_id: dict[str, _ToolEvidence] = {}
    task_index = -1
    order = 0

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        if role == "user":
            task_index += 1
        current_task = max(task_index, 0)
        content = _summarize_text(
            message.get("content", ""),
            limit=_MAX_EVIDENCE_TEXT,
        )
        if role == "user" and content:
            user_entries.append(
                _EvidenceEntry(current_task, order, content)
            )
            order += 1
        elif role == "assistant":
            tool_calls = message.get("tool_calls")
            if content and not tool_calls:
                final_results.append(
                    _EvidenceEntry(
                        current_task,
                        order,
                        _summarize_text(
                            content,
                            limit=_MAX_TOOL_RESULT_TEXT,
                        ),
                    )
                )
                order += 1
            elif content:
                execution_notes.append(
                    _EvidenceEntry(
                        current_task,
                        order,
                        _summarize_text(
                            content,
                            limit=_MAX_EXECUTION_NOTE_TEXT,
                        ),
                    )
                )
                order += 1
            if isinstance(tool_calls, list):
                for call_index, tool_call in enumerate(tool_calls):
                    name, arguments, call_id = _tool_call_details(tool_call)
                    if name == "skill_view":
                        try:
                            parsed_arguments = json.loads(arguments)
                        except (TypeError, ValueError):
                            parsed_arguments = None
                        if isinstance(parsed_arguments, dict):
                            skill_name = parsed_arguments.get("name")
                            if isinstance(skill_name, str) and skill_name:
                                loaded_skill_names.add(
                                    _summarize_text(
                                        skill_name,
                                        limit=_MAX_TOOL_RESULT_TEXT,
                                    )
                                )
                    event = _ToolEvidence(
                        task_index=current_task,
                        order=order,
                        name=name,
                        arguments=arguments,
                        assistant_note=(
                            _summarize_text(
                                content,
                                limit=_MAX_EXECUTION_NOTE_TEXT,
                            )
                            if call_index == 0
                            else ""
                        ),
                        call_id=call_id,
                    )
                    tool_events.append(event)
                    if call_id:
                        events_by_call_id[call_id] = event
                    order += 1
        elif role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            result = _summarize_text(
                message.get("content", ""),
                limit=_MAX_TOOL_RESULT_TEXT,
            )
            if not result:
                continue
            event = events_by_call_id.get(call_id)
            if event is not None:
                event.result = result
            else:
                tool_events.append(
                    _ToolEvidence(
                        task_index=current_task,
                        order=order,
                        name="unknown",
                        arguments="",
                        assistant_note="",
                        call_id=call_id,
                        result=result,
                    )
                )
                order += 1

    for event in tool_events:
        details = _format_tool_event(event)
        entry = _EvidenceEntry(
            event.task_index,
            event.order,
            details,
            priority=_tool_event_priority(event),
        )
        if event.name == "skill_view":
            skill_view_entries.append(entry)
        elif event.result and _is_tool_error(event.result):
            tool_errors.append(entry)
        else:
            key_tool_calls.append(entry)
            if event.result:
                successful_tool_results.append(
                    _EvidenceEntry(
                        event.task_index,
                        event.order,
                        f"Tool result: {event.name}\n{event.result}",
                        priority=_tool_event_priority(event),
                    )
                )

    for name in sorted(loaded_skill_names):
        skill_view_entries.append(
            _EvidenceEntry(0, order, f"Loaded Skill: {name}", priority=1)
        )
        order += 1
    if not final_results:
        final_results.append(
            _EvidenceEntry(0, order, "[no textual final result]")
        )

    sections = (
        (
            "User goals and corrections",
            user_entries,
            _SECTION_BUDGETS["user"],
        ),
        (
            "Tool errors and failure reasons",
            tool_errors,
            _SECTION_BUDGETS["errors"],
        ),
        (
            "Execution decisions and strategy transitions",
            execution_notes,
            _SECTION_BUDGETS["decisions"],
        ),
        (
            "skill_view calls and loaded Skills",
            skill_view_entries,
            _SECTION_BUDGETS["skill_view"],
        ),
        (
            "Assistant final reports (verify against tool evidence)",
            final_results,
            _SECTION_BUDGETS["final"],
        ),
        (
            "Key tool calls and parameters",
            key_tool_calls,
            _SECTION_BUDGETS["calls"],
        ),
        (
            "Successful tool output",
            successful_tool_results,
            _SECTION_BUDGETS["success"],
        ),
    )
    parts = ["Fixed-window Skill Review evidence:"]
    for title, entries, budget in sections:
        entry_limit = (
            _MAX_TOOL_EVENT_TEXT
            if title in {
                "Tool errors and failure reasons",
                "skill_view calls and loaded Skills",
                "Key tool calls and parameters",
            }
            else _MAX_EVIDENCE_TEXT
        )
        section = _format_evidence_section(
            title,
            entries,
            budget=budget,
            entry_limit=entry_limit,
        )
        if section:
            parts.append(section)
    evidence = _truncate_evidence(
        "\n".join(parts),
        limit=_MAX_REVIEW_EVIDENCE_TEXT,
    )
    return [{"role": "user", "content": evidence}]


class SkillReviewDriver:
    """将独立的 Skill Review 存储状态适配为通用 Review 运行契约。"""

    kind = ReviewKind.SKILL

    def __init__(
        self,
        *,
        store: SkillReviewStore,
        skill_tool_batch_interval: int,
        claim_ttl_seconds: float,
        retry_cooldown_seconds: float,
        max_iterations: int,
    ):
        self.store = store
        self.skill_tool_batch_interval = skill_tool_batch_interval
        self.claim_ttl_seconds = claim_ttl_seconds
        self.retry_cooldown_seconds = retry_cooldown_seconds
        self.max_iterations = max_iterations

    def record_progress(self, conn, event: ForegroundReviewEvent) -> None:
        if not event.completed:
            return
        message_upto = self.store.get_last_message_id(conn, event.session_id)
        if message_upto is None:
            logger.warning("skill review skipped progress without messages")
            return
        self.store.record_progress(
            conn,
            event.session_id,
            tool_batches=event.tool_batches,
            message_upto=message_upto,
        )

    def claim_due(self, conn, session_id: str) -> ReviewClaim | None:
        raw_claim = self.store.claim_due(
            conn,
            session_id,
            skill_tool_batch_interval=self.skill_tool_batch_interval,
            claim_ttl_seconds=self.claim_ttl_seconds,
        )
        if raw_claim is None:
            return None
        return ReviewClaim(
            kind=ReviewKind.SKILL,
            session_id=raw_claim["session_id"],
            token=raw_claim["claim_token"],
            payload={
                "tool_batch_upto": raw_claim["tool_batch_upto"],
                "message_after": raw_claim["message_after"],
                "message_upto": raw_claim["message_upto"],
            },
        )

    def validate_claim(self, claim: ReviewClaim) -> bool:
        if not isinstance(claim, ReviewClaim) or claim.kind is not ReviewKind.SKILL:
            return False
        if not isinstance(claim.session_id, str) or not claim.session_id.strip():
            return False
        if not isinstance(claim.token, str) or not claim.token:
            return False
        if not isinstance(claim.payload, Mapping):
            return False
        tool_batch_upto = claim.payload.get("tool_batch_upto")
        message_after = claim.payload.get("message_after")
        message_upto = claim.payload.get("message_upto")
        return (
            not isinstance(tool_batch_upto, bool)
            and isinstance(tool_batch_upto, int)
            and tool_batch_upto > 0
            and not isinstance(message_after, bool)
            and isinstance(message_after, int)
            and message_after >= 0
            and not isinstance(message_upto, bool)
            and isinstance(message_upto, int)
            and message_upto > message_after
        )

    def claim_is_valid(self, conn, claim: ReviewClaim) -> bool:
        return self.store.claim_is_valid(conn, claim.session_id, claim.token)

    def prepare_run(self, conn, claim: ReviewClaim) -> ReviewRunSpec:
        if not self.validate_claim(claim):
            raise ValueError("invalid skill review claim")
        if not self.claim_is_valid(conn, claim):
            raise RuntimeError("skill review claim expired before loading")
        window = self.store.load_message_window(
            conn,
            claim.session_id,
            after_message_id=claim.payload["message_after"],
            upto_message_id=claim.payload["message_upto"],
        )
        if not window:
            raise ValueError("skill review message window is empty")
        if not self.claim_is_valid(conn, claim):
            raise RuntimeError("skill review claim expired after loading")
        return ReviewRunSpec(
            messages=_build_review_messages(window),
            system_prompt=SKILL_REVIEW_SYSTEM_PROMPT,
            instruction=SKILL_REVIEW_INSTRUCTION,
            tool_policy=ToolPolicy(
                ExecutionEnvironment.BACKGROUND_REVIEW,
                enabled_toolsets=frozenset({"skill_read", "skill_manage"}),
                unattended=True,
                allowed_approval_modes=frozenset({ApprovalMode.NONE.value}),
                max_risk_level=ToolRiskLevel.MEDIUM,
            ),
            max_iterations=self.max_iterations,
            tool_context={
                "skill_actor": "background_review",
                "interactive_approval": False,
            },
        )

    def complete(self, conn, claim: ReviewClaim) -> bool:
        return self.store.complete(conn, claim.session_id, claim.token)

    def fail(self, conn, claim: ReviewClaim, error: str) -> bool:
        return self.store.fail(
            conn,
            claim.session_id,
            claim.token,
            error=error,
            retry_cooldown_seconds=self.retry_cooldown_seconds,
        )
