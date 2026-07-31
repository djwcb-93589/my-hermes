"""Gateway Backend 状态与异步控制请求路由。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request, status

from hermes.backend_control import (
    BackendControlAction,
    BackendControlCreation,
    BackendControlRequest,
    BackendControlUnavailable,
    BackendStatusSnapshot,
)
from hermes.web.backend_service import BackendControlService, BackendStatusReadService
from hermes.web.schemas import (
    BackendControlAcceptedResponse,
    BackendControlRequestResponse,
    BackendGatewayStatusResponse,
    BackendStatusResponse,
    BackendSupervisorStatusResponse,
)


router = APIRouter(prefix="/api/backend", tags=["backend"])


def _status_service(request: Request) -> BackendStatusReadService:
    service = getattr(request.app.state, "backend_status_read_service", None)
    if service is None:
        raise BackendControlUnavailable()
    return service


def _control_service(request: Request) -> BackendControlService:
    service = getattr(request.app.state, "backend_control_service", None)
    if service is None:
        raise BackendControlUnavailable()
    return service


@router.get("/status", response_model=BackendStatusResponse)
def get_backend_status(request: Request) -> BackendStatusResponse:
    return _status_response(_status_service(request).read_status())


@router.get(
    "/requests/{request_id}",
    response_model=BackendControlRequestResponse,
)
def get_backend_request(
    request: Request,
    request_id: str,
) -> BackendControlRequestResponse:
    return _request_response(_status_service(request).get_request(request_id))


@router.post(
    "/gateway/start",
    response_model=BackendControlAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_gateway(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BackendControlAcceptedResponse:
    return _submit(request, BackendControlAction.START, idempotency_key)


@router.post(
    "/gateway/stop",
    response_model=BackendControlAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def stop_gateway(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BackendControlAcceptedResponse:
    return _submit(request, BackendControlAction.STOP, idempotency_key)


@router.post(
    "/gateway/restart",
    response_model=BackendControlAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def restart_gateway(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> BackendControlAcceptedResponse:
    return _submit(request, BackendControlAction.RESTART, idempotency_key)


def _submit(
    request: Request,
    action: BackendControlAction,
    idempotency_key: str | None,
) -> BackendControlAcceptedResponse:
    creation = _control_service(request).submit_gateway_action(
        action,
        idempotency_key=idempotency_key,
        actor_security_id=getattr(request.state, "actor_security_id", None),
    )
    return _accepted_response(creation)


def _accepted_response(
    creation: BackendControlCreation,
) -> BackendControlAcceptedResponse:
    return BackendControlAcceptedResponse(
        request_id=creation.request.request_id,
        action=creation.request.action,
        status=creation.request.status,
    )


def _request_response(
    request: BackendControlRequest,
) -> BackendControlRequestResponse:
    return BackendControlRequestResponse(
        request_id=request.request_id,
        backend_type=request.backend_type,
        action=request.action,
        status=request.status,
        created_at=request.created_at,
        started_at=request.started_at,
        completed_at=request.completed_at,
        result_code=request.result_code,
        result_reference=request.result_reference,
        exception_type=request.exception_type,
        forced_termination=request.forced_termination,
    )


def _status_response(snapshot: BackendStatusSnapshot) -> BackendStatusResponse:
    return BackendStatusResponse(
        observed_at=snapshot.observed_at,
        supervisor=BackendSupervisorStatusResponse(
            online=snapshot.supervisor.online,
            lease_expires_at=snapshot.supervisor.lease_expires_at,
            instance_state=snapshot.supervisor.instance_state,
        ),
        gateway=BackendGatewayStatusResponse(
            observed_state=snapshot.gateway.observed_state,
            ownership=snapshot.gateway.ownership,
            lease_active=snapshot.gateway.lease_active,
            managed=snapshot.gateway.managed,
            started_at=snapshot.gateway.started_at,
            last_exit_at=snapshot.gateway.last_exit_at,
            last_exit_code=snapshot.gateway.last_exit_code,
            config_changed_since_start=(
                snapshot.gateway.config_changed_since_start
            ),
            restart_recommended=snapshot.gateway.restart_recommended,
        ),
        latest_request=(
            None
            if snapshot.latest_request is None
            else _request_response(snapshot.latest_request)
        ),
    )


__all__ = ["router"]
