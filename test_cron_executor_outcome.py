from types import SimpleNamespace

from hermes.cron.capability import CronCapabilityGuard
from hermes.cron.executor import _terminal_outcome


def _loop_result(*, ok: bool, status: str, summary: str = ""):
    return SimpleNamespace(
        ok=ok,
        status=status,
        summary=summary,
        error_type=None,
    )


def _guard_with_violation() -> CronCapabilityGuard:
    guard = CronCapabilityGuard({"scope": {}, "allowed_tool_names": []})
    guard.violation = {
        "tool_name": "terminal",
        "category": "terminal_shell_operator_not_granted",
    }
    return guard


def test_completed_loop_recovers_from_earlier_capability_denial():
    outcome = _terminal_outcome(
        _loop_result(ok=True, status="completed", summary="task completed"),
        timed_out=False,
        cancelled=False,
        guard=_guard_with_violation(),
        artifact_limit=False,
    )

    assert outcome == ("completed", None, "task completed", None)


def test_timeout_is_not_hidden_by_earlier_capability_denial():
    outcome = _terminal_outcome(
        _loop_result(ok=False, status="cancelled"),
        timed_out=True,
        cancelled=False,
        guard=_guard_with_violation(),
        artifact_limit=False,
    )

    assert outcome == (
        "cancelled",
        "timeout",
        "Cron task timed out before completion.",
        "timeout",
    )


def test_unrecovered_capability_denial_still_blocks_run():
    outcome = _terminal_outcome(
        _loop_result(ok=False, status="max_iterations"),
        timed_out=False,
        cancelled=False,
        guard=_guard_with_violation(),
        artifact_limit=False,
    )

    assert outcome == (
        "blocked",
        "cron_capability_denied",
        "Cron capability authorization does not permit a requested operation. "
        "Update the task or request authorization again.",
        "cron_capability_denied",
    )
