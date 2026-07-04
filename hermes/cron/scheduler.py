"""
JobScheduler: background thread that fires due jobs.

Why a thread instead of asyncio? Because the scheduler must work in both CLI
(sync) and Gateway (async) mode. A thread is the common denominator.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from hermes.cron.job import CronJob
from hermes.cron.store import JobStore


class JobScheduler:
    """Background thread that checks for due jobs every interval."""

    def __init__(
        self,
        store: JobStore,
        fire_callback: Callable[[CronJob], None],
        interval: int = 30,
    ):
        self._store = store
        self._fire = fire_callback
        self._interval = interval
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            for job in self._store.get_due():
                try:
                    self._fire(job)
                except Exception as e:
                    print(f"  [scheduler] job {job.job_id} failed: {e}")
                self._store.advance(job)
            time.sleep(self._interval)
