"""FastAPI 路由：提交 / 查询 / 取消，并把领域异常映射为可区分的失败响应。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.errors import AppError, ConflictError, NotFoundError
from app.logging_setup import get_logger
from app.models import OptimizeRequest, OptimizeResponse, ReplanRequest
from app.service.idempotency import JobStatus
from app.service.optimizer import OptimizerService

logger = get_logger("app.api")

# RUNNING/FAILED 时查询接口的轮询间隔上限。
_POLL_WAIT_SECONDS = 5.0


def build_router(service: OptimizerService) -> APIRouter:
    router = APIRouter()

    def _record_response(record) -> JSONResponse | OptimizeResponse:
        if record.status is JobStatus.SUCCEEDED and record.result is not None:
            return record.result
        status_code = 202 if record.status is JobStatus.RUNNING else 200
        body: dict = {
            "idempotency_key": record.key,
            "status": record.status.value,
            "request_fingerprint": record.fingerprint,
        }
        if record.status is JobStatus.FAILED:
            body["error"] = {
                "code": record.error_code,
                "message": record.error_message,
                "details": record.error_details,
            }
        return JSONResponse(status_code=status_code, content=body)

    @router.post("/optimizations", response_model=OptimizeResponse)
    def submit_optimization(
        payload: OptimizeRequest,
        request: Request,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        try:
            outcome = service.submit(idempotency_key.strip(), payload)
        except ConflictError:
            raise
        record = outcome.record
        if record.status is JobStatus.SUCCEEDED and record.result is not None:
            # 重复提交且已有等价结果：幂等返回同一份结果（200）。
            logger.info(
                "幂等命中 key=%s 返回既有结果（等价请求）", idempotency_key
            )
            return record.result

        # 首次提交（后台求解中）：202 Accepted。
        return JSONResponse(
            status_code=202,
            content={
                "idempotency_key": record.key,
                "status": record.status.value,
                "request_fingerprint": record.fingerprint,
                "message": "作业已受理，使用 GET 查询结果",
            },
        )

    @router.get("/optimizations/{key}", response_model=OptimizeResponse)
    def get_optimization(key: str):
        record = service.get_result(key)
        return _record_response(record)

    @router.post("/replans", response_model=OptimizeResponse)
    def submit_replan(
        payload: ReplanRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        """带已确认额度的重规划：接续一份既有成功方案，只重排未确认部分。"""
        try:
            outcome = service.submit_replan(idempotency_key.strip(), payload)
        except ConflictError:
            raise
        record = outcome.record
        if record.status is JobStatus.SUCCEEDED and record.result is not None:
            logger.info(
                "重规划幂等命中 key=%s based_on=%s 返回既有结果",
                idempotency_key, payload.based_on_key,
            )
            return record.result

        return JSONResponse(
            status_code=202,
            content={
                "idempotency_key": record.key,
                "status": record.status.value,
                "request_fingerprint": record.fingerprint,
                "based_on_key": payload.based_on_key,
                "message": "重规划作业已受理，使用 GET 查询结果",
            },
        )

    @router.post("/optimizations/{key}/cancel")
    def cancel_optimization(key: str):
        record = service.cancel(key)
        accepted = record.status is JobStatus.RUNNING
        return JSONResponse(
            status_code=202 if accepted else 200,
            content={
                "idempotency_key": key,
                "status": record.status.value,
                "cancel_requested": accepted,
                "message": (
                    "取消信号已置位，求解将在下一检查点中止"
                    if accepted
                    else "作业不在运行中，取消请求未生效"
                ),
            },
        )

    return router


def install_exception_handlers(app) -> None:
    """把领域异常统一翻译为带可区分 code 的 JSON 错误体。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        logger.info(
            "请求失败 path=%s code=%s message=%s",
            request.url.path,
            exc.code,
            exc.message,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_dict()},
        )

    return app
