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
from hermes.db import init_db, create_session
from hermes.backends import cleanup_all_backends
from hermes.prompt import build_system_prompt
from hermes.tools import register_all


def cli_loop():
    """默认模式：原始 input → run_conversation REPL。"""
    register_all()

    print("=== s15: Scheduled Tasks (CLI mode) ===")
    print(f"Profile (HERMES_HOME): {HERMES_HOME}")
    print(f"Model: {MODEL} | Base URL: {BASE_URL}")

    conn = init_db(DB_PATH)
    session_id = create_session(conn)
    cached_prompt = build_system_prompt(os.getcwd())
    print(f"System prompt: {len(cached_prompt)} chars")

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
        conn.close()
        cleanup_all_backends()


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
