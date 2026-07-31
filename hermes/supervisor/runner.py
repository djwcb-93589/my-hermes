"""Gateway Supervisor 的串行控制状态机与崩溃恢复。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hermes.backend_control import (
    BackendControlAction,
    BackendControlRepository,
    BackendControlRequest,
    BackendControlRequestStatus,
    BackendControlResult,
    BackendControlStage,
    BackendObservedState,
    BackendProcessBinding,
    BackendProcessInspection,
    BackendProcessState,
    BackendResultCode,
    BackendType,
    ConfigRevisionReader,
    GatewayProcessInspector,
    GatewayProcessLauncher,
    RuntimeLeaseSnapshot,
    SupervisorFence,
    SupervisorLeaseController,
    SupervisorLeaseLost,
    validate_config_revision,
)
from hermes.gateway.constants import GATEWAY_RUNTIME_LEASE_NAME


logger = logging.getLogger(__name__)

SUPERVISOR_POLL_INTERVAL_SECONDS = 0.25
GATEWAY_START_TIMEOUT_SECONDS = 30.0
GATEWAY_GRACEFUL_STOP_TIMEOUT_SECONDS = 15.0
GATEWAY_TERMINATE_TIMEOUT_SECONDS = 5.0
GATEWAY_KILL_TIMEOUT_SECONDS = 5.0
GATEWAY_LEASE_EXPIRY_WAIT_LIMIT_SECONDS = 60.0


class BackendSupervisor:
    """只处理 gateway 请求；所有 OS 操作通过注入的 Launcher/Inspector。"""

    __slots__ = (
        "_clock",
        "_config_revision_reader",
        "_inspector",
        "_launcher",
        "_lease",
        "_monotonic",
        "_repository",
        "_sleep",
    )

    def __init__(
        self,
        repository: BackendControlRepository,
        launcher: GatewayProcessLauncher,
        inspector: GatewayProcessInspector,
        config_revision_reader: ConfigRevisionReader,
        lease: SupervisorLeaseController,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        required_repository_methods = (
            "list_inflight_requests",
            "adopt_request",
            "claim_next_request",
            "mark_request_executing",
            "update_request_stage",
            "mark_forced_termination",
            "complete_request",
            "read_runtime_lease",
            "get_process_binding",
            "put_process_binding",
        )
        if any(
            not callable(getattr(repository, name, None))
            for name in required_repository_methods
        ):
            raise TypeError("backend control repository is invalid")
        if any(
            not callable(getattr(launcher, name, None))
            for name in ("launch", "request_graceful_stop", "terminate", "kill")
        ):
            raise TypeError("gateway process launcher is invalid")
        if not callable(getattr(inspector, "inspect", None)):
            raise TypeError("gateway process inspector is invalid")
        if not callable(getattr(config_revision_reader, "read_revision", None)):
            raise TypeError("config revision reader is invalid")
        if (
            type(getattr(lease, "instance_id", None)) is not str
            or not callable(getattr(lease, "require_valid", None))
        ):
            raise TypeError("supervisor lease is invalid")
        if not all(callable(value) for value in (clock, monotonic, sleep)):
            raise TypeError("supervisor clock is invalid")
        self._repository = repository
        self._launcher = launcher
        self._inspector = inspector
        self._config_revision_reader = config_revision_reader
        self._lease = lease
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep

    def run(self, shutdown: threading.Event) -> None:
        """恢复遗留请求后串行领取；关闭只停止领取，不停止 Gateway。"""
        if not isinstance(shutdown, threading.Event):
            raise TypeError("supervisor shutdown event is invalid")
        self._require_lease(force=True)
        self._reconcile_binding()
        self._recover_inflight_requests()
        while not shutdown.is_set():
            self._require_lease()
            request = self._repository.claim_next_request(
                self._lease.fence,
                claimed_at=self._now(),
            )
            if request is None:
                shutdown.wait(SUPERVISOR_POLL_INTERVAL_SECONDS)
                continue
            self._execute_request(request)

    def _recover_inflight_requests(self) -> None:
        """按实际 lease 与绑定对账，绝不直接重放未知进程。"""
        for previous in self._repository.list_inflight_requests():
            self._require_lease()
            adopted = self._repository.adopt_request(
                previous.request_id,
                self._lease.fence,
                claimed_at=self._now(),
            )
            if adopted is None:
                continue
            self._execute_request(adopted)

    def _execute_request(self, request: BackendControlRequest) -> None:
        action = request.action
        try:
            self._require_lease(force=True)
            executing = self._repository.mark_request_executing(
                request.request_id,
                self._lease.fence,
                started_at=self._now(),
            )
            _log_event("execute", action, executing.status)
            if action is BackendControlAction.START:
                result = self._start(executing, restarting=False)
            elif action is BackendControlAction.STOP:
                result = self._stop(executing, restarting=False)
            elif action is BackendControlAction.RESTART:
                result = self._restart(executing)
            else:
                raise ValueError("unsupported backend action")
        except SupervisorLeaseLost:
            _log_event(
                "lease_lost",
                action,
                BackendControlRequestStatus.EXECUTING,
                result_code=BackendResultCode.SUPERVISOR_LEASE_LOST,
                exception_type="SupervisorLeaseLost",
            )
            raise
        except Exception as exc:
            result = BackendControlResult(
                status=BackendControlRequestStatus.FAILED,
                result_code=_failure_code(action),
                exception_type=_safe_exception_type(exc),
            )
        self._require_lease()
        completed = self._repository.complete_request(
            request.request_id,
            self._lease.fence,
            result,
            completed_at=self._now(),
        )
        _log_event(
            "complete",
            action,
            completed.status,
            result_code=completed.result_code,
            exception_type=completed.exception_type,
        )

    def _start(
        self,
        request: BackendControlRequest,
        *,
        restarting: bool,
    ) -> BackendControlResult:
        stage = (
            BackendControlStage.STARTING_NEW
            if restarting
            else BackendControlStage.STARTING
        )
        self._repository.update_request_stage(
            request.request_id,
            self._lease.fence,
            stage,
        )
        binding = self._reconcile_binding()
        before = self._gateway_lease()
        if before.active:
            if self._binding_is_current_and_verified(binding):
                return BackendControlResult(
                    status=BackendControlRequestStatus.SUCCEEDED,
                    result_code=(
                        BackendResultCode.RESTARTED
                        if restarting
                        else BackendResultCode.ALREADY_RUNNING
                    ),
                    result_reference=None if binding is None else binding.launch_id,
                )
            return BackendControlResult(
                status=BackendControlRequestStatus.REJECTED,
                result_code=(
                    BackendResultCode.OWNERSHIP_UNCERTAIN
                    if binding is not None and binding.pid is not None
                    else BackendResultCode.UNMANAGED_INSTANCE
                ),
            )

        if binding is not None and binding.pid is not None:
            inspection = self._inspector.inspect(binding)
            if (
                inspection.state is BackendProcessState.MATCHED
                and binding.last_request_id == request.request_id
            ):
                return self._await_gateway_start(
                    request,
                    binding,
                    previous_lease=before,
                    restarting=restarting,
                )
            return BackendControlResult(
                status=BackendControlRequestStatus.REJECTED,
                result_code=BackendResultCode.OWNERSHIP_UNCERTAIN,
            )

        revision = self._config_revision_reader.read_revision()
        if revision is None:
            return BackendControlResult(
                status=BackendControlRequestStatus.FAILED,
                result_code=_start_failure_code(restarting),
                exception_type="ConfigRevisionUnavailable",
            )
        try:
            revision = validate_config_revision(revision)
        except ValueError:
            return BackendControlResult(
                status=BackendControlRequestStatus.FAILED,
                result_code=_start_failure_code(restarting),
                exception_type="ConfigRevisionUnavailable",
            )
        launch_id = str(uuid.uuid4())
        self._require_lease(force=True)
        launched = self._launcher.launch(launch_id)
        now = self._now()
        current = BackendProcessBinding(
            backend_type=BackendType.GATEWAY,
            observed_state=BackendObservedState.STARTING,
            supervisor_instance_id=self._lease.instance_id,
            launch_id=launched.launch_id,
            pid=launched.pid,
            process_identity_token=launched.process_identity_token,
            identity_verified=True,
            started_at=launched.started_at,
            config_revision_at_launch=revision,
            last_exit_at=None if binding is None else binding.last_exit_at,
            last_exit_code=None if binding is None else binding.last_exit_code,
            last_request_id=request.request_id,
            updated_at=now,
        )
        try:
            self._repository.put_process_binding(current, self._lease.fence)
        except Exception:
            self._abort_unpersisted_launch(current)
            raise
        return self._await_gateway_start(
            request,
            current,
            previous_lease=before,
            restarting=restarting,
        )

    def _await_gateway_start(
        self,
        request: BackendControlRequest,
        binding: BackendProcessBinding,
        *,
        previous_lease: RuntimeLeaseSnapshot,
        restarting: bool,
    ) -> BackendControlResult:
        deadline = self._monotonic() + GATEWAY_START_TIMEOUT_SECONDS
        while self._monotonic() < deadline:
            self._require_lease()
            inspection = self._inspector.inspect(binding)
            gateway_lease = self._gateway_lease()
            if (
                inspection.state is BackendProcessState.MATCHED
                and gateway_lease.active
                and gateway_lease.heartbeat_at is not None
                and binding.started_at is not None
                and gateway_lease.heartbeat_at >= binding.started_at - timedelta(seconds=1)
                and (
                    previous_lease.lease_epoch is None
                    or gateway_lease.lease_epoch is not None
                    and gateway_lease.lease_epoch > previous_lease.lease_epoch
                )
            ):
                running = replace(
                    binding,
                    observed_state=BackendObservedState.RUNNING,
                    identity_verified=True,
                    updated_at=self._now(),
                )
                self._repository.put_process_binding(running, self._lease.fence)
                return BackendControlResult(
                    status=BackendControlRequestStatus.SUCCEEDED,
                    result_code=(
                        BackendResultCode.RESTARTED
                        if restarting
                        else BackendResultCode.STARTED
                    ),
                    result_reference=binding.launch_id,
                )
            if inspection.state is BackendProcessState.NOT_FOUND:
                self._clear_binding(
                    binding,
                    request_id=request.request_id,
                    exit_code=inspection.exit_code,
                )
                return BackendControlResult(
                    status=BackendControlRequestStatus.FAILED,
                    result_code=_start_failure_code(restarting),
                    result_reference=binding.launch_id,
                    exception_type="GatewayExitedEarly",
                )
            if inspection.state in {
                BackendProcessState.MISMATCHED,
                BackendProcessState.UNAVAILABLE,
            }:
                self._mark_binding_uncertain(binding, request.request_id)
                return BackendControlResult(
                    status=BackendControlRequestStatus.FAILED,
                    result_code=BackendResultCode.OWNERSHIP_UNCERTAIN,
                    result_reference=binding.launch_id,
                )
            self._sleep(SUPERVISOR_POLL_INTERVAL_SECONDS)

        self._repository.put_process_binding(
            replace(
                binding,
                observed_state=BackendObservedState.UNKNOWN,
                updated_at=self._now(),
            ),
            self._lease.fence,
        )
        return BackendControlResult(
            status=BackendControlRequestStatus.FAILED,
            result_code=BackendResultCode.CONTROL_TIMEOUT,
            result_reference=binding.launch_id,
        )

    def _stop(
        self,
        request: BackendControlRequest,
        *,
        restarting: bool,
    ) -> BackendControlResult:
        self._repository.update_request_stage(
            request.request_id,
            self._lease.fence,
            (
                BackendControlStage.STOPPING_OLD
                if restarting
                else BackendControlStage.STOPPING
            ),
        )
        binding = self._reconcile_binding()
        gateway_lease = self._gateway_lease()
        if binding is None or binding.pid is None:
            if gateway_lease.active:
                return BackendControlResult(
                    status=BackendControlRequestStatus.REJECTED,
                    result_code=BackendResultCode.UNMANAGED_INSTANCE,
                )
            return BackendControlResult(
                status=BackendControlRequestStatus.SUCCEEDED,
                result_code=BackendResultCode.ALREADY_STOPPED,
            )

        inspection = self._inspector.inspect(binding)
        if inspection.state is BackendProcessState.NOT_FOUND:
            self._clear_binding(
                binding,
                request_id=request.request_id,
                exit_code=inspection.exit_code,
            )
            if gateway_lease.active:
                return BackendControlResult(
                    status=BackendControlRequestStatus.REJECTED,
                    result_code=BackendResultCode.UNMANAGED_INSTANCE,
                )
            return BackendControlResult(
                status=BackendControlRequestStatus.SUCCEEDED,
                result_code=BackendResultCode.ALREADY_STOPPED,
                result_reference=binding.launch_id,
            )
        if (
            inspection.state is not BackendProcessState.MATCHED
            or not self._binding_is_current_and_verified(binding)
        ):
            self._mark_binding_uncertain(binding, request.request_id)
            return BackendControlResult(
                status=BackendControlRequestStatus.REJECTED,
                result_code=BackendResultCode.OWNERSHIP_UNCERTAIN,
                result_reference=binding.launch_id,
            )

        stopping = replace(
            binding,
            observed_state=BackendObservedState.STOPPING,
            last_request_id=request.request_id,
            updated_at=self._now(),
        )
        self._repository.put_process_binding(stopping, self._lease.fence)
        self._require_lease()
        self._launcher.request_graceful_stop(stopping)
        completed = self._wait_for_gateway_stop(
            stopping,
            GATEWAY_GRACEFUL_STOP_TIMEOUT_SECONDS,
        )
        forced = False
        if completed is None:
            if self._inspector.inspect(stopping).state is BackendProcessState.MATCHED:
                self._require_lease()
                self._launcher.terminate(stopping)
            completed = self._wait_for_gateway_stop(
                stopping,
                GATEWAY_TERMINATE_TIMEOUT_SECONDS,
            )
        if completed is None:
            if self._inspector.inspect(stopping).state is BackendProcessState.MATCHED:
                self._require_lease()
                forced = self._launcher.kill(stopping)
                if forced:
                    self._repository.mark_forced_termination(
                        request.request_id,
                        self._lease.fence,
                    )
            completed = self._wait_for_gateway_stop(
                stopping,
                GATEWAY_KILL_TIMEOUT_SECONDS,
            )
        if completed is None:
            final_inspection = self._inspector.inspect(stopping)
            if final_inspection.state is BackendProcessState.NOT_FOUND:
                self._clear_binding(
                    stopping,
                    request_id=request.request_id,
                    exit_code=final_inspection.exit_code,
                )
                code = (
                    BackendResultCode.RESTART_FAILED
                    if restarting
                    else BackendResultCode.STOP_FAILED
                )
            elif final_inspection.state in {
                BackendProcessState.MISMATCHED,
                BackendProcessState.UNAVAILABLE,
            }:
                self._mark_binding_uncertain(stopping, request.request_id)
                code = BackendResultCode.OWNERSHIP_UNCERTAIN
            else:
                code = (
                    BackendResultCode.RESTART_FAILED
                    if restarting
                    else BackendResultCode.STOP_FAILED
                )
            return BackendControlResult(
                status=BackendControlRequestStatus.FAILED,
                result_code=code,
                result_reference=stopping.launch_id,
                forced_termination=forced,
            )

        self._clear_binding(
            stopping,
            request_id=request.request_id,
            exit_code=completed.exit_code,
        )
        return BackendControlResult(
            status=BackendControlRequestStatus.SUCCEEDED,
            result_code=(
                BackendResultCode.STOPPED
                if not restarting
                else BackendResultCode.ALREADY_STOPPED
            ),
            result_reference=stopping.launch_id,
            forced_termination=forced,
        )

    def _restart(self, request: BackendControlRequest) -> BackendControlResult:
        if request.execution_stage is BackendControlStage.STARTING_NEW:
            return self._start(request, restarting=True)
        stopped = self._stop(request, restarting=True)
        if stopped.status is not BackendControlRequestStatus.SUCCEEDED:
            if stopped.result_code not in {
                BackendResultCode.UNMANAGED_INSTANCE,
                BackendResultCode.OWNERSHIP_UNCERTAIN,
            }:
                stopped = replace(
                    stopped,
                    result_code=BackendResultCode.RESTART_FAILED,
                )
            return stopped
        self._require_lease()
        self._repository.update_request_stage(
            request.request_id,
            self._lease.fence,
            BackendControlStage.STARTING_NEW,
        )
        started = self._start(request, restarting=True)
        if stopped.forced_termination and not started.forced_termination:
            started = replace(started, forced_termination=True)
        return started

    def _wait_for_gateway_stop(
        self,
        binding: BackendProcessBinding,
        timeout_seconds: float,
    ) -> BackendProcessInspection | None:
        deadline = self._monotonic() + timeout_seconds
        lease_wait_extended = False
        while self._monotonic() < deadline:
            self._require_lease()
            last_inspection = self._inspector.inspect(binding)
            gateway_lease = self._gateway_lease()
            if (
                last_inspection.state is BackendProcessState.NOT_FOUND
                and not gateway_lease.active
            ):
                return last_inspection
            if (
                last_inspection.state is BackendProcessState.NOT_FOUND
                and gateway_lease.active
                and gateway_lease.expires_at is not None
                and not lease_wait_extended
            ):
                remaining = max(
                    0.0,
                    (gateway_lease.expires_at - self._now()).total_seconds(),
                )
                deadline = max(
                    deadline,
                    self._monotonic()
                    + min(
                        remaining + SUPERVISOR_POLL_INTERVAL_SECONDS,
                        GATEWAY_LEASE_EXPIRY_WAIT_LIMIT_SECONDS,
                    ),
                )
                lease_wait_extended = True
            if last_inspection.state in {
                BackendProcessState.MISMATCHED,
                BackendProcessState.UNAVAILABLE,
            }:
                return None
            self._sleep(SUPERVISOR_POLL_INTERVAL_SECONDS)
        return None

    def _reconcile_binding(self) -> BackendProcessBinding | None:
        self._require_lease()
        binding = self._repository.get_process_binding(BackendType.GATEWAY)
        if binding is None or binding.pid is None:
            return binding
        inspection = self._inspector.inspect(binding)
        if inspection.state is BackendProcessState.MATCHED:
            gateway_lease = self._gateway_lease()
            reconciled = replace(
                binding,
                supervisor_instance_id=self._lease.instance_id,
                identity_verified=True,
                observed_state=(
                    BackendObservedState.RUNNING
                    if gateway_lease.active
                    else binding.observed_state
                    if binding.observed_state in {
                        BackendObservedState.STARTING,
                        BackendObservedState.STOPPING,
                    }
                    else BackendObservedState.UNKNOWN
                ),
                updated_at=self._now(),
            )
            self._repository.put_process_binding(reconciled, self._lease.fence)
            return reconciled
        if inspection.state is BackendProcessState.NOT_FOUND:
            return self._clear_binding(
                binding,
                request_id=binding.last_request_id,
                exit_code=inspection.exit_code,
            )
        return self._mark_binding_uncertain(binding, binding.last_request_id)

    def _abort_unpersisted_launch(
        self,
        binding: BackendProcessBinding,
    ) -> None:
        """仅清理由本次调用刚启动且身份仍匹配的未持久化子进程。"""
        try:
            if (
                self._inspector.inspect(binding).state
                is not BackendProcessState.MATCHED
            ):
                return
            self._launcher.terminate(binding)
            deadline = self._monotonic() + GATEWAY_TERMINATE_TIMEOUT_SECONDS
            while self._monotonic() < deadline:
                if (
                    self._inspector.inspect(binding).state
                    is BackendProcessState.NOT_FOUND
                ):
                    return
                self._sleep(SUPERVISOR_POLL_INTERVAL_SECONDS)
            if (
                self._inspector.inspect(binding).state
                is BackendProcessState.MATCHED
            ):
                self._launcher.kill(binding)
        except Exception:
            # 清理失败时保留原始持久化故障，后续 lease 会暴露未托管实例。
            return

    def _clear_binding(
        self,
        binding: BackendProcessBinding,
        *,
        request_id: str | None,
        exit_code: int | None,
    ) -> BackendProcessBinding:
        now = self._now()
        cleared = BackendProcessBinding(
            backend_type=BackendType.GATEWAY,
            observed_state=BackendObservedState.STOPPED,
            last_exit_at=now,
            last_exit_code=exit_code,
            last_request_id=request_id,
            updated_at=now,
        )
        self._repository.put_process_binding(cleared, self._lease.fence)
        return cleared

    def _mark_binding_uncertain(
        self,
        binding: BackendProcessBinding,
        request_id: str | None,
    ) -> BackendProcessBinding:
        uncertain = replace(
            binding,
            observed_state=BackendObservedState.UNKNOWN,
            identity_verified=False,
            last_request_id=request_id,
            updated_at=self._now(),
        )
        self._repository.put_process_binding(uncertain, self._lease.fence)
        return uncertain

    def _binding_is_current_and_verified(
        self,
        binding: BackendProcessBinding | None,
    ) -> bool:
        return bool(
            binding is not None
            and binding.pid is not None
            and binding.identity_verified
            and binding.supervisor_instance_id == self._lease.instance_id
            and self._inspector.inspect(binding).state is BackendProcessState.MATCHED
        )

    def _gateway_lease(self) -> RuntimeLeaseSnapshot:
        return self._repository.read_runtime_lease(
            GATEWAY_RUNTIME_LEASE_NAME,
            observed_at=self._now(),
        )

    def _require_lease(self, *, force: bool = False) -> SupervisorFence:
        return self._lease.require_valid(force_renew=force)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("supervisor clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _failure_code(action: BackendControlAction) -> BackendResultCode:
    if action is BackendControlAction.START:
        return BackendResultCode.START_FAILED
    if action is BackendControlAction.STOP:
        return BackendResultCode.STOP_FAILED
    return BackendResultCode.RESTART_FAILED


def _start_failure_code(restarting: bool) -> BackendResultCode:
    return (
        BackendResultCode.RESTART_FAILED
        if restarting
        else BackendResultCode.START_FAILED
    )


def _safe_exception_type(exc: Exception) -> str:
    name = type(exc).__name__
    return name if name and len(name) <= 128 else "SupervisorError"


def _log_event(
    stage: str,
    action: BackendControlAction,
    status: BackendControlRequestStatus,
    *,
    result_code: BackendResultCode | None = None,
    exception_type: str | None = None,
) -> None:
    logger.info(
        "Gateway Supervisor event: supervisor_stage=%s backend_type=gateway "
        "action=%s request_status=%s result_code=%s exception_type=%s",
        stage,
        action.value,
        status.value,
        "none" if result_code is None else result_code.value,
        "none" if exception_type is None else exception_type,
    )


__all__ = [
    "BackendSupervisor",
]
