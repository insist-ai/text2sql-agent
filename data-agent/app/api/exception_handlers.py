"""全局异常处理器：把异常统一转换为 ApiResponse 响应。

覆盖三类异常：
- BizException：业务异常，按异常自带 (code, message, http_status, headers) 返回；
- RequestValidationError：pydantic 参数校验失败，统一 400 + PARAM_ERROR；
- Exception：兜底，统一 500 + INTERNAL_ERROR，完整堆栈仅记录日志，不返回给客户端。

SSE 流式接口 /api/query 显式排除：其错误帧按 SSE 协议下发，不套用 ApiResponse。
日志格式已由 app/core/log.py 注入 request_id，异常日志会携带 request_id。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.schemas.common import ApiResponse
from app.core.error_code import ErrorCode
from app.core.exceptions import BizException, RateLimitException
from app.core.log import logger

# SSE 流式接口路径白名单：不套用 ApiResponse，错误以 SSE 帧下发
SSE_PATHS: frozenset[str] = frozenset({"/api/query"})


def _is_sse_request(request: Request) -> bool:
    """判定当前请求是否为 SSE 流式接口（需显式排除统一响应体）。"""
    return request.url.path in SSE_PATHS


def _validation_message(exc: RequestValidationError) -> str:
    """把 pydantic 校验错误压成一行可读提示（不泄露内部实现细节）。"""
    details: list[str] = []
    for err in exc.errors():
        loc = [str(item) for item in err.get("loc", ()) if item not in ("body", "query", "path")]
        field = ".".join(loc) or "请求体"
        details.append(f"{field}: {err.get('msg', '校验失败')}")
    return f"参数校验失败 - {'; '.join(details)}" if details else "参数校验失败"


def _json_response(
    *,
    http_status: int,
    code: int,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造统一 JSON 错误响应体 {code, message, data=None}。"""
    body = ApiResponse[None](code=int(code), message=message, data=None)
    return JSONResponse(
        status_code=http_status,
        content=body.model_dump(),
        headers=headers or {},
    )


def _sse_error_response(code: int, message: str) -> StreamingResponse:
    """构造 SSE 错误帧（兼容既有 {'type':'error'} 协议，附加 code 字段）。"""
    payload = {"type": "error", "code": int(code), "message": message}

    async def _event_stream() -> AsyncIterator[str]:
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


def register_exception_handlers(app: FastAPI) -> None:
    """在 FastAPI 实例上注册全局异常处理器。"""

    @app.exception_handler(RateLimitException)
    async def _rate_limit_exception_handler(
        request: Request, exc: RateLimitException
    ) -> JSONResponse:
        # 限流是"流式响应开始前"的拒绝：无论是否 SSE 接口都返回统一 JSON 429，
        # 并带 Retry-After 头供客户端退避重试（不再套用 SSE 错误帧）。
        logger.warning(f"限流返回 429: path={request.url.path} retry_after={exc.retry_after}s")
        return _json_response(
            http_status=exc.http_status,
            code=exc.code,
            message=exc.message,
            headers=exc.headers,
        )

    @app.exception_handler(BizException)
    async def _biz_exception_handler(
        request: Request, exc: BizException
    ) -> JSONResponse | StreamingResponse:
        # 业务异常属预期内，记录 warning（request_id 由日志格式自动注入）
        logger.warning(
            f"业务异常 code={int(exc.code)} http_status={exc.http_status} message={exc.message}"
        )
        if _is_sse_request(request):
            return _sse_error_response(exc.code, exc.message)
        return _json_response(
            http_status=exc.http_status,
            code=exc.code,
            message=exc.message,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse | StreamingResponse:
        message = _validation_message(exc)
        logger.warning(f"参数校验失败: {message}")
        if _is_sse_request(request):
            return _sse_error_response(ErrorCode.PARAM_ERROR, message)
        return _json_response(
            http_status=400,
            code=ErrorCode.PARAM_ERROR,
            message=message,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse | StreamingResponse:
        # 完整堆栈仅记录在日志（logger.exception 带 traceback），绝不返回给客户端
        logger.exception(f"未捕获异常: {exc!r}")
        message = "服务异常，请稍后重试"
        if _is_sse_request(request):
            return _sse_error_response(ErrorCode.INTERNAL_ERROR, message)
        return _json_response(
            http_status=500,
            code=ErrorCode.INTERNAL_ERROR,
            message=message,
        )
