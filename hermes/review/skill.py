"""尚未注册的 Skill Review Driver。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

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
    "remove_file. If this evidence has insufficient reusable value, reply exactly: "
    "Nothing to improve."
)


_MAX_EVIDENCE_TEXT = 1_600
_MAX_TOOL_RESULT_TEXT = 1_000
_MAX_REVIEW_EVIDENCE_TEXT = 12_000
_SECTION_BUDGETS = {
    "user": 2_300,
    "errors": 2_400,
    "skill_view": 1_800,
    "final": 2_400,
    "calls": 1_200,
    "success": 900,
}


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


def _unique_entries(entries: list[str]) -> list[str]:
    """保留首次出现的证据，裁剪重复日志。"""
    return list(dict.fromkeys(entries))


def _truncate_evidence(text: str, *, limit: int) -> str:
    """按字符预算确定性截断证据，并保留明确标记。"""
    if len(text) <= limit:
        return text
    marker = "\n[truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return f"{text[:limit - len(marker)].rstrip()}{marker}"


def _format_evidence_section(
    title: str,
    entries: list[str],
    *,
    budget: int,
) -> str:
    """在固定预算内输出一组同优先级证据。"""
    if not entries:
        return ""
    section = f"\n{title}:\n" + "\n".join(
        f"- {entry}" for entry in _unique_entries(entries)
    )
    return _truncate_evidence(section, limit=budget)


def _build_review_messages(messages: list[dict]) -> list[dict]:
    """把固定窗口按角色和工具证据整理为单个确定性审视输入。"""
    user_entries: list[str] = []
    final_results: list[str] = []
    skill_view_entries: list[str] = []
    key_tool_calls: list[str] = []
    successful_tool_results: list[str] = []
    tool_errors: list[str] = []
    loaded_skill_names: set[str] = set()
    call_names: dict[str, str] = {}

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        content = _summarize_text(
            message.get("content", ""),
            limit=_MAX_EVIDENCE_TEXT,
        )
        if role == "user" and content:
            user_entries.append(content)
        elif role == "assistant":
            tool_calls = message.get("tool_calls")
            if content and not tool_calls:
                final_results.append(
                    _summarize_text(content, limit=_MAX_TOOL_RESULT_TEXT)
                )
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    name, arguments, call_id = _tool_call_details(tool_call)
                    if call_id:
                        call_names[call_id] = name
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
                    details = f"Tool call: {name}"
                    if arguments:
                        details = f"{details}\nParameters: {arguments}"
                    if name == "skill_view":
                        skill_view_entries.append(details)
                    else:
                        key_tool_calls.append(details)
        elif role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            tool_name = call_names.get(call_id, "unknown")
            result = _summarize_text(
                message.get("content", ""),
                limit=_MAX_TOOL_RESULT_TEXT,
            )
            if not result:
                continue
            entry = f"Tool result: {tool_name}\n{result}"
            if _is_tool_error(result):
                tool_errors.append(entry)
            else:
                successful_tool_results.append(entry)

    skill_view_entries.extend(
        f"Loaded Skill: {name}" for name in sorted(loaded_skill_names)
    )
    if not final_results:
        final_results.append("[no textual final result]")

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
            "skill_view calls and loaded Skills",
            skill_view_entries,
            _SECTION_BUDGETS["skill_view"],
        ),
        (
            "Final results from this window",
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
        section = _format_evidence_section(title, entries, budget=budget)
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
