from typing import Annotated, List

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_conversation_service, get_current_user
from app.api.schemas.common import ApiResponse
from app.api.schemas.conversation_schema import (
    ConversationCreateSchema,
    ConversationRenameSchema,
    ConversationSchema,
    MessageSchema,
)
from app.models.user import UserMySQL
from app.services.conversation_service import ConversationService

conversation_router = APIRouter(prefix="/api/conversations", tags=["conversation"])


@conversation_router.get(
    "",
    response_model=ApiResponse[List[ConversationSchema]],
    summary="列出当前用户会话(分页)",
)
async def list_conversations(
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
):
    items = await service.list_conversations(current_user.id, offset=offset, limit=limit)
    return ApiResponse(data=[ConversationSchema.model_validate(c) for c in items])


@conversation_router.post(
    "",
    response_model=ApiResponse[ConversationSchema],
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
)
async def create_conversation(
    body: ConversationCreateSchema,
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
):
    conv = await service.create_conversation(current_user.id, body.title)
    return ApiResponse(data=ConversationSchema.model_validate(conv))


@conversation_router.get(
    "/{conversation_id}/messages",
    response_model=ApiResponse[List[MessageSchema]],
    summary="获取某会话历史消息(校验归属)",
)
async def list_messages(
    conversation_id: int,
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    limit: int = Query(100, ge=1, le=500, description="最多返回条数"),
):
    # 非本人会话抛 403,不存在抛 404
    msgs = await service.get_history(conversation_id, current_user.id, limit=limit)
    return ApiResponse(data=[MessageSchema.model_validate(m) for m in msgs])


@conversation_router.patch(
    "/{conversation_id}",
    response_model=ApiResponse[ConversationSchema],
    summary="重命名会话",
)
async def rename_conversation(
    conversation_id: int,
    body: ConversationRenameSchema,
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
):
    await service.rename(conversation_id, current_user.id, body.title)
    conv = await service.get_conversation(conversation_id, current_user.id)
    return ApiResponse(data=ConversationSchema.model_validate(conv))


@conversation_router.delete(
    "/{conversation_id}",
    response_model=ApiResponse[None],
    summary="删除会话(级联删除消息)",
)
async def delete_conversation(
    conversation_id: int,
    current_user: Annotated[UserMySQL, Depends(get_current_user)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
):
    await service.delete(conversation_id, current_user.id)
    return ApiResponse[None](data=None)
