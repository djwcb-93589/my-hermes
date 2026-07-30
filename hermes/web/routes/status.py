"""系统状态路由。"""

from fastapi import APIRouter, Request

from hermes.web.read_service import ReadService
from hermes.web.schemas import HealthzResponse, StatusResponse


router = APIRouter(tags=["status"])


def _service(request: Request) -> ReadService:
    return request.app.state.read_service


@router.get("/healthz", response_model=HealthzResponse)
def healthz() -> HealthzResponse:
    """返回不含配置和运行状态的最小未认证存活信号。"""
    return HealthzResponse()


@router.get("/api/status", response_model=StatusResponse)
def get_status(request: Request) -> StatusResponse:
    return _service(request).get_status()
