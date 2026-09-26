"""FastAPI HTTP 适配层。

路由:
  POST /api/v1/optimizations                 提交(或复用)优化请求
  GET  /api/v1/optimizations/{key}           查询状态与结果
  POST /api/v1/optimizations/{key}/cancel    主动取消
  GET  /health                               健康检查

异常 -> HTTP 状态码:
  InvalidRequestError 400 / ConflictError 409 /
  SolveTimeoutError 408 / SolverCancelledError 499 /
  InfeasibleError / SolverFailedError 422(不可行) / 500
  未知键 404;仍在求解 200(status=RUNNING/PENDING,result=null)
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    ConflictError,
    InfeasibleError,
    InvalidRequestError,
    SolverCancelledError,
    SolverFailedError,
    SolveTimeoutError,
)
from .models import Entity, Problem, ScenarioInput, SolverConfig, Window
from .service import AllocationService
from .store import Status

logger = logging.getLogger("sas.api")


# ---------- 请求/响应模型 ----------
class EntityIn(BaseModel):
    id: str
    min_guarantee: float = Field(ge=0)
    capacity: float = Field(ge=0)


class WindowIn(BaseModel):
    name: str
    total_supply: float = Field(ge=0)


class ScenarioIn(BaseModel):
    name: str
    # window -> entity -> demand
    demands: dict[str, dict[str, float]] = Field(default_factory=dict)


class ConfigIn(BaseModel):
    solve_timeout: float = 5.0
    tolerance: float = 1e-9
    max_iterations: int = 10000


class OptimizeRequest(BaseModel):
    idempotency_key: str
    baseline_name: str = "baseline"
    entities: list[EntityIn]
    windows: list[WindowIn]
    scenarios: list[ScenarioIn]
    config: ConfigIn | None = None


def _to_problem(req: OptimizeRequest) -> Problem:
    return Problem(
        baseline_name=req.baseline_name,
        entities=[Entity(e.id, e.min_guarantee, e.capacity) for e in req.entities],
        windows=[Window(w.name, w.total_supply) for w in req.windows],
        scenarios=[
            ScenarioInput(s.name, s.demands) for s in req.scenarios
        ],
    )


def _to_config(req: OptimizeRequest) -> SolverConfig:
    c = req.config or ConfigIn()
    return SolverConfig(
        solve_timeout=c.solve_timeout,
        tolerance=c.tolerance,
        max_iterations=c.max_iterations,
    )


def _result_body(status: Status, result: object | None) -> dict:
    if result is None:
        return {"status": status.value, "result": None}
    from dataclasses import asdict

    return {"status": status.value, "result": asdict(result)}


def create_app(service: AllocationService | None = None) -> FastAPI:
    """构造 FastAPI 应用,注入服务实例(默认配置)。"""
    app = FastAPI(
        title="scenario-allocation-solver",
        version="0.1.0",
    )
    svc = service or AllocationService()

    # ---------- 异常处理 ----------
    def _error(code: str, message: str, status_code: int, **extra: object) -> JSONResponse:
        body = {"error": {"code": code, "message": message}}
        body["error"].update(extra)
        return JSONResponse(status_code=status_code, content=body)

    @app.exception_handler(InvalidRequestError)
    def _invalid(_req: object, exc: InvalidRequestError) -> JSONResponse:
        return _error("INVALID_REQUEST", str(exc), 400)

    @app.exception_handler(ConflictError)
    def _conflict(_req: object, exc: ConflictError) -> JSONResponse:
        return _error("IDEMPOTENCY_CONFLICT", str(exc), 409)

    @app.exception_handler(InfeasibleError)
    def _infeasible(_req: object, exc: InfeasibleError) -> JSONResponse:
        return _error("INFEASIBLE", str(exc), 422, details=exc.details)

    @app.exception_handler(SolveTimeoutError)
    def _timeout(_req: object, exc: SolveTimeoutError) -> JSONResponse:
        return _error("SOLVE_TIMEOUT", str(exc), 408)

    @app.exception_handler(SolverCancelledError)
    def _cancelled(_req: object, exc: SolverCancelledError) -> JSONResponse:
        return _error("CANCELLED", str(exc), 499)

    @app.exception_handler(SolverFailedError)
    def _failed(_req: object, exc: SolverFailedError) -> JSONResponse:
        return _error("SOLVER_FAILED", str(exc), 500)

    # ---------- 路由 ----------
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/v1/optimizations")
    def submit_optimization(req: OptimizeRequest) -> dict:
        problem = _to_problem(req)
        config = _to_config(req)
        status, result = svc.submit(req.idempotency_key, problem, config)
        logger.info(
            "HTTP 提交响应 | key=%s status=%s", req.idempotency_key, status.value
        )
        return _result_body(status, result)

    @app.get("/api/v1/optimizations/{key}")
    def get_optimization(key: str) -> JSONResponse:
        try:
            status, result = svc.get_result(key)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {"code": "NOT_FOUND", "message": f"幂等键 {key!r} 不存在"}
                },
            )
        return JSONResponse(content=_result_body(status, result))

    @app.post("/api/v1/optimizations/{key}/cancel")
    def cancel_optimization(key: str) -> dict:
        try:
            status = svc.cancel(key)
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "NOT_FOUND", "message": f"幂等键 {key!r} 不存在"}},
            )
        return {"status": status.value}

    return app
