"""--gateway mode: GatewayRunner + ConsoleAdapter。"""

from __future__ import annotations

import asyncio
from hermes.config import _config, DB_PATH, MODEL
from hermes.gateway.runner import GatewayRunner
from hermes.gateway.adapters.console import ConsoleAdapter
from hermes.gateway.shutdown_signals import (
    install_gateway_shutdown_signals,
    start_gateway_until_shutdown,
    wait_for_gateway_shutdown,
)
from hermes.hooks import AsyncHookRegistry
from hermes.persistence.runtime import SQLiteRuntimeStatusPublisher
from hermes.plugins import AsyncPluginRuntime


async def run_gateway_console():
    """Gateway mode with ConsoleAdapter and lease-bound Cron scheduling."""
    print("=== s15: Scheduled Tasks (Console Gateway) ===")
    print(f"Model: {MODEL}")
    print("All messages flow through GatewayRunner → adapter → core loop.\n")

    shutdown_event = asyncio.Event()
    shutdown_signals = install_gateway_shutdown_signals(shutdown_event)
    plugin_runtime = None
    runner = None

    try:
        hook_registry = AsyncHookRegistry()
        plugin_runtime = AsyncPluginRuntime(
            hook_registry,
            plugins_config=_config["plugins"],
        )
        plugin_runtime.load()
        plugin_summary = plugin_runtime.summary
        print(
            "Plugins: "
            f"loaded={plugin_summary.loaded} "
            f"skipped={plugin_summary.skipped} "
            f"failed={plugin_summary.failed}"
        )
        runner = GatewayRunner(
            config=_config,
            db_path=DB_PATH,
            hook_registry=hook_registry,
            runtime_status_publisher=SQLiteRuntimeStatusPublisher(DB_PATH),
        )
        runner.add_adapter(ConsoleAdapter())
        await start_gateway_until_shutdown(runner.start, shutdown_event)
        while (
            not shutdown_event.is_set()
            and runner.adapters.get("console")
            and runner.adapters["console"]._running
        ):
            if await wait_for_gateway_shutdown(shutdown_event, 0.5):
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if runner is not None:
                await runner.stop()
        finally:
            try:
                if plugin_runtime is not None:
                    plugin_runtime.close()
            finally:
                shutdown_signals.close()
