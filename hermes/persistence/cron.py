from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from .database import (
    CRON_APPROVAL_STATUSES, CRON_DELIVERY_STATUSES, CRON_MISFIRE_POLICIES,
    CRON_OVERLAP_POLICIES, CRON_RUN_STATUSES, CRON_RUN_TRANSITIONS,
    CRON_SCHEDULE_TYPES, DBError, _cleanup_batch_limit, _immediate_transaction,
    transaction,
)
from .gateway import _gateway_lease_epoch_value, gateway_runtime_lease_is_valid

_CRON_JOB_COLUMNS = (
    "job_id, name, version, prompt, created_source, creator_id, session_key, "
    "schedule_type, schedule_expr, timezone, toolsets_json, skills_json, workdir, "
    "execution_timeout_seconds, max_agent_iterations, overlap_policy, misfire_policy, "
    "misfire_catch_up, delivery_config_json, retry_policy_json, artifact_policy_json, "
    "capability_spec_json, capability_grant_json, approval_status, paused, next_run_at, "
    "last_run_at, consecutive_failures, deleted_at, created_at, updated_at"
)
_CRON_RUN_COLUMNS = (
    "run_id, job_id, scheduled_for, claimed_at, started_at, finished_at, "
    "execution_instance_id, claim_lease_name, claim_instance_id, claim_epoch, status, "
    "error_type, result_summary, artifacts_json, delivery_status, delivery_ref_json, "
    "root_run_id, attempt_number, retry_due_at, created_at, updated_at"
)

def _serialize_cron_json(value, field_name: str) -> str:
    """把 Cron 的结构化字段编码为 JSON，拒绝不可恢复的数据。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DBError(f"Cron {field_name} JSON serialization failed") from exc


def _deserialize_cron_json(value: str | None, field_name: str):
    """还原 Cron 结构化字段，损坏数据不能伪装成空配置。"""
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError) as exc:
        raise DBError(f"Cron {field_name} JSON deserialization failed") from exc


def _require_cron_string(payload: dict, field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise DBError(f"Cron {field_name} must be a non-empty string")
    return value.strip()


def _normalize_cron_job_payload(payload: dict, *, now: float | None = None) -> dict:
    """校验并规范化创建任务所需的完整定义与当前调度摘要。"""
    if not isinstance(payload, dict):
        raise DBError("Cron job payload must contain an object")
    timestamp = time.time() if now is None else float(now)
    schedule_type = _require_cron_string(payload, "schedule_type")
    if schedule_type not in CRON_SCHEDULE_TYPES:
        raise DBError(f"invalid Cron schedule_type: {schedule_type}")
    overlap_policy = str(payload.get("overlap_policy", "skip"))
    if overlap_policy not in CRON_OVERLAP_POLICIES:
        raise DBError(f"invalid Cron overlap_policy: {overlap_policy}")
    misfire_policy = str(payload.get("misfire_policy", "run_once"))
    if misfire_policy not in CRON_MISFIRE_POLICIES:
        raise DBError(f"invalid Cron misfire_policy: {misfire_policy}")
    misfire_catch_up = int(misfire_policy == "catch_up")
    if misfire_policy == "catch_up":
        misfire_policy = "run_once"
    overlap_policy = str(payload.get("overlap_policy", "skip"))
    if overlap_policy == "parallel":
        overlap_policy = "allow"
    if overlap_policy not in {"skip", "queue", "allow"}:
        raise DBError(f"invalid Cron overlap_policy: {overlap_policy}")
    approval_status = str(payload.get("approval_status", "not_required"))
    if approval_status not in CRON_APPROVAL_STATUSES:
        raise DBError(f"invalid Cron approval_status: {approval_status}")
    try:
        timeout = float(payload.get("execution_timeout_seconds", 300.0))
    except (TypeError, ValueError) as exc:
        raise DBError("Cron execution_timeout_seconds must be positive") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise DBError("Cron execution_timeout_seconds must be positive")
    max_agent_iterations = payload.get("max_agent_iterations", 20)
    if (
        isinstance(max_agent_iterations, bool)
        or not isinstance(max_agent_iterations, int)
        or max_agent_iterations <= 0
    ):
        raise DBError("Cron max_agent_iterations must be a positive integer")
    version = payload.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise DBError("Cron version must be a positive integer")
    paused = payload.get("paused", False)
    if not isinstance(paused, bool):
        raise DBError("Cron paused must be a boolean")
    failures = payload.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise DBError("Cron consecutive_failures must be a non-negative integer")
    toolsets = payload.get("toolsets", [])
    skills = payload.get("skills", [])
    delivery_config = payload.get("delivery_config", {})
    retry_policy = payload.get("retry_policy", {})
    artifact_policy = payload.get("artifact_policy", {})
    capability_spec = payload.get("capability_spec", {})
    capability_grant = payload.get("capability_grant")
    if not isinstance(toolsets, list) or not all(isinstance(item, str) for item in toolsets):
        raise DBError("Cron toolsets must be a list of strings")
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        raise DBError("Cron skills must be a list of strings")
    if not isinstance(delivery_config, dict):
        raise DBError("Cron delivery_config must contain an object")
    if not isinstance(retry_policy, dict):
        raise DBError("Cron retry_policy must contain an object")
    if not isinstance(artifact_policy, dict):
        raise DBError("Cron artifact_policy must contain an object")
    if not isinstance(capability_spec, dict):
        raise DBError("Cron capability_spec must contain an object")
    if capability_grant is not None and not isinstance(capability_grant, dict):
        raise DBError("Cron capability_grant must contain an object or null")
    workdir = payload.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise DBError("Cron workdir must be a string or null")

    def normalize_timestamp(field_name: str):
        value = payload.get(field_name)
        if value is None:
            return None
        if isinstance(value, bool):
            raise DBError(f"Cron {field_name} must be a timestamp or null")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"Cron {field_name} must be a timestamp or null"
            ) from exc
        if not math.isfinite(result):
            raise DBError(f"Cron {field_name} must be finite")
        return result

    created_at = normalize_timestamp("created_at")
    updated_at = normalize_timestamp("updated_at")
    return {
        "job_id": _require_cron_string(payload, "job_id"),
        "name": _require_cron_string(payload, "name"),
        "version": version,
        "prompt": _require_cron_string(payload, "prompt"),
        "created_source": _require_cron_string(payload, "created_source"),
        "creator_id": _require_cron_string(payload, "creator_id"),
        "session_key": _require_cron_string(payload, "session_key"),
        "schedule_type": schedule_type,
        "schedule_expr": _require_cron_string(payload, "schedule_expr"),
        "timezone": _require_cron_string(payload, "timezone"),
        "toolsets_json": _serialize_cron_json(toolsets, "toolsets"),
        "skills_json": _serialize_cron_json(skills, "skills"),
        "workdir": workdir,
        "execution_timeout_seconds": timeout,
        "max_agent_iterations": max_agent_iterations,
        "overlap_policy": overlap_policy,
        "misfire_policy": misfire_policy,
        "misfire_catch_up": misfire_catch_up,
        "delivery_config_json": _serialize_cron_json(
            delivery_config,
            "delivery_config",
        ),
        "retry_policy_json": _serialize_cron_json(retry_policy, "retry_policy"),
        "artifact_policy_json": _serialize_cron_json(artifact_policy, "artifact_policy"),
        "capability_spec_json": _serialize_cron_json(
            capability_spec,
            "capability_spec",
        ),
        "capability_grant_json": (
            None
            if capability_grant is None
            else _serialize_cron_json(capability_grant, "capability_grant")
        ),
        "approval_status": approval_status,
        "paused": int(paused),
        "next_run_at": normalize_timestamp("next_run_at"),
        "last_run_at": normalize_timestamp("last_run_at"),
        "consecutive_failures": failures,
        "deleted_at": normalize_timestamp("deleted_at"),
        "created_at": timestamp if created_at is None else created_at,
        "updated_at": timestamp if updated_at is None else updated_at,
    }


def _cron_job_row(row) -> dict | None:
    """把任务定义行还原为上层可消费的字典。"""
    if row is None:
        return None
    values = dict(zip(
        (
            "job_id", "name", "version", "prompt", "created_source",
            "creator_id", "session_key", "schedule_type", "schedule_expr",
            "timezone", "toolsets_json", "skills_json", "workdir",
            "execution_timeout_seconds", "max_agent_iterations", "overlap_policy", "misfire_policy",
            "misfire_catch_up", "delivery_config_json", "retry_policy_json", "artifact_policy_json", "capability_spec_json", "capability_grant_json", "approval_status",
            "paused", "next_run_at", "last_run_at", "consecutive_failures", "deleted_at",
            "created_at", "updated_at",
        ),
        row,
    ))
    values["toolsets"] = _deserialize_cron_json(values.pop("toolsets_json"), "toolsets")
    values["skills"] = _deserialize_cron_json(values.pop("skills_json"), "skills")
    values["delivery_config"] = _deserialize_cron_json(
        values.pop("delivery_config_json"),
        "delivery_config",
    )
    values["retry_policy"] = _deserialize_cron_json(
        values.pop("retry_policy_json"), "retry_policy"
    ) or {}
    values["artifact_policy"] = _deserialize_cron_json(
        values.pop("artifact_policy_json"), "artifact_policy"
    ) or {}
    values["capability_spec"] = _deserialize_cron_json(
        values.pop("capability_spec_json"),
        "capability_spec",
    ) or {}
    values["capability_grant"] = _deserialize_cron_json(
        values.pop("capability_grant_json"),
        "capability_grant",
    )
    values["paused"] = bool(values["paused"])
    if values.pop("misfire_catch_up"):
        values["misfire_policy"] = "catch_up"
    if values["overlap_policy"] == "allow":
        values["overlap_policy"] = "parallel"
    return values


def _insert_cron_job(
    conn: sqlite3.Connection,
    payload: dict,
    *,
    ignore_existing: bool,
) -> bool:
    """在调用方事务中插入已规范化任务，返回是否真正创建。"""
    conflict = " ON CONFLICT(job_id) DO NOTHING" if ignore_existing else ""
    cursor = conn.execute(
        f"""
        INSERT INTO cron_jobs (
            job_id, name, version, prompt, created_source, creator_id,
            session_key, schedule_type, schedule_expr, timezone,
            toolsets_json, skills_json, workdir, execution_timeout_seconds,
            max_agent_iterations, overlap_policy, misfire_policy, misfire_catch_up, delivery_config_json, retry_policy_json, artifact_policy_json, capability_spec_json,
            capability_grant_json, approval_status, paused, next_run_at,
            last_run_at, consecutive_failures, deleted_at, created_at, updated_at
        ) VALUES (
            :job_id, :name, :version, :prompt, :created_source, :creator_id,
            :session_key, :schedule_type, :schedule_expr, :timezone,
            :toolsets_json, :skills_json, :workdir, :execution_timeout_seconds,
            :max_agent_iterations, :overlap_policy, :misfire_policy, :misfire_catch_up, :delivery_config_json, :retry_policy_json, :artifact_policy_json, :capability_spec_json,
            :capability_grant_json, :approval_status, :paused, :next_run_at,
            :last_run_at, :consecutive_failures, :deleted_at, :created_at, :updated_at
        ){conflict}
        """,
        payload,
    )
    if not ignore_existing and cursor.rowcount != 1:
        raise DBError("Cron job creation did not insert a row")
    return cursor.rowcount == 1


def create_cron_job(conn: sqlite3.Connection, payload: dict) -> dict:
    """原子创建一条任务定义；重复 job ID 明确报错。"""
    normalized = _normalize_cron_job_payload(payload)
    with transaction(conn):
        if get_cron_job(conn, normalized["job_id"]) is not None:
            raise DBError("Cron job already exists")
        _insert_cron_job(conn, normalized, ignore_existing=False)
    created = get_cron_job(conn, normalized["job_id"])
    if created is None:
        raise DBError("Cron job creation could not be read back")
    return created


def _cron_capability_grant_row(row) -> dict | None:
    """还原 Cron grant；审计载荷只允许由受控创建函数写入。"""
    if row is None:
        return None
    values = dict(zip((
        "grant_id", "job_id", "job_version", "policy_version",
        "prompt_digest", "capability_fingerprint", "scope_json",
        "allowed_tool_names_json", "creator_id", "approval_id", "status",
        "audit_json", "created_at", "updated_at", "revoked_at",
        "revoked_reason",
    ), row))
    values["scope"] = _deserialize_cron_json(values.pop("scope_json"), "grant_scope")
    values["allowed_tool_names"] = _deserialize_cron_json(
        values.pop("allowed_tool_names_json"),
        "grant_allowed_tool_names",
    )
    values["audit"] = _deserialize_cron_json(values.pop("audit_json"), "grant_audit")
    return values


def get_active_cron_capability_grant(
    conn: sqlite3.Connection,
    job_id: str,
) -> dict | None:
    """读取任务当前唯一有效的持久授权。"""
    row = conn.execute(
        """
        SELECT grant_id, job_id, job_version, policy_version, prompt_digest,
               capability_fingerprint, scope_json, allowed_tool_names_json,
               creator_id, approval_id, status, audit_json, created_at,
               updated_at, revoked_at, revoked_reason
        FROM cron_capability_grants
        WHERE job_id=? AND status='active'
        ORDER BY updated_at DESC, grant_id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    return _cron_capability_grant_row(row)


