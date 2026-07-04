"""CronJob dataclass."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CronJob:
    """A single scheduled task."""
    job_id: str
    schedule: str         # original expression
    prompt: str           # message to send when fired
    session_key: str      # which session to fire into
    created_at: str
    next_fire: float      # unix timestamp
    one_shot: bool        # True = delete after firing
