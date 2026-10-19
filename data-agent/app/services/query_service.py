import json
import time

from app.core.context import request_id_context_var
from app.core.log import logger
from app.core.query_cache import QueryCache, build_cache_key
from app.core.rejection import REJECT_USER_MESSAGE
from app.models.audit_log import AuditStatus
from app.services.audit_service import schedule_audit_log
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import CustomEmbeddings
from app.conf.app_config import app_config
from app.repositories.es.column_es_repository import ColumnESRepository
from app.repositories.es.metric_es_repository import MetricESRepository
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository
from app.services.conversation_service import ConversationService


class QueryService:
    def __init__(self,
                 embedding_client: CustomEmbeddings,
                 column_qdrant_repository: ColumnQdrantRepository,
                 value_es_repository: ValueESRepository,
                 metric_qdrant_repository: MetricQdrantRepository,
                 value_qdrant_repository: ValueQdrantRepository,
                 column_es_repository: ColumnESRepository,
                 metric_es_repository: MetricESRepository,
                 meta_mysql_repository: MetaMySQLRepository,
                 dw_mysql_repository: DWMySQLRepository,
                 session: AsyncSession,
                 conversation_service: ConversationService,
                 query_cache: QueryCache | None = None):
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.value_es_repository = value_es_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_qdrant_repository = value_qdrant_repository
        self.column_es_repository = column_es_repository
        self.metric_es_repository = metric_es_repository
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository
        # 与 conversation_service 共享同一个 app 库 session，保证会话/消息写入与查询在同一事务视图
        self.session = session
        self.conversation_service = conversation_service
        # 查询结果缓存（None 表示缓存关闭）；见 app/core/query_cache.py
        self.query_cache = query_cache

    async def prepare_conversation(
        self,
        query_text: str,
        *,
        user_id: int,
        conversation_id: int | None,
    ) -> tuple[int, list[dict]]:
        """在流式响应返回前完成：会话解析(归属校验)、落库 user 消息、拉取历史上下文。

        不在生成器内做这些，是为了让 404(不存在)/403(非本人) 能在响应头发出前抛出，
        而不是被 StreamingResponse 的 200 头吞掉。user 消息在此处已 commit，
        即便后续图执行失败也不会回滚成脏数据。
        """
        if conversation_id is None:
            conv = await self.conversation_service.create_conversation(user_id, None)
            conversation_id = conv.id
        else:
            # 不存在抛 404、非本人抛 403
            await self.conversation_service.get_conversation(conversation_id, user_id)

        # 先取最近 3 轮历史（6 条消息）再落库 user 消息，
        # 保证 history 只包含此前的问答（不含当前问题），供查询改写消解指代使用
        history_msgs = await self.conversation_service.get_recent_context(
            conversation_id, user_id, n=3
        )
        history = [
            {"role": m.role.value, "content": m.content}
            for m in history_msgs
        ]

        # append_user_message 内部已 commit；即便后续图执行失败也不会回滚成脏数据
        await self.conversation_service.append_user_message(conversation_id, user_id, query_text)
        return conversation_id, history

    async def stream_query(
        self,
        query_text: str,
        *,
        user_id: int,
        conversation_id: int,
        history: list[dict],
        data_scope: str = "default",
    ):
        """SSE 生成器：首帧下发 meta，随后透传 graph 的 progress/result 事件，
        图执行成功后落库 assistant 消息（失败仅记日志，不影响主流程）。

        查询缓存：仅对无历史上下文的单轮问题启用；命中时跳过 LangGraph 全流程，
        按原 progress/result 协议回放结果，并在首帧 meta 标注 cache_hit=true。
        """
        cache = self.query_cache
        # 审计计时起点：记录本轮流式查询的整体耗时（毫秒）
        start = time.perf_counter()
        # 无历史时改写节点直接返回原问题，故 query 即等价"改写后的问题"；
        # 带历史的问题依赖会话上下文、跨会话不可复用，一律不走缓存。
        cache_key = (
            build_cache_key(query_text, data_scope)
            if cache is not None and not history
            else None
        )

        # 命中缓存：跳过整图，直接回放首帧 + 事件 + 落库 assistant
        if cache_key is not None:
            cached = await cache.get(cache_key)
            if cached is not None:
                logger.info(
                    f"查询缓存命中: key={cache_key[:12]} conversation_id={conversation_id}"
                )
                yield self._sse(
                    {"type": "meta", "conversation_id": conversation_id, "cache_hit": True}
                )
                for event in cached.get("events", []):
                    yield self._sse(event)
                await self._save_assistant_message(
                    conversation_id, user_id, cached.get("sql"), cached.get("result_data")
                )
                # 命中缓存视为一次成功查询，异步落审计（独立 session，失败不影响主流程）
                self._record_audit(
                    start=start,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    question=query_text,
                    sql_text=cached.get("sql"),
                    status=AuditStatus.SUCCESS,
                    error_message=None,
                )
                return

        context = DataAgentContext(
            embedding_client=self.embedding_client,
            column_qdrant_repository=self.column_qdrant_repository,
            value_es_repository=self.value_es_repository,
            metric_qdrant_repository=self.metric_qdrant_repository,
            value_qdrant_repository=self.value_qdrant_repository,
            column_es_repository=self.column_es_repository,
            metric_es_repository=self.metric_es_repository,
            meta_mysql_repository=self.meta_mysql_repository,
            dw_mysql_repository=self.dw_mysql_repository,
        )
        state = DataAgentState(
            query=query_text,
            history=history,
            conversation_id=conversation_id,
            # SQL 自省闭环初始状态
            sql_attempts=[],
            error_history=[],
            attempt_count=0,
            max_retries=app_config.agent.max_retries,
            final_sql=None,
        )

        # 首帧 meta，便于前端绑定会话；cache_hit 标注本次是否命中缓存
        yield self._sse(
            {"type": "meta", "conversation_id": conversation_id, "cache_hit": False}
        )

        events: list[dict] = []
        final_state: dict | None = None
        result_data = None
        rejected = False
        reject_message = REJECT_USER_MESSAGE
        try:
            async for mode, payload in graph.astream(
                input=state, context=context, stream_mode=["custom", "values"]
            ):
                if mode == "custom":
                    # 透传 progress/result 事件，保持原协议不变；同时收集供缓存回放
                    events.append(payload)
                    yield self._sse(payload)
                    if isinstance(payload, dict):
                        if payload.get("type") == "result":
                            result_data = payload.get("data")
                        elif payload.get("type") == "rejected":
                            # 数据范围拒答：记录标记，落库/审计走下方专属出口
                            rejected = True
                            reject_message = payload.get("message") or REJECT_USER_MESSAGE
                elif mode == "values":
                    final_state = payload
        except Exception as e:
            logger.exception(f"问数图执行失败: {e}")
            yield self._sse({"type": "error", "message": str(e)})
            # 失败也要审计：记录可读错误原因（不含敏感信息），异步落库
            self._record_audit(
                start=start,
                user_id=user_id,
                conversation_id=conversation_id,
                question=query_text,
                sql_text=None,
                status=AuditStatus.ERROR,
                error_message=str(e),
            )
            return

        # 拒答出口：不写查询缓存（无结果集），落库友好话术供多轮上下文使用，
        # 审计记 success + 拒答原因（audit_log.status 为 enum('success','error')，不改表结构）
        if rejected:
            await self._save_assistant_message(
                conversation_id, user_id, sql=None, result_data=None, content=reject_message
            )
            self._record_audit(
                start=start,
                user_id=user_id,
                conversation_id=conversation_id,
                question=query_text,
                sql_text=None,
                status=AuditStatus.SUCCESS,
                error_message="rejected:out_of_scope",
            )
            return

        # 自省闭环下优先取 final_sql（最终执行成功的 SQL），未成功时回退到最后一次尝试的 sql
        if isinstance(final_state, dict):
            sql = final_state.get("final_sql") or final_state.get("sql")
        else:
            sql = None

        # 回写缓存：仅成功产出结果的无历史单轮问题才写入
        if cache is not None and cache_key is not None and result_data is not None:
            value = self._jsonable(
                {"events": events, "sql": sql, "result_data": result_data}
            )
            await cache.set(cache_key, value)
            logger.info(f"写入查询缓存: key={cache_key[:12]}")

        await self._save_assistant_message(conversation_id, user_id, sql, result_data)
        # 成功出口审计：记录最终 SQL 与整体耗时
        self._record_audit(
            start=start,
            user_id=user_id,
            conversation_id=conversation_id,
            question=query_text,
            sql_text=sql,
            status=AuditStatus.SUCCESS,
            error_message=None,
        )

    def _record_audit(
        self,
        *,
        start: float,
        user_id: int,
        conversation_id: int,
        question: str,
        sql_text: str | None,
        status: AuditStatus,
        error_message: str | None,
    ) -> None:
        """统一出口：输出含 user_id/request_id/duration_ms 的结构化日志，并异步落审计。

        审计写入走独立 session（见 audit_service.schedule_audit_log），
        与主流程事务解耦，且写入失败仅记日志、绝不向上抛。
        """
        duration_ms = int((time.perf_counter() - start) * 1000)
        request_id = request_id_context_var.get()
        logger.bind(
            user_id=user_id, request_id=request_id, duration_ms=duration_ms
        ).info(
            f"查询审计 status={status} user_id={user_id} "
            f"duration_ms={duration_ms} conversation_id={conversation_id}"
        )
        schedule_audit_log(
            user_id=user_id,
            request_id=request_id,
            conversation_id=conversation_id,
            question=question,
            sql_text=sql_text,
            status=status,
            error_message=error_message,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _sse(payload: dict) -> str:
        """事件 -> SSE 帧（保持既有 data: {json} 加空行的协议不变）。"""
        return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    @staticmethod
    def _jsonable(obj):
        """转成可 JSON 序列化的结构（datetime 等经 default=str 归一），
        保证内存/Redis 两种缓存后端存储一致、可安全反序列化。"""
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))

    async def _save_assistant_message(
        self, conversation_id: int, user_id: int, sql, result_data, content: str | None = None
    ) -> None:
        """落库 assistant 消息（content=SQL, sql_text=SQL, result_json=结果）；失败仅记日志、
        不影响主流程。命中缓存时同样落库，保证会话历史与多轮上下文正确。

        content 显式传入时不覆写（拒答场景落库的是友好话术而非空 SQL）。
        """
        try:
            await self.conversation_service.append_assistant_message(
                conversation_id,
                user_id,
                content=content if content is not None else (sql or ""),
                sql_text=sql,
                result_json=result_data,
            )
        except Exception as e:
            logger.exception(f"落库 assistant 消息失败(不影响主流程): {e}")
