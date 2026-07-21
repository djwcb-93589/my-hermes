"""Terminal 常用只读命令白名单边界测试。"""

from __future__ import annotations

import pytest

from hermes.approval_policy import (
    ALLOW,
    ASK,
    assess_terminal_operation,
    classify_terminal_command,
)


@pytest.mark.parametrize(
    "command",
    [
        "ls -ltr /e/reports/",
        "ls -lahtr --sort=time --time-style=long-iso /e/reports/",
        "tail -n 50 /var/log/app.log",
        "tail -c10K /tmp/output.log",
        "stat -c '%y %n' /e/report.docx",
        "readlink -f /e/latest",
        "realpath -m /e/reports/../latest",
        "basename /e/report.docx",
        "dirname /e/report.docx",
        "whoami",
        "uname -a",
        "which -a python",
        "type -a python",
        "command -v python",
        "df -h /e/",
        "ls -lt /e/reports/ | tail -n 5",
    ],
)
def test_common_readonly_commands_are_automatically_allowed(command):
    classification = classify_terminal_command(command)

    assert classification.automatically_allowed is True


@pytest.mark.parametrize(
    "command",
    [
        "ls -R /",
        "ls --recursive /",
        "tail -f /var/log/app.log",
        "tail --follow=name /var/log/app.log",
        "command python script.py",
        "uname unexpected-operand",
        "stat",
        "readlink /e/*",
        "df -B 1 /e/",
        "cat /e/report.docx",
        "date -s 2030-01-01",
    ],
)
def test_commands_outside_readonly_boundary_still_require_approval(command):
    classification = classify_terminal_command(command)

    assert classification.automatically_allowed is False


@pytest.mark.parametrize(
    ("command", "expected_decision", "expected_source"),
    [
        ("ls -lt .", ALLOW, "remote_blacklist_default_allow"),
        ("tail -f gateway.log", ALLOW, "remote_blacklist_default_allow"),
    ],
)
def test_remote_gateway_uses_blacklist_default_allow(
    command,
    expected_decision,
    expected_source,
):
    assessment = assess_terminal_operation(
        {"command": command},
        normalized_cwd="D:/my-hermes",
        session_key="agent:main:feishu:dm:test-user",
        remote_approval=True,
        interactive_approval=False,
        backend_context={"backend_type": "local"},
    )

    assert assessment.decision is expected_decision
    assert assessment.details["decision_source"] == expected_source
