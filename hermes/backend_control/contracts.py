"""不依赖 SQLite、Web 或具体进程实现的 Gateway 后端控制契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol


SUPERVISOR_RUNTIME_LEASE_NAME = "supervisor-main"
BACKEND_REQUEST_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_REQUEST_ID_PATTERN = re.compile(BACKEND_REQUEST_ID_PATTERN)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,127}$")
_IDENTITY_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,255}$")
_CONFIG_REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class BackendType(str, Enum):
    """M5.2 唯一允许控制的后端类型。"""

    GATEWAY = "gateway"


class BackendControlAction(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"


class BackendControlRequestStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class BackendControlStage(str, Enum):
    STARTING = "starting"
    STOPPING = "stopping"
    STOPPING_OLD = "stopping_old"
    STARTING_NEW = "starting_new"


class BackendObservedState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    UNKNOWN = "unknown"


class BackendOwnershipState(str, Enum):
    MANAGED = "managed"
    UNMANAGED = "unmanaged"
    NONE = "none"
    UNCERTAIN = "uncertain"


class SupervisorInstanceState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class BackendProcessState(str, Enum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    MISMATCHED = "mismatched"
    UNAVAILABLE = "unavailable"


class BackendResultCode(str, Enum):
    STARTED = "started"
    STOPPED = "stopped"
    RESTARTED = "restarted"
    ALREADY_RUNNING = "already_running"
    ALREADY_STOPPED = "already_stopped"
    UNMANAGED_INSTANCE = "unmanaged_instance"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    CONTROL_CONFLICT = "control_conflict"
    START_FAILED = "start_failed"
    STOP_FAILED = "stop_failed"
    RESTART_FAILED = "restart_failed"
    CONTROL_TIMEOUT = "control_timeout"
    SUPERVISOR_LEASE_LOST = "supervisor_lease_lost"


class BackendControlError(RuntimeError):
    """可由 Dashboard 映射的稳定控制错误。"""

    reason_code = "backend_control_unavailable"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


class BackendControlInvalidRequest(BackendControlError):
    reason_code = "backend_control_invalid_request"


class BackendControlRequestNotFound(BackendControlError):
    reason_code = "backend_request_not_found"


class BackendControlConflict(BackendControlError):
    def __init__(self, reason_code: str = "backend_control_conflict") -> None:
        allowed = {
            "backend_control_conflict",
            "idempotency_conflict",
            "backend_unmanaged",
            "backend_ownership_uncertain",
        }
        self.reason_code = reason_code if reason_code in allowed else "backend_control_conflict"
        RuntimeError.__init__(self, self.reason_code)


class BackendControlUnavailable(BackendControlError):
    def __init__(self, reason_code: str = "backend_control_unavailable") -> None:
        allowed = {
            "backend_control_unavailable",
            "supervisor_unavailable",
        }
        self.reason_code = reason_code if reason_code in allowed else "backend_control_unavailable"
        RuntimeError.__init__(self, self.reason_code)


class SupervisorLeaseLost(RuntimeError):
    """Supervisor 已不再持有允许控制进程的有效 fencing lease。"""


def _utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc_datetime(value: object, field_name: str) -> datetime | None:
    return None if value is None else _utc_datetime(value, field_name)


def _uuid(value: object, field_name: str, *, request: bool = False) -> str:
    pattern = _REQUEST_ID_PATTERN if request else _UUID_PATTERN
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_safe_type(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SAFE_TYPE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class SupervisorFence:
    """Supervisor 对持久化控制操作使用的 fencing 身份。"""

    instance_id: str = field(repr=False)
    lease_epoch: int = field(repr=False)

    def __post_init__(self) -> None:
        _uuid(self.instance_id, "supervisor instance_id")
        if type(self.lease_epoch) is not int or self.lease_epoch <= 0:
            raise ValueError("supervisor lease_epoch is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeLeaseSnapshot:
    """仅供执行和状态推导使用的租约快照，不进入 API。"""

    active: bool
    instance_id: str | None = field(default=None, repr=False)
    lease_epoch: int | None = field(default=None, repr=False)
    heartbeat_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.active) is not bool:
            raise TypeError("lease active must be a boolean")
        if self.instance_id is not None:
            _uuid(self.instance_id, "lease instance_id")
        if self.lease_epoch is not None and (
            type(self.lease_epoch) is not int or self.lease_epoch <= 0
        ):
            raise ValueError("lease epoch is invalid")
        if self.active and any(
            value is None
            for value in (
                self.instance_id,
                self.lease_epoch,
                self.heartbeat_at,
                self.expires_at,
            )
        ):
            raise ValueError("active lease snapshot is incomplete")
        object.__setattr__(
            self,
            "heartbeat_at",
            _optional_utc_datetime(self.heartbeat_at, "heartbeat_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            _optional_utc_datetime(self.expires_at, "expires_at"),
        )


@dataclass(frozen=True, slots=True)
class BackendControlRequest:
    """控制请求的安全投影，不包含主体或幂等摘要。"""

    request_id: str
    backend_type: BackendType
    action: BackendControlAction
    status: BackendControlRequestStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_code: BackendResultCode | None = None
    result_reference: str | None = None
    exception_type: str | None = None
    forced_termination: bool = False
    execution_stage: BackendControlStage | None = None

    def __post_init__(self) -> None:
        _uuid(self.request_id, "request_id", request=True)
        if not isinstance(self.backend_type, BackendType):
            raise TypeError("backend_type is invalid")
        if not isinstance(self.action, BackendControlAction):
            raise TypeError("backend action is invalid")
        if not isinstance(self.status, BackendControlRequestStatus):
            raise TypeError("request status is invalid")
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at, "created_at"))
        object.__setattr__(
            self,
            "started_at",
            _optional_utc_datetime(self.started_at, "started_at"),
        )
        object.__setattr__(
            self,
            "completed_at",
            _optional_utc_datetime(self.completed_at, "completed_at"),
        )
        if self.result_code is not None and not isinstance(
            self.result_code,
            BackendResultCode,
        ):
            raise TypeError("result_code is invalid")
        if self.result_reference is not None:
            _uuid(self.result_reference, "result_reference")
        _optional_safe_type(self.exception_type, "exception_type")
        if type(self.forced_termination) is not bool:
            raise TypeError("forced_termination must be a boolean")
        if self.execution_stage is not None and not isinstance(
            self.execution_stage,
            BackendControlStage,
        ):
            raise TypeError("execution_stage is invalid")
        terminal = self.status in {
            BackendControlRequestStatus.SUCCEEDED,
            BackendControlRequestStatus.FAILED,
            BackendControlRequestStatus.REJECTED,
        }
        if terminal != (self.completed_at is not None):
            raise ValueError("request completion state is inconsistent")
        if terminal != (self.result_code is not None):
            raise ValueError("request result state is inconsistent")


@dataclass(frozen=True, slots=True)
class BackendControlCreation:
    request: BackendControlRequest
    created: bool

    def __post_init__(self) -> None:
        if not isinstance(self.request, BackendControlRequest):
            raise TypeError("request is invalid")
        if type(self.created) is not bool:
            raise TypeError("created must be a boolean")


@dataclass(frozen=True, slots=True)
class BackendControlResult:
    status: BackendControlRequestStatus
    result_code: BackendResultCode
    result_reference: str | None = None
    exception_type: str | None = None
    forced_termination: bool = False

    def __post_init__(self) -> None:
        if self.status not in {
            BackendControlRequestStatus.SUCCEEDED,
            BackendControlRequestStatus.FAILED,
            BackendControlRequestStatus.REJECTED,
        }:
            raise ValueError("control result status must be terminal")
        if not isinstance(self.result_code, BackendResultCode):
            raise TypeError("control result code is invalid")
        if self.result_reference is not None:
            _uuid(self.result_reference, "result_reference")
        _optional_safe_type(self.exception_type, "exception_type")
        if type(self.forced_termination) is not bool:
            raise TypeError("forced_termination must be a boolean")


@dataclass(frozen=True, slots=True)
class BackendProcessBinding:
    """Supervisor 安全控制 Gateway 所需的最小持久事实。"""

    backend_type: BackendType
    observed_state: BackendObservedState
    supervisor_instance_id: str | None = field(default=None, repr=False)
    launch_id: str | None = None
    pid: int | None = field(default=None, repr=False)
    process_identity_token: str | None = field(default=None, repr=False)
    identity_verified: bool | None = None
    started_at: datetime | None = None
    config_revision_at_launch: str | None = field(default=None, repr=False)
    last_exit_at: datetime | None = None
    last_exit_code: int | None = None
    last_request_id: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.backend_type, BackendType):
            raise TypeError("binding backend_type is invalid")
        if not isinstance(self.observed_state, BackendObservedState):
            raise TypeError("binding observed_state is invalid")
        current_values = (
            self.supervisor_instance_id,
            self.launch_id,
            self.pid,
            self.process_identity_token,
            self.started_at,
            self.config_revision_at_launch,
        )
        current_presence = tuple(value is not None for value in current_values)
        if any(current_presence) and not all(current_presence):
            raise ValueError("binding current process fields are inconsistent")
        has_current = all(current_presence)
        if has_current:
            _uuid(self.supervisor_instance_id, "binding supervisor_instance_id")
            _uuid(self.launch_id, "binding launch_id")
            if type(self.pid) is not int or self.pid <= 0:
                raise ValueError("binding pid is invalid")
            if (
                type(self.process_identity_token) is not str
                or _IDENTITY_TOKEN_PATTERN.fullmatch(self.process_identity_token) is None
            ):
                raise ValueError("binding process identity is invalid")
            if type(self.identity_verified) is not bool:
                raise TypeError("binding identity_verified is invalid")
            _utc_datetime(self.started_at, "binding started_at")
            validate_config_revision(self.config_revision_at_launch)
        elif self.identity_verified is not None:
            raise ValueError("empty binding cannot have an identity state")
        if self.last_exit_code is not None and type(self.last_exit_code) is not int:
            raise TypeError("last_exit_code is invalid")
        if self.last_request_id is not None:
            _uuid(self.last_request_id, "last_request_id", request=True)
        for name in ("started_at", "last_exit_at", "updated_at"):
            value = getattr(self, name)
            object.__setattr__(self, name, _optional_utc_datetime(value, name))


@dataclass(frozen=True, slots=True)
class BackendProcessLaunch:
    launch_id: str
    pid: int = field(repr=False)
    process_identity_token: str = field(repr=False)
    started_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.launch_id, "launch_id")
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("process pid is invalid")
        if (
            type(self.process_identity_token) is not str
            or _IDENTITY_TOKEN_PATTERN.fullmatch(self.process_identity_token) is None
        ):
            raise ValueError("process identity token is invalid")
        object.__setattr__(self, "started_at", _utc_datetime(self.started_at, "started_at"))


@dataclass(frozen=True, slots=True)
class BackendProcessInspection:
    state: BackendProcessState
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, BackendProcessState):
            raise TypeError("process inspection state is invalid")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise TypeError("process exit_code is invalid")


@dataclass(frozen=True, slots=True)
class BackendSupervisorStatus:
    online: bool
    lease_expires_at: datetime | None
    instance_state: SupervisorInstanceState

    def __post_init__(self) -> None:
        if type(self.online) is not bool:
            raise TypeError("supervisor online must be a boolean")
        if not isinstance(self.instance_state, SupervisorInstanceState):
            raise TypeError("supervisor instance_state is invalid")
        object.__setattr__(
            self,
            "lease_expires_at",
            _optional_utc_datetime(self.lease_expires_at, "lease_expires_at"),
        )


@dataclass(frozen=True, slots=True)
class BackendGatewayStatus:
    observed_state: BackendObservedState
    ownership: BackendOwnershipState
    lease_active: bool
    managed: bool
    started_at: datetime | None
    last_exit_at: datetime | None
    last_exit_code: int | None
    config_changed_since_start: bool | None
    restart_recommended: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.observed_state, BackendObservedState):
            raise TypeError("gateway observed_state is invalid")
        if not isinstance(self.ownership, BackendOwnershipState):
            raise TypeError("gateway ownership is invalid")
        if type(self.lease_active) is not bool or type(self.managed) is not bool:
            raise TypeError("gateway status flags are invalid")
        if self.managed != (self.ownership is BackendOwnershipState.MANAGED):
            raise ValueError("gateway managed flag is inconsistent")
        object.__setattr__(
            self,
            "started_at",
            _optional_utc_datetime(self.started_at, "started_at"),
        )
        object.__setattr__(
            self,
            "last_exit_at",
            _optional_utc_datetime(self.last_exit_at, "last_exit_at"),
        )
        if self.last_exit_code is not None and type(self.last_exit_code) is not int:
            raise TypeError("gateway last_exit_code is invalid")
        for flag in (
            self.config_changed_since_start,
            self.restart_recommended,
        ):
            if flag is not None and type(flag) is not bool:
                raise TypeError("gateway revision flag is invalid")


@dataclass(frozen=True, slots=True)
class BackendStatusSnapshot:
    observed_at: datetime
    supervisor: BackendSupervisorStatus
    gateway: BackendGatewayStatus
    latest_request: BackendControlRequest | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        if not isinstance(self.supervisor, BackendSupervisorStatus):
            raise TypeError("supervisor status is invalid")
        if not isinstance(self.gateway, BackendGatewayStatus):
            raise TypeError("gateway status is invalid")
        if self.latest_request is not None and not isinstance(
            self.latest_request,
            BackendControlRequest,
        ):
            raise TypeError("latest_request is invalid")


class ConfigRevisionReader(Protocol):
    def read_revision(self) -> str | None:
        """读取原始配置文件 revision；不可读时返回 None。"""


class SupervisorLeaseController(Protocol):
    @property
    def instance_id(self) -> str: ...

    @property
    def fence(self) -> SupervisorFence: ...

    def require_valid(
        self,
        *,
        force_renew: bool = False,
    ) -> SupervisorFence:
        """返回当前 fence；lease 丢失时必须抛出 SupervisorLeaseLost。"""


class GatewayProcessLauncher(Protocol):
    def launch(self, launch_id: str) -> BackendProcessLaunch:
        """只按本地固定规范启动 Gateway。"""

    def request_graceful_stop(self, binding: BackendProcessBinding) -> bool:
        """验证身份后发送第一阶段优雅停止信号。"""

    def terminate(self, binding: BackendProcessBinding) -> bool:
        """验证身份后发送普通终止信号。"""

    def kill(self, binding: BackendProcessBinding) -> bool:
        """验证身份后执行最后阶段强制终止。"""


class GatewayProcessInspector(Protocol):
    def inspect(self, binding: BackendProcessBinding) -> BackendProcessInspection:
        """用 PID 与创建身份共同检查已绑定进程。"""


class BackendControlRepository(Protocol):
    def supervisor_online(self, *, observed_at: datetime) -> bool: ...

    def create_or_get_request(
        self,
        *,
        request_id: str,
        backend_type: BackendType,
        action: BackendControlAction,
        actor_security_id: str,
        idempotency_key_digest: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> BackendControlCreation: ...

    def get_request(self, request_id: str) -> BackendControlRequest | None: ...

    def list_inflight_requests(self) -> tuple[BackendControlRequest, ...]: ...

    def adopt_request(
        self,
        request_id: str,
        fence: SupervisorFence,
        *,
        claimed_at: datetime,
    ) -> BackendControlRequest | None: ...

    def claim_next_request(
        self,
        fence: SupervisorFence,
        *,
        claimed_at: datetime,
    ) -> BackendControlRequest | None: ...

    def mark_request_executing(
        self,
        request_id: str,
        fence: SupervisorFence,
        *,
        started_at: datetime,
    ) -> BackendControlRequest: ...

    def update_request_stage(
        self,
        request_id: str,
        fence: SupervisorFence,
        stage: BackendControlStage,
    ) -> None: ...

    def mark_forced_termination(
        self,
        request_id: str,
        fence: SupervisorFence,
    ) -> None: ...

    def complete_request(
        self,
        request_id: str,
        fence: SupervisorFence,
        result: BackendControlResult,
        *,
        completed_at: datetime,
    ) -> BackendControlRequest: ...

    def read_runtime_lease(
        self,
        lease_name: str,
        *,
        observed_at: datetime,
    ) -> RuntimeLeaseSnapshot: ...

    def get_process_binding(self, backend_type: BackendType) -> BackendProcessBinding | None: ...

    def put_process_binding(
        self,
        binding: BackendProcessBinding,
        fence: SupervisorFence,
    ) -> None: ...


class BackendStatusReadRepository(Protocol):
    def read_status(
        self,
        *,
        current_config_revision: str | None,
        observed_at: datetime,
    ) -> BackendStatusSnapshot: ...

    def get_request(self, request_id: str) -> BackendControlRequest | None: ...


def validate_security_digest(value: object, field_name: str) -> str:
    """校验内部安全摘要，不在异常中回显摘要内容。"""
    if type(value) is not str or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def validate_config_revision(value: object) -> str:
    """校验不包含配置正文的稳定 revision。"""
    if type(value) is not str or _CONFIG_REVISION_PATTERN.fullmatch(value) is None:
        raise ValueError("config revision is invalid")
    return value


__all__ = [name for name in globals() if name.startswith("Backend") or name in {
    "BACKEND_REQUEST_ID_PATTERN",
    "SUPERVISOR_RUNTIME_LEASE_NAME",
    "ConfigRevisionReader",
    "GatewayProcessInspector",
    "GatewayProcessLauncher",
    "RuntimeLeaseSnapshot",
    "SupervisorFence",
    "SupervisorInstanceState",
    "SupervisorLeaseController",
    "SupervisorLeaseLost",
    "validate_config_revision",
    "validate_security_digest",
}]
