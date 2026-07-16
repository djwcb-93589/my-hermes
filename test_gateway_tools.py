"""Gateway 平台工具能力专项测试。"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9")

from hermes.conversation import _select_conversation_tools  # noqa: E402
from hermes.gateway.runner import GatewayRunner  # noqa: E402
from hermes.gateway.types import MessageEvent, SessionSource  # noqa: E402
from hermes.tools import register_all  # noqa: E402


FEISHU_TOOLSETS = [
    "terminal",
    "file",
    "memory",
    "skill",
    "delegate",
]


def _tool_names(definitions: list[dict]) -> set[str]:
    return {
        definition["function"]["name"]
        for definition in definitions
    }


def test_feishu_toolsets_are_platform_scoped_and_exclude_cron(tmp_path):
    register_all()
    runner = GatewayRunner(
        config={
            "gateway": {
                "platforms": {
                    "feishu": {"toolsets": FEISHU_TOOLSETS},
                    "cli": {},
                },
            },
        },
        db_path=str(tmp_path / "gateway.db"),
    )
    feishu_source = SessionSource(platform="feishu")
    cli_source = SessionSource(platform="cli")

    assert runner._enabled_toolsets_for_source(feishu_source) == FEISHU_TOOLSETS
    assert runner._enabled_toolsets_for_source(cli_source) == []

    definitions, allowed_names = _select_conversation_tools(
        runner._enabled_toolsets_for_source(feishu_source)
    )
    names = _tool_names(definitions)
    assert names == {
        "terminal",
        "file",
        "memory",
        "skill_view",
        "skills_list",
        "skill_manage",
        "delegate_task",
        "delegate_status",
        "delegate_result",
        "delegate_cancel",
    }
    assert names == allowed_names
    assert "cron" not in names

    prompt = runner._build_gateway_prompt(feishu_source)
    assert "This gateway session has no access to local tools" not in prompt
    assert "Scheduled tasks are available" not in prompt

    asyncio.run(runner.persistence.close())


def test_gateway_rejects_cron_toolset_before_startup(tmp_path):
    with pytest.raises(ValueError, match="unsupported toolset: 'cron'"):
        GatewayRunner(
            config={
                "gateway": {
                    "platforms": {
                        "feishu": {"toolsets": ["cron"]},
                    },
                },
            },
            db_path=str(tmp_path / "gateway.db"),
        )


def test_gateway_passes_platform_tools_and_noninteractive_context(
    tmp_path,
    monkeypatch,
):
    import hermes.conversation as conversation

    captured: dict = {}

    async def fake_run_conversation_async(*args, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "final_response": "done"}

    monkeypatch.setattr(
        conversation,
        "run_conversation_async",
        fake_run_conversation_async,
    )

    runner = GatewayRunner(
        config={
            "gateway": {
                "platforms": {
                    "feishu": {"toolsets": FEISHU_TOOLSETS},
                },
            },
        },
        db_path=str(tmp_path / "gateway.db"),
    )
    runner.persistence.call = AsyncMock(return_value=None)
    runner._get_async_client = lambda: object()
    event = MessageEvent(
        message_id="message-1",
        text="run a tool",
        source=SessionSource(platform="feishu"),
    )
    ctx = SimpleNamespace(
        active_generation=0,
        generation=0,
        cancel_requested=False,
        route_key="route-1",
        conversation_id="conversation-1",
        system_prompt="system",
        delivery_generation=0,
        delivery_id=None,
    )

    asyncio.run(runner._run_agent_async(event, ctx))

    assert captured["enabled_toolsets"] == FEISHU_TOOLSETS
    assert captured["tool_context"] == {
        "interactive_approval": False,
        "approval_mode": "remote",
    }
    asyncio.run(runner.persistence.close())


def test_remote_dangerous_terminal_command_is_blocked_without_prompt(
    monkeypatch,
):
    import hermes.tools.terminal as terminal

    monkeypatch.setattr(
        terminal,
        "detect_dangerous_command",
        lambda command: [(0, "danger", "dangerous operation")],
    )

    def fail_if_called(command, matches):
        raise AssertionError("remote gateway must not request console input")

    monkeypatch.setattr(terminal, "approve_command", fail_if_called)
    payload = json.loads(terminal.run_terminal(
        {"command": "dangerous command"},
        interactive_approval=False,
    ))

    assert payload["ok"] is False
    assert payload["fatal"] is True
    assert payload["error_type"] == "safety_blocked"


def test_delegate_inherits_noninteractive_approval(monkeypatch):
    import hermes.tools.delegate as delegate

    captured: dict = {}
    monkeypatch.setattr(delegate, "_filter_definitions", lambda toolsets: [{}])

    def fake_run_delegate_child(*args, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "summary": "done",
            "iterations": 1,
            "tools_used": [],
            "error": None,
        }

    monkeypatch.setattr(
        delegate,
        "run_delegate_child",
        fake_run_delegate_child,
    )
    result = json.loads(delegate.handle_delegate(
        {"goal": "inspect", "toolsets": ["terminal"]},
        interactive_approval=False,
    ))

    assert result["ok"] is True
    assert captured["tool_context"] == {"interactive_approval": False}
