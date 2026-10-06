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

from app.errors import (
    AppError,
    ConfirmationConflictError,
    InvalidRequestError,
    NotFoundError,
)
from app.logging_setup import get_logger
from app.models import OptimizeRequest, OptimizeResponse, ReplanRequest
from app.service.idempotency import IdempotencyStore, JobRecord, JobStatus
from app.service.mapper import (
    build_response,
    replan_request_to_problem,
    request_to_problem,
)
from app.solver.engine import SOLVER_VERSION, solve
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
    def fingerprint_replan(request: ReplanRequest, base_fingerprint: str) -> str:
        """重规划指纹：在常规输入之上绑定基准方案与确认额度（血缘可复现）。"""
        payload = {
            "solver_version": SOLVER_VERSION,
            "kind": "replan",
            "base_key": request.base_key,
            "base_fingerprint": base_fingerprint,
            "confirmations": [c.model_dump() for c in request.confirmations],
            "entities": [e.model_dump() for e in request.entities],
            "windows": [w.model_dump() for w in request.windows],
            "scenarios": [s.model_dump() for s in request.scenarios],
            "quantize_decimals": request.quantize_decimals,
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _effective_timeout_value(self, timeout: float | None) -> float | None:
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
        problem = request_to_problem(request)
        validate_problem(problem)

        fp = self.fingerprint(request)
        logger.info(
            "收到提交 key=%s fingerprint=%s timeout_req=%s",
            key,
            fp[:12],
            request.timeout_seconds,
        )
        return self._begin_and_run(
            key,
            fp,
            problem,
            timeout_req=request.timeout_seconds,
            step_delay_req=request.solver_step_delay,
            quantize_decimals=request.quantize_decimals,
            base_key=None,
        )

    def submit_replan(self, key: str, request: ReplanRequest) -> SubmitOutcome:
        """提交带已确认额度的重规划，接着 base_key 指向的成功方案往下做。

        同步校验（不产生作业、不占幂等键）：
        - 结构非法 -> INVALID_REQUEST；
        - 基准方案不存在 -> NOT_FOUND；未成功 -> INVALID_REQUEST；
        - 确认额度超出基准方案对应格子 -> CONFIRMATION_CONFLICT。
        确认额度与最新输入的冲突在后台求解时判定，同样落
        CONFIRMATION_CONFLICT，与 INFEASIBLE 严格区分。
        """
        problem = replan_request_to_problem(request)
        validate_problem(problem)

        base = self._store.get(request.base_key)
        if base is None:
            raise NotFoundError(
                "重规划的基准方案不存在",
                details={"base_key": request.base_key},
            )
        if base.status is not JobStatus.SUCCEEDED or base.result is None:
            raise InvalidRequestError(
                "重规划必须接着一份已成功的方案进行",
                details={
                    "base_key": request.base_key,
                    "base_status": base.status.value,
                },
            )
        self._check_confirmations_against_base(request, base.result)

        fp = self.fingerprint_replan(request, base.fingerprint)
        logger.info(
            "收到重规划提交 key=%s base_key=%s fingerprint=%s 确认格子=%d",
            key,
            request.base_key,
            fp[:12],
            len(request.confirmations),
        )
        return self._begin_and_run(
            key,
            fp,
            problem,
            timeout_req=request.timeout_seconds,
            step_delay_req=request.solver_step_delay,
            quantize_decimals=request.quantize_decimals,
            base_key=request.base_key,
        )

    @staticmethod
    def _check_confirmations_against_base(
        request: ReplanRequest, base_result: OptimizeResponse
    ) -> None:
        """确认额度必须来自基准方案基线场景中的既有分配（不得超过原分配量）。"""
        baseline = next(s for s in base_result.scenarios if s.is_baseline)
        cells: dict[tuple[str, str], float] = {}
        for wa in baseline.window_allocations:
            for entity, amount in wa.entities.items():
                cells[(entity, wa.window)] = amount

        reasons: list[str] = []
        for c in request.confirmations:
            allocated = cells.get((c.entity, c.window))
            if allocated is None:
                reasons.append(
                    f"entity '{c.entity}' window '{c.window}': 基准方案 "
                    f"'{request.base_key}' 的基线场景中不存在该分配格子"
                )
            elif c.amount > allocated + 1e-9:
                reasons.append(
                    f"entity '{c.entity}' window '{c.window}': 确认量 {c.amount} "
                    f"超过基准方案中的分配量 {allocated}"
                )
        if reasons:
            logger.warning(
                "确认额度与基准方案不符 base_key=%s 原因=%s",
                request.base_key,
                reasons,
            )
            raise ConfirmationConflictError(
                "确认额度与基准方案的分配结果不符",
                details={"reasons": reasons, "base_key": request.base_key},
            )

    def _begin_and_run(
        self,
        key: str,
        fp: str,
        problem,
        *,
        timeout_req: float | None,
        step_delay_req: float,
        quantize_decimals: int,
        base_key: str | None,
    ) -> SubmitOutcome:
        record, started = self._store.begin(key, fp)
        if not started:
            return SubmitOutcome(record=record, started=False)

        timeout = self._effective_timeout_value(timeout_req)
        step_delay = step_delay_req or self._config.solver_step_delay

        thread = threading.Thread(
            target=self._run_solver,
            name=f"solver:{key}",
            args=(key, problem, fp, timeout, step_delay, quantize_decimals, base_key),
            daemon=True,
        )
        thread.start()
        logger.info(
            "已启动求解线程 key=%s thread=%s effective_timeout=%s base_key=%s",
            key,
            thread.name,
            timeout,
            base_key,
        )
        return SubmitOutcome(record=record, started=True)

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

    def _run_solver(
        self,
        key: str,
        problem,
        fp: str,
        timeout: float | None,
        step_delay: float,
        quantize_decimals: int,
        base_key: str | None,
    ) -> None:
        """后台执行求解并把终态写回存储；任何异常都不向上游线程泄漏。"""
        record = self._store.get(key)
        if record is None:
            return
        try:
            logger.info(
                "开始求解 key=%s scenarios=%d decimals=%d base_key=%s",
                key,
                len(problem.scenarios),
                quantize_decimals,
                base_key,
            )
            result = solve(
                problem,
                timeout_seconds=timeout,
                is_cancelled=record.cancel_event.is_set,
                step_delay=step_delay,
                quantize_decimals=quantize_decimals,
            )
            response = build_response(key, fp, result, base_key=base_key)
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
