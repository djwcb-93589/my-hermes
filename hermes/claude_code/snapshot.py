"""组合规范化、检测结果与 ProcessManager 生命周期事实。"""

from __future__ import annotations

from hermes.claude_code.contracts import (
    CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
    ClaudeCodeProcessLog,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeSessionRef,
    ClaudeCodeSnapshot,
)
from hermes.claude_code.detector import ClaudeCodeOutputDetector
from hermes.claude_code.normalizer import (
    ClaudeCodeOutputNormalizer,
    NormalizedOutputDelta,
    redact_claude_code_output,
)


class ClaudeCodeObservationState:
    """保存单个受管 SessionRef 的有界解释状态，不拥有进程或日志。"""

    def __init__(self, *, initial_cursor: int = 0) -> None:
        self._normalizer = ClaudeCodeOutputNormalizer(
            initial_cursor=initial_cursor
        )
        self._detector = ClaudeCodeOutputDetector()
        self._task_submitted = False
        self._interrupt_requested = False

    def mark_input_submitted(self) -> None:
        """只记录输入送达事实，不保存输入正文。"""

        self._interrupt_requested = False
        if not self._task_submitted:
            self._task_submitted = True
            self._detector.begin_task()
        else:
            self._detector.acknowledge_input()

    def mark_interrupt_requested(self) -> None:
        """记录一次明确送达的受管 interrupt。"""

        self._interrupt_requested = True

    def build(
        self,
        *,
        session_ref: ClaudeCodeSessionRef,
        page: ClaudeCodeProcessLog | None,
        process_snapshot: ClaudeCodeProcessSnapshot | None,
        timestamp: float,
        lost: bool = False,
        observation_errors: tuple[tuple[str, str, str], ...] = (),
    ) -> ClaudeCodeSnapshot:
        """构造一次 Snapshot，不轮询、不等待且不执行任何输入。"""

        if page is not None and page.process_id != session_ref.process_id:
            raise ValueError("observation page process id changed")
        if (
            process_snapshot is not None
            and process_snapshot.process_id != session_ref.process_id
        ):
            raise ValueError("observation process id changed")

        process_status = self._process_status(
            page=page,
            process_snapshot=process_snapshot,
            lost=lost,
        )
        exit_code = (
            process_snapshot.exit_code
            if process_snapshot is not None
            else page.exit_code if page is not None else None
        )
        delta = self._normalize_page(
            session_ref=session_ref,
            page=page,
            process_status=process_status,
        )
        detection = self._detector.detect(
            process_id=session_ref.process_id,
            delta=delta,
            process_status=process_status,
            exit_code=exit_code,
            timestamp=timestamp,
            task_submitted=self._task_submitted,
            interrupt_requested=self._interrupt_requested,
            lost=lost,
            observation_errors=observation_errors,
        )
        return ClaudeCodeSnapshot(
            session_ref=session_ref,
            state=detection.state,
            events=detection.events,
            action_required=detection.action_required,
            raw_cursor=session_ref.cursor,
            normalized_output=redact_claude_code_output(
                delta.normalized_output
            ),
            process_status=process_status,
            exit_code=exit_code,
            last_activity_at=session_ref.last_activity_at,
        )

    def _normalize_page(
        self,
        *,
        session_ref: ClaudeCodeSessionRef,
        page: ClaudeCodeProcessLog | None,
        process_status: str | None,
    ) -> NormalizedOutputDelta:
        if page is None:
            return NormalizedOutputDelta(
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

        cursor_start = min(
            max(page.requested_cursor, page.available_from_cursor),
            page.next_cursor,
        )
        cursor_gap = (
            page.output_truncated
            or page.requested_cursor < page.available_from_cursor
        )
        return self._normalizer.feed(
            page.output,
            cursor_start=cursor_start,
            cursor_end=page.next_cursor,
            cursor_gap=cursor_gap,
            final=process_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
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
