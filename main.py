"""
Hermes 入口。

默认模式：交互式 CLI REPL —— input → run_conversation → output。
其它模式经 argv 分发：--gateway、--simulate、--test。
"""

from __future__ import annotations

import asyncio
import os
import sys

from hermes.config import DB_PATH, MODEL, BASE_URL, HERMES_HOME
from hermes.conversation import run_conversation
from hermes.cron import get_job_store, JobScheduler
from hermes.cron.job import CronJob
from hermes.db import init_db, create_session
from hermes.backends import cleanup_all_backends
from hermes.prompt import build_system_prompt
from hermes.tools import register_all


def cli_loop():
    """默认模式：原始 input → run_conversation REPL，带 scheduler。"""
    register_all()

    print("=== s15: Scheduled Tasks (CLI mode) ===")
    print(f"Profile (HERMES_HOME): {HERMES_HOME}")
    print(f"Model: {MODEL} | Base URL: {BASE_URL}")

    conn = init_db(DB_PATH)
    session_id = create_session(conn)
    cached_prompt = build_system_prompt(os.getcwd())
    print(f"System prompt: {len(cached_prompt)} chars")

    store = get_job_store()

    def fire_cli(job: CronJob):
        """CLI 模式的 fire callback：直接调 run_conversation。"""
        print(f"\n  [cron] firing job {job.job_id}: {job.prompt[:60]}")
        result = run_conversation(job.prompt, conn, session_id, cached_prompt)
        print(f"\n  [cron] result: {result['final_response'][:200]}\n")

    scheduler = JobScheduler(store, fire_callback=fire_cli, interval=30)
    scheduler.start()
    print(f"Scheduler started ({len(store.list_all())} jobs loaded)")
    print("Type 'quit' to exit.\n")

    try:
        while True:
            user_input = input("You: ").strip()
            if not user_input or user_input.lower() in ("quit", "exit"):
                break
            result = run_conversation(
                user_input, conn, session_id, cached_prompt,
                session_key=session_id,
            )
            print(f"\nAssistant: {result['final_response']}\n")
    finally:
        scheduler.stop()
        conn.close()
        cleanup_all_backends()


def main():
    if "--gateway" in sys.argv:
        from hermes.gateway_console import run_gateway_console
        asyncio.run(run_gateway_console())
    elif "--simulate" in sys.argv:
        from hermes.gateway_simulated import run_gateway_simulated
        asyncio.run(run_gateway_simulated())
    elif "--test" in sys.argv:
        from hermes.tests import run_unit_tests
        run_unit_tests()
    else:
        cli_loop()


if __name__ == "__main__":
    main()
