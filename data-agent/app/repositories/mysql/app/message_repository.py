from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageMySQL, MessageRole


class MessageRepository:
    """消息纯数据访问层。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        conversation_id: int,
        role: MessageRole,
        content: str,
        sql_text: str | None = None,
        result_json: dict | list | None = None,
    ) -> MessageMySQL:
        message = MessageMySQL(
            conversation_id=conversation_id,
            role=role,
            content=content,
            sql_text=sql_text,
            result_json=result_json,
        )
        self.session.add(message)
        await self.session.flush()        # 触发自增主键回填
        await self.session.refresh(message)  # 回读 DB 生成的默认值(created_at/updated_at)
        return message

    async def list_by_conversation(
        self,
        conversation_id: int,
        limit: int = 100,
    ) -> list[MessageMySQL]:
        """按会话查询历史消息，按 id 升序(时间顺序)。"""
        stmt = (
            select(MessageMySQL)
            .where(MessageMySQL.conversation_id == conversation_id)
            .order_by(MessageMySQL.id.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_recent(
        self,
        conversation_id: int,
        n: int,
    ) -> list[MessageMySQL]:
        """最近 n 条消息：按 id 倒序取 n 条，再翻转为时间升序返回。"""
        stmt = (
            select(MessageMySQL)
            .where(MessageMySQL.conversation_id == conversation_id)
            .order_by(MessageMySQL.id.desc())
            .limit(n)
        )
        result = await self.session.execute(stmt)
        return list(reversed(result.scalars().all()))

    async def delete_by_conversation(self, conversation_id: int) -> None:
        """删除某会话下的全部消息(会话级联删除前置)。"""
        stmt = delete(MessageMySQL).where(
            MessageMySQL.conversation_id == conversation_id
        )
        await self.session.execute(stmt)
        await self.session.flush()
