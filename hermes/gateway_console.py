"""--gateway mode: GatewayRunner + ConsoleAdapter。"""

from __future__ import annotations

import asyncio
from hermes.config import _config, DB_PATH, MODEL
from hermes.gateway.runner import GatewayRunner
from hermes.gateway.adapters.console import ConsoleAdapter


async def run_gateway_console():
    """Gateway mode with ConsoleAdapter and lease-bound Cron scheduling."""
    print("=== s15: Scheduled Tasks (Console Gateway) ===")
    print(f"Model: {MODEL}")
    print("All messages flow through GatewayRunner → adapter → core loop.\n")

    runner = GatewayRunner(config=_config, db_path=DB_PATH)
    runner.add_adapter(ConsoleAdapter())

    await runner.start()

    try:
        while runner.adapters.get("console") and runner.adapters["console"]._running:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()
