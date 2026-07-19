"""cron tool: agent-facing create/list/delete for scheduled tasks."""

from __future__ import annotations

import uuid
from datetime import datetime

from hermes.cron.job import CronJob
from hermes.cron.parser import parse_schedule
from hermes.cron.store import get_job_store


def handle_cron_tool(args, **kwargs):
    """Agent-facing tool for creating, listing, and deleting scheduled tasks."""
    store = get_job_store()
    action = args.get("action", "list")

    if action == "create":
        schedule_expr = args.get("schedule", "")
        prompt = args.get("prompt", "")
        if not schedule_expr or not prompt:
            return "Error: 'schedule' and 'prompt' are required."
        try:
            next_fire, one_shot = parse_schedule(schedule_expr)
        except ValueError as e:
            return f"Error parsing schedule: {e}"

        session_key = kwargs.get("session_key", "cli")
        job = CronJob(
            job_id=uuid.uuid4().hex[:8],
            schedule=schedule_expr,
            prompt=prompt,
            session_key=session_key,
            created_at=datetime.now().isoformat(),
            next_fire=next_fire,
            one_shot=one_shot,
            created_source=str(kwargs.get("source", "cli")),
            creator_id=str(kwargs.get("creator_id", session_key)),
        )
        store.add(job)
        fire_time = datetime.fromtimestamp(next_fire).strftime("%Y-%m-%d %H:%M")
        kind = "one-shot" if one_shot else "recurring"
        return f"Job {job.job_id} created ({kind}). Next fire: {fire_time}"

    elif action == "list":
        jobs = store.list_all()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            fire_time = (
                datetime.fromtimestamp(j.next_fire).strftime("%m-%d %H:%M")
                if j.next_fire is not None
                else "-"
            )
            kind = "once" if j.one_shot else "recurring"
            lines.append(
                f"  {j.job_id}  {j.schedule:15s}  {kind:9s}  "
                f"next: {fire_time}  {j.prompt[:40]}"
            )
        return "Jobs:\n" + "\n".join(lines)

    elif action == "delete":
        job_id = args.get("job_id", "")
        if not job_id:
            return "Error: 'job_id' is required."
        if store.remove(job_id):
            return f"Job {job_id} deleted."
        return f"Job {job_id} not found."

    return f"Unknown action: {action}"


def register(registry):
    registry.register(
        name="cron",
        toolset="cron",
        schema={
            "name": "cron",
            "description": (
                "Create, list, or delete scheduled tasks. "
                "Schedule formats: '30m' (one-shot delay), "
                "'every 2h' (recurring interval), '0 9 * * 1-5' (cron expression)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "delete"],
                        "description": "What to do.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Schedule expression. Examples: '30m', 'every 2h', "
                            "'0 9 * * 1-5'. Required for action=create."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "What the agent should do when the job fires. "
                            "Required for action=create."
                        ),
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Job ID to delete. Required for action=delete.",
                    },
                },
                "required": ["action"],
            },
        },
        handler=handle_cron_tool,
        execution_environments=("cli",),
        unattended_allowed=False,
        approval_mode="none",
        risk_level="medium",
        default_enabled_environments=("cli",),
    )
