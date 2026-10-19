from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationMySQL


class ConversationRepository:
    """会话纯数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, user_id: int, title: str | None = None) -> ConversationMySQL:
        conversation = ConversationMySQL(user_id=user_id, title=title)
        self.session.add(conversation)
        await self.session.flush()        # 触发自增主键回填
        await self.session.refresh(conversation)  # 回读 DB 生成的默认值(created_at/updated_at)
        return conversation

    async def list_by_user(
        self,
        user_id: int,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ConversationMySQL]:
        """按用户分页查询会话，按 updated_at 倒序（最近活跃在前）。"""
        stmt = (
            select(ConversationMySQL)
            .where(ConversationMySQL.user_id == user_id)
            .order_by(ConversationMySQL.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, conversation_id: int) -> ConversationMySQL | None:
        return await self.session.get(ConversationMySQL, conversation_id)

    async def rename(self, conversation_id: int, title: str) -> None:
        stmt = (
            update(ConversationMySQL)
            .where(ConversationMySQL.id == conversation_id)
            .values(title=title)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def delete(self, conversation_id: int) -> None:
        conversation = await self.session.get(ConversationMySQL, conversation_id)
        if conversation is not None:
            await self.session.delete(conversation)
            await self.session.flush()

    async def touch(self, conversation_id: int) -> None:
        """更新 updated_at 为当前时间，用于会话排序保活。"""
        stmt = (
            update(ConversationMySQL)
            .where(ConversationMySQL.id == conversation_id)
            .values(updated_at=func.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()
