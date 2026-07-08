"""Core conversation loop."""

from __future__ import annotations

import json
import sqlite3
import time

from hermes.config import (
    client,
    MODEL,
    MAX_ITERATIONS,
    MAX_RETRIES,
    MAX_CONTINUATIONS,
    COMPRESSION_THRESHOLD,
    CONTINUE_MESSAGE,
)
from hermes.db import add_message, get_session_messages
from hermes.errors import classify_error, jittered_backoff, switch_to_fallback
from hermes.tokens import estimate_tokens, compress
from hermes.tools import registry
# ponytail: 主循环的大部分能力(DB / 压缩 / fallback / continuation)暂留在
# conversation.py,AgentLoop 只抽公共的"assistant message 组装"helper。
# tool_call 处理由于副作用(print + DB 持久化)特殊,仍内联。
from hermes.agent_loop import build_assistant_msg_dict


ENABLED_TOOLSETS = ["terminal", "file", "memory", "skill", "delegate", "cron"]


def run_conversation(
    user_message: str,
    conn: sqlite3.Connection,
    session_id: str,
    cached_prompt: str,
    session_key: str | None = None,
) -> dict:
    """Run a conversation loop with all features enabled."""
    messages = get_session_messages(conn, session_id)
    user_msg = {"role": "user", "content": user_message}
    messages.append(user_msg)
    add_message(conn, session_id, user_msg)

    tools = registry.get_definitions(ENABLED_TOOLSETS)
    active_client = client
    active_model = MODEL
    retry_count = 0
    continuation_count = 0

    for iteration in range(MAX_ITERATIONS):
        if estimate_tokens(messages) >= COMPRESSION_THRESHOLD:
            messages = compress(messages)

        api_messages = (
            [{"role": "system", "content": cached_prompt}] + messages
        )

        try:
            response = active_client.chat.completions.create(
                model=active_model,
                messages=api_messages,
                tools=tools,
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            classified = classify_error(status, str(exc))
            print(f"  [error] {classified['reason']} (status={status})")

            if classified["should_compress"]:
                messages = compress(messages)
                continue

            if classified["should_fallback"]:
                fallback_client, fallback_model = switch_to_fallback()
                if fallback_client:
                    active_client = fallback_client
                    active_model = fallback_model
                    continue
                raise

            if classified["retryable"] and retry_count < MAX_RETRIES:
                retry_count += 1
                time.sleep(jittered_backoff(retry_count))
                continue

            raise

        retry_count = 0
        assistant_msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        # 与 delegate 子 agent 共用同一份 assistant → dict 组装逻辑
        msg_dict = build_assistant_msg_dict(assistant_msg)
        messages.append(msg_dict)
        add_message(conn, session_id, msg_dict)

        if finish_reason == "length" and continuation_count < MAX_CONTINUATIONS:
            continuation_count += 1
            cont_msg = {"role": "user", "content": CONTINUE_MESSAGE}
            messages.append(cont_msg)
            add_message(conn, session_id, cont_msg)
            continue

        if not assistant_msg.tool_calls:
            return {
                "final_response": assistant_msg.content,
                "messages": messages,
            }

        continuation_count = 0
        for tool_call in assistant_msg.tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)
            print(
                f"  [tool] {tool_name}: "
                f"{json.dumps(tool_args, ensure_ascii=False)[:120]}"
            )
            output = registry.dispatch(
                tool_name, tool_args,
                session_key=session_key or session_id,
            )
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            }
            messages.append(tool_msg)
            add_message(conn, session_id, tool_msg)

    return {
        "final_response": "(max iterations reached)",
        "messages": messages,
    }
