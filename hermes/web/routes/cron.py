"""Cron 查询与控制路由。认证由应用级中间件统一执行。"""

from fastapi import APIRouter, Depends, Header, Request, status

from hermes.web.control_service import CronControlService
from hermes.web.pagination import PageParams, page_params
from hermes.web.read_service import CronReadService
from hermes.web.schemas import (
    CronControlResponse,
    CronJobDetailResponse,
    CronJobListResponse,
    CronRunRequestResponse,
)
from hermes.web.security import ControlUnavailable


router = APIRouter(prefix="/api/cron/jobs", tags=["cron"])


def _service(request: Request) -> CronReadService:
    return request.app.state.cron_read_service


def _control_service(request: Request) -> CronControlService:
    service = request.app.state.control_service
    if service is None:
        raise ControlUnavailable()
    return service


@router.get("", response_model=CronJobListResponse)
def list_cron_jobs(
    request: Request,
    page: PageParams = Depends(page_params),
) -> CronJobListResponse:
    return _service(request).list_cron_jobs(page=page)


@router.get("/{job_id}", response_model=CronJobDetailResponse)
def get_cron_job(
    request: Request,
    job_id: str,
    page: PageParams = Depends(page_params),
) -> CronJobDetailResponse:
    return _service(request).get_cron_job(job_id, page=page)


@router.post("/{job_id}/pause", response_model=CronControlResponse)
def pause_cron_job(
    request: Request,
    job_id: str,
) -> CronControlResponse:
    return _control_service(request).pause_cron_job(job_id)


@router.post("/{job_id}/resume", response_model=CronControlResponse)
def resume_cron_job(
    request: Request,
    job_id: str,
) -> CronControlResponse:
    return _control_service(request).resume_cron_job(job_id)


@router.post(
    "/{job_id}/run",
    response_model=CronRunRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_cron_run(
    request: Request,
    job_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CronRunRequestResponse:
    return _control_service(request).request_cron_run(job_id, idempotency_key)
