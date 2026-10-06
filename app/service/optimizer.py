"""优化编排服务：提交、查询、取消，超时/取消/失败的区分与恢复。

- 提交走 IdempotencyStore.begin：并发/重复提交只有一个后台线程真正求解。
- 参数非法(400)/不可行(422) 在登记前同步判定，不产生作业；
  超时(504)/取消(499)/求解失败(500) 在后台线程中落为 FAILED 终态，
  不污染已成功结果，且允许同键同指纹重试。
- 超时与主动取消通过引擎检查点严格区分（取消优先）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass

from app.errors import AppError, InvalidRequestError, NotFoundError
from app.logging_setup import get_logger
from app.models import OptimizeRequest, ReplanRequest
from app.service.idempotency import IdempotencyStore, JobRecord, JobStatus
from app.service.mapper import (
    build_response,
    replan_request_to_parts,
    request_to_problem,
)
from app.solver.engine import SOLVER_VERSION, solve
from app.solver.replan import replan, validate_replan_structure
from app.solver.validate import validate_problem

logger = get_logger("app.service.optimizer")


@dataclass
class ServiceConfig:
    default_timeout_seconds: float | None = None
    max_timeout_seconds: float | None = None
    solver_step_delay: float = 0.0


@dataclass
class SubmitOutcome:
    record: JobRecord
    started: bool  # 本次调用是否启动了新的求解


class OptimizerService:
    def __init__(
        self,
        store: IdempotencyStore | None = None,
        config: ServiceConfig | None = None,
    ) -> None:
        self._store = store or IdempotencyStore()
        self._config = config or ServiceConfig()

    @property
    def store(self) -> IdempotencyStore:
        return self._store

    @staticmethod
    def fingerprint(request: OptimizeRequest) -> str:
        """对求解相关输入与配置做稳定指纹（canonical JSON + sha256）。

        只纳入影响结果的字段与求解器版本，保证“同输入+同配置+稳定版本”
        产生同一指纹与等价结果。
        """
        payload = {
            "solver_version": SOLVER_VERSION,
            "entities": [e.model_dump() for e in request.entities],
            "windows": [w.model_dump() for w in request.windows],
            "scenarios": [s.model_dump() for s in request.scenarios],
            "quantize_decimals": request.quantize_decimals,
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def replan_fingerprint(request: ReplanRequest) -> str:
        """重规划指纹：含最新输入、确认额度与所接续的既有方案键。

        ``based_on_key`` 也纳入指纹：同样的新输入从不同既有方案延续属于
        不同的重规划请求（谱系不同，确认额度的含义也不同）。
        """
        payload = {
            "kind": "replan",
            "solver_version": SOLVER_VERSION,
            "based_on_key": request.based_on_key,
            "entities": [e.model_dump() for e in request.entities],
            "windows": [w.model_dump() for w in request.windows],
            "scenarios": [s.model_dump() for s in request.scenarios],
            "confirmed_allocations": [
                c.model_dump() for c in request.confirmed_allocations
            ],
            "quantize_decimals": request.quantize_decimals,
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _effective_timeout(self, request: OptimizeRequest) -> float | None:
        timeout = request.timeout_seconds
        if timeout is None:
            timeout = self._config.default_timeout_seconds
        if (
            self._config.max_timeout_seconds is not None
            and timeout is not None
            and timeout > self._config.max_timeout_seconds
        ):
            logger.info(
                "请求超时 %.3fs 超过上限 %.3fs，钳制为上限",
                timeout,
                self._config.max_timeout_seconds,
            )
            timeout = self._config.max_timeout_seconds
        return timeout

    def submit(self, key: str, request: OptimizeRequest) -> SubmitOutcome:
        """提交（或复用）一个优化作业；新作业在后台守护线程求解。

        参数非法(INVALID_REQUEST)在登记前同步抛出，不产生作业、不占幂等键；
        不可行(INFEASIBLE)属于合法输入的优化结果，登记后由后台判定落 FAILED。
        """
        # 登记前同步结构校验：非法请求不产生作业。
        validate_problem(request_to_problem(request))

        fp = self.fingerprint(request)
        logger.info(
            "收到提交 key=%s fingerprint=%s timeout_req=%s",
            key,
            fp[:12],
            request.timeout_seconds,
        )

        record, started = self._store.begin(key, fp)
        if not started:
            return SubmitOutcome(record=record, started=False)

        timeout = self._effective_timeout(request)
        step_delay = request.solver_step_delay or self._config.solver_step_delay

        thread = threading.Thread(
            target=self._run_solver,
            name=f"solver:{key}",
            args=(key, request, fp, timeout, step_delay),
            daemon=True,
        )
        thread.start()
        logger.info(
            "已启动求解线程 key=%s thread=%s effective_timeout=%s",
            key,
            thread.name,
            timeout,
        )
        return SubmitOutcome(record=record, started=True)

    # ------------------------------------------------------------------
    # 带已确认额度的重规划
    # ------------------------------------------------------------------
    def submit_replan(self, key: str, request: ReplanRequest) -> SubmitOutcome:
        """提交一个接续既有成功方案的重规划作业。

        登记前同步判定（不产生作业、不占幂等键）：
        - 最新输入结构非法 / 确认额度结构非法 -> INVALID_REQUEST(400)；
        - 接续目标不存在 -> NOT_FOUND(404)；未成功或非优化结果
          -> INVALID_REQUEST(400)；
        - 确认额度超过该既有方案在对应格子的实发量
          -> INVALID_REQUEST(400)。
        确认额度与**最新输入**的冲突(412)、整体不可行(422)、超时/取消
        沿用后台求解语义，与普通提交一致。
        """
        problem, confirmed = replan_request_to_parts(request)
        validate_replan_structure(problem, confirmed)
        self._validate_based_on(request, confirmed)

        fp = self.replan_fingerprint(request)
        logger.info(
            "收到重规划提交 key=%s based_on=%s fingerprint=%s confirmed=%d",
            key, request.based_on_key, fp[:12], len(confirmed),
        )

        record, started = self._store.begin(key, fp)
        if not started:
            return SubmitOutcome(record=record, started=False)

        timeout = self._effective_timeout(request)
        step_delay = request.solver_step_delay or self._config.solver_step_delay

        thread = threading.Thread(
            target=self._run_replan,
            name=f"replan:{key}",
            args=(key, request, fp, timeout, step_delay),
            daemon=True,
        )
        thread.start()
        logger.info(
            "已启动重规划线程 key=%s thread=%s based_on=%s effective_timeout=%s",
            key, thread.name, request.based_on_key, timeout,
        )
        return SubmitOutcome(record=record, started=True)

    def _validate_based_on(self, request: ReplanRequest, confirmed) -> None:
        """核验接续目标存在且成功，且确认额度不超过既有方案的实发量。"""
        base = self._store.get(request.based_on_key)
        if base is None:
            raise NotFoundError(
                f"接续的既有方案 '{request.based_on_key}' 不存在",
                details={"based_on_key": request.based_on_key},
            )
        if base.status is not JobStatus.SUCCEEDED or base.result is None:
            raise InvalidRequestError(
                f"接续的既有方案 '{request.based_on_key}' 尚未成功"
                f"（当前状态 {base.status.value}），不能作为重规划基线",
                details={
                    "based_on_key": request.based_on_key,
                    "status": base.status.value,
                },
            )

        # 既有方案的实发矩阵（逐场景 entity@window -> amount）。
        actual: dict[tuple[str, str, str], float] = {}
        for s in base.result.scenarios:
            for w in s.window_allocations:
                for entity, amount in w.entities.items():
                    actual[(s.scenario, entity, w.window)] = amount

        over: list[dict] = []
        for c in confirmed:
            issued = actual.get((c.scenario, c.entity, c.window), 0.0)
            if c.amount > issued + 1e-9:
                over.append({
                    "scenario": c.scenario,
                    "entity": c.entity,
                    "window": c.window,
                    "confirmed": c.amount,
                    "issued_in_based_on": issued,
                })
        if over:
            raise InvalidRequestError(
                "确认额度超过了既有方案在对应格子上的实发量，"
                "确认只能基于已经真实下发的额度",
                details={"based_on_key": request.based_on_key, "violations": over},
            )
        logger.info(
            "重规划基线核验通过 based_on=%s，确认格子 %d 个均不超过实发量",
            request.based_on_key, len(confirmed),
        )

    def _run_replan(
        self,
        key: str,
        request: ReplanRequest,
        fp: str,
        timeout: float | None,
        step_delay: float,
    ) -> None:
        """后台执行重规划；任何异常都不向上游线程泄漏。"""
        record = self._store.get(key)
        if record is None:
            return
        try:
            logger.info(
                "开始重规划 key=%s based_on=%s scenarios=%d decimals=%d",
                key, request.based_on_key,
                len(request.scenarios), request.quantize_decimals,
            )
            problem, confirmed = replan_request_to_parts(request)
            result = replan(
                problem,
                confirmed,
                timeout_seconds=timeout,
                is_cancelled=record.cancel_event.is_set,
                step_delay=step_delay,
                quantize_decimals=request.quantize_decimals,
            )
            response = build_response(
                key, fp, result, based_on_key=request.based_on_key
            )
            self._store.succeed(key, response)
        except AppError as exc:
            logger.info(
                "重规划结束为失败 key=%s code=%s message=%s",
                key, exc.code, exc.message,
            )
            self._store.fail(key, exc.code, exc.message, exc.details)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须兜底
            logger.exception("重规划发生未预期异常 key=%s", key)
            self._store.fail(
                key,
                "SOLVER_FAILURE",
                f"重规划内部错误: {exc!r}",
                {"type": type(exc).__name__},
            )

    def get_result(self, key: str) -> JobRecord:
        """查询作业；不存在抛 NotFoundError。"""
        record = self._store.get(key)
        if record is None:
            raise NotFoundError("优化作业不存在", details={"idempotency_key": key})
        logger.info(
            "查询作业 key=%s status=%s", key, record.status.value
        )
        return record

    def cancel(self, key: str) -> JobRecord:
        """请求取消作业（仅 RUNNING 生效），返回当前记录。"""
        record = self.get_result(key)
        self._store.request_cancel(key)
        return record
        """请求取消作业（仅 RUNNING 生效），返回当前记录。"""
        record = self.get_result(key)
        self._store.request_cancel(key)
        return record

    def _run_solver(
        self,
        key: str,
        request: OptimizeRequest,
        fp: str,
        timeout: float | None,
        step_delay: float,
    ) -> None:
        """后台执行求解并把终态写回存储；任何异常都不向上游线程泄漏。"""
        record = self._store.get(key)
        if record is None:
            return
        try:
            logger.info(
                "开始求解 key=%s scenarios=%d decimals=%d",
                key,
                len(request.scenarios),
                request.quantize_decimals,
            )
            problem = request_to_problem(request)
            result = solve(
                problem,
                timeout_seconds=timeout,
                is_cancelled=record.cancel_event.is_set,
                step_delay=step_delay,
                quantize_decimals=request.quantize_decimals,
            )
            response = build_response(key, fp, result)
            self._store.succeed(key, response)
        except AppError as exc:
            logger.info(
                "求解结束为失败 key=%s code=%s message=%s",
                key,
                exc.code,
                exc.message,
            )
            self._store.fail(key, exc.code, exc.message, exc.details)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须兜底
            logger.exception("求解发生未预期异常 key=%s", key)
            self._store.fail(
                key,
                "SOLVER_FAILURE",
                f"求解器内部错误: {exc!r}",
                {"type": type(exc).__name__},
            )
