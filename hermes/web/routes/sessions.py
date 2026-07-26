"""会话只读路由。"""

from fastapi import APIRouter, Query, Request

from hermes.web.read_service import ReadService
from hermes.web.schemas import SessionDetailResponse, SessionListResponse


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _service(request: Request) -> ReadService:
    return request.app.state.read_service


@router.get("", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SessionListResponse:
    return _service(request).list_sessions(limit=limit, offset=offset)


@router.get("/{conversation_id}", response_model=SessionDetailResponse)
def get_session(request: Request, conversation_id: str) -> SessionDetailResponse:
    return _service(request).get_session(conversation_id)
