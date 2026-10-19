import uuid

from fastapi import FastAPI, Request

from app.api.exception_handlers import register_exception_handlers
from app.api.lifespan import lifespan
from app.api.routers.audit_router import audit_router
from app.api.routers.auth_router import auth_router
from app.api.routers.conversation_router import conversation_router
from app.api.routers.demo_router import demo_router
from app.api.routers.query_router import query_router
from app.core.context import request_id_context_var

app = FastAPI(lifespan=lifespan)

app.include_router(query_router)
app.include_router(auth_router)
app.include_router(conversation_router)
app.include_router(demo_router)
app.include_router(audit_router)

# 注册全局异常处理器：统一把异常转为 ApiResponse（/api/query 的 SSE 响应显式排除）
register_exception_handlers(app)


# 每个请求生成唯一 request_id：注入上下文供日志打点，并回写响应头便于排查
@app.middleware("http")
async def add_request_id_header(request: Request, call_next):
    request_id = uuid.uuid4().hex
    request_id_context_var.set(request_id)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response
