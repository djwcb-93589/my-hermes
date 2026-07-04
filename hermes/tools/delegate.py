"""
Subagent delegation.

handle_delegate spawns a child agent with restricted tools (no recursion,
no memory writes, no skill edits) and an isolated message list, then returns
the child's final response.
"""

from __future__ import annotations

import json
from datetime import datetime
import os

from hermes.config import client, MODEL, MAX_CHILD_ITERATIONS
from hermes.tools import registry


DELEGATE_BLOCKED_TOOLS = {"delegate_task", "memory", "skill_manage"}


def build_child_agent(
    goal: str,
    context: str,
    toolsets: list[str],
) -> dict:
    """Build a child agent environment: isolated messages, restricted tools."""
    child_tools = [
        tool_def
        for tool_def in registry.get_definitions(toolsets)
        if tool_def["function"]["name"] not in DELEGATE_BLOCKED_TOOLS
    ]

    child_prompt = (
        "You are a focused sub-agent. "
        "Complete the task and report results.\n"
        f"# Task\n{goal}\n\n"
    )
    if context:
        child_prompt += f"# Context\n{context}\n\n"

    child_prompt += (
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Working directory: {os.getcwd()}"
    )

    return {
        "system_prompt": child_prompt,
        "messages": [{"role": "user", "content": goal}],
        "tools": child_tools,
    }


def run_child_conversation(child_env: dict) -> str:
    """Run the child agent's conversation loop and return the final response."""
    messages = child_env["messages"]
    tools = child_env["tools"]
    system_prompt = child_env["system_prompt"]

    for iteration in range(MAX_CHILD_ITERATIONS):
        api_messages = (
            [{"role": "system", "content": system_prompt}] + messages
        )

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                tools=tools if tools else None,
            )
        except Exception as exc:
            return f"(child error: {exc})"

        assistant_msg = response.choices[0].message

        msg_dict: dict = {
            "role": "assistant",
            "content": assistant_msg.content or "",
        }
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_msg.tool_calls
            ]
        messages.append(msg_dict)

        if not assistant_msg.tool_calls:
            return assistant_msg.content or "(empty)"

        for tool_call in assistant_msg.tool_calls:
            tool_name = tool_call.function.name

            if tool_name in DELEGATE_BLOCKED_TOOLS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": (
                        f"(error: '{tool_name}' blocked for sub-agents)"
                    ),
                })
            else:
                tool_args = json.loads(tool_call.function.arguments)
                print(
                    f"    [child-tool] {tool_name}: "
                    f"{json.dumps(tool_args, ensure_ascii=False)[:100]}"
                )
                output = registry.dispatch(tool_name, tool_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                })

    return "(child: max iterations)"


def handle_delegate(args, **kwargs):
    """Handle the delegate_task tool: spawn a child agent."""
    goal = args.get("goal", "")
    if not goal:
        return "(error: goal required)"

    print(f"  [delegate] child: {goal[:80]}")

    child_env = build_child_agent(
        goal,
        args.get("context", ""),
        args.get("toolsets", ["terminal", "file"]),
    )
    result = run_child_conversation(child_env)

    print(f"  [delegate] done ({len(result)} chars)")
    return result


def register(registry):
    registry.register(
        name="delegate_task",
        toolset="delegate",
        schema={
            "name": "delegate_task",
            "description": (
                "Delegate a task to a sub-agent with isolated context. "
                "Returns final result text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Task description",
                    },
                    "context": {
                        "type": "string",
                        "description": "Relevant context",
                    },
                    "toolsets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Available tool sets",
                    },
                },
                "required": ["goal"],
            },
        },
        handler=handle_delegate,
    )
