"""系统状态路由。"""

from fastapi import APIRouter, Request

from hermes.web.read_service import ReadService
from hermes.web.schemas import StatusResponse


router = APIRouter(tags=["status"])


def _service(request: Request) -> ReadService:
    return request.app.state.read_service


@router.get("/api/status", response_model=StatusResponse)
def get_status(request: Request) -> StatusResponse:
    return _service(request).get_status()
