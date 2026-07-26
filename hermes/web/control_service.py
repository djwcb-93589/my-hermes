"""Cron 控制意图的 Web 适配层。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Iterator

from hermes.cron.control import (
    CronControlConflict,
    CronControlDataUnavailable,
    CronControlNotFound,
    pause_job,
    request_manual_run,
    resume_job,
)
from hermes.persistence.database import DBError
from hermes.persistence.write_existing import existing_write_connection
from hermes.web.schemas import CronControlResponse, CronRunRequestResponse
from hermes.web.security import (
    ControlBadRequest,
    ControlConflict,
    ControlNotFound,
    ControlUnavailable,
)


_RUN_ID_NAMESPACE = uuid.UUID("e31c4e1a-ea0d-5c6d-b44a-c7260790bc1a")


class CronControlService:
    """通过一次性写连接提交 Cron 控制意图。"""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path

    def pause_cron_job(self, job_id: str) -> CronControlResponse:
        try:
            with self._connection() as conn:
                job = pause_job(conn, job_id)
        except CronControlNotFound as exc:
            raise ControlNotFound() from exc
        except CronControlDataUnavailable as exc:
            raise ControlUnavailable() from exc
        return CronControlResponse(
            job_id=self._job_id(job),
            action="pause",
            status="paused",
        )

    def resume_cron_job(self, job_id: str) -> CronControlResponse:
        try:
            with self._connection() as conn:
                job = resume_job(conn, job_id)
        except CronControlNotFound as exc:
            raise ControlNotFound() from exc
        except CronControlDataUnavailable as exc:
            raise ControlUnavailable() from exc
        return CronControlResponse(
            job_id=self._job_id(job),
            action="resume",
            status="scheduled",
        )

    def request_cron_run(
        self,
        job_id: str,
        idempotency_key: str | None,
    ) -> CronRunRequestResponse:
        key = self._idempotency_key(idempotency_key)
        identity = json.dumps(
            [job_id, key],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        run_id = str(uuid.uuid5(_RUN_ID_NAMESPACE, identity))
        try:
            with self._connection() as conn:
                run = request_manual_run(conn, job_id, run_id)
        except CronControlNotFound as exc:
            raise ControlNotFound() from exc
        except CronControlConflict as exc:
            raise ControlConflict() from exc
        except CronControlDataUnavailable as exc:
            raise ControlUnavailable() from exc
        if str(run.get("job_id")) != job_id:
            raise ControlConflict()
        return CronRunRequestResponse(
            job_id=job_id,
            action="run",
            status="queued",
            run_id=run_id,
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self._db_path:
            raise ControlUnavailable()
        try:
            with existing_write_connection(self._db_path) as conn:
                yield conn
        except (sqlite3.Error, DBError, OSError) as exc:
            raise ControlUnavailable() from exc

    @staticmethod
    def _idempotency_key(value: str | None) -> str:
        if not isinstance(value, str):
            raise ControlBadRequest()
        normalized = value.strip()
        if len(normalized) < 8 or len(normalized) > 128:
            raise ControlBadRequest()
        return normalized

    @staticmethod
    def _job_id(job: dict) -> str:
        value = job.get("job_id")
        if not isinstance(value, str) or not value:
            raise ControlUnavailable()
        return value
