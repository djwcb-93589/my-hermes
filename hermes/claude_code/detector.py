"""集中使用多条弱证据识别 Claude Code 事件与逻辑状态。"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass

from hermes.claude_code.contracts import (
    ClaudeCodeActionKind,
    ClaudeCodeActionRequired,
    ClaudeCodeEvent,
    ClaudeCodeEventType,
    ClaudeCodeState,
)
from hermes.claude_code.normalizer import (
    NormalizedOutputDelta,
    redact_claude_code_output,
)


MAX_EVENT_TEXT_CHARS = 2_048
MAX_DETECTION_CONTEXT_CHARS = 8_192
MAX_RECENT_EVENT_FINGERPRINTS = 128
MAX_ACTION_OPTIONS = 8
_EVENT_TRUNCATION_MARKER = "\n[… event text truncated …]\n"

_TERMINAL_PROCESS_STATUSES = frozenset(
    {"exited", "killed", "lost", "failed_start"}
)
_EXIT_PROCESS_STATUSES = frozenset({"exited", "killed", "failed_start"})
_INTERRUPT_EXIT_CODES = frozenset({-15, -2, 130})

_READY_RE = re.compile(
    r"(?i)(?:welcome\s+to\s+claude|how\s+can\s+i\s+help|"
    r"what\s+would\s+you\s+like|enter\s+(?:a\s+)?(?:task|prompt)|"
    r"ready\s+(?:for|to\s+accept)|可以开始|请输入任务)"
)
_PROGRESS_RE = re.compile(
    r"(?i)\b(?:analys(?:e|ing)|inspect(?:ing)?|read(?:ing)?|"
    r"search(?:ing)?|edit(?:ing)?|writ(?:e|ing)|creat(?:e|ing)|"
    r"updat(?:e|ing)|implement(?:ing)?|fix(?:ing)?|run(?:ning)?|"
    r"check(?:ing)?|test(?:ing)?|build(?:ing)?|review(?:ing)?)\b|"
    r"(?:正在|开始)(?:分析|读取|检查|修改|执行|运行|测试)"
)
_COMPLETION_RE = re.compile(
    r"(?i)(?:\b(?:completed|finished|done|implemented|resolved)\b|"
    r"\bsuccessfully\b|(?:任务|修改|实现)(?:已经|已)?完成|完成总结)"
)
_NEGATED_COMPLETION_RE = re.compile(
    r"(?i)\b(?:not|isn['’]?t|wasn['’]?t|incomplete)\s+"
    r"(?:successfully\s+)?(?:completed|finished|done)\b|"
    r"\bnothing\s+(?:was\s+)?done\b|"
    r"(?:尚未|未)(?:成功)?(?:完成|结束)"
)
_FAILURE_RE = re.compile(
    r"(?i)(?:\b(?:error|fatal|failed|failure|crash(?:ed)?|exception|"
    r"unable\s+to|cannot|could\s+not|command\s+not\s+found|"
    r"timed\s+out|connection\s+refused|"
    r"authentication\s+(?:failed|required))\b|"
    r"(?:失败|错误|异常|无法继续|命令不存在|需要认证))"
)
_AUTH_RE = re.compile(
    r"(?i)(?:\b(?:sign[ -]?in|log[ -]?in|authenticate|authentication|"
    r"oauth|api[ _-]?key|access[ _-]?token|credential|password|"
    r"passcode|authorization\s+code|verification\s+code|"
    r"device\s+code|one[ -]?time\s+code)\b|"
    r"登录|认证|凭据|令牌|密码|验证码)"
)
_AUTH_REQUIRED_RE = re.compile(
    r"(?i)(?:\b(?:required|please|must|to\s+continue|visit|"
    r"open\s+(?:the\s+)?browser|enter)\b|需要|请登录|请认证)"
)
_DESTRUCTIVE_RE = re.compile(
    r"(?i)(?:\b(?:delete|remove|overwrite|erase|reset|format|drop|"
    r"destroy|purge|force)\b|删除|覆盖|清空|重置|破坏性|强制)"
)
_EXTERNAL_RE = re.compile(
    r"(?i)(?:\b(?:network|internet|external|connect|download|upload|"
    r"fetch|remote|outside\s+(?:the\s+)?workspace)\b|"
    r"网络|外部访问|工作区外|下载|上传)"
)
_APPROVAL_RE = re.compile(
    r"(?i)(?:\b(?:allow|approve|permission|confirm|proceed|continue|"
    r"authorize|grant|run\s+(?:this|the)\s+command|"
    r"modify\s+(?:this|the)\s+file)\b|允许|批准|权限|确认|继续执行)"
)
_CLARIFICATION_RE = re.compile(
    r"(?i)(?:\b(?:clarify|provide|specify|which|what|where|when|how|"
    r"choose|select|need\s+(?:more|additional))\b|"
    r"请提供|请说明|请选择|需要更多|哪个|什么|哪里|如何)"
)
_PROMPT_VERB_RE = re.compile(
    r"(?i)(?:\b(?:enter|select|choose|provide|confirm|allow|approve)\b"
    r"[^\n]{0,120}:\s*$|(?:请输入|请选择|请确认|是否允许)[^\n]*$)"
)
_INLINE_OPTION_RE = re.compile(
    r"(?i)(?:\[(?:y|yes)/(?:n|no)\]|\((?:y|yes)/(?:n|no)\)|"
    r"\byes\s*/\s*no\b|\ballow\s*/\s*deny\b)"
)
_LINE_OPTION_RE = re.compile(
    r"(?m)^\s*(?:\d{1,2}[.)]|[-*]|\[[ xX]?\])\s+(.{1,160})\s*$"
)


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """检测器对单次观察给出的新事件、状态和待决动作。"""

    state: ClaudeCodeState
    events: tuple[ClaudeCodeEvent, ...]
    action_required: ClaudeCodeActionRequired | None


class ClaudeCodeOutputDetector:
    """按进程事实、增量文本和提示结构组合判断，未知时安全降级。"""

    def __init__(self) -> None:
        self._state = ClaudeCodeState.STARTING
        self._action_required: ClaudeCodeActionRequired | None = None
        self._last_action_fingerprint: str | None = None
        self._recent_fingerprints: deque[str] = deque()
        self._recent_fingerprint_set: set[str] = set()
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._last_process_status: str | None = None
        self._context_complete = True

    def begin_task(self) -> None:
        """记录首个任务已送达，不保存任务文本。"""

        self._state = ClaudeCodeState.STARTING
        self._action_required = None
        self._last_action_fingerprint = None
        self._completion_seen = False
        self._failure_seen = False
        self._progress_seen = False
        self._ready_seen = False
        self._context_complete = True
        self._clear_recent_event_fingerprints()

    def acknowledge_input(self) -> None:
        """输入明确送达后清除旧提示，但不保存回答内容。"""

        self._action_required = None
        self._last_action_fingerprint = None
        self._completion_seen = False
        self._failure_seen = False
        self._state = ClaudeCodeState.STARTING
        self._clear_recent_event_fingerprints()

    def detect(
        self,
        *,
        process_id: str,
        delta: NormalizedOutputDelta,
        process_status: str | None,
        exit_code: int | None,
        timestamp: float,
        task_submitted: bool,
        interrupt_requested: bool,
        lost: bool = False,
        observation_errors: tuple[tuple[str, str, str], ...] = (),
    ) -> DetectionResult:
        """生成本轮新事件；不轮询、不输入，也不执行任何审批。"""

        events: list[ClaudeCodeEvent] = []
        candidate = redact_claude_code_output(delta.text).strip()
        context = delta.normalized_output[-MAX_DETECTION_CONTEXT_CHARS:]

        if delta.cursor_gap:
            self._context_complete = False
            self._action_required = None
            self._last_action_fingerprint = None
            self._completion_seen = False
            self._failure_seen = False
            self._progress_seen = False
            self._ready_seen = False
            self._clear_recent_event_fingerprints()
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.CURSOR_GAP,
                process_id=process_id,
                cursor_start=(
                    delta.gap_start
                    if delta.gap_start is not None
                    else delta.cursor_start
                ),
                cursor_end=(
                    delta.gap_end
                    if delta.gap_end is not None
                    else delta.cursor_start
                ),
                timestamp=timestamp,
                text="ProcessManager output has a cursor gap",
                metadata={
                    "context_complete": False,
                    "next_page_cursor_start": delta.cursor_start,
                },
            )

        for phase, error_type, safe_message in observation_errors:
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.READ_ERROR,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text=safe_message,
                metadata={"phase": phase, "error_type": error_type},
            )
        if observation_errors:
            self._action_required = None
            self._last_action_fingerprint = None

        if candidate:
            output_metadata: dict[str, object] = {}
            if delta.limits_hit:
                output_metadata["limits_hit"] = delta.limits_hit
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.OUTPUT,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text=candidate,
                metadata=output_metadata,
            )
        elif delta.limits_hit:
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.OUTPUT,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text="Claude Code output reached a normalization limit",
                metadata={"limits_hit": delta.limits_hit},
            )

        prompt_structure = bool(
            candidate and self._looks_like_prompt(candidate)
        )
        progress_signal = bool(candidate and _PROGRESS_RE.search(candidate))
        completion_signal = bool(
            candidate
            and _COMPLETION_RE.search(candidate)
            and not _NEGATED_COMPLETION_RE.search(candidate)
            and not prompt_structure
        )
        failure_signal = bool(candidate and _FAILURE_RE.search(candidate))
        ready_signal = bool(candidate and _READY_RE.search(candidate))

        if not delta.cursor_gap:
            if progress_signal:
                self._progress_seen = True
                self._append_event(
                    events,
                    event_type=ClaudeCodeEventType.PROGRESS,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                    text=candidate,
                )
            if completion_signal:
                self._completion_seen = True
                self._append_event(
                    events,
                    event_type=ClaudeCodeEventType.COMPLETION_SIGNAL,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                    text=candidate,
                )
            if failure_signal:
                self._failure_seen = True
                self._append_event(
                    events,
                    event_type=ClaudeCodeEventType.FAILURE_SIGNAL,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                    text=candidate,
                )
            if ready_signal:
                self._ready_seen = True

            action_source = (
                candidate
                if ready_signal
                else self._action_source(candidate, context)
            )
            if self._context_complete:
                action = self._classify_action(
                    action_source,
                    delta.cursor_end,
                )
            elif self._looks_like_prompt(candidate):
                action = self._action(
                    ClaudeCodeActionKind.UNKNOWN_PROMPT,
                    "Claude Code emitted a prompt after an output gap",
                    candidate,
                    self._extract_options(candidate),
                    "unknown",
                    delta.cursor_end,
                )
                action_source = candidate
            else:
                action = None
            if (
                not task_submitted
                and ready_signal
                and action is not None
                and action.kind
                in {
                    ClaudeCodeActionKind.CLARIFICATION,
                    ClaudeCodeActionKind.UNKNOWN_PROMPT,
                }
            ):
                action = None
            if action is not None:
                self._record_action(
                    events,
                    action=action,
                    source_text=action_source,
                    process_id=process_id,
                    cursor_start=delta.cursor_start,
                    cursor_end=delta.cursor_end,
                    timestamp=timestamp,
                )
            elif candidate and (
                progress_signal
                or completion_signal
                or failure_signal
                or ready_signal
            ):
                self._action_required = None
                self._last_action_fingerprint = None
        elif prompt_structure:
            action = self._action(
                ClaudeCodeActionKind.UNKNOWN_PROMPT,
                "Claude Code emitted a prompt in a cursor-gap page",
                candidate,
                self._extract_options(candidate),
                "unknown",
                delta.cursor_end,
            )
            self._record_action(
                events,
                action=action,
                source_text=candidate,
                process_id=process_id,
                cursor_start=delta.cursor_start,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
            )

        if self._process_just_exited(process_status):
            self._append_event(
                events,
                event_type=ClaudeCodeEventType.PROCESS_EXIT,
                process_id=process_id,
                cursor_start=delta.cursor_end,
                cursor_end=delta.cursor_end,
                timestamp=timestamp,
                text="Claude Code process entered a terminal state",
                metadata={
                    "process_status": process_status,
                    "exit_code": exit_code,
                },
            )

        has_observation_error = bool(observation_errors)
        self._state = self._resolve_state(
            process_status=process_status,
            exit_code=exit_code,
            task_submitted=task_submitted,
            interrupt_requested=interrupt_requested,
            lost=lost,
            cursor_gap=delta.cursor_gap,
            has_observation_error=has_observation_error,
            context_complete=self._context_complete,
            candidate=bool(candidate),
            context=context,
            progress_signal=progress_signal,
            completion_signal=completion_signal,
            failure_signal=failure_signal,
            ready_signal=ready_signal,
        )
        self._last_process_status = process_status
        return DetectionResult(
            state=self._state,
            events=tuple(events),
            action_required=self._action_required,
        )

    def _resolve_state(
        self,
        *,
        process_status: str | None,
        exit_code: int | None,
        task_submitted: bool,
        interrupt_requested: bool,
        lost: bool,
        cursor_gap: bool,
        has_observation_error: bool,
        context_complete: bool,
        candidate: bool,
        context: str,
        progress_signal: bool,
        completion_signal: bool,
        failure_signal: bool,
        ready_signal: bool,
    ) -> ClaudeCodeState:
        if lost or process_status == "lost":
            return ClaudeCodeState.LOST
        if cursor_gap:
            return ClaudeCodeState.UNKNOWN

        terminal = process_status in _TERMINAL_PROCESS_STATUSES
        if terminal:
            if process_status == "failed_start":
                return ClaudeCodeState.FAILED
            if interrupt_requested and (
                process_status == "killed" or exit_code in _INTERRUPT_EXIT_CODES
            ):
                return ClaudeCodeState.INTERRUPTED
            if (
                process_status == "exited"
                and exit_code == 0
                and task_submitted
                and self._completion_seen
                and context_complete
                and not cursor_gap
                and not has_observation_error
            ):
                return ClaudeCodeState.COMPLETED
            if (
                (exit_code is not None and exit_code != 0)
                or self._failure_seen
                or failure_signal
            ):
                return ClaudeCodeState.FAILED
            return ClaudeCodeState.UNKNOWN

        if process_status is None or has_observation_error:
            return ClaudeCodeState.UNKNOWN
        if not context_complete:
            return ClaudeCodeState.UNKNOWN
        if process_status == "starting":
            return ClaudeCodeState.STARTING

        action = self._action_required
        if action is not None:
            if action.kind == ClaudeCodeActionKind.CLARIFICATION:
                return ClaudeCodeState.WAITING_INPUT
            if action.kind == ClaudeCodeActionKind.UNKNOWN_PROMPT:
                return ClaudeCodeState.UNKNOWN
            return ClaudeCodeState.WAITING_APPROVAL

        if not task_submitted and (
            ready_signal or self._ready_seen or _READY_RE.search(context)
        ):
            return ClaudeCodeState.READY
        if (
            task_submitted
            and completion_signal
            and ready_signal
        ):
            return ClaudeCodeState.READY
        if task_submitted and (
            progress_signal
            or self._progress_seen
            or completion_signal
            or self._state == ClaudeCodeState.WORKING
        ):
            return ClaudeCodeState.WORKING
        if candidate:
            return ClaudeCodeState.UNKNOWN
        if self._state in {
            ClaudeCodeState.READY,
            ClaudeCodeState.WORKING,
            ClaudeCodeState.WAITING_INPUT,
            ClaudeCodeState.WAITING_APPROVAL,
            ClaudeCodeState.UNKNOWN,
        }:
            return self._state
        return ClaudeCodeState.STARTING

    def _classify_action(
        self,
        candidate: str,
        cursor: int,
    ) -> ClaudeCodeActionRequired | None:
        if not candidate:
            return None
        prompt_like = self._looks_like_prompt(candidate)
        auth = bool(_AUTH_RE.search(candidate))
        destructive = bool(_DESTRUCTIVE_RE.search(candidate))
        external = bool(_EXTERNAL_RE.search(candidate))
        approval = bool(_APPROVAL_RE.search(candidate))
        clarification = bool(_CLARIFICATION_RE.search(candidate))
        options = self._extract_options(candidate)

        if auth and (
            prompt_like
            or approval
            or clarification
            or _AUTH_REQUIRED_RE.search(candidate)
        ):
            return self._action(
                ClaudeCodeActionKind.AUTHENTICATION,
                "Claude Code requires authentication",
                candidate,
                options,
                "high",
                cursor,
            )
        if destructive and prompt_like and (approval or options):
            return self._action(
                ClaudeCodeActionKind.DESTRUCTIVE_ACTION,
                "Claude Code requests confirmation for a destructive action",
                candidate,
                options,
                "critical",
                cursor,
            )
        if external and prompt_like and (approval or options):
            return self._action(
                ClaudeCodeActionKind.EXTERNAL_ACCESS,
                "Claude Code requests external access",
                candidate,
                options,
                "high",
                cursor,
            )
        if approval and prompt_like:
            return self._action(
                ClaudeCodeActionKind.APPROVAL,
                "Claude Code requests permission or confirmation",
                candidate,
                options,
                "medium",
                cursor,
            )
        if clarification and prompt_like:
            return self._action(
                ClaudeCodeActionKind.CLARIFICATION,
                "Claude Code requests additional information",
                candidate,
                options,
                "low",
                cursor,
            )
        if prompt_like:
            return self._action(
                ClaudeCodeActionKind.UNKNOWN_PROMPT,
                "Claude Code emitted an unclassified interactive prompt",
                candidate,
                options,
                "unknown",
                cursor,
            )
        return None

    @staticmethod
    def _action_source(candidate: str, context: str) -> str:
        """提示或选项跨页出现时补入有界上下文。"""

        if (
            ClaudeCodeOutputDetector._looks_like_prompt(candidate)
            or _LINE_OPTION_RE.search(candidate)
        ):
            contextual = redact_claude_code_output(context).strip()
            if contextual:
                return contextual[-MAX_DETECTION_CONTEXT_CHARS:]
        return candidate

    @staticmethod
    def _action(
        kind: ClaudeCodeActionKind,
        summary: str,
        prompt_text: str,
        options: tuple[str, ...],
        risk: str,
        cursor: int,
    ) -> ClaudeCodeActionRequired:
        return ClaudeCodeActionRequired(
            kind=kind,
            summary=summary,
            prompt_text=ClaudeCodeOutputDetector._bounded_text(prompt_text),
            options=options,
            risk=risk,
            cursor=cursor,
        )

    @staticmethod
    def _looks_like_prompt(text: str) -> bool:
        tail = text.rstrip()[-2_048:]
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        last_line = lines[-1] if lines else tail.strip()
        question = last_line.endswith(("?", "？"))
        prompt_verb = bool(_PROMPT_VERB_RE.search(tail))
        options = bool(_INLINE_OPTION_RE.search(tail)) or len(
            _LINE_OPTION_RE.findall(tail)
        ) >= 2
        return question or prompt_verb or options

    @staticmethod
    def _extract_options(text: str) -> tuple[str, ...]:
        options: list[str] = []
        inline = _INLINE_OPTION_RE.search(text)
        if inline:
            lowered = inline.group(0).casefold()
            options.extend(
                ("allow", "deny")
                if "allow" in lowered
                else ("yes", "no")
            )
        for match in _LINE_OPTION_RE.finditer(text):
            option = redact_claude_code_output(match.group(1)).strip()
            option = option[:160]
            if option and option not in options:
                options.append(option)
            if len(options) >= MAX_ACTION_OPTIONS:
                break
        return tuple(options[:MAX_ACTION_OPTIONS])

    @staticmethod
    def _action_event_type(
        kind: ClaudeCodeActionKind,
    ) -> ClaudeCodeEventType:
        if kind == ClaudeCodeActionKind.AUTHENTICATION:
            return ClaudeCodeEventType.AUTH_REQUIRED
        if kind == ClaudeCodeActionKind.CLARIFICATION:
            return ClaudeCodeEventType.QUESTION
        if kind == ClaudeCodeActionKind.UNKNOWN_PROMPT:
            return ClaudeCodeEventType.UNKNOWN_PROMPT
        return ClaudeCodeEventType.APPROVAL_REQUEST

    def _record_action(
        self,
        events: list[ClaudeCodeEvent],
        *,
        action: ClaudeCodeActionRequired,
        source_text: str,
        process_id: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
    ) -> None:
        """仅在动作内容实质变化时保存待决动作并生成事件。"""

        action_fingerprint = self._action_fingerprint(action, source_text)
        if action_fingerprint == self._last_action_fingerprint:
            return
        self._action_required = action
        self._last_action_fingerprint = action_fingerprint
        self._append_event(
            events,
            event_type=self._action_event_type(action.kind),
            process_id=process_id,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            timestamp=timestamp,
            text=action.prompt_text or action.summary,
            metadata={
                "kind": action.kind.value,
                "risk": action.risk,
                "options": action.options,
                "prompt_fingerprint": action_fingerprint,
            },
        )

    def _process_just_exited(self, process_status: str | None) -> bool:
        return (
            process_status in _EXIT_PROCESS_STATUSES
            and self._last_process_status not in _EXIT_PROCESS_STATUSES
        )

    def _clear_recent_event_fingerprints(self) -> None:
        """在可信交互边界丢弃旧去重窗口，不保存事件正文。"""

        self._recent_fingerprints.clear()
        self._recent_fingerprint_set.clear()

    def _append_event(
        self,
        events: list[ClaudeCodeEvent],
        *,
        event_type: ClaudeCodeEventType,
        process_id: str,
        cursor_start: int,
        cursor_end: int,
        timestamp: float,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        full_safe_text = redact_claude_code_output(text)
        safe_text = self._bounded_text(full_safe_text)
        safe_metadata = dict(metadata or {})
        if len(full_safe_text) > MAX_EVENT_TEXT_CHARS:
            safe_metadata["event_text_truncated"] = True
        fingerprint = self._event_fingerprint(
            event_type,
            full_safe_text,
            safe_metadata,
            cursor_start,
            cursor_end,
        )
        if fingerprint in self._recent_fingerprint_set:
            return
        self._recent_fingerprints.append(fingerprint)
        self._recent_fingerprint_set.add(fingerprint)
        while len(self._recent_fingerprints) > MAX_RECENT_EVENT_FINGERPRINTS:
            removed = self._recent_fingerprints.popleft()
            self._recent_fingerprint_set.discard(removed)
        events.append(
            ClaudeCodeEvent(
                event_type=event_type,
                process_id=process_id,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
                timestamp=timestamp,
                text=safe_text,
                metadata=safe_metadata,
            )
        )

    @staticmethod
    def _bounded_text(text: str) -> str:
        safe_text = redact_claude_code_output(text)
        if len(safe_text) <= MAX_EVENT_TEXT_CHARS:
            return safe_text
        content_budget = MAX_EVENT_TEXT_CHARS - len(
            _EVENT_TRUNCATION_MARKER
        )
        head_budget = content_budget // 2
        tail_budget = content_budget - head_budget
        return (
            f"{safe_text[:head_budget]}"
            f"{_EVENT_TRUNCATION_MARKER}"
            f"{safe_text[-tail_budget:]}"
        )

    @staticmethod
    def _event_fingerprint(
        event_type: ClaudeCodeEventType,
        text: str,
        metadata: dict[str, object],
        cursor_start: int,
        cursor_end: int,
    ) -> str:
        cursor_identity = (
            (cursor_start, cursor_end)
            if event_type
            in {
                ClaudeCodeEventType.CURSOR_GAP,
                ClaudeCodeEventType.PROCESS_EXIT,
                ClaudeCodeEventType.READ_ERROR,
            }
            else None
        )
        payload = (
            event_type.value,
            " ".join(text.split()).casefold(),
            tuple(sorted((key, repr(value)) for key, value in metadata.items())),
            cursor_identity,
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _action_fingerprint(
        action: ClaudeCodeActionRequired,
        source_text: str,
    ) -> str:
        payload = (
            action.kind.value,
            " ".join(action.prompt_text.split()).casefold(),
            tuple(" ".join(option.split()).casefold() for option in action.options),
            action.risk,
            " ".join(
                redact_claude_code_output(source_text).split()
            ).casefold(),
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_ACTION_OPTIONS",
    "MAX_DETECTION_CONTEXT_CHARS",
    "MAX_EVENT_TEXT_CHARS",
    "MAX_RECENT_EVENT_FINGERPRINTS",
    "ClaudeCodeOutputDetector",
    "DetectionResult",
]
