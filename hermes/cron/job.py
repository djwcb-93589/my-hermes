"""Cron 任务定义与运行记录的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def _schedule_type(schedule: str, one_shot: bool) -> str:
    """从保留的旧表达式推导正式的调度类型。"""
    if one_shot:
        return "one_shot"
    if schedule.strip().startswith("every "):
        return "interval"
    return "cron"


@dataclass
class CronJob:
    """任务定义及其当前调度摘要；运行历史由 ``CronRun`` 独立保存。"""

    job_id: str
    schedule: str
    prompt: str
    session_key: str
    created_at: str
    next_fire: float | None
    one_shot: bool
    name: str = ""
    version: int = 1
    created_source: str = "cli"
    creator_id: str = ""
    schedule_type: str = ""
    timezone: str = "UTC"
    toolsets: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    workdir: str | None = None
    execution_timeout_seconds: float = 300.0
    max_agent_iterations: int = 20
    overlap_policy: str = "skip"
    misfire_policy: str = "run_once"
    delivery_config: dict = field(default_factory=dict)
    retry_policy: dict = field(default_factory=dict)
    artifact_policy: dict = field(default_factory=dict)
    capability_spec: dict = field(default_factory=dict)
    capability_grant: dict | None = None
    approval_status: str = "not_required"
    paused: bool = False
    last_run_at: float | None = None
    consecutive_failures: int = 0
    deleted_at: float | None = None

    def __post_init__(self) -> None:
        """补齐旧调用点没有提供的新字段。"""
        if not self.name:
            self.name = self.job_id
        if not self.creator_id:
            self.creator_id = self.session_key
        if not self.schedule_type:
            self.schedule_type = _schedule_type(self.schedule, self.one_shot)

    @classmethod
    def from_record(cls, record: dict) -> "CronJob":
        """把数据库定义行还原为保留兼容字段的任务对象。"""
        created_at = datetime.fromtimestamp(
            float(record["created_at"])
        ).isoformat()
        return cls(
            job_id=str(record["job_id"]),
            schedule=str(record["schedule_expr"]),
            prompt=str(record["prompt"]),
            session_key=str(record["session_key"]),
            created_at=created_at,
            next_fire=record["next_run_at"],
            one_shot=str(record["schedule_type"]) == "one_shot",
            name=str(record["name"]),
            version=int(record["version"]),
            created_source=str(record["created_source"]),
            creator_id=str(record["creator_id"]),
            schedule_type=str(record["schedule_type"]),
            timezone=str(record["timezone"]),
            toolsets=list(record["toolsets"]),
            skills=list(record["skills"]),
            workdir=record["workdir"],
            execution_timeout_seconds=float(record["execution_timeout_seconds"]),
            max_agent_iterations=int(record["max_agent_iterations"]),
            overlap_policy=str(record["overlap_policy"]),
            misfire_policy=str(record["misfire_policy"]),
            delivery_config=dict(record["delivery_config"]),
            retry_policy=dict(record.get("retry_policy") or {}),
            artifact_policy=dict(record.get("artifact_policy") or {}),
            capability_spec=dict(record.get("capability_spec") or {}),
            capability_grant=record["capability_grant"],
            approval_status=str(record["approval_status"]),
            paused=bool(record["paused"]),
            last_run_at=record["last_run_at"],
            consecutive_failures=int(record["consecutive_failures"]),
            deleted_at=record.get("deleted_at"),
        )

    def to_record(self) -> dict:
        """转换为数据库层的完整任务定义载荷。"""
        try:
            created_at = datetime.fromisoformat(
                self.created_at.replace("Z", "+00:00")
            ).timestamp()
        except (AttributeError, ValueError) as exc:
            raise ValueError("Cron created_at must be an ISO timestamp") from exc
        return {
            "job_id": self.job_id,
            "name": self.name,
            "version": self.version,
            "prompt": self.prompt,
            "created_source": self.created_source,
            "creator_id": self.creator_id,
            "session_key": self.session_key,
            "schedule_type": self.schedule_type,
            "schedule_expr": self.schedule,
            "timezone": self.timezone,
            "toolsets": list(self.toolsets),
            "skills": list(self.skills),
            "workdir": self.workdir,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "max_agent_iterations": self.max_agent_iterations,
            "overlap_policy": self.overlap_policy,
            "misfire_policy": self.misfire_policy,
            "delivery_config": dict(self.delivery_config),
            "retry_policy": dict(self.retry_policy),
            "artifact_policy": dict(self.artifact_policy),
            "capability_spec": dict(self.capability_spec),
            "capability_grant": self.capability_grant,
            "approval_status": self.approval_status,
            "paused": self.paused,
            "next_run_at": self.next_fire,
            "last_run_at": self.last_run_at,
            "consecutive_failures": self.consecutive_failures,
            "deleted_at": self.deleted_at,
            "created_at": created_at,
        }


@dataclass(frozen=True)
class CronRun:
    """单次计划运行的独立事实记录。"""

    run_id: str
    job_id: str
    scheduled_for: float
    claimed_at: float
    execution_instance_id: str
    claim_lease_name: str | None = None
    claim_instance_id: str | None = None
    claim_epoch: int | None = None
    status: str = "claimed"
    started_at: float | None = None
    finished_at: float | None = None
    error_type: str | None = None
    result_summary: str | None = None
    artifacts: list = field(default_factory=list)
    delivery_status: str = "not_requested"
    delivery_ref: dict | None = None

    @classmethod
    def from_record(cls, record: dict) -> "CronRun":
        """从数据库运行事实构造领域对象。"""
        return cls(
            run_id=str(record["run_id"]),
            job_id=str(record["job_id"]),
            scheduled_for=float(record["scheduled_for"]),
            claimed_at=float(record["claimed_at"]),
            execution_instance_id=str(record["execution_instance_id"]),
            claim_lease_name=record.get("claim_lease_name"),
            claim_instance_id=record.get("claim_instance_id"),
            claim_epoch=record.get("claim_epoch"),
            status=str(record["status"]),
            started_at=record["started_at"],
            finished_at=record["finished_at"],
            error_type=record["error_type"],
            result_summary=record["result_summary"],
            artifacts=list(record["artifacts"]),
            delivery_status=str(record["delivery_status"]),
            delivery_ref=record["delivery_ref"],
        )

    def to_record(self) -> dict:
        """生成用于首次领取的运行记录载荷。"""
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "scheduled_for": self.scheduled_for,
            "claimed_at": self.claimed_at,
            "execution_instance_id": self.execution_instance_id,
            "claim_lease_name": self.claim_lease_name,
            "claim_instance_id": self.claim_instance_id,
            "claim_epoch": self.claim_epoch,
            "status": self.status,
            "artifacts": list(self.artifacts),
            "delivery_status": self.delivery_status,
            "delivery_ref": self.delivery_ref,
        }
