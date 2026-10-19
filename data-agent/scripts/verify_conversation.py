"""临时验收脚本：验证 ConversationService / 仓储层 全链路。

流程：建用户 → 建会话 → 塞消息 → 按用户查列表 → 查历史 → 越权校验 → 删除会话 → 验证消息一并消失。

用法（在 data-agent 目录）：
    .venv\\Scripts\\python.exe scripts\\verify_conversation.py
    # 或
    uv run python scripts/verify_conversation.py
"""
import asyncio
import pathlib
import sys
import time

# 以直接文件方式运行时，把项目根目录加入 sys.path，使 `import app.*` 可用
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.clients.mysql_client_manager import app_mysql_client_manager
from app.core.security import hash_password
from app.models.conversation import ConversationMySQL  # noqa: F401 注册到 AppBase.metadata
from app.models.message import MessageMySQL  # noqa: F401 注册到 AppBase.metadata
from app.models.user import UserMySQL  # noqa: F401 注册到 AppBase.metadata
from app.repositories.mysql.app.conversation_repository import ConversationRepository
from app.repositories.mysql.app.message_repository import MessageRepository
from app.repositories.mysql.app.user_repository import UserRepository
from app.services.conversation_service import ConversationService


async def main() -> int:
    app_mysql_client_manager.init()

    async with app_mysql_client_manager.session_factory() as session:
        user_repo = UserRepository(session)
        conv_repo = ConversationRepository(session)
        msg_repo = MessageRepository(session)
        service = ConversationService(session, conv_repo, msg_repo)

        ts = int(time.time())
        username = f"verify_{ts}"
        other_name = f"other_{ts}"
        results: list[bool] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            results.append(bool(ok))
            line = f"{'[PASS]' if ok else '[FAIL]'} {name}"
            if detail:
                line += f"  ({detail})"
            print(line)

        try:
            # 1. 建用户
            user = await user_repo.create_user(username, hash_password("123456"))
            check("建用户", user.id is not None, f"id={user.id}")

            # 2. 建会话(标题先留空)
            conv = await service.create_conversation(user.id)
            check("建会话(初始title为空)", conv.title is None, f"id={conv.id} title={conv.title!r}")

            # 3. 塞几条消息(2 轮问答)
            await service.append_user_message(
                conv.id, user.id, "统计每个省份的销售额并按月拆分"
            )
            await service.append_assistant_message(
                conv.id,
                user.id,
                "按省份和月份分组求和",
                sql_text="SELECT province, month, SUM(amount) FROM orders GROUP BY province, month",
                result_json=[{"province": "广东", "month": "2026-01", "sum": 100}],
            )
            await service.append_user_message(conv.id, user.id, "再按渠道拆分")
            await service.append_assistant_message(
                conv.id, user.id, "增加 channel 维度",
                sql_text="...GROUP BY province, month, channel",
            )
            check("塞 4 条消息", True)

            # 4. 默认标题应为首轮问题前 20 字
            conv_reloaded = await conv_repo.get_by_id(conv.id)
            expected = "统计每个省份的销售额并按月拆分"[:20]
            check("默认标题取首轮前20字", conv_reloaded.title == expected,
                  f"title={conv_reloaded.title!r}")

            # 5. 按用户查会话列表
            convs = await service.list_conversations(user.id)
            check("用户会话列表含本会话", any(c.id == conv.id for c in convs),
                  f"共{len(convs)}条")

            # 6. 查历史
            history = await service.get_history(conv.id, user.id)
            check("会话历史 4 条", len(history) == 4, f"实际{len(history)}条")

            # 7. 最近 1 轮上下文(2 条)
            ctx = await service.get_recent_context(conv.id, user.id, n=1)
            check("最近1轮上下文 2 条", len(ctx) == 2, f"实际{len(ctx)}条")

            # 8. 越权校验：另一用户访问该会话历史应抛 ForbiddenException(403)
            other = await user_repo.create_user(other_name, hash_password("123456"))
            from app.core.exceptions import ForbiddenException
            forbidden = False
            try:
                await service.get_history(conv.id, other.id)
            except ForbiddenException:
                forbidden = True
            except Exception as e:
                check("越权校验抛 ForbiddenException", False,
                      f"抛了 {type(e).__name__}: {e}")
            check("越权访问被拦截(403)", forbidden)

            # 9. 删除会话(级联删除消息)
            await service.delete(conv.id, user.id)
            check("删除会话", True)

            # 10. 验证会话与消息均已消失
            remaining_conv = await conv_repo.get_by_id(conv.id)
            remaining_msgs = await msg_repo.list_by_conversation(conv.id)
            check("删除后会话不存在", remaining_conv is None)
            check("删除后消息一并消失", len(remaining_msgs) == 0,
                  f"残留{len(remaining_msgs)}条")

            # 清理测试用户(会话与消息已删,无外键阻塞)
            await session.delete(user)
            await session.delete(other)
            await session.commit()
        except Exception as e:
            await session.rollback()
            print(f"[FAIL] 运行异常: {type(e).__name__}: {e}")
            await app_mysql_client_manager.close()
            return 1

    await app_mysql_client_manager.close()

    total = len(results)
    passed = sum(results)
    print(f"\n== 结果: {passed}/{total} 通过 ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
