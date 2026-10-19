from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import enforce_auth_rate_limit, get_auth_service
from app.api.schemas.auth_schema import (
    AccessTokenSchema,
    LoginSchema,
    RefreshTokenSchema,
    RegisterSchema,
    TokenSchema,
    UserSchema,
)
from app.api.schemas.common import ApiResponse
from app.services.auth_service import AuthService

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


@auth_router.post(
    "/register",
    response_model=ApiResponse[UserSchema],
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterSchema,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    # 认证入口限流：按 IP，防批量注册；在业务逻辑前执行，超限抛 429
    _rate_limit: Annotated[None, Depends(enforce_auth_rate_limit)],
):
    user = await auth_service.register(body.username, body.password)
    return ApiResponse(data=UserSchema.model_validate(user))


@auth_router.post("/login", response_model=ApiResponse[TokenSchema])
async def login(
    body: LoginSchema,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    # 认证入口限流：按 IP+用户名，防密码爆破
    _rate_limit: Annotated[None, Depends(enforce_auth_rate_limit)],
):
    tokens = await auth_service.login(body.username, body.password)
    return ApiResponse(data=TokenSchema.model_validate(tokens))


@auth_router.post("/refresh", response_model=ApiResponse[AccessTokenSchema])
async def refresh(
    body: RefreshTokenSchema,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
):
    access = await auth_service.refresh(body.refresh_token)
    return ApiResponse(data=AccessTokenSchema.model_validate(access))
