"""异常分类：参数非法 / 冲突 / 不可行 / 超时 / 取消 / 求解失败 / 未找到。

所有失败原因均可通过 ``code`` 区分，API 层据此映射不同的 HTTP 状态码。
"""

from __future__ import annotations


class ErrorCode:
    INVALID_REQUEST = "INVALID_REQUEST"
    INFEASIBLE = "INFEASIBLE"
    CONFLICT = "CONFLICT"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    SOLVER_FAILURE = "SOLVER_FAILURE"
    NOT_FOUND = "NOT_FOUND"


class AppError(Exception):
    code: str = "APP_ERROR"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class InvalidRequestError(AppError):
    code = ErrorCode.INVALID_REQUEST
    http_status = 400


class InfeasibleError(AppError):
    code = ErrorCode.INFEASIBLE
    http_status = 422


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    http_status = 409


class TimeoutError(AppError):  # noqa: A001 - 领域语义明确，覆盖内建名
    code = ErrorCode.TIMEOUT
    http_status = 504


class CancelledError(AppError):
    code = ErrorCode.CANCELLED
    http_status = 499


class SolverFailureError(AppError):
    code = ErrorCode.SOLVER_FAILURE
    http_status = 500


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404
