"""应用统一业务异常体系。

- BizException：业务异常基类，携带 (code, message, http_status)；
  错误码集中定义在 app/core/error_code.py，禁止散落魔法数字。
- 子类覆盖默认 code/http_status/message，业务代码按语义抛出即可；
  全局异常处理器会统一转为 ApiResponse 响应。
- 异常信息不得泄露内部实现细节；鉴权异常携带标准 WWW-Authenticate 头。
"""
from __future__ import annotations

from app.core.error_code import ErrorCode


class BizException(Exception):
    """业务异常基类。

    Attributes:
        code: 业务错误码(见 app/core/error_code.py)，默认 50000。
        message: 面向用户的错误提示(不得泄露内部实现细节)。
        http_status: 对应 HTTP 状态码。
        headers: 附加响应头(如鉴权所需的 WWW-Authenticate)。
    """

    http_status: int = 500
    code: int = ErrorCode.INTERNAL_ERROR
    message: str = "服务异常"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: int | None = None,
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.headers = headers
        super().__init__(self.message)


class ParamException(BizException):
    """参数错误(400)。"""

    http_status = 400
    code = ErrorCode.PARAM_ERROR
    message = "请求参数错误"


class UnauthorizedException(BizException):
    """未认证(401)：默认携带标准 WWW-Authenticate: Bearer 头。"""

    http_status = 401
    code = ErrorCode.UNAUTHENTICATED
    message = "未认证"

    def __init__(self, message: str | None = None, *, code: int | None = None) -> None:
        super().__init__(message, code=code, headers={"WWW-Authenticate": "Bearer"})


class ForbiddenException(BizException):
    """无权限(403)。"""

    http_status = 403
    code = ErrorCode.FORBIDDEN
    message = "权限不足"


class NotFoundException(BizException):
    """资源不存在(404)。"""

    http_status = 404
    code = ErrorCode.RESOURCE_NOT_FOUND
    message = "资源不存在"


class ConflictException(BizException):
    """业务冲突(409)。"""

    http_status = 409
    code = ErrorCode.BUSINESS_CONFLICT
    message = "业务冲突"


class ServiceException(BizException):
    """服务异常(500)。"""

    http_status = 500
    code = ErrorCode.INTERNAL_ERROR
    message = "服务异常"


class RateLimitException(BizException):
    """请求过于频繁(429)：携带标准 Retry-After 头提示重试等待秒数。"""

    http_status = 429
    code = ErrorCode.TOO_MANY_REQUESTS
    message = "请求过于频繁，请稍后重试"

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__(message, headers={"Retry-After": str(self.retry_after)})
