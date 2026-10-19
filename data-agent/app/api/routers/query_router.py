from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.dependencies import enforce_rate_limit, get_current_user, get_query_service
from app.api.schemas.query_schema import QueryRequest
from app.models.user import UserMySQL
from app.services.query_service import QueryService

query_router = APIRouter()


@query_router.post("/api/query")
async def query_handler(
    body: QueryRequest,
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
    query_service: Annotated[QueryService, Depends(get_query_service)],
    # 限流依赖：在返回 StreamingResponse 之前执行，不影响 SSE 流式返回
    _rate_limit: Annotated[None, Depends(enforce_rate_limit)],
):
    # 在返回 StreamingResponse 前完成会话归属校验与 user 消息落库，
    # 确保 404/403 在响应头发出前抛出，而非被 200 头吞掉
    conversation_id, history = await query_service.prepare_conversation(
        body.query,
        user_id=current_user.id,
        conversation_id=body.conversation_id,
    )
    return StreamingResponse(
        query_service.stream_query(
            body.query,
            user_id=current_user.id,
            conversation_id=conversation_id,
            history=history,
            # 数据范围标识：本项目暂以角色作为"用户可见数据范围"的代理，
            # 接入行级/租户级数据权限时在此替换为真实的数据范围标识
            data_scope=str(current_user.role),
        ),
        media_type="text/event-stream",
    )
