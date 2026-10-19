from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.log import logger
from app.models.conversation import ConversationMySQL
from app.models.message import MessageMySQL, MessageRole
from app.repositories.mysql.app.conversation_repository import ConversationRepository
from app.repositories.mysql.app.message_repository import MessageRepository

# 默认标题取首轮问题前 20 字
_DEFAULT_TITLE_LEN = 20


class ConversationService:
    """会话业务层：负责会话生命周期与消息追加，并强制校验会话归属。

    所有涉及会话的读写均经 _get_owned 校验 user_id 归属，防止越权访问他人会话。
    写用例在返回前显式 commit：FastAPI 的 yield 依赖清理发生在响应发出之后，
    不能依赖其后置提交，否则紧接请求可能读不到刚写入的会话/消息。
    """

    def __init__(
        self,
        session: AsyncSession,
        conversation_repository: ConversationRepository,
        message_repository: MessageRepository,
    ) -> None:
        self.session = session
        self.conversation_repository = conversation_repository
        self.message_repository = message_repository

    async def _get_owned(
        self,
        conversation_id: int,
        user_id: int,
    ) -> ConversationMySQL:
        """获取会话并校验归属；不存在抛 404，非本人抛 403。"""
        conversation = await self.conversation_repository.get_by_id(conversation_id)
        if conversation is None:
            raise NotFoundException("会话不存在")
        if conversation.user_id != user_id:
            raise ForbiddenException("无权访问该会话")
        return conversation

    async def create_conversation(
        self,
        user_id: int,
        title: str | None = None,
    ) -> ConversationMySQL:
        conversation = await self.conversation_repository.create(user_id, title)
        await self.session.commit()
        logger.info(f"创建会话: id={conversation.id} user_id={user_id}")
        return conversation

    async def get_conversation(
        self,
        conversation_id: int,
        user_id: int,
    ) -> ConversationMySQL:
        """获取单个会话(校验归属)，供路由读取/回写后回显使用。"""
        return await self._get_owned(conversation_id, user_id)

    async def list_conversations(
        self,
        user_id: int,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ConversationMySQL]:
        return await self.conversation_repository.list_by_user(
            user_id, offset=offset, limit=limit
        )

    async def get_history(
        self,
        conversation_id: int,
        user_id: int,
        *,
        limit: int = 100,
    ) -> list[MessageMySQL]:
        """校验归属后返回会话历史消息(时间升序)。"""
        await self._get_owned(conversation_id, user_id)
        return await self.message_repository.list_by_conversation(conversation_id, limit)

    async def rename(
        self,
        conversation_id: int,
        user_id: int,
        title: str,
    ) -> None:
        await self._get_owned(conversation_id, user_id)
        await self.conversation_repository.rename(conversation_id, title)
        await self.session.commit()
        logger.info(f"重命名会话: id={conversation_id}")

    async def delete(self, conversation_id: int, user_id: int) -> None:
        """删除会话(校验归属)；先删消息再删会话，实现级联删除。"""
        await self._get_owned(conversation_id, user_id)
        await self.message_repository.delete_by_conversation(conversation_id)
        await self.conversation_repository.delete(conversation_id)
        await self.session.commit()
        logger.info(f"删除会话及消息: id={conversation_id}")

    async def append_user_message(
        self,
        conversation_id: int,
        user_id: int,
        content: str,
    ) -> MessageMySQL:
        """追加用户消息；首轮自动以问题前 20 字作为默认标题，并刷新会话排序时间。"""
        conversation = await self._get_owned(conversation_id, user_id)
        message = await self.message_repository.add(
            conversation_id, MessageRole.USER, content
        )
        if conversation.title is None:
            default_title = content[:_DEFAULT_TITLE_LEN]
            if default_title:
                await self.conversation_repository.rename(conversation_id, default_title)
        await self.conversation_repository.touch(conversation_id)
        await self.session.commit()
        logger.info(f"追加用户消息: conversation_id={conversation_id}")
        return message

    async def append_assistant_message(
        self,
        conversation_id: int,
        user_id: int,
        content: str,
        *,
        sql_text: str | None = None,
        result_json: dict | list | None = None,
    ) -> MessageMySQL:
        """追加助手消息(可附 sql 与结果)，并刷新会话排序时间。"""
        await self._get_owned(conversation_id, user_id)
        message = await self.message_repository.add(
            conversation_id,
            MessageRole.ASSISTANT,
            content,
            sql_text=sql_text,
            result_json=result_json,
        )
        await self.conversation_repository.touch(conversation_id)
        await self.session.commit()
        logger.info(f"追加助手消息: conversation_id={conversation_id}")
        return message

    async def get_recent_context(
        self,
        conversation_id: int,
        user_id: int,
        *,
        n: int = 3,
    ) -> list[MessageMySQL]:
        """返回最近 n 轮问答(共 2n 条消息，时间升序)，供大模型上下文使用。"""
        await self._get_owned(conversation_id, user_id)
        return await self.message_repository.list_recent(conversation_id, n * 2)
