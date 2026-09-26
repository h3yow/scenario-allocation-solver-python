"""幂等与并发去重单测。"""
from __future__ import annotations

import concurrent.futures
import logging

import pytest

from sas.errors import (
    ConflictError,
    InfeasibleError,
    InvalidRequestError,
)
from sas.models import Entity, Problem, ScenarioInput, Window
from sas.service import AllocationService
from sas.store import Status

logger = logging.getLogger("sas.test.idem")


def test_duplicate_submit_returns_same_result(sample_problem: Problem) -> None:
    svc = AllocationService()
    svc.submit("k", sample_problem)
    assert svc.wait_until_terminal("k", 5)
    status, first = svc.get_result("k")
    assert status is Status.SUCCEEDED and first is not None

    # 重复提交返回内存中同一对象——不产生第二次求解
    s2, r2 = svc.submit("k", sample_problem)
    assert s2 is Status.SUCCEEDED
    assert r2 is first
    record = svc.store.get("k")
    running_moves = [m for m in record.transition_log if "-> RUNNING" in m]
    assert running_moves == ["PENDING -> RUNNING"]


def test_concurrent_duplicate_submits_solve_once(sample_problem: Problem) -> None:
    # 让求解停在第一个检查点,放大并发窗口
    entered = __import__("threading").Event()
    release = __import__("threading").Event()

    def checkpoint(scenario, window, cancel_event) -> None:
        logger.info("并发测试:检查点 %s/%s", scenario, window)
        entered.set()
        while not release.is_set():
            cancel_event.wait(0.02)

    svc = AllocationService(checkpoint=checkpoint)
    results: list[object] = []
    errors: list[Exception] = []

    def submit() -> None:
        try:
            results.append(svc.submit("same-key", sample_problem))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(submit) for _ in range(8)]
        # 等第一批进入
        assert entered.wait(2.0)
        release.set()
        for f in futures:
            f.result(timeout=5)

    assert errors == []
    assert svc.wait_until_terminal("same-key", 5)
    status, result = svc.get_result("same-key")
    assert status is Status.SUCCEEDED
    # 8 次提交都拿到同一个结果对象,工作线程只有一次真正求解
    assert all(r is result for _, r in results if r is not None)
    record = svc.store.get("same-key")
    running_moves = [m for m in record.transition_log if "-> RUNNING" in m]
    assert len(running_moves) == 1, record.transition_log
    logger.info("状态迁移记录=%s", record.transition_log)


def test_conflicting_payload_raises(sample_problem: Problem) -> None:
    svc = AllocationService()
    svc.submit("k", sample_problem)
    assert svc.wait_until_terminal("k", 5)

    changed = Problem(
        entities=[Entity("a", 1.0, 9.0), Entity("b", 0.0, 6.0)],
        windows=sample_problem.windows,
        scenarios=sample_problem.scenarios,
    )
    with pytest.raises(ConflictError):
        svc.submit("k", changed)
    # 冲突不得覆盖既有结果
    status, result = svc.get_result("k")
    assert status is Status.SUCCEEDED
    assert result is not None


def test_invalid_request_does_not_create_record(sample_problem: Problem) -> None:
    svc = AllocationService()
    bad = Problem(
        entities=[Entity("a", 9.0, 1.0)],
        windows=sample_problem.windows,
        scenarios=sample_problem.scenarios,
    )
    with pytest.raises(InvalidRequestError):
        svc.submit("bad", bad)
    assert svc.store.get("bad") is None


def test_empty_key_rejected(sample_problem: Problem) -> None:
    svc = AllocationService()
    with pytest.raises(InvalidRequestError):
        svc.submit("", sample_problem)


def test_infeasible_marked_failed_and_retryable(sample_problem: Problem) -> None:
    svc = AllocationService()
    infeasible = Problem(
        entities=[Entity("a", 8.0, 8.0)],
        windows=[Window("w1", 1.0), Window("w2", 5.0)],
        scenarios=[ScenarioInput("baseline", {"w1": {"a": 1}, "w2": {"a": 1}})],
    )
    with pytest.raises(InfeasibleError):
        svc.submit("inf", infeasible)
    record = svc.store.get("inf")
    assert record.status is Status.FAILED
    assert record.error_code == "INFEASIBLE"
    assert record.result is None  # 绝不返回部分/伪造结果

    # 修正为可行问题但用同一键 -> 视为不同载荷 -> 冲突
    feasible = Problem(
        entities=[Entity("a", 0.0, 8.0)],
        windows=[Window("w1", 1.0), Window("w2", 5.0)],
        scenarios=[ScenarioInput("baseline", {"w1": {"a": 1}, "w2": {"a": 1}})],
    )
    with pytest.raises(ConflictError):
        svc.submit("inf", feasible)


def test_reproducible_result_under_same_version(sample_problem: Problem) -> None:
    svc1 = AllocationService()
    svc2 = AllocationService()
    svc1.submit("k", sample_problem)
    svc2.submit("k", sample_problem)
    assert svc1.wait_until_terminal("k", 5)
    assert svc2.wait_until_terminal("k", 5)
    _, r1 = svc1.get_result("k")
    _, r2 = svc2.get_result("k")
    assert r1.allocations == r2.allocations
    assert r1.model_version == r2.model_version
    assert r1.config_digest == r2.config_digest
    assert r1.payload_hash == r2.payload_hash