def create_cron_capability_grant(conn: sqlite3.Connection, grant: dict) -> dict:
    """原子写入新授权并撤销同任务旧授权，绝不存储 prompt 或文件内容。"""
    required = (
        "grant_id", "job_id", "job_version", "policy_version", "prompt_digest",
        "capability_fingerprint", "scope", "allowed_tool_names", "creator_id", "audit",
    )
    if not isinstance(grant, dict) or any(key not in grant for key in required):
        raise DBError("Cron capability grant payload is incomplete")
    if not isinstance(grant["scope"], dict) or not isinstance(grant["audit"], dict):
        raise DBError("Cron capability grant scope and audit must contain objects")
    if not isinstance(grant["allowed_tool_names"], list):
        raise DBError("Cron capability grant allowed_tool_names must contain a list")
    now = time.time()
    with transaction(conn):
        job = get_cron_job(conn, str(grant["job_id"]))
        if job is None:
            raise DBError("Cron capability grant job not found")
        if int(job["version"]) != int(grant["job_version"]):
            raise DBError("Cron capability grant job version is stale")
        conn.execute(
            """
            UPDATE cron_capability_grants
            SET status='revoked', revoked_at=?, revoked_reason=?, updated_at=?
            WHERE job_id=? AND status='active'
            """,
            (now, "superseded", now, grant["job_id"]),
        )
        conn.execute(
            """
            INSERT INTO cron_capability_grants (
                grant_id, job_id, job_version, policy_version, prompt_digest,
                capability_fingerprint, scope_json, allowed_tool_names_json,
                creator_id, approval_id, status, audit_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                str(grant["grant_id"]), str(grant["job_id"]), int(grant["job_version"]),
                int(grant["policy_version"]), str(grant["prompt_digest"]),
                str(grant["capability_fingerprint"]),
                _serialize_cron_json(grant["scope"], "grant_scope"),
                _serialize_cron_json(grant["allowed_tool_names"], "grant_allowed_tool_names"),
                str(grant["creator_id"]), grant.get("approval_id"),
                _serialize_cron_json(grant["audit"], "grant_audit"), now, now,
            ),
        )
        stored = dict(grant)
        stored.update({"status": "active", "created_at": now, "updated_at": now})
        conn.execute(
            """
            UPDATE cron_jobs
            SET capability_grant_json=?, approval_status='granted', updated_at=?
            WHERE job_id=?
            """,
            (_serialize_cron_json(stored, "capability_grant"), now, grant["job_id"]),
        )
    active = get_active_cron_capability_grant(conn, str(grant["job_id"]))
    if active is None:
        raise DBError("Cron capability grant could not be read back")
    return active


def revoke_cron_capability_grants(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    reason: str,
) -> None:
    """撤销任务的有效授权，并在任务摘要中清除过期快照。"""
    now = time.time()
    conn.execute(
        """
        UPDATE cron_capability_grants
        SET status='revoked', revoked_at=?, revoked_reason=?, updated_at=?
        WHERE job_id=? AND status='active'
        """,
        (now, reason[:80], now, job_id),
    )
    conn.execute(
        """
        UPDATE cron_jobs
        SET capability_grant_json=NULL, approval_status='revoked', updated_at=?
        WHERE job_id=?
        """,
        (now, job_id),
    )


def get_cron_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """读取单个任务定义，不把运行历史混入定义行。"""
    if not isinstance(job_id, str) or not job_id:
        raise DBError("Cron job_id must be a non-empty string")
    row = conn.execute(
        f"SELECT {_CRON_JOB_COLUMNS} FROM cron_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    return _cron_job_row(row)


def list_cron_jobs(
    conn: sqlite3.Connection,
    *,
    include_paused: bool = True,
) -> list[dict]:
    """按下次运行和创建时间稳定列出任务定义。"""
    clause = "WHERE deleted_at IS NULL"
    if not include_paused:
        clause += " AND paused=0"
    rows = conn.execute(
        f"""
        SELECT {_CRON_JOB_COLUMNS}
        FROM cron_jobs
        {clause}
        ORDER BY (next_run_at IS NULL), next_run_at, created_at, job_id
        """
    ).fetchall()
    return [item for row in rows if (item := _cron_job_row(row)) is not None]


def update_cron_job_definition(
    conn: sqlite3.Connection,
    job_id: str,
    changes: dict,
) -> dict:
    """更新定义字段并递增版本；运行摘要只能走独立状态接口。"""
    if not isinstance(changes, dict) or not changes:
        raise DBError("Cron job changes must contain an object")
    definition_fields = {
        "name", "prompt", "schedule_type", "schedule_expr", "timezone",
        "toolsets", "skills", "workdir", "execution_timeout_seconds",
        "max_agent_iterations",
        "overlap_policy", "misfire_policy", "delivery_config", "retry_policy",
        "artifact_policy", "capability_spec",
        "capability_grant", "approval_status", "session_key",
    }
    unknown = set(changes) - definition_fields
    if unknown:
        raise DBError(f"Cron job definition fields are invalid: {sorted(unknown)}")
    with transaction(conn):
        current = get_cron_job(conn, job_id)
        if current is None:
            raise DBError("Cron job not found")
        if current.get("deleted_at") is not None:
            raise DBError("Cron job is deleted")
        merged = dict(current)
        merged.update(changes)
        merged["version"] = int(current["version"]) + 1
        merged["updated_at"] = time.time()
        from hermes.cron.capability import (
            build_capability_scope,
            capability_change_requires_reauthorization,
            capability_fingerprint,
        )
        from hermes.cron.job import CronJob

        previous_job = CronJob.from_record(current)
        candidate_record = dict(merged)
        candidate_record["created_at"] = current["created_at"]
        candidate_job = CronJob.from_record(candidate_record)
        active_grant = get_active_cron_capability_grant(conn, job_id)
        if active_grant is not None and capability_change_requires_reauthorization(
            previous_job,
            candidate_job,
        ):
            revoke_cron_capability_grants(
                conn,
                job_id,
                reason="capability_changed",
            )
            merged["capability_grant"] = None
            merged["approval_status"] = "revoked"
        elif active_grant is not None:
            # 任务名称、暂停和缩短超时不会扩大权限；同步版本和缩小后的快照。
            scope = build_capability_scope(candidate_job)
            active_grant.update({
                "job_version": int(candidate_job.version),
                "prompt_digest": scope["prompt_digest"],
                "capability_fingerprint": capability_fingerprint(scope),
                "scope": scope,
                "updated_at": time.time(),
            })
            conn.execute(
                """
                UPDATE cron_capability_grants
                SET job_version=?, prompt_digest=?, capability_fingerprint=?,
                    scope_json=?, updated_at=?
                WHERE grant_id=? AND status='active'
                """,
                (
                    active_grant["job_version"], active_grant["prompt_digest"],
                    active_grant["capability_fingerprint"],
                    _serialize_cron_json(active_grant["scope"], "grant_scope"),
                    active_grant["updated_at"], active_grant["grant_id"],
                ),
            )
            merged["capability_grant"] = active_grant
            merged["approval_status"] = "granted"
        elif current.get("capability_grant") is not None:
            # 旧摘要没有对应持久授权记录时不能继续被视为有效。
            merged["capability_grant"] = None
            merged["approval_status"] = "revoked"
        normalized = _normalize_cron_job_payload(merged)
        conn.execute(
            """
            UPDATE cron_jobs SET
                name=:name, version=:version, prompt=:prompt,
                created_source=:created_source, creator_id=:creator_id,
                session_key=:session_key, schedule_type=:schedule_type,
                schedule_expr=:schedule_expr, timezone=:timezone,
                toolsets_json=:toolsets_json, skills_json=:skills_json,
                workdir=:workdir,
                execution_timeout_seconds=:execution_timeout_seconds,
                max_agent_iterations=:max_agent_iterations,
                overlap_policy=:overlap_policy, misfire_policy=:misfire_policy,
                misfire_catch_up=:misfire_catch_up,
                delivery_config_json=:delivery_config_json,
                retry_policy_json=:retry_policy_json,
                artifact_policy_json=:artifact_policy_json,
                capability_spec_json=:capability_spec_json,
                capability_grant_json=:capability_grant_json,
                approval_status=:approval_status, updated_at=:updated_at
            WHERE job_id=:job_id
            """,
            normalized,
        )
    updated = get_cron_job(conn, job_id)
    if updated is None:
        raise DBError("Cron job update could not be read back")
    return updated


def set_cron_job_paused(
    conn: sqlite3.Connection,
    job_id: str,
    paused: bool,
) -> dict:
    """单独切换调度开关，不改写任务定义版本。"""
    if not isinstance(paused, bool):
        raise DBError("Cron paused must be a boolean")
    with transaction(conn):
        changed = conn.execute(
            "UPDATE cron_jobs SET paused=?, updated_at=? WHERE job_id=? AND deleted_at IS NULL",
            (int(paused), time.time(), job_id),
        ).rowcount
        if changed != 1:
            raise DBError("Cron job not found")
    job = get_cron_job(conn, job_id)
    if job is None:
        raise DBError("Cron job pause state could not be read back")
    return job


def pause_cron_one_shot_job(conn: sqlite3.Connection, job_id: str) -> dict:
    """一次性任务结束后原子暂停并清空下次运行，不删除定义。"""
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE cron_jobs
            SET paused=1, next_run_at=NULL, updated_at=?
            WHERE job_id=? AND schedule_type='one_shot'
            """,
            (time.time(), job_id),
        ).rowcount
        if changed != 1:
            raise DBError("Cron one-shot job not found")
    job = get_cron_job(conn, job_id)
    if job is None:
        raise DBError("Cron one-shot pause state could not be read back")
    return job


