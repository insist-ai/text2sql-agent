from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UserMySQL, UserRole


class UserRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_username(self, username: str) -> UserMySQL | None:
        stmt = select(UserMySQL).where(UserMySQL.username == username)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> UserMySQL | None:
        return await self.session.get(UserMySQL, user_id)

    async def create_user(
        self,
        username: str,
        password_hash: str,
        role: UserRole = UserRole.USER,
    ) -> UserMySQL:
        user = UserMySQL(
            username=username,
            password_hash=password_hash,
            role=role,
        )
        self.session.add(user)
        await self.session.flush()        # 触发自增主键回填
        await self.session.refresh(user)  # 回读 DB 生成的默认值(created_at/updated_at/is_active/role)
        return user

    async def update_last_login_at(self, user_id: int, last_login_at: datetime) -> None:
        # 命中身份映射则零额外 SELECT；直接改属性，flush 时生成 UPDATE
        user = await self.session.get(UserMySQL, user_id)
        if user is not None:
            user.last_login_at = last_login_at
            await self.session.flush()
