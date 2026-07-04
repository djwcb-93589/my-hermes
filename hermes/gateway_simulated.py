"""--simulate mode: GatewayRunner + SimulatedAdapter (demos batching + dedup)."""

from __future__ import annotations

import asyncio

from hermes.config import _config, DB_PATH, MODEL
from hermes.gateway.runner import GatewayRunner
from hermes.gateway.adapters.simulated import SimulatedAdapter


async def run_gateway_simulated():
    """Gateway mode with SimulatedAdapter (demos batching + dedup)."""
    print("=== s13: Platform Adapters (Simulated Gateway) ===")
    print(f"Model: {MODEL}")
    print("Replaying scripted messages to demo batching + dedup...\n")

    runner = GatewayRunner(config=_config, db_path=DB_PATH)
    sim = SimulatedAdapter()
    runner.add_adapter(sim)

    await runner.start()

    try:
        while sim._running:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()

    print("\n--- Simulation Summary ---")
    print(f"Replies sent: {len(sim._replies)}")
    for chat_id, content in sim._replies:
        print(f"  → {chat_id}: {content[:80]}...")