def update_cron_job_schedule_state(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    next_run_at: float | None,
    last_run_at: float | None = None,
    consecutive_failures: int | None = None,
) -> dict:
    """更新调度摘要，不覆盖任务定义或任一运行事实。"""
    if consecutive_failures is not None and (
        isinstance(consecutive_failures, bool)
        or not isinstance(consecutive_failures, int)
        or consecutive_failures < 0
    ):
        raise DBError("Cron consecutive_failures must be a non-negative integer")
    for field_name, value in (("next_run_at", next_run_at), ("last_run_at", last_run_at)):
        if value is not None:
            try:
                if isinstance(value, bool) or not math.isfinite(float(value)):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise DBError(f"Cron {field_name} must be a finite timestamp") from exc
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE cron_jobs
            SET next_run_at=?,
                last_run_at=COALESCE(?, last_run_at),
                consecutive_failures=COALESCE(?, consecutive_failures),
                updated_at=?
            WHERE job_id=?
            """,
            (next_run_at, last_run_at, consecutive_failures, time.time(), job_id),
        ).rowcount
        if changed != 1:
            raise DBError("Cron job not found")
    job = get_cron_job(conn, job_id)
    if job is None:
        raise DBError("Cron schedule state could not be read back")
    return job


def delete_cron_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """删除没有运行历史的任务；已有事实必须保留以便审计与重试。"""
    with transaction(conn):
        existing = get_cron_job(conn, job_id)
        if existing is None:
            return False
        has_runs = conn.execute(
            "SELECT 1 FROM cron_runs WHERE job_id=? LIMIT 1",
            (job_id,),
        ).fetchone()
        if has_runs is not None:
            raise DBError("cannot delete Cron job with run history")
        conn.execute(
            "DELETE FROM cron_capability_grants WHERE job_id=?",
            (job_id,),
        )
        conn.execute("DELETE FROM cron_jobs WHERE job_id=?", (job_id,))
    return True


def soft_delete_cron_job(conn: sqlite3.Connection, job_id: str) -> dict:
    """停止未来 claim 并保留运行、授权和投递审计。"""
    now = time.time()
    with transaction(conn):
        current = get_cron_job(conn, job_id)
        if current is None:
            raise DBError("Cron job not found")
        if current.get("deleted_at") is None:
            conn.execute(
                """
                UPDATE cron_jobs
                SET paused=1, next_run_at=NULL, deleted_at=?, updated_at=?
                WHERE job_id=?
                """,
                (now, now, job_id),
            )
            # 尚未被任何 Gateway 领取的手工请求不再有执行资格；已领取运行继续自行收敛。
            conn.execute(
                """
                UPDATE cron_runs
                SET status='cancelled', finished_at=?, error_type=?,
                    result_summary=?, updated_at=?
                WHERE job_id=? AND status='claimed' AND claim_lease_name IS NULL
                """,
                (
                    now,
                    "job_deleted_before_claim",
                    "Cron job was deleted before this manual run was claimed.",
                    now,
                    job_id,
                ),
            )
    job = get_cron_job(conn, job_id)
    if job is None:
        raise DBError("Cron soft-deleted job could not be read back")
    return job


def resume_cron_job(conn: sqlite3.Connection, job_id: str, next_run_at: float | None) -> dict:
    """恢复未来调度，不从暂停期间的旧窗口补建运行记录。"""
    if next_run_at is None or not math.isfinite(float(next_run_at)):
        raise DBError("Cron resume next_run_at must be a finite timestamp")
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE cron_jobs SET paused=0, next_run_at=?, updated_at=?
            WHERE job_id=? AND deleted_at IS NULL
            """,
            (float(next_run_at), time.time(), job_id),
        ).rowcount
        if changed != 1:
            raise DBError("Cron job not found or deleted")
    job = get_cron_job(conn, job_id)
    if job is None:
        raise DBError("Cron resumed job could not be read back")
    return job


