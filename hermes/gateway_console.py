"""--gateway mode: GatewayRunner + ConsoleAdapter + scheduler."""

from __future__ import annotations

import asyncio
import time

from hermes.config import _config, DB_PATH, MODEL
from hermes.cron import get_job_store, JobScheduler
from hermes.cron.job import CronJob
from hermes.gateway.runner import GatewayRunner
from hermes.gateway.adapters.console import ConsoleAdapter
from hermes.gateway.types import MessageEvent, SessionSource


async def run_gateway_console():
    """Gateway mode with ConsoleAdapter + scheduler."""
    print("=== s15: Scheduled Tasks (Console Gateway) ===")
    print(f"Model: {MODEL}")
    print("All messages flow through GatewayRunner → adapter → core loop.\n")

    runner = GatewayRunner(config=_config, db_path=DB_PATH)
    runner.add_adapter(ConsoleAdapter())

    store = get_job_store()
    loop = asyncio.get_event_loop()

    def fire_gateway(job: CronJob):
        """Fire callback for Gateway mode: inject MessageEvent."""
        print(f"\n  [cron] firing job {job.job_id}: {job.prompt[:60]}")
        event = MessageEvent(
            message_id=f"cron-{job.job_id}-{int(time.time())}",
            text=job.prompt,
            source=SessionSource(
                platform="cron",
                chat_id=job.session_key,
                chat_type="dm",
                user_id="scheduler",
                user_name="Scheduler",
            ),
        )
        asyncio.run_coroutine_threadsafe(
            runner._handle_message(event), loop
        )

    scheduler = JobScheduler(store, fire_callback=fire_gateway, interval=30)
    scheduler.start()
    print(f"Scheduler started ({len(store.list_all())} jobs loaded)")

    await runner.start()

    try:
        while runner.adapters.get("console") and runner.adapters["console"]._running:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        await runner.stop()
