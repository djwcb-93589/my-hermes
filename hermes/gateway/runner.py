"""
GatewayRunner: starts adapters, routes inbound messages, runs the agent.

_handle_message is the convergence point: every adapter's _on_message points
here. Per-session locking ensures only one message per session is processed
at a time; later messages interrupt and queue.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

from hermes.conversation import run_conversation
from hermes.db import init_db, get_session_messages
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.types import MessageEvent
from hermes.prompt import build_system_prompt


class GatewayRunner:
    """
    Starts adapters, routes inbound messages to the right session,
    calls the core loop, and sends replies back.
    """

    def __init__(self, config: dict, db_path: str):
        self.config = config
        self.db_path = db_path
        self.adapters: dict[str, BasePlatformAdapter] = {}
        self.agent_name = config.get("gateway", {}).get("agent_name", "main")

        # session key → agent 运行状态
        self._active_sessions: dict[str, asyncio.Event] = {}
        self._pending_messages: dict[str, MessageEvent] = {}
        # session key → cached system prompt
        self._prompts: dict[str, str] = {}

    def add_adapter(self, adapter: BasePlatformAdapter):
        """Register an adapter (call before start)."""
        adapter._on_message = self._handle_message
        self.adapters[adapter.platform_name] = adapter

    async def start(self):
        """Connect all registered adapters."""
        for name, adapter in self.adapters.items():
            ok = await adapter.connect()
            if ok:
                print(f"  [gateway] {name} connected")
            else:
                print(f"  [gateway] {name} FAILED to connect")

    async def stop(self):
        """Disconnect all adapters."""
        for adapter in self.adapters.values():
            await adapter.disconnect()

    # ----- the core routing function -----

    async def _handle_message(self, event: MessageEvent):
        """
        All platforms converge here. This function:
        1. Builds a session key
        2. If a session is already active → interrupt it, queue the new message
        3. Otherwise → process the message in background
        """
        from hermes.gateway.types import build_session_key
        session_key = build_session_key(event.source, self.agent_name)

        if session_key in self._active_sessions:
            self._pending_messages[session_key] = event
            self._active_sessions[session_key].set()  # interrupt signal
            print(f"  [gateway] {session_key}: queued (agent busy)")
            return

        self._active_sessions[session_key] = asyncio.Event()
        asyncio.create_task(self._process_in_background(event, session_key))

    async def _process_in_background(
        self, event: MessageEvent, session_key: str
    ):
        """Process one message, then check for pending follow-ups."""
        try:
            response = await self._run_agent(event, session_key)

            adapter = self.adapters.get(event.source.platform)
            if adapter and response:
                await adapter.send(event.source.chat_id, response)

        except Exception as exc:
            print(f"  [gateway] error: {exc}")

        if session_key in self._pending_messages:
            next_event = self._pending_messages.pop(session_key)
            self._active_sessions[session_key] = asyncio.Event()
            await self._process_in_background(next_event, session_key)
        else:
            del self._active_sessions[session_key]

    async def _run_agent(
        self, event: MessageEvent, session_key: str
    ) -> str | None:
        """
        Run the core conversation loop for one message.

        复用 run_conversation()。Gateway 不修改核心循环，只是换了一个"消息从哪来"。
        """
        conn = init_db(self.db_path)
        try:
            existing = get_session_messages(conn, session_key)
            if not existing:
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, created_at) VALUES (?, ?)",
                    (session_key, datetime.now().isoformat()),
                )
                conn.commit()

            if session_key not in self._prompts:
                self._prompts[session_key] = build_system_prompt(os.getcwd())

            result = run_conversation(
                event.text, conn, session_key, self._prompts[session_key],
                session_key=session_key,
            )
            return result.get("final_response")
        finally:
            conn.close()
