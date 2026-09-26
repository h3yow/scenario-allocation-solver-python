"""幂等与并发重复提交测试。

覆盖：
- 同键并发 N 次提交：恰好一次 started，最终一份等价结果；
- 成功后重复提交：幂等返回同一份固化结果；
- 同键不同指纹：CONFLICT(409)；
- FAILED 后同指纹可重试、且使用全新取消事件（旧取消不污染重试）。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.errors import ConflictError
from app.logging_setup import get_logger
from app.service.idempotency import IdempotencyStore, JobStatus
from app.service.optimizer import OptimizerService
from tests.factories import make_request

logger = get_logger("tests.test_idempotency")


def wait_for_terminal(service: OptimizerService, key: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = service.get_result(key)
        if record.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            return record
        time.sleep(0.005)
    raise AssertionError(f"作业 {key} 未在 {timeout}s 内到达终态")


def test_concurrent_duplicate_submissions_start_single_solve() -> None:
    logger.info("=== 用例：同键并发 20 次提交 ===")
    service = OptimizerService()
    request = make_request()
    key = "k-concurrent"
    n = 20
    barrier = threading.Barrier(n)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()  # 尽量同时冲击 begin
        outcome = service.submit(key, request)
        with lock:
            outcomes.append(outcome.started)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    started = sum(outcomes)
    logger.info("关键结果：%d 次提交中 started=True 的次数=%d", n, started)
    assert started == 1, "并发重复提交必须只启动一次求解"

    record = wait_for_terminal(service, key)
    assert record.status is JobStatus.SUCCEEDED
    assert record.result is not None
    assert record.result.idempotency_key == key
    logger.info("状态迁移终态=%s，结果唯一固化", record.status.value)


def test_repeated_submit_after_success_returns_same_result() -> None:
    logger.info("=== 用例：成功后重复提交幂等返回 ===")
    service = OptimizerService()
    request = make_request()
    key = "k-repeat"

    service.submit(key, request)
    wait_for_terminal(service, key)
    first = service.get_result(key).result

    again = service.submit(key, request)
    assert again.started is False
    second = service.get_result(key).result
    assert first is second, "必须返回同一份固化结果对象"
    logger.info("判定依据：两次返回同一结果对象 started=False")


def test_same_key_different_payload_conflicts() -> None:
    logger.info("=== 用例：同键不同指纹冲突 ===")
    service = OptimizerService()
    key = "k-conflict"
    service.submit(key, make_request(supply=120.0))
    wait_for_terminal(service, key)

    with pytest.raises(ConflictError):
        service.submit(key, make_request(supply=99.0))
    # 冲突不得覆盖既有结果。
    record = service.get_result(key)
    assert record.status is JobStatus.SUCCEEDED
    assert record.result.scenarios[0].total_supply == 120.0
    logger.info("判定依据：冲突返回 CONFLICT，且既有 120.0 结果未被污染")

def test_failed_job_retries_with_fresh_cancel_event() -> None:
    logger.info("=== 用例：不可行失败后同指纹重试仍可成功（先改输入再重提不可行案例）===")
    store = IdempotencyStore()
    service = OptimizerService(store=store)
    key = "k-retry"

    # 第一次提交一个不可行输入：落 FAILED。
    infeasible_req = make_request(
        supply=50.0,
        capacities=(5.0,),  # 容量不足
    )
    service.submit(key, infeasible_req)
    failed = wait_for_terminal(service, key)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "INFEASIBLE"
    old_event = failed.cancel_event
    old_event.set()  # 模拟旧作业被取消过
    logger.info(
        "首次失败 code=%s，并置位旧取消事件 attempts=%d",
        failed.error_code,
        failed.attempts,
    )

    # 同指纹重试（仍是不可行输入）会重新求解、再次 FAILED，但新事件不受旧事件影响。
    again = service.submit(key, infeasible_req)
    assert again.started is True
    assert again.record.cancel_event is not old_event
    assert not again.record.cancel_event.is_set(), "新作业必须使用全新取消事件"
    retried = wait_for_terminal(service, key)
    assert retried.status is JobStatus.FAILED
    assert retried.attempts == 2
    logger.info("判定依据：重试使用全新取消事件，attempts=%d 未受旧取消污染", retried.attempts)


def test_failed_then_conflicting_fingerprint_still_conflicts() -> None:
    logger.info("=== 用例：FAILED 状态下冲突指纹仍拒绝 ===")
    service = OptimizerService()
    key = "k-failed-conflict"
    service.submit(key, make_request(supply=50.0, capacities=(5.0,)))
    wait_for_terminal(service, key)

    with pytest.raises(ConflictError):
        service.submit(key, make_request(supply=10.0, capacities=(5.0,)))
    logger.info("判定依据：即使 FAILED，不同指纹仍返回 CONFLICT")
