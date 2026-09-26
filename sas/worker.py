"""求解工作线程管理:超时与主动取消的区分。

- 主动取消:外部置 cancel_event,求解器在场景/窗口边界看到信号 -> CANCELLED
- 超时:由看门狗在 deadline 到达后置同一信号,并把结果标记为 TIMEOUT
  (求解器内部只认“被取消”,TIMEOUT 语义由编排层依据 deadline 判定,
   从而保证超时与主动取消返回可区分的状态)。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("sas.worker")


@dataclass
class SolveContext:
    """一次求解执行的上下文(取消信号 + 截止时间)。

    - cancel_event:求解器唯一需要轮询的信号(超时和主动取消都会置位)
    - timed_out:   看门狗判定超时后置位
    - manual_cancel:仅主动取消置位——用于区分 CANCELLED 与 TIMEOUT
    - finished:    求解线程结束(任意终态)时置位,看门狗据此放弃超时判定
    """

    key: str
    deadline: float
    cancel_event: threading.Event
    timed_out: threading.Event
    manual_cancel: threading.Event
    finished: threading.Event

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


class WorkerManager:
    """为每次 PENDING 记录启动一个 daemon 线程执行求解。"""

    def __init__(self, service: object) -> None:
        self.service = service
        self._threads: dict[str, threading.Thread] = {}
        self._contexts: dict[str, SolveContext] = {}
        self._lock = threading.Lock()

    def submit(
        self, key: str, problem: object, config: object, payload_hash: str
    ) -> SolveContext:
        """登记上下文并启动求解线程与超时看门狗。"""
        cancel_event = threading.Event()
        timed_out = threading.Event()
        manual_cancel = threading.Event()
        finished = threading.Event()
        deadline = time.monotonic() + config.solve_timeout
        ctx = SolveContext(
            key=key,
            deadline=deadline,
            cancel_event=cancel_event,
            timed_out=timed_out,
            manual_cancel=manual_cancel,
            finished=finished,
        )
        thread = threading.Thread(
            target=self.service._run_solve,
            args=(key, problem, config, payload_hash, ctx),
            name=f"solver-{key}",
            daemon=True,
        )
        watchdog = threading.Thread(
            target=self._watch_timeout,
            args=(ctx,),
            name=f"watchdog-{key}",
            daemon=True,
        )
        with self._lock:
            self._contexts[key] = ctx
            self._threads[key] = thread
        logger.info("提交求解线程 | key=%s deadline_in=%.3fs", key, config.solve_timeout)
        thread.start()
        watchdog.start()
        return ctx

    def _watch_timeout(self, ctx: SolveContext) -> None:
        remaining = ctx.deadline - time.monotonic()
        if remaining > 0:
            # 任一信号到达即醒来:正常/失败结束(finished)、主动取消(timed_out)
            if ctx.finished.wait(remaining):
                return
        # 醒来且求解线程仍未结束:若不是主动取消,则判定超时
        if ctx.finished.is_set():
            return
        if not ctx.cancel_event.is_set() and not ctx.manual_cancel.is_set():
            logger.warning("求解超时 | key=%s,置取消信号(状态将记为 TIMEOUT)", ctx.key)
            ctx.timed_out.set()
            ctx.cancel_event.set()

    def request_cancel(self, key: str) -> bool:
        """置主动取消信号;上下文不存在或信号已置位返回 False。"""
        with self._lock:
            ctx = self._contexts.get(key)
        if ctx is None or ctx.cancel_event.is_set():
            return False
        logger.info("收到主动取消 | key=%s", key)
        ctx.manual_cancel.set()
        ctx.cancel_event.set()
        # 让看门狗尽快醒来(它看到 manual_cancel 已置位便不会再标超时)
        ctx.timed_out.set()
        return True

    def join(self, key: str, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._threads.get(key)
        if thread is not None:
            thread.join(timeout)
