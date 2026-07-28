"""Hermes 入口。

默认模式：交互式 CLI REPL；其他模式经 argv 分发。
"""

from __future__ import annotations

import asyncio
import os
import sys

from hermes.cli_state_machine import CLIController, CLIEventQueue, CLIWorker
from hermes.cli_ui import CLIInput, CLIUI, patched_cli_stdout
from hermes.config import BASE_URL, BROWSER_CONFIG, HERMES_HOME, MODEL, _config
from hermes.hooks import SyncHookRegistry
from hermes.plugins import SyncPluginRuntime
from hermes.prompt import build_system_prompt
from hermes.session_resources import cleanup_all_session_resources
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


def cli_loop() -> None:
    """启动默认 CLI 的事件驱动输入、路由和单 worker 执行流程。"""
    register_all()
    hook_registry = SyncHookRegistry()
    plugin_runtime = SyncPluginRuntime(
        hook_registry,
        plugins_config=_config["plugins"],
    )
    plugin_runtime.load()
    tool_policy = _cli_tool_policy()
    enabled_toolsets = sorted(registry.resolve(tool_policy).toolsets)
    cached_prompt = build_system_prompt(
        os.getcwd(),
        enabled_toolsets=enabled_toolsets,
    )
    events = CLIEventQueue()
    cli_ui = CLIUI(
        cli_input=CLIInput(),
        post_user_input=events.post_user_input,
        post_shutdown=events.post_shutdown,
        post_cancel_request=events.post_cancel_request,
    )
    worker = CLIWorker(
        stream_sink=events.post_stream_event,
        publish_result=events.post_worker_result,
        hook_registry=hook_registry,
    )
    controller = CLIController(
        events=events,
        worker=worker,
        ui=cli_ui,
        cached_prompt=cached_prompt,
        tool_policy=tool_policy,
    )
    worker_started = False

    try:
        with patched_cli_stdout():
            cli_ui.show_startup(
                profile=HERMES_HOME,
                model=MODEL,
                base_url=BASE_URL,
                prompt_length=len(cached_prompt),
            )
            plugin_summary = plugin_runtime.summary
            print(
                "Plugins: "
                f"loaded={plugin_summary.loaded} "
                f"skipped={plugin_summary.skipped} "
                f"failed={plugin_summary.failed}"
            )
            worker.start()
            worker_started = True
            cli_ui.start_input()
            controller.run()
    finally:
        cli_ui.stop_input()
        if worker_started:
            worker.shutdown()
        plugin_runtime.close()
        cleanup_all_session_resources()


def main() -> None:
    if "--gateway" in sys.argv or "--gateway-unified" in sys.argv:
        # 统一 Gateway 入口（读取 config.yaml gateway.platforms）
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
