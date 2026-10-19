from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_code import ErrorCode
from app.core.exceptions import (
    ConflictException,
    ForbiddenException,
    UnauthorizedException,
)
from app.core.log import logger
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import UserRole
from app.repositories.mysql.app.user_repository import UserRepository

# 防用户枚举：用户不存在与密码错误返回完全一致的信息
_INVALID_CREDENTIALS = "用户名或密码错误"


class AuthService:
    def __init__(self, session: AsyncSession, user_repository: UserRepository):
        # 写用例在返回前显式 commit：FastAPI 的 yield 依赖清理发生在响应发出之后，
        # 不能依赖其后置提交，否则紧接的登录请求会读不到刚注册的用户。
        self.session = session
        self.user_repository = user_repository

    async def register(self, username: str, password: str) -> dict:
        existing = await self.user_repository.get_by_username(username)
        if existing is not None:
            raise ConflictException("用户名已存在", code=ErrorCode.USERNAME_EXISTS)
        password_hash = hash_password(password)
        user = await self.user_repository.create_user(
            username=username,
            password_hash=password_hash,
            role=UserRole.USER,
        )
        await self.session.commit()
        logger.info(f"用户注册成功: id={user.id} username={username}")
        return {"id": user.id, "username": user.username, "role": str(user.role)}

    async def login(self, username: str, password: str) -> dict:
        user = await self.user_repository.get_by_username(username)
        # 用户不存在与密码错误统一抛 401，信息一致，防止用户枚举
        if user is None or not verify_password(password, user.password_hash):
            raise UnauthorizedException(
                _INVALID_CREDENTIALS, code=ErrorCode.INVALID_CREDENTIALS
            )
        if not user.is_active:
            raise ForbiddenException("账号已被禁用", code=ErrorCode.ACCOUNT_DISABLED)
        await self.user_repository.update_last_login_at(
            user.id, datetime.now(timezone.utc)
        )
        await self.session.commit()
        access_token = create_access_token(user.id, str(user.role))
        refresh_token = create_refresh_token(user.id)
        logger.info(f"用户登录成功: id={user.id} username={username}")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }

    async def refresh(self, refresh_token: str) -> dict:
        try:
            payload = decode_token(refresh_token, "refresh")
        except TokenError as e:
            raise UnauthorizedException(str(e), code=ErrorCode.TOKEN_INVALID)
        user_id = int(payload["sub"])
        user = await self.user_repository.get_by_id(user_id)
        if user is None or not user.is_active:
            raise UnauthorizedException(
                _INVALID_CREDENTIALS, code=ErrorCode.INVALID_CREDENTIALS
            )
        access_token = create_access_token(user.id, str(user.role))
        return {"access_token": access_token, "token_type": "bearer"}
