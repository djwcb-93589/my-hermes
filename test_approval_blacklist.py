"""黑名单审批策略的回归测试。"""

from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from hermes.approval_policy import (  # noqa: E402
    ALLOW,
    ASK,
    ApprovalSecurityPolicy,
    assess_file_operation,
    assess_terminal_operation,
)
from hermes.tools import (  # noqa: E402
    ExecutionEnvironment,
    ToolPolicy,
    ToolRegistry,
)
from hermes.cron.job import CronJob  # noqa: E402
from hermes.cron.tool import _requires_gateway_authorization  # noqa: E402


def _policy(**kwargs) -> ApprovalSecurityPolicy:
    remote_default_allow = kwargs.pop("remote_default_allow", True)
    return ApprovalSecurityPolicy(
        approval_command_patterns=[r"\brm\b"],
        approval_file_rules=[
            {"actions": ["write"], "path_under": "D:/published"},
        ],
        remote_default_allow=remote_default_allow,
        **kwargs,
    )


def _terminal(command: str, policy: ApprovalSecurityPolicy):
    return assess_terminal_operation(
        {"command": command},
        normalized_cwd="D:/reports",
        session_key="gateway-test",
        remote_approval=True,
        interactive_approval=False,
        backend_context={"backend_type": "local"},
        security_policy=policy,
    )


def _file(path: str, policy: ApprovalSecurityPolicy):
    return assess_file_operation(
        {"action": "write", "path": path, "content": "draft"},
        normalized_path=path,
        session_key="gateway-test",
        remote_approval=True,
        sensitive=False,
        allow_sensitive=False,
        backend_context={"backend_type": "local"},
        security_policy=policy,
    )


def test_gateway_allows_operations_outside_blacklist():
    policy = _policy()

    terminal = _terminal("date", policy)
    file_write = _file("D:/reports/biweekly.md", policy)

    assert terminal.decision is ALLOW
    assert file_write.decision is ALLOW
    assert terminal.details["decision_source"] == "remote_blacklist_default_allow"
    assert file_write.details["decision_source"] == "remote_blacklist_default_allow"


def test_gateway_requests_approval_for_blacklisted_operations():
    policy = _policy()

    terminal = _terminal("rm draft.md", policy)
    file_write = _file("D:/published/biweekly.md", policy)

    assert terminal.decision is ASK
    assert file_write.decision is ASK


def test_remote_default_allow_can_be_disabled():
    policy = _policy(remote_default_allow=False)

    assert _terminal("date", policy).decision is ASK
    assert _file("D:/reports/biweekly.md", policy).decision is ASK


def test_registry_enforces_the_resolved_execution_boundary():
    registry = ToolRegistry()
    registry.register(
        name="allowed",
        toolset="test",
        schema={"name": "allowed", "parameters": {"type": "object"}},
        handler=lambda args, **kwargs: "ok",
        execution_environments=(ExecutionEnvironment.GATEWAY,),
        unattended_allowed=True,
        retry_safe=True,
    )

    result = registry.dispatch("allowed", {}, allowed_tool_names=set())

    assert '"error_type": "tool_not_authorized"' in result


def test_one_shot_file_only_cron_is_outside_approval_blacklist():
    base = dict(
        job_id="123456789abc",
        schedule="5m",
        prompt="write the report",
        session_key="gateway-test",
        created_at="2026-01-01T00:00:00",
        next_fire=1.0,
        one_shot=True,
        toolsets=["file"],
        capability_spec={"allow_file_write": True},
    )

    assert not _requires_gateway_authorization(CronJob(**base))
    assert _requires_gateway_authorization(CronJob(
        **{**base, "one_shot": False, "schedule": "every 5m"}
    ))
    assert _requires_gateway_authorization(CronJob(
        **{**base, "toolsets": ["terminal"]}
    ))
