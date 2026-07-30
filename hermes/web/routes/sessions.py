"""会话只读路由。"""

from fastapi import APIRouter, Depends, Request

from hermes.web.pagination import PageParams, page_params
from hermes.web.read_service import SessionReadService
from hermes.web.schemas import SessionDetailResponse, SessionListResponse


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _service(request: Request) -> SessionReadService:
    return request.app.state.session_read_service


@router.get("", response_model=SessionListResponse)
def list_sessions(
    request: Request,
    page: PageParams = Depends(page_params),
) -> SessionListResponse:
    return _service(request).list_sessions(page=page)


@router.get("/{conversation_id}", response_model=SessionDetailResponse)
def get_session(request: Request, conversation_id: str) -> SessionDetailResponse:
    return _service(request).get_session(conversation_id)