def create_manual_cron_run(conn: sqlite3.Connection, job_id: str, run_id: str) -> dict:
    """建立不推进 next_run_at 的手工运行请求，等待持有 lease 的 Gateway 领取。"""
    now = time.time()
    with _immediate_transaction(conn):
        job = get_cron_job(conn, job_id)
        if job is None or job.get("deleted_at") is not None:
            raise DBError("Cron job not found or deleted")
        active = conn.execute(
            """
            SELECT 1 FROM cron_runs
            WHERE job_id=? AND status IN ('claimed', 'running') LIMIT 1
            """,
            (job_id,),
        ).fetchone() is not None
        if active and job["overlap_policy"] == "skip":
            raise DBError("Cron manual run skipped by overlap policy")
        try:
            conn.execute(
                """
                INSERT INTO cron_runs (
                    run_id, job_id, scheduled_for, claimed_at, started_at,
                    finished_at, execution_instance_id, claim_lease_name,
                    claim_instance_id, claim_epoch, status, error_type,
                    result_summary, artifacts_json, delivery_status,
                    delivery_ref_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL,
                          'claimed', NULL, NULL, '[]', 'not_requested', NULL, ?, ?)
                """,
                (run_id, job_id, now, now, f"manual-request-{run_id}", now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError("Cron manual run identity already exists") from exc
    run = get_cron_run(conn, run_id)
    if run is None:
        raise DBError("Cron manual run could not be read back")
    return run


def create_cron_retry_run(
    conn: sqlite3.Connection,
    previous_run_id: str,
    run_id: str,
    retry_due_at: float,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> dict:
    """为一次可重试失败追加新的运行事实，不覆盖原失败记录或任务计划。"""
    try:
        due = float(retry_due_at)
    except (TypeError, ValueError) as exc:
        raise DBError("Cron retry_due_at must be a timestamp") from exc
    if not math.isfinite(due):
        raise DBError("Cron retry_due_at must be finite")
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    now = time.time()
    with _immediate_transaction(conn):
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            raise DBError("Cron retry lease is no longer valid")
        previous = get_cron_run(conn, previous_run_id)
        if previous is None or previous["status"] != "failed":
            raise DBError("Cron retry requires a failed run")
        if (
            previous["claim_lease_name"], previous["claim_instance_id"], previous["claim_epoch"]
        ) != fence:
            raise DBError("Cron retry run no longer belongs to this lease")
        job = get_cron_job(conn, previous["job_id"])
        if job is None or job.get("deleted_at") is not None:
            raise DBError("Cron retry job is unavailable")
        root_run_id = str(previous.get("root_run_id") or previous["run_id"])
        attempt = int(previous.get("attempt_number") or 1) + 1
        try:
            conn.execute(
                """
                INSERT INTO cron_runs (
                    run_id, job_id, scheduled_for, claimed_at, started_at,
                    finished_at, execution_instance_id, claim_lease_name,
                    claim_instance_id, claim_epoch, status, error_type,
                    result_summary, artifacts_json, delivery_status,
                    delivery_ref_json, root_run_id, attempt_number, retry_due_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL,
                          'claimed', NULL, NULL, '[]', 'not_requested', NULL,
                          ?, ?, ?, ?, ?)
                """,
                (
                    run_id, previous["job_id"], due, now,
                    f"retry-request-{run_id}", root_run_id, attempt, due, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError("Cron retry run identity already exists") from exc
    run = get_cron_run(conn, run_id)
    if run is None:
        raise DBError("Cron retry run could not be read back")
    return run


def recover_interrupted_cron_runs(
    conn: sqlite3.Connection,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> int:
    """收敛失效 lease 遗留的活动运行，避免它们永久占用 overlap。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    now = time.time()
    recovered = 0
    run_columns = ", ".join(
        f"run.{column.strip()}" for column in _CRON_RUN_COLUMNS.split(",")
    )
    with _immediate_transaction(conn):
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            raise DBError("Cron recovery lease is no longer valid")
        rows = conn.execute(
            f"""
            SELECT {run_columns}, job.delivery_config_json
            FROM cron_runs AS run
            INNER JOIN cron_jobs AS job ON job.job_id=run.job_id
            WHERE run.status IN ('claimed', 'running')
              AND run.claim_lease_name=?
              AND (run.claim_instance_id<>? OR run.claim_epoch<>?)
            """,
            fence,
        ).fetchall()
        for row in rows:
            run = _cron_run_row(row[:-1])
            if run is None:
                continue
            delivery_config = _deserialize_cron_json(
                row[-1], "delivery_config"
            )
            policy = str((delivery_config or {}).get("policy", "text")).strip().lower()
            delivery_status = (
                "not_requested"
                if policy in {"silent", "none"}
                else "preparation_pending"
            )
            changed = conn.execute(
                """
                UPDATE cron_runs
                SET status='failed', finished_at=?, error_type=?,
                    result_summary=?, delivery_status=?, updated_at=?
                WHERE run_id=? AND status IN ('claimed', 'running')
                  AND claim_lease_name=? AND claim_instance_id=?
                  AND claim_epoch=?
                """,
                (
                    now,
                    "execution_interrupted",
                    "Cron task was interrupted before its execution result could be recorded.",
                    delivery_status,
                    now,
                    run["run_id"],
                    run["claim_lease_name"],
                    run["claim_instance_id"],
                    run["claim_epoch"],
                ),
            ).rowcount
            if changed != 1:
                continue
            conn.execute(
                """
                UPDATE cron_jobs
                SET last_run_at=?, consecutive_failures=consecutive_failures + 1,
                    updated_at=?
                WHERE job_id=?
                """,
                (now, now, run["job_id"]),
            )
            recovered += 1
    return recovered


def list_unclaimed_manual_cron_runs(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """读取尚未归属任何 Gateway lease 的手工运行请求。"""
    rows = conn.execute(
        f"""
        SELECT {_CRON_RUN_COLUMNS}
        FROM cron_runs AS run
        WHERE run.status='claimed' AND run.claim_lease_name IS NULL
          AND (run.retry_due_at IS NULL OR run.retry_due_at <= ?)
          AND EXISTS (
              SELECT 1 FROM cron_jobs AS job
              WHERE job.job_id=run.job_id AND job.deleted_at IS NULL
          )
        ORDER BY run.claimed_at, run.run_id LIMIT ?
        """,
        (time.time(), max(1, int(limit))),
    ).fetchall()
    return [item for row in rows if (item := _cron_run_row(row)) is not None]


def claim_manual_cron_run(
    conn: sqlite3.Connection,
    run_id: str,
    execution_instance_id: str,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> dict:
    """把手工请求原子归属给当前 Gateway，避免多个实例重复执行。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return {"outcome": "lease_lost"}
        run = get_cron_run(conn, run_id)
        if run is None or run["status"] != "claimed" or run["claim_lease_name"] is not None:
            return {"outcome": "not_pending"}
        job = get_cron_job(conn, run["job_id"])
        if job is None or job.get("deleted_at") is not None:
            return {"outcome": "job_deleted"}
        other_active = conn.execute(
            """
            SELECT 1 FROM cron_runs
            WHERE job_id=? AND run_id<>? AND status IN ('claimed', 'running')
            LIMIT 1
            """,
            (run["job_id"], run_id),
        ).fetchone() is not None
        if other_active and job["overlap_policy"] == "queue":
            return {"outcome": "queued"}
        if other_active and job["overlap_policy"] == "skip":
            conn.execute(
                """
                UPDATE cron_runs
                SET status='cancelled', finished_at=?, error_type=?,
                    result_summary=?, updated_at=?
                WHERE run_id=? AND status='claimed' AND claim_lease_name IS NULL
                """,
                (
                    now,
                    "manual_overlap_skipped",
                    "Manual Cron run was skipped by overlap policy.",
                    now,
                    run_id,
                ),
            )
            return {"outcome": "skipped"}
        changed = conn.execute(
            """
            UPDATE cron_runs
            SET execution_instance_id=?, claim_lease_name=?, claim_instance_id=?,
                claim_epoch=?, updated_at=?
            WHERE run_id=? AND status='claimed' AND claim_lease_name IS NULL
            """,
            (execution_instance_id, fence[0], fence[1], fence[2], now, run_id),
        ).rowcount
        if changed != 1:
            return {"outcome": "not_pending"}
    claimed = get_cron_run(conn, run_id)
    if claimed is None:
        raise DBError("Cron manual run claim could not be read back")
    return {"outcome": "claimed", "job": job, "run": claimed}


def list_due_cron_jobs(
    conn: sqlite3.Connection,
    now: float | None = None,
) -> list[dict]:
    """读取可调度任务定义；实际领取和执行由后续 CronExecutor 负责。"""
    timestamp = time.time() if now is None else float(now)
    rows = conn.execute(
        f"""
        SELECT {_CRON_JOB_COLUMNS}
        FROM cron_jobs
        WHERE deleted_at IS NULL AND paused=0 AND next_run_at IS NOT NULL AND next_run_at <= ?
        ORDER BY next_run_at, job_id
        """,
        (timestamp,),
    ).fetchall()
    return [item for row in rows if (item := _cron_job_row(row)) is not None]


def _cron_run_fence_values(
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> tuple[str, str, int]:
    """校验 Cron claim 使用的 Gateway lease fencing 身份。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("Cron run lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("Cron run instance_id must not be empty")
    return lease_name, instance_id, _gateway_lease_epoch_value(lease_epoch)


def _cron_schedule_update_values(
    next_run_at: float | None,
    *,
    pause: bool,
) -> tuple[float | None, int]:
    """校验调度器计算出的下一次计划时间与一次性暂停标志。"""
    if not isinstance(pause, bool):
        raise DBError("Cron pause_after_claim must be a boolean")
    if next_run_at is not None:
        if isinstance(next_run_at, bool):
            raise DBError("Cron next_run_at must be a finite timestamp or null")
        try:
            next_run_at = float(next_run_at)
        except (TypeError, ValueError) as exc:
            raise DBError("Cron next_run_at must be a finite timestamp or null") from exc
        if not math.isfinite(next_run_at):
            raise DBError("Cron next_run_at must be a finite timestamp or null")
    return next_run_at, int(pause)


def claim_due_cron_job_run(
    conn: sqlite3.Connection,
    job_id: str,
    scheduled_for: float,
    run_id: str,
    execution_instance_id: str,
    next_run_at: float | None,
    *,
    pause_after_claim: bool,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> dict:
    """在有效 Gateway lease 下原子领取一个到期窗口并推进任务调度状态。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    if not isinstance(job_id, str) or not job_id:
        raise DBError("Cron job_id must be a non-empty string")
    if not isinstance(run_id, str) or not run_id:
        raise DBError("Cron run_id must be a non-empty string")
    if not isinstance(execution_instance_id, str) or not execution_instance_id:
        raise DBError("Cron execution_instance_id must not be empty")
    try:
        scheduled_timestamp = float(scheduled_for)
    except (TypeError, ValueError) as exc:
        raise DBError("Cron scheduled_for must be a timestamp") from exc
    if not math.isfinite(scheduled_timestamp):
        raise DBError("Cron scheduled_for must be finite")
    normalized_next, paused = _cron_schedule_update_values(
        next_run_at,
        pause=pause_after_claim,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return {"outcome": "lease_lost"}
        row = conn.execute(
            f"SELECT {_CRON_JOB_COLUMNS} FROM cron_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        job = _cron_job_row(row)
        if (
            job is None
            or job["paused"]
            or job["next_run_at"] is None
            or float(job["next_run_at"]) != scheduled_timestamp
            or scheduled_timestamp > now
        ):
            return {"outcome": "not_due"}
        active = conn.execute(
            """
            SELECT 1 FROM cron_runs
            WHERE job_id=? AND status IN ('claimed', 'running')
            LIMIT 1
            """,
            (job_id,),
        ).fetchone() is not None
        overlap_policy = str(job["overlap_policy"])
        if active and overlap_policy == "queue":
            return {"outcome": "queued"}
        if active and overlap_policy == "skip":
            changed = conn.execute(
                """
                UPDATE cron_jobs
                SET next_run_at=?, paused=?, updated_at=?
                WHERE job_id=? AND next_run_at=? AND paused=0
                """,
                (normalized_next, paused, now, job_id, scheduled_timestamp),
            ).rowcount
            return {"outcome": "skipped" if changed == 1 else "not_due"}
        duplicate = conn.execute(
            "SELECT 1 FROM cron_runs WHERE job_id=? AND scheduled_for=?",
            (job_id, scheduled_timestamp),
        ).fetchone()
        if duplicate is not None:
            return {"outcome": "duplicate"}
        conn.execute(
            """
            INSERT INTO cron_runs (
                run_id, job_id, scheduled_for, claimed_at, started_at,
                finished_at, execution_instance_id, claim_lease_name,
                claim_instance_id, claim_epoch, status, error_type,
                result_summary, artifacts_json, delivery_status,
                delivery_ref_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, 'claimed', NULL,
                      NULL, '[]', 'not_requested', NULL, ?, ?)
            """,
            (
                run_id, job_id, scheduled_timestamp, now,
                execution_instance_id, fence[0], fence[1], fence[2], now, now,
            ),
        )
        changed = conn.execute(
            """
            UPDATE cron_jobs
            SET next_run_at=?, paused=?, updated_at=?
            WHERE job_id=? AND next_run_at=? AND paused=0
            """,
            (normalized_next, paused, now, job_id, scheduled_timestamp),
        ).rowcount
        if changed != 1:
            raise DBError("Cron job claim lost its schedule-state claim")
    run = get_cron_run(conn, run_id)
    if run is None:
        raise DBError("Cron run claim could not be read back")
    return {"outcome": "claimed", "job": job, "run": run}


def advance_due_cron_job_without_run(
    conn: sqlite3.Connection,
    job_id: str,
    scheduled_for: float,
    next_run_at: float | None,
    *,
    pause_after_advance: bool,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """在有效 lease 下跳过一个错过窗口，不创建运行事实。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    normalized_next, paused = _cron_schedule_update_values(
        next_run_at,
        pause=pause_after_advance,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return False
        changed = conn.execute(
            """
            UPDATE cron_jobs
            SET next_run_at=?, paused=?, updated_at=?
            WHERE job_id=? AND next_run_at=? AND paused=0
            """,
            (normalized_next, paused, now, job_id, float(scheduled_for)),
        ).rowcount
        return changed == 1


def _normalize_cron_run_payload(payload: dict, *, now: float | None = None) -> dict:
    """校验首次领取时写入的运行身份与可选关联信息。"""
    if not isinstance(payload, dict):
        raise DBError("Cron run payload must contain an object")
    timestamp = time.time() if now is None else float(now)
    status = str(payload.get("status", "claimed"))
    if status != "claimed":
        raise DBError("Cron runs must be created in claimed status")
    try:
        scheduled_for = float(payload.get("scheduled_for"))
    except (TypeError, ValueError) as exc:
        raise DBError("Cron scheduled_for must be a timestamp") from exc
    if not math.isfinite(scheduled_for):
        raise DBError("Cron scheduled_for must be finite")
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise DBError("Cron artifacts must be a list")
    delivery_ref = payload.get("delivery_ref")
    if delivery_ref is not None and not isinstance(delivery_ref, dict):
        raise DBError("Cron delivery_ref must contain an object or null")
    claim_lease_name = payload.get("claim_lease_name")
    claim_instance_id = payload.get("claim_instance_id")
    claim_epoch = payload.get("claim_epoch")
    claim_values = (claim_lease_name, claim_instance_id, claim_epoch)
    if any(value is not None for value in claim_values):
        if any(value is None for value in claim_values):
            raise DBError("Cron run claim fencing identity is incomplete")
        claim_lease_name = _require_cron_string(payload, "claim_lease_name")
        claim_instance_id = _require_cron_string(payload, "claim_instance_id")
        claim_epoch = _gateway_lease_epoch_value(claim_epoch)
    claimed_at = float(payload.get("claimed_at", timestamp))
    created_at = float(payload.get("created_at", timestamp))
    updated_at = float(payload.get("updated_at", timestamp))
    if not all(math.isfinite(value) for value in (
        claimed_at,
        created_at,
        updated_at,
    )):
        raise DBError("Cron run timestamps must be finite")
    return {
        "run_id": _require_cron_string(payload, "run_id"),
        "job_id": _require_cron_string(payload, "job_id"),
        "scheduled_for": scheduled_for,
        "claimed_at": claimed_at,
        "started_at": None,
        "finished_at": None,
        "execution_instance_id": _require_cron_string(
            payload,
            "execution_instance_id",
        ),
        "claim_lease_name": claim_lease_name,
        "claim_instance_id": claim_instance_id,
        "claim_epoch": claim_epoch,
        "status": status,
        "error_type": None,
        "result_summary": None,
        "artifacts_json": _serialize_cron_json(artifacts, "artifacts"),
        "delivery_status": str(payload.get("delivery_status", "not_requested")),
        "delivery_ref_json": (
            None
            if delivery_ref is None
            else _serialize_cron_json(delivery_ref, "delivery_ref")
        ),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _cron_run_row(row) -> dict | None:
    """把运行事实行还原为结构化记录。"""
    if row is None:
        return None
    values = dict(zip(
        (
            "run_id", "job_id", "scheduled_for", "claimed_at", "started_at",
            "finished_at", "execution_instance_id", "claim_lease_name",
            "claim_instance_id", "claim_epoch", "status", "error_type",
            "result_summary", "artifacts_json", "delivery_status",
        "delivery_ref_json", "root_run_id", "attempt_number", "retry_due_at",
        "created_at", "updated_at",
        ),
        row,
    ))
    values["artifacts"] = _deserialize_cron_json(values.pop("artifacts_json"), "artifacts")
    values["delivery_ref"] = _deserialize_cron_json(
        values.pop("delivery_ref_json"),
        "delivery_ref",
    )
    return values


def create_cron_run(conn: sqlite3.Connection, payload: dict) -> dict:
    """原子记录一次领取；同一任务同一计划时间只能有一个运行身份。"""
    normalized = _normalize_cron_run_payload(payload)
    with transaction(conn):
        if get_cron_job(conn, normalized["job_id"]) is None:
            raise DBError("Cron run job does not exist")
        try:
            conn.execute(
                """
                INSERT INTO cron_runs (
                    run_id, job_id, scheduled_for, claimed_at, started_at,
                    finished_at, execution_instance_id, claim_lease_name,
                    claim_instance_id, claim_epoch, status, error_type,
                    result_summary, artifacts_json, delivery_status,
                    delivery_ref_json, created_at, updated_at
                ) VALUES (
                    :run_id, :job_id, :scheduled_for, :claimed_at, :started_at,
                    :finished_at, :execution_instance_id, :claim_lease_name,
                    :claim_instance_id, :claim_epoch, :status, :error_type,
                    :result_summary, :artifacts_json, :delivery_status,
                    :delivery_ref_json, :created_at, :updated_at
                )
                """,
                normalized,
            )
        except sqlite3.IntegrityError as exc:
            raise DBError("Cron run identity already exists") from exc
    run = get_cron_run(conn, normalized["run_id"])
    if run is None:
        raise DBError("Cron run creation could not be read back")
    return run


def get_cron_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """读取单次运行事实。"""
    if not isinstance(run_id, str) or not run_id:
        raise DBError("Cron run_id must be a non-empty string")
    row = conn.execute(
        f"SELECT {_CRON_RUN_COLUMNS} FROM cron_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return _cron_run_row(row)


def list_cron_runs(
    conn: sqlite3.Connection,
    job_id: str,
) -> list[dict]:
    """按计划时间倒序读取任务的独立运行历史。"""
    rows = conn.execute(
        f"""
        SELECT {_CRON_RUN_COLUMNS}
        FROM cron_runs WHERE job_id=?
        ORDER BY scheduled_for DESC, run_id
        """,
        (job_id,),
    ).fetchall()
    return [item for row in rows if (item := _cron_run_row(row)) is not None]


def transition_cron_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    *,
    error_type: str | None = None,
    result_summary: str | None = None,
    artifacts: list | None = None,
    delivery_status: str | None = None,
    delivery_ref: dict | None = None,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict:
    """按受限状态机推进运行记录，并原子更新任务的结果摘要。"""
    if status not in CRON_RUN_STATUSES:
        raise DBError(f"invalid Cron run status: {status}")
    if error_type is not None and not isinstance(error_type, str):
        raise DBError("Cron error_type must be a string or null")
    if result_summary is not None and not isinstance(result_summary, str):
        raise DBError("Cron result_summary must be a string or null")
    if artifacts is not None and not isinstance(artifacts, list):
        raise DBError("Cron artifacts must be a list or null")
    if delivery_status is not None and not isinstance(delivery_status, str):
        raise DBError("Cron delivery_status must be a string or null")
    if delivery_status is not None and delivery_status not in CRON_DELIVERY_STATUSES:
        raise DBError("invalid Cron delivery_status")
    if delivery_ref is not None and not isinstance(delivery_ref, dict):
        raise DBError("Cron delivery_ref must contain an object or null")
    fence_values = (lease_name, instance_id, lease_epoch)
    if any(value is not None for value in fence_values):
        if any(value is None for value in fence_values):
            raise DBError("Cron run lease fencing identity is incomplete")
        fence = _cron_run_fence_values(
            str(lease_name),
            str(instance_id),
            lease_epoch,
        )
    else:
        fence = None
    now = time.time()
    with _immediate_transaction(conn):
        if fence is not None and not gateway_runtime_lease_is_valid(
            conn,
            *fence,
            now=now,
        ):
            raise DBError("Cron run lease is no longer valid")
        current = get_cron_run(conn, run_id)
        if current is None:
            raise DBError("Cron run not found")
        if fence is not None and (
            current["claim_lease_name"] != fence[0]
            or current["claim_instance_id"] != fence[1]
            or current["claim_epoch"] != fence[2]
        ):
            raise DBError("Cron run claim no longer belongs to this lease")
        if status not in CRON_RUN_TRANSITIONS[current["status"]]:
            raise DBError(
                f"invalid Cron run transition: {current['status']} -> {status}"
            )
        started_at = current["started_at"]
        finished_at = current["finished_at"]
        if status == "running":
            started_at = now
        if status in {"completed", "failed", "blocked", "cancelled"}:
            finished_at = now
        encoded_artifacts = (
            _serialize_cron_json(artifacts, "artifacts")
            if artifacts is not None
            else _serialize_cron_json(current["artifacts"], "artifacts")
        )
        encoded_delivery_ref = (
            _serialize_cron_json(delivery_ref, "delivery_ref")
            if delivery_ref is not None
            else (
                None
                if current["delivery_ref"] is None
                else _serialize_cron_json(current["delivery_ref"], "delivery_ref")
            )
        )
        changed = conn.execute(
            """
            UPDATE cron_runs
            SET status=?, started_at=?, finished_at=?, error_type=?,
                result_summary=?, artifacts_json=?, delivery_status=?,
                delivery_ref_json=?, updated_at=?
            WHERE run_id=? AND status=?
            """,
            (
                status,
                started_at,
                finished_at,
                error_type,
                result_summary,
                encoded_artifacts,
                delivery_status or current["delivery_status"],
                encoded_delivery_ref,
                now,
                run_id,
                current["status"],
            ),
        ).rowcount
        if changed != 1:
            raise DBError("Cron run transition lost its current-state claim")
        if status == "completed":
            conn.execute(
                """
                UPDATE cron_jobs
                SET last_run_at=?, consecutive_failures=0, updated_at=?
                WHERE job_id=?
                """,
                (now, now, current["job_id"]),
            )
        elif status == "failed":
            conn.execute(
                """
                UPDATE cron_jobs
                SET last_run_at=?, consecutive_failures=consecutive_failures + 1,
                    updated_at=?
                WHERE job_id=?
                """,
                (now, now, current["job_id"]),
            )
        elif status in {"blocked", "cancelled"}:
            conn.execute(
                "UPDATE cron_jobs SET last_run_at=?, updated_at=? WHERE job_id=?",
                (now, now, current["job_id"]),
            )
    run = get_cron_run(conn, run_id)
    if run is None:
        raise DBError("Cron run transition could not be read back")
    return run


def create_cron_run_artifact(conn: sqlite3.Connection, artifact: dict) -> dict:
    """持久化一次 Cron 运行的已验证产物及其独立投递关联。"""
    if not isinstance(artifact, dict):
        raise DBError("Cron artifact must be an object")
    required = ("artifact_id", "run_id", "display_name", "local_path", "sha256")
    for field_name in required:
        if not isinstance(artifact.get(field_name), str) or not artifact[field_name]:
            raise DBError(f"Cron artifact {field_name} is required")
    try:
        size_bytes = int(artifact.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise DBError("Cron artifact size_bytes must be positive") from exc
    if size_bytes <= 0:
        raise DBError("Cron artifact size_bytes must be positive")
    delivery_status = str(artifact.get("delivery_status", "not_requested"))
    preparation_error_type = artifact.get("preparation_error_type")
    if preparation_error_type is not None and not isinstance(preparation_error_type, str):
        raise DBError("Cron artifact preparation_error_type must be a string or null")
    preparation_retryable = bool(artifact.get("preparation_retryable", False))
    delivery_id = artifact.get("delivery_id")
    if delivery_id is not None and not isinstance(delivery_id, str):
        raise DBError("Cron artifact delivery_id must be a string or null")
    now = time.time()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO cron_run_artifacts (
                artifact_id, run_id, display_name, local_path, size_bytes,
                sha256, delivery_id, delivery_status, preparation_error_type,
                preparation_retryable, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                delivery_id=excluded.delivery_id,
                delivery_status=excluded.delivery_status,
                preparation_error_type=excluded.preparation_error_type,
                preparation_retryable=excluded.preparation_retryable,
                updated_at=excluded.updated_at
            """,
            (
                artifact["artifact_id"], artifact["run_id"],
                artifact["display_name"], artifact["local_path"], size_bytes,
                artifact["sha256"], delivery_id, delivery_status,
                preparation_error_type, int(preparation_retryable), now, now,
            ),
        )
    return get_cron_run_artifact(conn, str(artifact["artifact_id"])) or {}


def get_cron_run_artifact(conn: sqlite3.Connection, artifact_id: str) -> dict | None:
    """读取单个 Cron 产物记录。"""
    row = conn.execute(
        """
        SELECT artifact_id, run_id, display_name, local_path, size_bytes,
               sha256, delivery_id, delivery_status, preparation_error_type,
               preparation_retryable, created_at, updated_at
        FROM cron_run_artifacts WHERE artifact_id=?
        """,
        (artifact_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "artifact_id": str(row[0]), "run_id": str(row[1]),
        "display_name": str(row[2]), "local_path": str(row[3]),
        "size_bytes": int(row[4]), "sha256": str(row[5]),
        "delivery_id": str(row[6]) if row[6] is not None else None,
        "delivery_status": str(row[7]), "preparation_error_type": row[8],
        "preparation_retryable": bool(row[9]), "created_at": float(row[10]),
        "updated_at": float(row[11]),
    }


def list_cron_run_artifacts(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """按创建顺序返回一次运行的产物，供投递恢复与展示使用。"""
    rows = conn.execute(
        "SELECT artifact_id FROM cron_run_artifacts WHERE run_id=? "
        "ORDER BY created_at, artifact_id",
        (run_id,),
    ).fetchall()
    return [item for row in rows if (item := get_cron_run_artifact(conn, str(row[0]))) is not None]


def update_cron_run_delivery(
    conn: sqlite3.Connection,
    run_id: str,
    delivery_status: str,
    delivery_ref: dict | None,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """在不改变 Agent 终态的前提下更新独立投递摘要。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    if not isinstance(delivery_ref, dict) and delivery_ref is not None:
        raise DBError("Cron delivery_ref must contain an object or null")
    now = time.time()
    with _immediate_transaction(conn):
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return False
        cursor = conn.execute(
            """
            UPDATE cron_runs
            SET delivery_status=?, delivery_ref_json=?, updated_at=?
            WHERE run_id=? AND claim_lease_name=? AND claim_instance_id=?
              AND claim_epoch=?
            """,
            (
                str(delivery_status),
                None if delivery_ref is None else _serialize_cron_json(
                    delivery_ref, "delivery_ref"
                ),
                now, run_id, fence[0], fence[1], fence[2],
            ),
        )
        return cursor.rowcount == 1


def list_cron_delivery_preparation_candidates(
    conn: sqlite3.Connection,
    *,
    stale_after_seconds: float = 120.0,
    limit: int = 20,
) -> list[dict]:
    """读取待准备或已由失效实例遗留的 Cron 投递，不触碰 Agent 执行终态。"""
    now = time.time()
    stale_before = now - max(1.0, float(stale_after_seconds))
    rows = conn.execute(
        f"""
        SELECT {_CRON_RUN_COLUMNS}
        FROM cron_runs
        WHERE status IN ('completed', 'failed', 'blocked', 'cancelled')
          AND (
              delivery_status='preparation_pending'
              OR (delivery_status='preparing' AND updated_at <= ?)
          )
        ORDER BY updated_at, run_id LIMIT ?
        """,
        (stale_before, max(1, int(limit))),
    ).fetchall()
    return [item for row in rows if (item := _cron_run_row(row)) is not None]


def claim_cron_delivery_preparation(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    stale_after_seconds: float,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> dict:
    """以当前 runtime lease 原子领取投递准备；旧 preparing 仅在超时后可恢复。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    now = time.time()
    stale_before = now - max(1.0, float(stale_after_seconds))
    with _immediate_transaction(conn):
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return {"outcome": "lease_lost"}
        current = get_cron_run(conn, run_id)
        if current is None:
            return {"outcome": "not_found"}
        if current["delivery_status"] == "preparation_pending":
            eligible = True
        elif current["delivery_status"] == "preparing" and float(current["updated_at"]) <= stale_before:
            eligible = True
        else:
            eligible = False
        if not eligible:
            return {"outcome": "not_pending"}
        reference = dict(current.get("delivery_ref") or {})
        reference["preparation_claim"] = {
            "lease_name": fence[0], "instance_id": fence[1], "lease_epoch": fence[2],
        }
        changed = conn.execute(
            """
            UPDATE cron_runs SET delivery_status='preparing', delivery_ref_json=?, updated_at=?
            WHERE run_id=? AND delivery_status=?
            """,
            (
                _serialize_cron_json(reference, "delivery_ref"), now, run_id,
                current["delivery_status"],
            ),
        ).rowcount
        if changed != 1:
            return {"outcome": "lost_claim"}
        job = get_cron_job(conn, current["job_id"])
    claimed = get_cron_run(conn, run_id)
    if claimed is None or job is None:
        raise DBError("Cron delivery preparation claim could not be read back")
    return {"outcome": "claimed", "run": claimed, "job": job}


def finish_cron_delivery_preparation(
    conn: sqlite3.Connection,
    run_id: str,
    delivery_status: str,
    delivery_ref: dict,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """只有领取准备的当前 lease 可以结束准备，避免旧实例覆盖新实例。"""
    if delivery_status not in CRON_DELIVERY_STATUSES - {"preparation_pending", "preparing"}:
        raise DBError("invalid completed Cron delivery status")
    if not isinstance(delivery_ref, dict):
        raise DBError("Cron delivery_ref must contain an object")
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    now = time.time()
    with _immediate_transaction(conn):
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return False
        current = get_cron_run(conn, run_id)
        if current is None or current["delivery_status"] != "preparing":
            return False
        claim = dict(current.get("delivery_ref") or {}).get("preparation_claim")
        if not isinstance(claim, dict) or (
            claim.get("lease_name"), claim.get("instance_id"), claim.get("lease_epoch")
        ) != fence:
            return False
        changed = conn.execute(
            """
            UPDATE cron_runs SET delivery_status=?, delivery_ref_json=?, updated_at=?
            WHERE run_id=? AND delivery_status='preparing'
            """,
            (delivery_status, _serialize_cron_json(delivery_ref, "delivery_ref"), now, run_id),
        ).rowcount
        return changed == 1


def refresh_cron_delivery_statuses(
    conn: sqlite3.Connection,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    limit: int = 50,
) -> int:
    """汇总已准备文本和文件的实际投递终态，不改变对应 Agent 运行结果。"""
    fence = _cron_run_fence_values(lease_name, instance_id, lease_epoch)
    now = time.time()
    changed_count = 0
    with _immediate_transaction(conn):
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            return 0
        rows = conn.execute(
            f"""
            SELECT {_CRON_RUN_COLUMNS} FROM cron_runs
            WHERE delivery_status IN ('pending', 'partial_failed')
            ORDER BY updated_at, run_id LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            run = _cron_run_row(row)
            if run is None:
                continue
            reference = dict(run.get("delivery_ref") or {})
            ids = [str(value) for value in reference.get("outbox_ids", [])]
            file_ids = [str(value) for value in reference.get("file_delivery_ids", [])]
            states: list[str] = []
            for outbox_id in ids:
                item = conn.execute(
                    "SELECT status FROM gateway_outbox WHERE id=?", (outbox_id,)
                ).fetchone()
                if item is not None:
                    states.append(str(item[0]))
            for delivery_id in file_ids:
                item = conn.execute(
                    "SELECT status FROM gateway_file_deliveries WHERE id=?", (delivery_id,)
                ).fetchone()
                if item is not None:
                    states.append(str(item[0]))
            if not states:
                continue
            successful = sum(state == "delivered" for state in states)
            failed = sum(state in {"permanent_failed", "cancelled"} for state in states)
            unfinished = len(states) - successful - failed
            if unfinished:
                target = "partial_failed" if successful and failed else "pending"
            elif failed and successful:
                target = "partial_failed"
            elif failed:
                target = "permanent_failed"
            else:
                target = "delivered"
            if target == run["delivery_status"]:
                continue
            reference["delivered_count"] = successful
            reference["delivery_failed_count"] = failed
            cursor = conn.execute(
                "UPDATE cron_runs SET delivery_status=?, delivery_ref_json=?, updated_at=? "
                "WHERE run_id=? AND delivery_status=?",
                (
                    target, _serialize_cron_json(reference, "delivery_ref"), now,
                    run["run_id"], run["delivery_status"],
                ),
            )
            changed_count += cursor.rowcount
    return changed_count


def _parse_legacy_cron_timestamp(value) -> float:
    """把旧 jobs.json 的 ISO 时间转换为 SQLite 时间戳。"""
    if not isinstance(value, str) or not value.strip():
        raise DBError("legacy Cron created_at must be a non-empty ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise DBError("legacy Cron created_at is invalid") from exc


def _legacy_cron_job_payload(item: dict, *, now: float) -> dict:
    """把旧 jobs.json 单条记录映射为正式任务定义。"""
    if not isinstance(item, dict):
        raise DBError("legacy Cron job must contain an object")
    one_shot = item.get("one_shot")
    if not isinstance(one_shot, bool):
        raise DBError("legacy Cron one_shot must be a boolean")
    schedule_expr = _require_cron_string(item, "schedule")
    if one_shot:
        schedule_type = "one_shot"
    elif schedule_expr.startswith("every "):
        schedule_type = "interval"
    else:
        schedule_type = "cron"
    next_fire = item.get("next_fire")
    if isinstance(next_fire, bool):
        raise DBError("legacy Cron next_fire must be a timestamp")
    try:
        next_run_at = float(next_fire)
    except (TypeError, ValueError) as exc:
        raise DBError("legacy Cron next_fire must be a timestamp") from exc
    if not math.isfinite(next_run_at):
        raise DBError("legacy Cron next_fire must be finite")
    job_id = _require_cron_string(item, "job_id")
    session_key = _require_cron_string(item, "session_key")
    return _normalize_cron_job_payload({
        "job_id": job_id,
        "name": f"legacy-{job_id}",
        "version": 1,
        "prompt": _require_cron_string(item, "prompt"),
        "created_source": "legacy_jobs_json",
        "creator_id": session_key,
        "session_key": session_key,
        "schedule_type": schedule_type,
        "schedule_expr": schedule_expr,
        "timezone": "UTC",
        "toolsets": [],
        "skills": [],
        "workdir": None,
        "execution_timeout_seconds": 300.0,
        "overlap_policy": "skip",
        "misfire_policy": "run_once",
        "delivery_config": {},
        "capability_grant": None,
        "approval_status": "not_required",
        "paused": False,
        "next_run_at": next_run_at,
        "last_run_at": None,
        "consecutive_failures": 0,
        "created_at": _parse_legacy_cron_timestamp(item.get("created_at")),
        "updated_at": now,
    }, now=now)


def migrate_legacy_cron_jobs_json(
    conn: sqlite3.Connection,
    legacy_path: str | Path,
) -> int:
    """幂等导入旧 jobs.json；任何读取或数据错误都向调用方显式报告。"""
    path = Path(legacy_path)
    if not path.exists():
        return 0
    try:
        raw = path.read_bytes()
        decoded = raw.decode("utf-8")
        items = json.loads(decoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DBError("legacy Cron jobs.json migration failed") from exc
    if not isinstance(items, list):
        raise DBError("legacy Cron jobs.json must contain a list")
    source_path = str(path.resolve())
    source_sha256 = hashlib.sha256(raw).hexdigest()
    marker = conn.execute(
        "SELECT source_sha256 FROM cron_legacy_imports WHERE source_path=?",
        (source_path,),
    ).fetchone()
    if marker is not None and str(marker[0]) == source_sha256:
        return 0
    now = time.time()
    normalized = [_legacy_cron_job_payload(item, now=now) for item in items]
    with transaction(conn):
        imported = sum(
            1
            for item in normalized
            if _insert_cron_job(conn, item, ignore_existing=True)
        )
        conn.execute(
            """
            INSERT INTO cron_legacy_imports (
                source_path, source_sha256, imported_count, imported_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                source_sha256=excluded.source_sha256,
                imported_count=excluded.imported_count,
                imported_at=excluded.imported_at
            """,
            (source_path, source_sha256, imported, now),
        )
    return imported


def prune_cron_terminal_history(
    conn: sqlite3.Connection,
    *,
    updated_before: float,
    limit: int = 200,
) -> list[str]:
    """删除无活动投递引用的旧终态运行，并返回可安全清理的产物路径。"""
    batch_limit = _cleanup_batch_limit(limit, "Cron history")
    cutoff = float(updated_before)
    terminal_delivery = (
        "delivered", "cancelled", "permanent_failed", "not_requested",
        "invalid_target", "adapter_unavailable",
    )
    paths: list[str] = []
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT run.run_id
            FROM cron_runs AS run
            WHERE run.updated_at < ?
              AND run.status IN ('completed', 'failed', 'blocked', 'cancelled')
              AND NOT EXISTS (
                  SELECT 1 FROM gateway_file_deliveries AS delivery
                  WHERE delivery.cron_run_id=run.run_id
                    AND delivery.status NOT IN (?, ?, ?, ?, ?, ?)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM gateway_outbox AS outbox
                  WHERE outbox.delivery_kind LIKE 'cron_%'
                    AND outbox.event_json LIKE '%' || run.run_id || '%'
                    AND outbox.status NOT IN (
                        'delivered', 'cancelled', 'partial_cancelled', 'permanent_failed'
                    )
              )
            ORDER BY run.updated_at, run.run_id LIMIT ?
            """,
            (cutoff, *terminal_delivery, batch_limit),
        ).fetchall()
        for (run_id,) in rows:
            artifacts = conn.execute(
                "SELECT local_path FROM cron_run_artifacts WHERE run_id=?",
                (str(run_id),),
            ).fetchall()
            cursor = conn.execute(
                """
                DELETE FROM cron_runs
                WHERE run_id=? AND updated_at < ?
                  AND status IN ('completed', 'failed', 'blocked', 'cancelled')
                """,
                (str(run_id), cutoff),
            )
            if cursor.rowcount == 1:
                paths.extend(str(item[0]) for item in artifacts)
    return paths

