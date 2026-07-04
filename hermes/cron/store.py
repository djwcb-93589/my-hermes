"""
JobStore: CRUD + persistence for scheduled tasks.

Persists to HERMES_HOME/jobs.json. Uses a tmp-file-then-rename pattern so a
half-written file can never corrupt the store. A module-level singleton is
kept for convenience (get_job_store); tests can swap it via set_job_store.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from hermes.config import HERMES_HOME
from hermes.cron.job import CronJob
from hermes.cron.parser import parse_schedule


class JobStore:
    """
    CRUD + persistence for scheduled tasks.

    Uses jobs.json (not SQLite) because:
    - Few jobs (typically <20 per user)
    - Human-readable for debugging
    - No need for FTS or concurrent writes
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else HERMES_HOME / "jobs.json"
        self._jobs: dict[str, CronJob] = {}
        self._lock = threading.Lock()
        self._load()

    def add(self, job: CronJob):
        with self._lock:
            self._jobs[job.job_id] = job
            self._save()

    def remove(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                self._save()
                return True
            return False

    def list_all(self) -> list[CronJob]:
        with self._lock:
            return list(self._jobs.values())

    def get_due(self) -> list[CronJob]:
        """Return all jobs whose next_fire has passed."""
        now = time.time()
        with self._lock:
            return [j for j in self._jobs.values() if now >= j.next_fire]

    def advance(self, job: CronJob):
        """Update next_fire for recurring jobs, or delete one-shot jobs."""
        with self._lock:
            if job.one_shot:
                self._jobs.pop(job.job_id, None)
            else:
                next_ts, _ = parse_schedule(job.schedule)
                job.next_fire = next_ts
            self._save()

    def _save(self):
        data = [
            {
                "job_id": j.job_id,
                "schedule": j.schedule,
                "prompt": j.prompt,
                "session_key": j.session_key,
                "created_at": j.created_at,
                "next_fire": j.next_fire,
                "one_shot": j.one_shot,
            }
            for j in self._jobs.values()
        ]
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        # os.replace is atomic on both POSIX and Windows; Path.rename is not.
        os.replace(tmp_path, self._path)

    def _load(self):
        if not self._path.exists():
            return
        try:
            for item in json.loads(self._path.read_text()):
                job = CronJob(**item)
                self._jobs[job.job_id] = job
        except (json.JSONDecodeError, TypeError):
            pass


_job_store: Optional[JobStore] = None


def get_job_store() -> JobStore:
    """Return the module-global JobStore, creating it on first use."""
    global _job_store
    if _job_store is None:
        _job_store = JobStore()
    return _job_store


def set_job_store(store: Optional[JobStore]) -> None:
    """Test helper: override the global job store."""
    global _job_store
    _job_store = store
