"""Cron 只读路由。"""

from fastapi import APIRouter, Request

from hermes.web.read_service import ReadService
from hermes.web.schemas import CronJobDetailResponse, CronJobListResponse


router = APIRouter(prefix="/api/cron/jobs", tags=["cron"])


def _service(request: Request) -> ReadService:
    return request.app.state.read_service


@router.get("", response_model=CronJobListResponse)
def list_cron_jobs(request: Request) -> CronJobListResponse:
    return _service(request).list_cron_jobs()


@router.get("/{job_id}", response_model=CronJobDetailResponse)
def get_cron_job(request: Request, job_id: str) -> CronJobDetailResponse:
    return _service(request).get_cron_job(job_id)
