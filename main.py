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
from datetime import datetime

from hermes.config import BROWSER_CONFIG, DB_PATH, MODEL, BASE_URL, HERMES_HOME
from hermes.cli_approval import execute_cli_approval
from hermes.cli_streaming import CLIStreamRenderer
from hermes.cli_ui import CLIInput, patched_cli_stdout
from hermes.conversation import run_conversation
from hermes.db import (
    create_session,
    get_session_messages,
    init_db,
    list_cli_sessions,
    replace_tool_message_content,
    session_exists,
)
from hermes.session_resources import cleanup_all_session_resources
from hermes.prompt import build_system_prompt
from hermes.tools import ExecutionEnvironment, ToolPolicy, register_all, registry


CLI_SESSION_LIST_LIMIT = 10
CLI_REPLAY_MESSAGE_LIMIT = 4000


def _format_cli_session_time(timestamp: object) -> str:
    """把会话摘要时间转换为便于终端确认的本地时间。"""
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "unknown time"


def _format_cli_preview(content: object, limit: int = 120) -> str:
    """将多行消息压缩为一行预览。"""
    preview = " ".join(str(content or "").split())
    if len(preview) > limit:
        return f"{preview[:limit - 3]}..."
    return preview


def _show_cli_sessions(
    conn,
    current_session_id: str | None,
) -> dict[str, str]:
    """显示最近 CLI 会话，并返回本次列表对应的选择映射。"""
    sessions = list_cli_sessions(conn, limit=CLI_SESSION_LIST_LIMIT, offset=0)
    if not sessions:
        print("\nAssistant: no saved CLI sessions\n")
        return {}

    session_choices: dict[str, str] = {}
    print("\nSessions:")
    for index, session in enumerate(sessions, start=1):
        choice = str(index)
        session_id = str(session["session_id"])
        session_choices[choice] = session_id
        current_marker = " [current]" if session_id == current_session_id else ""
        print(
            f"  {choice}. {_format_cli_preview(session['preview'])} "
            f"({_format_cli_session_time(session['timestamp'])}){current_marker}"
        )
    print("Use /resume <number> to restore a session.\n")
    return session_choices


def _format_replayed_content(content: object) -> str:
    """保留历史正文，并为单条超长消息设置终端显示上限。"""
    text = str(content or "")
    if len(text) <= CLI_REPLAY_MESSAGE_LIMIT:
        return text
    return f"{text[:CLI_REPLAY_MESSAGE_LIMIT]}\n[message truncated]"


def _tool_call_names(tool_calls: object) -> list[str]:
    """从已保存的工具调用中提取名称，避免回放原始参数。"""
    if not isinstance(tool_calls, list):
        return []

    names: list[str] = []
    for tool_call in tool_calls:
        if isinstance(tool_call, dict):
            function = tool_call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        else:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", None)
        if name:
            names.append(str(name))
    return names


def _show_resumed_context(conn, session_id: str) -> None:
    """逐条回放已保存的会话历史，不输出原始工具结果。"""
    messages = get_session_messages(conn, session_id)
    if not messages:
        print("Restored conversation: no messages\n")
        return

    print("Restored conversation:")
    for message in messages:
        role = message.get("role")
        content = _format_replayed_content(message.get("content"))
        if role == "user":
            print(f"You: {content}")
            continue
        if role == "assistant":
            if content:
                print(f"Assistant: {content}")
            tool_names = _tool_call_names(message.get("tool_calls"))
            if tool_names:
                print(f"Assistant: [tool: {', '.join(tool_names)}]")
            elif not content and message.get("tool_calls"):
                print("Assistant: [tool call]")
            elif not content:
                print("Assistant: [empty message]")
            continue
        if role == "tool":
            print("Tool: [result omitted]")
    print()


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
            "Type '/quit' to exit. Use /sessions, /resume <number>, or /new. "
            "Use /approve [once|session] or /deny when prompted.\n"
        )
        pending_approval: dict | None = None
        session_choices: dict[str, str] = {}

        with patched_cli_stdout():
            while True:
                raw_user_input = cli_input.prompt()
                stripped_user_input = raw_user_input.lstrip()
                literal_input = (
                    raw_user_input.startswith("//")
                    or raw_user_input[:1].isspace()
                )
                user_input = (
                    stripped_user_input[1:]
                    if literal_input and stripped_user_input.startswith("//")
                    else raw_user_input.strip()
                )
                if not user_input or (
                    not literal_input and user_input.lower() in ("quit", "exit")
                ):
                    break
                if pending_approval is not None:
                    command, _, requested_scope = user_input.partition(" ")
                    command = "" if literal_input else command.lower()
                    if command in {"/quit", "/exit"}:
                        break
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
                command = "" if literal_input else command.lower()
                if command in {"/quit", "/exit"}:
                    break
                if command == "/resume":
                    selection = command_argument.strip()
                    if not selection:
                        session_choices = _show_cli_sessions(conn, session_id)
                        continue
                    requested_session_id = session_choices.get(selection, selection)
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
                    _show_resumed_context(conn, session_id)
                    continue
                if command == "/new":
                    session_id = None
                    print("\nAssistant: new session will start with your next message\n")
                    continue
                if command == "/sessions":
                    session_choices = _show_cli_sessions(conn, session_id)
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
