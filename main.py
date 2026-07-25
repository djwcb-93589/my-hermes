"""
Hermes 入口。

默认模式：交互式 CLI REPL —— PromptSession → CLI worker → output。
其它模式经 argv 分发：--gateway、--simulate。
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

from hermes.config import BROWSER_CONFIG, MODEL, BASE_URL, HERMES_HOME
from hermes.cli_state_machine import CLIWorker, CLIWorkerResult, CLIWorkerTask
from hermes.cli_streaming import CLIStreamRenderer
from hermes.cli_ui import CLIInput, patched_cli_stdout
from hermes.session_resources import cleanup_all_session_resources
from hermes.prompt import build_system_prompt
from hermes.tools import ExecutionEnvironment, ToolPolicy, register_all, registry


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
    sessions: tuple[dict, ...],
    current_session_id: str | None,
) -> dict[str, str]:
    """显示最近 CLI 会话，并返回本次列表对应的选择映射。"""
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


def _show_resumed_context(messages: tuple[dict, ...]) -> None:
    """逐条回放已保存的会话历史，不输出原始工具结果。"""
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


def _show_approval_prompt(result: dict, renderer: CLIStreamRenderer) -> None:
    """显示保留的 CLI 审批提示。"""
    renderer.ensure_line_break()
    request = result.get("approval_request")
    if not isinstance(request, dict):
        print("\nAssistant: approval request is invalid\n")
        return
    scopes = request.get("details", {}).get("allowed_grant_scopes", [])
    summary = str(request.get("summary", "需要批准的工具操作"))
    print(
        "\nAssistant: "
        f"{summary}\nApprove with /approve"
        f" (available scopes: {', '.join(scopes)}) or /deny\n"
    )


def _render_worker_result(
    worker_result: CLIWorkerResult,
    renderer: CLIStreamRenderer,
) -> None:
    """由 worker 调用，保持现有 CLI 的结果输出格式。"""
    if worker_result.error is not None:
        print(f"\nAssistant: {worker_result.error}\n")
        return

    if worker_result.kind == "list_sessions":
        _show_cli_sessions(
            worker_result.sessions,
            worker_result.current_session_id,
        )
        return
    if worker_result.kind == "resume":
        print(f"\nAssistant: resumed session {worker_result.session_id}\n")
        _show_resumed_context(worker_result.messages)
        return
    if worker_result.kind == "deny":
        print("\nAssistant: approval denied\n")
        return

    result = worker_result.conversation_result
    if not isinstance(result, dict):
        print("\nAssistant: worker returned an invalid result\n")
        return
    if result.get("status") == "awaiting_approval":
        _show_approval_prompt(result, renderer)
        return
    final_response = str(result.get("final_response", ""))
    if not renderer.was_final_response_streamed(final_response):
        print(f"\nAssistant: {final_response}\n")


def cli_loop():
    """默认模式：主线程读输入，单 worker 串行运行 CLI 工作。"""
    register_all()
    tool_policy = _cli_tool_policy()
    enabled_toolsets = sorted(registry.resolve(tool_policy).toolsets)
    cli_input = CLIInput()
    cli_renderer = CLIStreamRenderer()
    worker = CLIWorker(
        renderer=cli_renderer,
        on_result=lambda result: _render_worker_result(result, cli_renderer),
    )
    worker_started = False

    try:
        with patched_cli_stdout():
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
                worker.start()
                worker_started = True

                def consume_worker_results() -> None:
                    """把 worker 的完成状态交回 CLI 路由状态。"""
                    nonlocal pending_approval, session_choices, session_id
                    for worker_result in worker.drain_results():
                        if worker_result.kind == "list_sessions":
                            if worker_result.error is None:
                                session_choices = {
                                    str(index): str(session["session_id"])
                                    for index, session in enumerate(
                                        worker_result.sessions,
                                        start=1,
                                    )
                                }
                            continue
                        if worker_result.kind == "resume":
                            if worker_result.error is None:
                                session_id = worker_result.session_id
                            continue
                        if worker_result.session_id is not None:
                            session_id = worker_result.session_id
                        if worker_result.kind == "deny" and worker_result.error is None:
                            pending_approval = None
                            continue
                        result = worker_result.conversation_result
                        if not isinstance(result, dict):
                            continue
                        if result.get("status") == "awaiting_approval":
                            request = result.get("approval_request")
                            pending_approval = request if isinstance(request, dict) else None
                        else:
                            pending_approval = None

                while True:
                    consume_worker_results()
                    raw_user_input = cli_input.prompt()
                    consume_worker_results()
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
                    command, _, command_argument = user_input.partition(" ")
                    command = "" if literal_input else command.lower()
                    if command in {"/quit", "/exit"}:
                        break
                    if worker.is_busy():
                        print(
                            "\nAssistant: agent is running; later phases will support "
                            "queuing.\n"
                        )
                        continue
                    if pending_approval is not None:
                        if command == "/deny":
                            task = CLIWorkerTask(
                                kind="deny",
                                session_id=session_id,
                                approval_request=pending_approval,
                            )
                        elif command == "/approve":
                            task = CLIWorkerTask(
                                kind="approve",
                                session_id=session_id,
                                cached_prompt=cached_prompt,
                                tool_policy=tool_policy,
                                approval_request=pending_approval,
                                approval_scope=(
                                    command_argument.strip().lower() or "once"
                                ),
                            )
                        else:
                            print("\nAssistant: enter /approve [once|session] or /deny\n")
                            continue
                        if session_id is None or not worker.submit(task):
                            print(
                                "\nAssistant: agent is running; later phases will "
                                "support queuing.\n"
                            )
                        continue

                    if command == "/resume":
                        selection = command_argument.strip()
                        if not selection:
                            task = CLIWorkerTask(
                                kind="list_sessions",
                                current_session_id=session_id,
                            )
                        else:
                            task = CLIWorkerTask(
                                kind="resume",
                                session_id=session_choices.get(selection, selection),
                            )
                    elif command == "/new":
                        session_id = None
                        print("\nAssistant: new session will start with your next message\n")
                        continue
                    elif command == "/sessions":
                        task = CLIWorkerTask(
                            kind="list_sessions",
                            current_session_id=session_id,
                        )
                    elif command.startswith("/"):
                        print(f"\nAssistant: unknown command: {command}\n")
                        continue
                    else:
                        task = CLIWorkerTask(
                            kind="conversation",
                            session_id=session_id,
                            user_input=user_input,
                            cached_prompt=cached_prompt,
                            tool_policy=tool_policy,
                        )
                    if not worker.submit(task):
                        print(
                            "\nAssistant: agent is running; later phases will support "
                            "queuing.\n"
                        )
            finally:
                if worker_started:
                    worker.shutdown()
    finally:
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
