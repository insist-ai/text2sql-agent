from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.embedding_client_manager import embedding_client_manager, CustomEmbeddings
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import meta_mysql_client_manager, dw_mysql_client_manager, app_mysql_client_manager
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.conf.app_config import app_config
from app.core.error_code import ErrorCode
from app.core.exceptions import (
    ForbiddenException,
    RateLimitException,
    UnauthorizedException,
)
from app.core.log import logger
from app.core.query_cache import QueryCache, get_query_cache
from app.core.rate_limit import get_limiter, get_rate_limiter
from app.core.security import TokenError, decode_token
from app.models.user import UserMySQL
from app.repositories.es.column_es_repository import ColumnESRepository
from app.repositories.es.metric_es_repository import MetricESRepository
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.app.conversation_repository import ConversationRepository
from app.repositories.mysql.app.message_repository import MessageRepository
from app.repositories.mysql.app.user_repository import UserRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.repositories.qdrant.value_qdrant_repository import ValueQdrantRepository
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.conversation_service import ConversationService
from app.services.query_service import QueryService


async def get_meta_session():
    async with meta_mysql_client_manager.session_factory() as session:
        yield session

async def get_dw_session():
    async with dw_mysql_client_manager.session_factory() as session:
        yield session

async def get_embedding_client():
    return embedding_client_manager.client

async def get_column_qdrant_repository():
    return ColumnQdrantRepository(qdrant_client_manager.client)

async def get_value_es_repository():
    return ValueESRepository(es_client_manager.client)

async def get_metric_qdrant_repository():
    return MetricQdrantRepository(qdrant_client_manager.client)

async def get_value_qdrant_repository():
    return ValueQdrantRepository(qdrant_client_manager.client)

async def get_column_es_repository():
    return ColumnESRepository(es_client_manager.client)

async def get_metric_es_repository():
    return MetricESRepository(es_client_manager.client)

async def get_meta_mysql_repository(session: AsyncSession = Depends(get_meta_session)):
    return MetaMySQLRepository(session)

async def get_dw_mysql_repository(session: AsyncSession = Depends(get_dw_session)):
    return DWMySQLRepository(session)


# 认证模块依赖
async def get_app_session():
    # 仅负责连接生命周期与异常回滚；不做 yield 后的自动 commit。
    # FastAPI 在响应发出之后才恢复 yield 依赖，若在此处 commit，
    # 客户端的紧接请求可能在提交生效前读到旧快照（读己之写失败）。
    # 写事务统一由 service 层在用例返回前显式 commit。
    async with app_mysql_client_manager.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_user_repository(session: AsyncSession = Depends(get_app_session)):
    return UserRepository(session)


async def get_auth_service(
    session: AsyncSession = Depends(get_app_session),
    user_repository: UserRepository = Depends(get_user_repository),
) -> AuthService:
    return AuthService(session=session, user_repository=user_repository)


async def get_query_service(
    session: AsyncSession = Depends(get_app_session),
    embedding_client: CustomEmbeddings = Depends(get_embedding_client),
    column_qdrant_repository: ColumnQdrantRepository = Depends(get_column_qdrant_repository),
    value_es_repository: ValueESRepository = Depends(get_value_es_repository),
    metric_qdrant_repository: MetricQdrantRepository = Depends(get_metric_qdrant_repository),
    value_qdrant_repository: ValueQdrantRepository = Depends(get_value_qdrant_repository),
    column_es_repository: ColumnESRepository = Depends(get_column_es_repository),
    metric_es_repository: MetricESRepository = Depends(get_metric_es_repository),
    meta_mysql_repository: MetaMySQLRepository = Depends(get_meta_mysql_repository),
    dw_mysql_repository: DWMySQLRepository = Depends(get_dw_mysql_repository),
    query_cache: QueryCache | None = Depends(get_query_cache),
) -> QueryService:
    # 与 get_conversation_service 共享同一个 app 库 session：查询过程中写会话/消息
    # 与鉴权归属校验处于同一事务视图
    conversation_service = ConversationService(
        session,
        ConversationRepository(session),
        MessageRepository(session),
    )
    return QueryService(
        embedding_client=embedding_client,
        column_qdrant_repository=column_qdrant_repository,
        value_es_repository=value_es_repository,
        metric_qdrant_repository=metric_qdrant_repository,
        value_qdrant_repository=value_qdrant_repository,
        column_es_repository=column_es_repository,
        metric_es_repository=metric_es_repository,
        meta_mysql_repository=meta_mysql_repository,
        dw_mysql_repository=dw_mysql_repository,
        session=session,
        conversation_service=conversation_service,
        query_cache=query_cache,
    )


# 会话模块依赖：两个仓储共享同一个 app 库 session，保证跨仓储操作在同一事务内
async def get_conversation_service(
    session: AsyncSession = Depends(get_app_session),
) -> ConversationService:
    return ConversationService(
        session,
        ConversationRepository(session),
        MessageRepository(session),
    )


# 审计模块依赖：管理端查询走请求 session
async def get_audit_service(
    session: AsyncSession = Depends(get_app_session),
) -> AuditService:
    return AuditService(session)


