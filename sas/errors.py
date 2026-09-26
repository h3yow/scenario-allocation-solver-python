"""异常分类:所有可预期失败都映射到这里的具体类型。"""
from __future__ import annotations


class SolverError(Exception):
    """求解服务异常基类。"""


class InvalidRequestError(SolverError):
    """请求参数非法(结构、取值、一致性校验失败)。"""


class InfeasibleError(SolverError):
    """问题可行域为空:容量/最小保障/总量守恒约束无法同时满足。"""

    def __init__(self, reason: str = "", details: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


class ConflictError(SolverError):
    """同一幂等键携带了不同的请求体。"""


class SolveTimeoutError(SolverError):
    """求解超过配置的时间预算。"""


class SolverCancelledError(SolverError):
    """求解被主动取消(区别于超时)。"""


class SolverFailedError(SolverError):
    """求解器内部错误。"""
