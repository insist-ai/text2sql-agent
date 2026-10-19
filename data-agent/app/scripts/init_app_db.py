import asyncio

from app.clients.mysql_client_manager import app_mysql_client_manager
from app.models.app_base import AppBase
from app.models.audit_log import AuditLogMySQL  # noqa: F401 注册到 AppBase.metadata
from app.models.conversation import ConversationMySQL  # noqa: F401 注册到 AppBase.metadata
from app.models.message import MessageMySQL  # noqa: F401 注册到 AppBase.metadata
from app.models.user import UserMySQL  # noqa: F401 注册到 AppBase.metadata


async def init_app_db():
    app_mysql_client_manager.init()  # 初始化应用库MySQL客户端
    async with app_mysql_client_manager.engine.begin() as conn:
        await conn.run_sync(AppBase.metadata.create_all)
    await app_mysql_client_manager.close()  # 关闭应用库MySQL客户端
    print("应用库表创建完成：user, conversation, message, audit_log")


if __name__ == "__main__":
    asyncio.run(init_app_db())
