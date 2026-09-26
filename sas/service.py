"""优化服务编排层:幂等、并发去重、失败恢复、可复现性。

提交语义(按幂等键 key):
  1. 键不存在           -> 校验 -> 建 PENDING -> 异步求解
  2. 键存在、同载荷      -> 等价请求:返回同一记录状态/结果(绝不二次求解)
  3. 键存在、不同载荷    -> ConflictError(冲突可区分)
  4. FAILED/CANCELLED/TIMEOUT 且同载荷 -> 重置 PENDING 后重试一次新求解
  5. SUCCEEDED          -> 永久返回同一结果

异常分类:
  InvalidRequestError 参数非法;InfeasibleError 可行域为空(记录 FAILED/INFEASIBLE);
  ConflictError 幂等键冲突;SolveTimeoutError/SolverCancelledError 查询终态时抛出。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .errors import (
    ConflictError,
    InfeasibleError,
    InvalidRequestError,
    SolverCancelledError,
    SolverFailedError,
    SolveTimeoutError,
)
from .feasibility import check_feasibility
from .models import Problem, SolverConfig, SolveResult, payload_digest
from .solver import MODEL_VERSION, solve
from .store import RETRYABLE_STATUSES, IdempotencyStore, Status
from .validation import validate_config, validate_problem
from .worker import WorkerManager

logger = logging.getLogger("sas.service")


class AllocationService:
    """提交/查询/取消优化请求的门面。"""

    def __init__(
        self,
        default_config: SolverConfig | None = None,
        checkpoint: "Callable[[str, str, threading.Event], None] | None" = None,
    ) -> None:
        self.default_config = default_config or SolverConfig()
        # 求解器检查点钩子:生产为 None,测试注入阻塞点制造超时/取消窗口
        self.checkpoint = checkpoint
        self.store = IdempotencyStore()
        self.workers = WorkerManager(self)

    # ---------- 提交 ----------
    def submit(
        self, key: str, problem: Problem, config: SolverConfig | None = None
    ) -> tuple[Status, SolveResult | None]:
        if not isinstance(key, str) or not key:
            raise InvalidRequestError("idempotency_key 必须为非空字符串")
        cfg = config or self.default_config

        # 1) 先校验:非法请求不落任何状态(重复提交非法体也拿到同样的 400)
        validate_config(cfg)
        validate_problem(problem)

        digest = payload_digest(problem, cfg)
        logger.info(
            "收到提交 | key=%s payload=%s model=%s",
            key,
            digest[:12],
            MODEL_VERSION,
        )

        # 2) 幂等状态机(并发下 create 是原子的)
        record, created = self.store.create(key, digest)
        if not created:
            if record.payload_hash != digest:
                logger.warning(
                    "幂等冲突 | key=%s 已绑定载荷 %s,新载荷 %s",
                    key,
                    record.payload_hash[:12],
                    digest[:12],
                )
                raise ConflictError(
                    f"幂等键 {key!r} 已用于另一份不同的请求体"
                    f"(既有={record.payload_hash[:12]}, 提交={digest[:12]})"
                )
            if record.status in RETRYABLE_STATUSES:
                # 同载荷重试:污染的只是上一条执行痕迹,记录重置后重新求解
                logger.info(
                    "同载荷重试 | key=%s 上次终态=%s", key, record.status.value
                )
                self.store.reset_for_retry(key, digest)
                created = True
            else:
                logger.info(
                    "等价重复提交 | key=%s 复用状态=%s(不产生新求解)",
                    key,
                    record.status.value,
                )
                return self.get_result(key)

        # 3) 同步可行性判定(快速、确定,失败落 FAILED/INFEASIBLE,不产生部分结果)
        try:
            check_feasibility(problem, cfg.tolerance)
        except InfeasibleError as exc:
            self.store.transition(
                key,
                Status.PENDING,
                Status.FAILED,
                error_code="INFEASIBLE",
                error_message=str(exc) or "问题可行域为空,详见可行性判定日志",
            )
            raise

        # 4) PENDING -> RUNNING,投递后台求解
        self.store.transition(key, Status.PENDING, Status.RUNNING)
        self.workers.submit(key, problem, cfg, digest)
        return self.get_result(key)

    # ---------- 执行体(工作线程) ----------
    def _run_solve(
        self,
        key: str,
        problem: Problem,
        config: SolverConfig,
        payload_hash: str,
        ctx: object,
    ) -> None:
        started = time.perf_counter()
        try:
            result = solve(
                problem,
                config,
                idempotency_key=key,
                payload_hash=payload_hash,
                cancel_event=ctx.cancel_event,
                checkpoint=self.checkpoint,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000

            # 完成瞬间再判一次:可能恰好在最后一个窗口后超时/取消
            if ctx.timed_out.is_set() and not _was_manual_cancel(ctx):
                self.store.transition(
                    key,
                    Status.RUNNING,
                    Status.TIMEOUT,
                    error_code="SOLVE_TIMEOUT",
                    error_message=f"求解超过 {config.solve_timeout}s 时间预算",
                )
                return
            if ctx.cancel_event.is_set():
                self.store.transition(
                    key,
                    Status.RUNNING,
                    Status.CANCELLED,
                    error_code="CANCELLED",
                    error_message="求解被主动取消",
                )
                return

            if self.store.transition(
                key, Status.RUNNING, Status.SUCCEEDED, result=result
            ):
                logger.info("结果已提交 | key=%s elapsed_ms=%.3f", key, elapsed_ms)
        except SolverCancelledError:
            # 求解器在边界响应了信号:依据信号来源区分超时/取消
            if ctx.timed_out.is_set() and not _was_manual_cancel(ctx):
                self.store.transition(
                    key,
                    Status.RUNNING,
                    Status.TIMEOUT,
                    error_code="SOLVE_TIMEOUT",
                    error_message=f"求解超过 {config.solve_timeout}s 时间预算",
                )
            else:
                self.store.transition(
                    key,
                    Status.RUNNING,
                    Status.CANCELLED,
                    error_code="CANCELLED",
                    error_message="求解被主动取消",
                )
        except Exception as exc:  # noqa: BLE001 - 任何求解器异常都收敛为 FAILED
            logger.exception("求解失败 | key=%s", key)
            self.store.transition(
                key,
                Status.RUNNING,
                Status.FAILED,
                error_code="SOLVER_FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            # 状态落定后再唤醒看门狗,避免它把已完成的任务误判为超时
            ctx.finished.set()

    # ---------- 查询/取消/等待 ----------
    def get_result(self, key: str) -> tuple[Status, SolveResult | None]:
        record = self.store.get(key)
        if record is None:
            raise KeyError(key)
        status = record.status
        if status is Status.SUCCEEDED:
            return status, record.result
        if status is Status.FAILED:
            raise SolverFailedError(record.error_message or "求解失败")
        if status is Status.CANCELLED:
            raise SolverCancelledError(record.error_message or "求解已取消")
        if status is Status.TIMEOUT:
            raise SolveTimeoutError(record.error_message or "求解超时")
        return status, None

    def cancel(self, key: str) -> Status:
        """主动取消;返回取消后的(当前)状态。仅运行中/排队中可取消。"""
        record = self.store.get(key)
        if record is None:
            raise KeyError(key)
        if record.status in (Status.PENDING, Status.RUNNING):
            self.workers.request_cancel(key)
            logger.info("已请求取消 | key=%s 当前状态=%s", key, record.status.value)
        return self.store.get(key).status

    def wait_until_terminal(self, key: str, timeout: float | None = None) -> bool:
        """阻塞至该键进入终态(测试/同步客户端用)。"""
        return self.store.wait_terminal(key, timeout)


def _was_manual_cancel(ctx: object) -> bool:
    """看门狗超时只置 timed_out+cancel;主动取消会额外置 manual_cancel。"""
    return bool(getattr(ctx, "manual_cancel", None) and ctx.manual_cancel.is_set())
