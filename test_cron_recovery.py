from __future__ import annotations

from datetime import datetime

import pytest

from hermes.cron.job import CronJob
from hermes.cron.parser import parse_schedule
from hermes.db import (
    acquire_gateway_runtime_lease,
    create_cron_job,
    create_cron_run,
    get_cron_run,
    init_db,
    recover_interrupted_cron_runs,
    release_gateway_runtime_lease,
)


def _create_job(conn, job_id: str) -> CronJob:
    job = CronJob(
        job_id=job_id,
        schedule="every 1h",
        prompt="summarize the report",
        session_key="test-session",
        created_at=datetime.now().isoformat(),
        next_fire=1.0,
        one_shot=False,
        toolsets=["memory"],
        delivery_config={"policy": "text"},
        approval_status="granted",
    )
    create_cron_job(conn, job.to_record())
    return job


def test_recovery_finishes_run_owned_by_expired_lease(tmp_path):
    conn = init_db(str(tmp_path / "cron.db"))
    try:
        old = acquire_gateway_runtime_lease(
            conn, "gateway-runtime", "old-instance", 30.0
        )
        assert old is not None
        job = _create_job(conn, "job-recovery")
        create_cron_run(conn, {
            "run_id": "run-recovery",
            "job_id": job.job_id,
            "scheduled_for": 1.0,
            "claimed_at": 1.0,
            "execution_instance_id": "old-execution",
            "claim_lease_name": "gateway-runtime",
            "claim_instance_id": "old-instance",
            "claim_epoch": old["lease_epoch"],
        })
        assert release_gateway_runtime_lease(
            conn, "gateway-runtime", "old-instance", old["lease_epoch"]
        )
        current = acquire_gateway_runtime_lease(
            conn, "gateway-runtime", "new-instance", 30.0
        )
        assert current is not None

        assert recover_interrupted_cron_runs(
            conn,
            lease_name="gateway-runtime",
            instance_id="new-instance",
            lease_epoch=current["lease_epoch"],
        ) == 1
        recovered = get_cron_run(conn, "run-recovery")
        assert recovered is not None
        assert recovered["status"] == "failed"
        assert recovered["error_type"] == "execution_interrupted"
        assert recovered["delivery_status"] == "preparation_pending"
        assert recover_interrupted_cron_runs(
            conn,
            lease_name="gateway-runtime",
            instance_id="new-instance",
            lease_epoch=current["lease_epoch"],
        ) == 0
    finally:
        conn.close()


@pytest.mark.parametrize("schedule", ["every 0s", "every -1m", "every nans"])
def test_recurring_duration_must_be_positive_and_finite(schedule):
    with pytest.raises(ValueError, match="positive finite"):
        parse_schedule(schedule, now=0.0)
