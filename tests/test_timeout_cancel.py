"""超时、主动取消与失败恢复单测。"""
from __future__ import annotations

import logging
import threading
import time

from sas.errors import SolverCancelledError, SolveTimeoutError
from sas.models import Problem
from sas.service import AllocationService
from sas.store import Status

logger = logging.getLogger("sas.test.tc")


def test_timeout_is_distinguished_from_cancel(
    sample_problem: Problem, fast_config
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp(scenario, window, cancel_event) -> None:
        entered.set()
        # 阻塞至被释放或被取消信号中断
        while not release.is_set():
            if cancel_event.wait(0.02):
                from sas.errors import SolverCancelledError
                raise SolverCancelledError("checkpoint interrupted by timeout")

    svc = AllocationService(default_config=fast_config, checkpoint=cp)
    svc.submit("slow", sample_problem, fast_config)
    assert entered.wait(2.0)
    assert svc.wait_until_terminal("slow", timeout=3.0)

    record = svc.store.get("slow")
    logger.info("超时用例终态=%s error=%s", record.status, record.error_code)
    assert record.status is Status.TIMEOUT
    assert record.error_code == "SOLVE_TIMEOUT"
    assert record.result is None
    release.set()

    with __import__("pytest").raises(SolveTimeoutError):
        svc.get_result("slow")


def test_manual_cancel_is_distinguished_from_timeout(
    sample_problem: Problem, fast_config
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp(scenario, window, cancel_event) -> None:
        entered.set()
        while not release.is_set():
            if cancel_event.wait(0.02):
                from sas.errors import SolverCancelledError
                raise SolverCancelledError("checkpoint cancelled")

    svc = AllocationService(default_config=fast_config, checkpoint=cp)
    svc.submit("cancel-me", sample_problem, fast_config)
    assert entered.wait(2.0)

    status = svc.cancel("cancel-me")
    # 调用返回时工作线程可能恰好已完成 CANCELLED 迁移(正常竞态)
    assert status in (Status.PENDING, Status.RUNNING, Status.CANCELLED)
    assert svc.wait_until_terminal("cancel-me", timeout=2.0)
    release.set()

    record = svc.store.get("cancel-me")
    logger.info("取消用例终态=%s error=%s", record.status, record.error_code)
    assert record.status is Status.CANCELLED
    assert record.error_code == "CANCELLED"
    assert record.result is None  # 取消不产生部分结果

    with __import__("pytest").raises(SolverCancelledError):
        svc.get_result("cancel-me")


def test_retry_after_cancel_succeeds_and_does_not_pollute(
    sample_problem: Problem, fast_config
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp(scenario, window, cancel_event) -> None:
        entered.set()
        while not release.is_set():
            if cancel_event.wait(0.02):
                from sas.errors import SolverCancelledError
                raise SolverCancelledError("checkpoint cancelled")

    svc = AllocationService(default_config=fast_config, checkpoint=cp)
    svc.submit("retry", sample_problem, fast_config)
    assert entered.wait(2.0)
    svc.cancel("retry")
    assert svc.wait_until_terminal("retry", 2.0)
    release.set()
    time.sleep(0.05)  # 让被取消的工作线程完全退出

    # 同载荷重试:不带检查点,应正常完成
    svc.checkpoint = None
    status, _ = svc.submit("retry", sample_problem, fast_config)
    assert svc.wait_until_terminal("retry", 5.0)
    status, result = svc.get_result("retry")
    assert status is Status.SUCCEEDED
    assert result is not None
    # 迁移日志保留了完整的恢复轨迹
    assert any("CANCELLED -> PENDING" in m for m in svc.store.get("retry").transition_log)


def test_retry_after_timeout_succeeds(sample_problem: Problem, fast_config) -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp(scenario, window, cancel_event) -> None:
        entered.set()
        while not release.is_set():
            if cancel_event.wait(0.02):
                from sas.errors import SolverCancelledError
                raise SolverCancelledError("checkpoint interrupted")

    svc = AllocationService(default_config=fast_config, checkpoint=cp)
    svc.submit("retry-timeout", sample_problem, fast_config)
    assert entered.wait(2.0)
    assert svc.wait_until_terminal("retry-timeout", 3.0)
    release.set()
    time.sleep(0.05)

    svc.checkpoint = None
    svc.submit("retry-timeout", sample_problem, fast_config)
    assert svc.wait_until_terminal("retry-timeout", 5.0)
    status, result = svc.get_result("retry-timeout")
    assert status is Status.SUCCEEDED and result is not None
    assert any(
        "TIMEOUT -> PENDING" in m
        for m in svc.store.get("retry-timeout").transition_log
    )


def test_successful_result_immutable_and_not_retried(
    sample_problem: Problem, fast_config
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp_first(scenario, window, cancel_event) -> None:
        pass  # 首次直接快速完成

    svc = AllocationService(default_config=fast_config, checkpoint=cp_first)
    svc.submit("done", sample_problem, fast_config)
    assert svc.wait_until_terminal("done", 5.0)
    _, first = svc.get_result("done")

    # 即使后续注入阻塞检查点,重复提交也只返回既有结果,不会触发新求解
    def cp_should_not_run(scenario, window, cancel_event) -> None:
        entered.set()
        release.wait(5.0)

    svc.checkpoint = cp_should_not_run
    status, result = svc.submit("done", sample_problem, fast_config)
    assert status is Status.SUCCEEDED
    assert result is first
    assert not entered.wait(0.2), "成功结果被重复求解了"
