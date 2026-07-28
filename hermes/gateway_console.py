"""--gateway mode: GatewayRunner + ConsoleAdapter。"""

from __future__ import annotations

import asyncio
from hermes.config import _config, DB_PATH, MODEL
from hermes.gateway.runner import GatewayRunner
from hermes.gateway.adapters.console import ConsoleAdapter
from hermes.hooks import AsyncHookRegistry
from hermes.plugins import AsyncPluginRuntime


async def run_gateway_console():
    """Gateway mode with ConsoleAdapter and lease-bound Cron scheduling."""
    print("=== s15: Scheduled Tasks (Console Gateway) ===")
    print(f"Model: {MODEL}")
    print("All messages flow through GatewayRunner → adapter → core loop.\n")

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
    )
    runner.add_adapter(ConsoleAdapter())

    try:
        await runner.start()
        while runner.adapters.get("console") and runner.adapters["console"]._running:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()
        plugin_runtime.close()
