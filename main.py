"""
Hermes 入口。

默认模式：交互式 CLI REPL —— PromptSession → run_conversation → output。
其它模式经 argv 分发：--gateway、--simulate。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from hermes.config import BROWSER_CONFIG, DB_PATH, MODEL, BASE_URL, HERMES_HOME
from hermes.cli_approval import execute_cli_approval
from hermes.cli_streaming import CLIStreamRenderer
from hermes.cli_ui import CLIInput, patched_cli_stdout
from hermes.conversation import run_conversation
from hermes.db import (
    create_session,
    init_db,
    replace_tool_message_content,
    session_exists,
)
from hermes.session_resources import cleanup_all_session_resources
from hermes.prompt import build_system_prompt
from hermes.tools import ExecutionEnvironment, ToolPolicy, register_all, registry


def _cli_tool_policy() -> ToolPolicy:
    """只在配置启用时把 browser 加入当前 CLI 会话的工具边界。"""
    base_policy = ToolPolicy(ExecutionEnvironment.CLI)
    enabled_toolsets = set(registry.default_toolsets_for_policy(base_policy))
    if BROWSER_CONFIG["enabled"]:
        enabled_toolsets.add("browser")
    return ToolPolicy(
        ExecutionEnvironment.CLI,
        enabled_toolsets=frozenset(enabled_toolsets),
    )


def cli_loop():
    """默认模式：复用 PromptSession 的交互式 CLI REPL。"""
    register_all()
    tool_policy = _cli_tool_policy()
    enabled_toolsets = sorted(registry.resolve(tool_policy).toolsets)
    cli_input = CLIInput()
    cli_renderer = CLIStreamRenderer()
    conn = init_db(DB_PATH)

    try:
        print(f"Profile (HERMES_HOME): {HERMES_HOME}")
        print(f"Model: {MODEL} | Base URL: {BASE_URL}")

        session_id: str | None = None
        cached_prompt = build_system_prompt(
            os.getcwd(),
            enabled_toolsets=enabled_toolsets,
        )
        print(f"System prompt: {len(cached_prompt)} chars")

        print(
            "Type 'quit' to exit. Use /resume <session_id> or /new. "
            "Use /approve [once|session] or /deny when prompted.\n"
        )
        pending_approval: dict | None = None

        with patched_cli_stdout():
            while True:
                user_input = cli_input.prompt().strip()
                if not user_input or user_input.lower() in ("quit", "exit"):
                    break
                if pending_approval is not None:
                    command, _, requested_scope = user_input.partition(" ")
                    command = command.lower()
                    if command == "/deny":
                        denied = json.dumps({
                            "ok": False,
                            "error_type": "approval_denied",
                            "error": "operation was denied by the user",
                        }, ensure_ascii=False)
                        if not replace_tool_message_content(
                            conn,
                            session_id,
                            str(pending_approval.get("tool_call_id", "")),
                            denied,
                        ):
                            print("\nAssistant: approval result could not be recorded\n")
                        else:
                            print("\nAssistant: approval denied\n")
                        pending_approval = None
                        continue
                    if command != "/approve":
                        print("\nAssistant: enter /approve [once|session] or /deny\n")
                        continue
                    scope = (requested_scope.strip().lower() or "once")
                    try:
                        execute_cli_approval(
                            conn,
                            session_id=session_id,
                            request=pending_approval,
                            scope=scope,
                        )
                    except (RuntimeError, TypeError, ValueError) as exc:
                        print(f"\nAssistant: approval execution failed: {exc}\n")
                    else:
                        cli_renderer.begin_request()
                        resumed = run_conversation(
                            "",
                            conn,
                            session_id,
                            cached_prompt,
                            session_key=session_id,
                            resume_from_history=True,
                            tool_policy=tool_policy,
                            stream_sink=cli_renderer.handle_event,
                        )
                        if resumed.get("status") == "awaiting_approval":
                            cli_renderer.ensure_line_break()
                            request = resumed.get("approval_request")
                            if isinstance(request, dict):
                                pending_approval = request
                                scopes = request.get("details", {}).get(
                                    "allowed_grant_scopes", []
                                )
                                summary = str(request.get(
                                    "summary", "需要批准的工具操作"
                                ))
                                print(
                                    "\nAssistant: "
                                    f"{summary}\nApprove with /approve"
                                    f" (available scopes: {', '.join(scopes)}) or /deny\n"
                                )
                                continue
                            print("\nAssistant: approval request is invalid\n")
                        elif not cli_renderer.was_final_response_streamed(
                            resumed["final_response"]
                        ):
                            print(f"\nAssistant: {resumed['final_response']}\n")
                    pending_approval = None
                    continue

                command, _, command_argument = user_input.partition(" ")
                command = command.lower()
                if command == "/resume":
                    requested_session_id = command_argument.strip()
                    if not requested_session_id:
                        print("\nAssistant: usage: /resume <session_id>\n")
                        continue
                    if not session_exists(
                        conn,
                        requested_session_id,
                        source="cli",
                    ):
                        print(
                            "\nAssistant: session not found: "
                            f"{requested_session_id}\n"
                        )
                        continue
                    session_id = requested_session_id
                    print(f"\nAssistant: resumed session {session_id}\n")
                    continue
                if command == "/new":
                    session_id = None
                    print("\nAssistant: new session will start with your next message\n")
                    continue
                if command == "/sessions":
                    print("\nAssistant: /sessions is not available yet\n")
                    continue
                if command.startswith("/"):
                    print(f"\nAssistant: unknown command: {command}\n")
                    continue

                if session_id is None:
                    session_id = create_session(conn)
                cli_renderer.begin_request()
                result = run_conversation(
                    user_input,
                    conn,
                    session_id,
                    cached_prompt,
                    session_key=session_id,
                    tool_policy=tool_policy,
                    stream_sink=cli_renderer.handle_event,
                )
                if result.get("status") == "awaiting_approval":
                    cli_renderer.ensure_line_break()
                    request = result.get("approval_request")
                    if not isinstance(request, dict):
                        print("\nAssistant: approval request is invalid\n")
                        continue
                    pending_approval = request
                    scopes = request.get("details", {}).get(
                        "allowed_grant_scopes", []
                    )
                    summary = str(request.get("summary", "需要批准的工具操作"))
                    print(
                        "\nAssistant: "
                        f"{summary}\nApprove with /approve"
                        f" (available scopes: {', '.join(scopes)}) or /deny\n"
                    )
                    continue
                if not cli_renderer.was_final_response_streamed(
                    result["final_response"]
                ):
                    print(f"\nAssistant: {result['final_response']}\n")
    finally:
        conn.close()
        cleanup_all_session_resources()


def main():
    if "--gateway" in sys.argv or "--gateway-unified" in sys.argv:
        # 统一 Gateway 入口(读 config.yaml gateway.platforms)
        from hermes.gateway_entry import run_gateway
        asyncio.run(run_gateway())
    elif "--weixin-login" in sys.argv:
        # 个人微信二维码登录
        from hermes.gateway_weixin_login import run as run_weixin_login
        run_weixin_login()
    elif "--gateway-console" in sys.argv:
        # ConsoleAdapter Gateway 入口（保留向后兼容）
        from hermes.gateway_console import run_gateway_console
        asyncio.run(run_gateway_console())
    elif "--simulate" in sys.argv:
        from hermes.gateway_simulated import run_gateway_simulated
        asyncio.run(run_gateway_simulated())
    else:
        cli_loop()


if __name__ == "__main__":
    main()
