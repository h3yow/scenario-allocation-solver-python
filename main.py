"""服务入口：uvicorn main:app 启动。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import build_router, install_exception_handlers
from app.logging_setup import configure_logging, get_logger
from app.service.idempotency import IdempotencyStore
from app.service.optimizer import OptimizerService, ServiceConfig

logger = get_logger("main")


def create_app(
    service: OptimizerService | None = None,
    config: ServiceConfig | None = None,
) -> FastAPI:
    configure_logging()
    app = FastAPI(title="Scenario Allocation Solver", version="0.1.0")

    svc = service or OptimizerService(IdempotencyStore(), config or ServiceConfig())
    app.state.service = svc
    app.include_router(build_router(svc))
    install_exception_handlers(app)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        # 参数非法：统一 400 + INVALID_REQUEST，错误体带字段定位。
        logger.info("参数校验失败 path=%s errors=%s", request.url.path, exc.errors())
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "请求参数不合法",
                    "details": {"validation": exc.errors()},
                }
            },
        )

    @app.get("/")
    def read_root() -> dict:
        return {"service": "scenario-allocation-solver", "status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
