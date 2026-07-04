"""Token estimation and context compression."""

from __future__ import annotations

from hermes.config import (
    client,
    MODEL,
    PROTECT_FIRST,
    KEEP_RECENT_TOOL_RESULTS,
    TAIL_TOKEN_BUDGET,
)


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: character count / 4."""
    return sum(len(str(msg.get("content", ""))) for msg in messages) // 4


def compress(messages: list[dict]) -> list[dict]:
    """Perform one round of context compression."""
    tool_indices = [
        index for index, msg in enumerate(messages)
        if msg.get("role") == "tool"
    ]
    for index in tool_indices[:-KEEP_RECENT_TOOL_RESULTS]:
        messages[index] = {
            **messages[index],
            "content": "[Old tool output cleared]",
        }

    head_end = PROTECT_FIRST
    tail_start = len(messages)
    tail_tokens = 0

    for index in range(len(messages) - 1, head_end - 1, -1):
        msg_tokens = len(str(messages[index].get("content", ""))) // 4
        if tail_tokens + msg_tokens > TAIL_TOKEN_BUDGET:
            break
        tail_tokens += msg_tokens
        tail_start = index

    if tail_start <= head_end:
        return messages

    middle = messages[head_end:tail_start]
    summary_parts = [
        f"[{msg['role']}] {str(msg.get('content', ''))[:500]}"
        for msg in middle
    ]
    prompt = "Summarize concisely:\n" + "\n".join(summary_parts)

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
        )
        summary = response.choices[0].message.content or "(summary failed)"
    except Exception as exc:
        summary = f"(summary error: {exc})"

    print(f"  [compress] {len(middle)} messages -> summary")

    return (
        messages[:head_end]
        + [{"role": "user", "content": f"[CONTEXT COMPACTION]\n{summary}"}]
        + messages[tail_start:]
    )