# 鉴权与 RBAC 依赖
async def get_current_user(
    user_repository: UserRepository = Depends(get_user_repository),
    authorization: str | None = Header(default=None),
) -> UserMySQL:
    """解析 Authorization: Bearer <access_token>，校验通过返回当前用户对象。

    token 缺失/格式错误/过期/非法/类型不匹配均抛 401(带 WWW-Authenticate 头)，
    并区分提示(令牌已过期 / 无效的令牌 / 令牌类型不匹配 / 缺少认证令牌 ...)。
    """
    if not authorization:
        raise UnauthorizedException("缺少认证令牌", code=ErrorCode.UNAUTHENTICATED)
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthorizedException("认证头格式错误", code=ErrorCode.UNAUTHENTICATED)
    token = parts[1]
    try:
        payload = decode_token(token, "access")
    except TokenError as e:
        # 区分"令牌已过期"/"无效的令牌"/"令牌类型不匹配"
        raise UnauthorizedException(str(e), code=ErrorCode.TOKEN_INVALID)
    user_id = int(payload["sub"])
    user = await user_repository.get_by_id(user_id)
    if user is None:
        raise UnauthorizedException("用户不存在", code=ErrorCode.UNAUTHENTICATED)
    if not user.is_active:
        raise UnauthorizedException("账号已被禁用", code=ErrorCode.ACCOUNT_DISABLED)
    return user


def require_role(*roles: str):
    """依赖工厂：限制接口仅允许指定角色访问，不满足抛 403。

    用法: Depends(require_role("admin")) 或 Depends(require_role("admin","operator"))
    """
    required = set(roles)

    async def _role_checker(
        current_user: UserMySQL = Depends(get_current_user),
    ) -> UserMySQL:
        if str(current_user.role) not in required:
            raise ForbiddenException("权限不足")
        return current_user

    return _role_checker


# 限流依赖：按「用户 + 接口」维度做滑动窗口限流
async def enforce_rate_limit(
    request: Request,
    current_user: UserMySQL = Depends(get_current_user),
) -> None:
    """对当前用户访问当前接口做滑动窗口限流，超限抛 RateLimitException(429)。

    作为路由依赖注入执行，位于返回 StreamingResponse 之前，不介入/不缓冲流式响应体，
    因此不会影响 SSE 的流式返回。限流关闭时``get_rate_limiter()``返回 None，直接放行。
    """
    limiter = get_rate_limiter()
    if limiter is None:
        return
    key = f"{current_user.id}:{request.url.path}"
    allowed, retry_after = await limiter.is_allowed(key)
    if not allowed:
        logger.warning(
            f"触发限流: user_id={current_user.id} path={request.url.path} retry_after={retry_after}s"
        )
        raise RateLimitException(retry_after=retry_after)


def get_client_ip(request: Request) -> str:
    """取客户端 IP 作为未认证请求的限流维度。

    优先取反向代理透传的 X-Forwarded-For 首段（最接近真实客户端的一跳），
    否则回退到 TCP 直连地址；拿不到时统一归入 "unknown" 兜底，避免抛错。
    注意：XFF 可被伪造，生产环境应由可信反代重写该头；本项目单机/本地演示够用。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


async def _peek_username(request: Request) -> str:
    """从请求体里"窥探"username，用于把登录限流 key 精确到账号。

    Starlette 会缓存已读取的 body（``request._body``），这里读一次不会影响
    后续 FastAPI 对本体的解析。任何异常（非 JSON / 空体）都回退为 "anon"，
    此时同 IP 的匿名请求共用一个桶，仍能起到兜底限流作用。
    """
    try:
        body = await request.json()
    except Exception:
        return "anon"
    if isinstance(body, dict):
        username = body.get("username")
        if username:
            return str(username).strip().lower()
    return "anon"


# 限流依赖：认证入口（登录/注册）专用。此时尚无 token，不能依赖 get_current_user。
async def enforce_auth_rate_limit(request: Request) -> None:
    """对登录/注册做滑动窗口限流，超限抛 RateLimitException(429)。

    与 ``enforce_rate_limit`` 的关键差异：
    - **不依赖 get_current_user**：登录/注册发生在签发 token 之前，若依赖认证
      会直接抛 401「缺少认证令牌」，把接口本身打挂；
    - **按客户端 IP（登录再拼用户名）计数**：登录用 ``login:{ip}:{username}``
      精准防单账号密码爆破，注册用 ``register:{ip}`` 防批量刷号；
    - **额度取认证分段配置**（``rate_limit.auth``），比业务接口更严。

    在路由依赖注入阶段执行，早于业务逻辑与 token 签发，命中即快速失败。
    """
    cfg = app_config.rate_limit
    if not cfg.enable:
        return

    ip = get_client_ip(request)
    if request.url.path.endswith("/login"):
        max_requests = cfg.auth.login_max_requests
        key = f"login:{ip}:{await _peek_username(request)}"
    else:
        max_requests = cfg.auth.register_max_requests
        key = f"register:{ip}"

    limiter = get_limiter(cfg.window_seconds, max_requests)
    if limiter is None:
        return
    allowed, retry_after = await limiter.is_allowed(key)
    if not allowed:
        logger.warning(
            f"触发认证限流: path={request.url.path} key={key} retry_after={retry_after}s"
        )
        raise RateLimitException(retry_after=retry_after)