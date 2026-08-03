"""组合规范化、检测结果与 ProcessManager 生命周期事实。"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.claude_code.contracts import (
    CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
    MAX_NATIVE_INTERACTION_PROMPT_CHARS,
    ClaudeCodeActionRequired,
    ClaudeCodeProcessLog,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeSessionRef,
    ClaudeCodeSnapshot,
    ClaudeCodeState,
)
from hermes.claude_code.detector import ClaudeCodeOutputDetector
from hermes.claude_code.normalizer import (
    MAX_NORMALIZED_TEXT_CHARS,
    ClaudeCodeOutputNormalizer,
    NormalizedOutputDelta,
    redact_claude_code_output,
)


_DISPLAY_CURSOR_GAP_MARKER = "[ProcessManager cursor gap]"


@dataclass(frozen=True, slots=True)
class _ClaudeCodeObservationResult:
    """携带 Snapshot 与本轮是否发生实质活动。"""

    snapshot: ClaudeCodeSnapshot
    activity_detected: bool


class ClaudeCodeObservationState:
    """保存单个受管 SessionRef 的有界解释状态，不拥有进程或日志。"""

    def __init__(
        self,
        *,
        initial_cursor: int = 0,
        initial_process_status: str | None = None,
    ) -> None:
        self._normalizer = ClaudeCodeOutputNormalizer(
            initial_cursor=initial_cursor
        )
        self._interaction_normalizer = ClaudeCodeOutputNormalizer(
            max_raw_buffer=MAX_NATIVE_INTERACTION_PROMPT_CHARS,
            max_normalized_text=MAX_NATIVE_INTERACTION_PROMPT_CHARS,
            initial_cursor=initial_cursor,
            redact_output=False,
        )
        self._detector = ClaudeCodeOutputDetector()
        self._last_process_status = initial_process_status
        self._task_submitted = False
        self._interrupt_requested = False
        self._display_output = ""
        self._current_interaction_action: ClaudeCodeActionRequired | None = None

    def current_interaction(
        self,
    ) -> ClaudeCodeActionRequired | None:
        """返回当前短暂原生交互视图，绝不写入公开 Snapshot。"""

        return self._current_interaction_action

    def record_outbound_input(
        self,
        data: str,
        *,
        input_kind: str,
        sent_at: float,
        cursor_before: int,
        cursor_after: int,
    ) -> None:
        """立即转为安全 fingerprint，不保存输入正文。"""

        self._detector.record_outbound_input(
            data,
            input_kind=input_kind,
            sent_at=sent_at,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
        )

    def mark_input_submitted(self) -> None:
        """只记录输入送达事实，不保存输入正文。"""

        self._interrupt_requested = False
        if not self._task_submitted:
            self._task_submitted = True
            self._detector.begin_task()
        else:
            self._detector.acknowledge_input()
        self._clear_current_interaction_view()

    def mark_input_started(self) -> None:
        """用户开始新的 write 输入后不再保留旧原生提示副本。"""

        self._clear_current_interaction_view()

    def mark_input_delivery_unknown(self) -> None:
        """未知送达时使当前提示失效，避免旧动作被自动重复消费。"""

        self._interrupt_requested = False
        self._detector.acknowledge_input_delivery_unknown()
        self._clear_current_interaction_view()

    def mark_interrupt_requested(self) -> None:
        """记录一次明确送达的受管 interrupt。"""

        self._interrupt_requested = True
        self._detector.acknowledge_interrupt()
        self._clear_current_interaction_view()

    def mark_interrupt_delivery_unknown(self) -> None:
        """中断送达未知时保留请求事实，并使旧提示失效。"""

        self._interrupt_requested = True
        self._detector.acknowledge_interrupt()
        self._clear_current_interaction_view()

    def mark_terminal(self) -> None:
        """在已确认终态时立即使当前原生交互及旧动作失效。"""

        self._detector.invalidate_current_action()
        self._clear_current_interaction_view()

    def note_process_status(self, process_status: str | None) -> bool:
        """只在 ProcessStatus 确实变化时报告活动。"""

        if process_status is None:
            return False
        previous = self._last_process_status
        self._last_process_status = process_status
        return previous is not None and previous != process_status

    def build(
        self,
        *,
        session_ref: ClaudeCodeSessionRef,
        page: ClaudeCodeProcessLog | None,
        process_snapshot: ClaudeCodeProcessSnapshot | None,
        timestamp: float,
        interaction_output: str | None = None,
        lost: bool = False,
        observation_errors: tuple[tuple[str, str, str], ...] = (),
    ) -> ClaudeCodeSnapshot:
        """保持既有 Snapshot 返回契约。"""

        return self.build_result(
            session_ref=session_ref,
            page=page,
            process_snapshot=process_snapshot,
            timestamp=timestamp,
            interaction_output=interaction_output,
            lost=lost,
            observation_errors=observation_errors,
        ).snapshot

    def build_result(
        self,
        *,
        session_ref: ClaudeCodeSessionRef,
        page: ClaudeCodeProcessLog | None,
        process_snapshot: ClaudeCodeProcessSnapshot | None,
        timestamp: float,
        interaction_output: str | None = None,
        lost: bool = False,
        observation_errors: tuple[tuple[str, str, str], ...] = (),
    ) -> _ClaudeCodeObservationResult:
        """构造一次 Snapshot，不轮询、不等待且不执行任何输入。"""

        if page is not None and page.process_id != session_ref.process_id:
            raise ValueError("observation page process id changed")
        if (
            process_snapshot is not None
            and process_snapshot.process_id != session_ref.process_id
        ):
            raise ValueError("observation process id changed")
        if interaction_output is not None and not isinstance(
            interaction_output,
            str,
        ):
            raise ValueError("observation interaction output must be text")

        process_status = self._process_status(
            page=page,
            process_snapshot=process_snapshot,
            lost=lost,
        )
        status_changed = self.note_process_status(process_status)
        exit_code = (
            process_snapshot.exit_code
            if process_snapshot is not None
            else page.exit_code if page is not None else None
        )
        delta, interaction_delta = self._normalize_page(
            session_ref=session_ref,
            page=page,
            process_status=process_status,
            interaction_output=interaction_output,
        )
        detection = self._detector.detect(
            process_id=session_ref.process_id,
            session_owner=session_ref.session_owner,
            delta=delta,
            interaction_delta=interaction_delta,
            process_status=process_status,
            exit_code=exit_code,
            timestamp=timestamp,
            task_submitted=self._task_submitted,
            interrupt_requested=self._interrupt_requested,
            lost=lost,
            observation_errors=observation_errors,
        )
        interaction_action = self._detector._current_native_interaction()
        if self._has_native_interaction_view(interaction_action):
            self._current_interaction_action = interaction_action
        else:
            self._current_interaction_action = None
        if self._should_clear_interaction_normalizer(
            detection_action=interaction_action,
            delta=delta,
            process_status=process_status,
            lost=lost,
            observation_errors=observation_errors,
            state=detection.state,
            discard_interaction_view=detection.discard_interaction_view,
        ):
            self._interaction_normalizer.clear_view()
        display_output = self._update_display_output(
            delta=delta,
            latest_output=detection.display_output,
        )
        return _ClaudeCodeObservationResult(
            snapshot=ClaudeCodeSnapshot(
                session_ref=session_ref,
                state=detection.state,
                events=detection.events,
                action_required=detection.action_required,
                raw_cursor=session_ref.cursor,
                normalized_output=display_output,
                process_status=process_status,
                exit_code=exit_code,
                last_activity_at=session_ref.last_activity_at,
                last_observed_at=timestamp,
            ),
            activity_detected=(
                status_changed or detection.activity_detected
            ),
        )

    def _update_display_output(
        self,
        *,
        delta: NormalizedOutputDelta,
        latest_output: str,
    ) -> str:
        """为对外 Snapshot 保留不含输入 echo 的固定滚动视图。"""

        if delta.cursor_gap:
            self._display_output = _DISPLAY_CURSOR_GAP_MARKER
        safe_latest_output = redact_claude_code_output(latest_output).strip()
        if safe_latest_output:
            if self._display_output:
                self._display_output = (
                    f"{self._display_output}\n{safe_latest_output}"
                )
            else:
                self._display_output = safe_latest_output
        if len(self._display_output) > MAX_NORMALIZED_TEXT_CHARS:
            self._display_output = self._display_output[
                -MAX_NORMALIZED_TEXT_CHARS:
            ]
        return self._display_output

    def _normalize_page(
        self,
        *,
        session_ref: ClaudeCodeSessionRef,
        page: ClaudeCodeProcessLog | None,
        process_status: str | None,
        interaction_output: str | None,
    ) -> tuple[NormalizedOutputDelta, NormalizedOutputDelta]:
        if page is None:
            safe_delta = NormalizedOutputDelta(
                text="",
                normalized_output=self._normalizer.normalized_output,
                cursor_start=session_ref.cursor,
                cursor_end=session_ref.cursor,
                cursor_gap=False,
                gap_start=None,
                gap_end=None,
                redraw_only=False,
                limits_hit=(),
            )
            interaction_delta = NormalizedOutputDelta(
                text="",
                normalized_output=(
                    self._interaction_normalizer.normalized_output
                ),
                cursor_start=session_ref.cursor,
                cursor_end=session_ref.cursor,
                cursor_gap=False,
                gap_start=None,
                gap_end=None,
                redraw_only=False,
                limits_hit=(),
            )
            return safe_delta, interaction_delta

        cursor_start = min(
            max(page.requested_cursor, page.available_from_cursor),
            page.next_cursor,
        )
        cursor_gap = (
            page.output_truncated
            or page.requested_cursor < page.available_from_cursor
        )
        safe_delta = self._normalizer.feed(
            page.output,
            cursor_start=cursor_start,
            cursor_end=page.next_cursor,
            cursor_gap=cursor_gap,
            final=process_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
        )
        interaction_delta = self._interaction_normalizer.feed(
            interaction_output or "",
            cursor_start=cursor_start,
            cursor_end=page.next_cursor,
            cursor_gap=cursor_gap,
            final=process_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
        )
        return safe_delta, interaction_delta

    def _clear_current_interaction_view(self) -> None:
        """在输入、中断或终态边界删除原生 Prompt 的临时副本。"""

        self._current_interaction_action = None
        self._detector.clear_native_interaction_view()
        self._interaction_normalizer.clear_view()

    @staticmethod
    def _has_native_interaction_view(
        action: ClaudeCodeActionRequired | None,
    ) -> bool:
        return bool(
            action is not None
            and action.raw_prompt_text is not None
            and action.raw_options is not None
        )

    @staticmethod
    def _should_clear_interaction_normalizer(
        *,
        detection_action: ClaudeCodeActionRequired | None,
        delta: NormalizedOutputDelta,
        process_status: str | None,
        lost: bool,
        observation_errors: tuple[tuple[str, str, str], ...],
        state: ClaudeCodeState,
        discard_interaction_view: bool,
    ) -> bool:
        """只在原生视图已生成或已失效时丢弃其提取缓冲。"""

        return bool(
            detection_action is not None
            or delta.cursor_gap
            or lost
            or observation_errors
            or discard_interaction_view
            or (
                process_status is not None
                and process_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
            )
            or state == ClaudeCodeState.WORKING
        )

    @staticmethod
    def _process_status(
        *,
        page: ClaudeCodeProcessLog | None,
        process_snapshot: ClaudeCodeProcessSnapshot | None,
        lost: bool,
    ) -> str | None:
        if lost:
            return None
        if process_snapshot is not None:
            return process_snapshot.status
        if page is not None:
            return page.status
        return None


__all__ = ["ClaudeCodeObservationState"]
