"""带已确认额度的重规划测试。

覆盖：
- 重规划保留确认格子、未确认部分按最新输入重排，跨场景约束一致成立；
- 确认额度与最新输入冲突（窗口容量/需求上限/供给总量）-> CONFIRMATION_CONFLICT，
  与 INVALID_REQUEST、INFEASIBLE 严格区分；
- 确认额度超出基准方案分配、基准缺失/未成功 -> 同步拒绝，不产生作业；
- 同一重规划标识并发/重复提交只求解一次，结果血缘（base_key）可见；
- 低精度边界：成功结果严格达标；精度不足明确拒绝而非表面成功；
- 重规划不影响既有方案，取消语义沿用。
"""

from __future__ import annotations

import threading

import pytest

from app.errors import (
    ConfirmationConflictError,
    InvalidRequestError,
    NotFoundError,
)
from app.logging_setup import get_logger
from app.models import ScenarioIn
from app.service.idempotency import JobStatus
from app.service.optimizer import OptimizerService
from app.solver.engine import solve
from app.solver.models import AllocationProblem, EntitySpec, Scenario, WindowSpec
from tests.factories import make_replan_request, make_request
from tests.test_idempotency import wait_for_terminal

logger = get_logger("tests.test_replan")


def _run_base_plan(service: OptimizerService, key: str = "base-plan", **kwargs):
    service.submit(key, make_request(**kwargs))
    record = wait_for_terminal(service, key)
    assert record.status is JobStatus.SUCCEEDED
    return record.result


def _cell(result, scenario: str, entity: str, window: str) -> float:
    s = next(x for x in result.scenarios if x.scenario == scenario)
    w = next(x for x in s.window_allocations if x.window == window)
    return w.entities.get(entity, 0.0)


def _assert_constraints(result, decimals: int) -> None:
    tol = 2 * 10**-decimals
    for s in result.scenarios:
        assert abs(s.total_allocated - s.total_supply) <= tol
        for w in s.window_allocations:
            assert w.allocated <= w.capacity + tol
        for e in s.entity_allocations:
            assert e.amount >= e.min_share - tol
            assert e.amount <= e.demand + tol


def test_replan_preserves_confirmations_and_replans_rest() -> None:
    logger.info("=== 用例：带确认额度的重规划，确认格子保留、其余重排 ===")
    service = OptimizerService()
    base = _run_base_plan(service)
    base_cell_a_w0 = _cell(base, "base", "a", "w0")
    base_cell_b_w1 = _cell(base, "base", "b", "w1")
    logger.info(
        "基准方案基线格子: (a,w0)=%.6f (b,w1)=%.6f", base_cell_a_w0, base_cell_b_w1
    )
    assert base_cell_a_w0 > 30.0 and base_cell_b_w1 > 10.0

    # 确认 (a,w0)=30、(b,w1)=10；最新输入：供给与场景结构变化。
    replan = make_replan_request(
        base_key="base-plan",
        confirmations=(("a", "w0", 30.0), ("b", "w1", 10.0)),
        scenarios=[
            ScenarioIn(name="base", total_supply=100.0, is_baseline=True),
            ScenarioIn(name="tight", total_supply=40.0),
        ],
    )
    service.submit_replan("replan-1", replan)
    record = wait_for_terminal(service, "replan-1")
    assert record.status is JobStatus.SUCCEEDED
    result = record.result

    logger.info(
        "锁定情况: (a,w0)>=30 (b,w1)>=10；血缘 base_key=%s", result.base_key
    )
    # 血缘可见：结果指向延续的既有方案。
    assert result.base_key == "base-plan"
    # 确认额度在所有场景原样保留（确认部分不被挪走/少算）。
    for scenario in ("base", "tight"):
        got_a = _cell(result, scenario, "a", "w0")
        got_b = _cell(result, scenario, "b", "w1")
        logger.info("场景=%s (a,w0)=%.6f (b,w1)=%.6f", scenario, got_a, got_b)
        assert got_a >= 30.0 - 1e-9, "确认格子 (a,w0) 被少算"
        assert got_b >= 10.0 - 1e-9, "确认格子 (b,w1) 被少算"
    # 所有场景约束口径一致成立。
    _assert_constraints(result, decimals=6)
    # 未确认部分确实按最新供给重排（总量不同于基准方案的 120）。
    for s in result.scenarios:
        assert abs(s.total_allocated - s.total_supply) <= 2e-6
    # 既有方案未被新请求改掉。
    assert service.get_result("base-plan").result is base
    logger.info("判定依据：确认格子保留 + 跨场景约束成立 + 基准方案未被污染")


def test_confirmation_conflicts_are_distinct_from_invalid_and_infeasible() -> None:
    logger.info("=== 用例：确认额度与最新输入冲突 -> CONFIRMATION_CONFLICT ===")
    service = OptimizerService()
    _run_base_plan(service)

    cases = {
        # 窗口容量收缩后装不下确认量。
        "replan-cap": make_replan_request(
            base_key="base-plan",
            confirmations=(("a", "w0", 30.0),),
            capacities=(10.0, 110.0),
        ),
        # 实体需求上限下降后低于确认量。
        "replan-demand": make_replan_request(
            base_key="base-plan",
            confirmations=(("a", "w0", 30.0),),
            scenarios=[
                ScenarioIn(
                    name="base",
                    total_supply=100.0,
                    demand_multipliers={"a": 0.2},
                    is_baseline=True,
                )
            ],
        ),
        # 确认总量超过最新供给总量。
        "replan-supply": make_replan_request(
            base_key="base-plan",
            confirmations=(("a", "w0", 40.0), ("b", "w1", 30.0)),
            supply=50.0,
        ),
    }
    for key, req in cases.items():
        service.submit_replan(key, req)
        record = wait_for_terminal(service, key)
        assert record.status is JobStatus.FAILED
        assert record.error_code == "CONFIRMATION_CONFLICT"
        assert record.error_code not in ("INVALID_REQUEST", "INFEASIBLE")
        reasons = record.error_details["reasons"]
        logger.info("key=%s 冲突原因=%s", key, reasons)
        assert reasons, "冲突必须给出可定位的原因"
        assert record.result is None, "冲突不得产生半成品结果"

    cap_record = service.get_result("replan-cap")
    assert any("w0" in r for r in cap_record.error_details["reasons"])
    demand_record = service.get_result("replan-demand")
    assert any("'a'" in r for r in demand_record.error_details["reasons"])
    logger.info("判定依据：三类冲突均为 CONFIRMATION_CONFLICT 且指明实体/窗口/场景")


def test_confirmation_must_come_from_succeeded_base_plan() -> None:
    logger.info("=== 用例：确认额度必须来自已成功基准方案（同步拒绝）===")
    service = OptimizerService()
    base = _run_base_plan(service)
    base_cell_a_w0 = _cell(base, "base", "a", "w0")

    # 确认量超过基准方案对应格子。
    with pytest.raises(ConfirmationConflictError) as exc:
        service.submit_replan(
            "r-over",
            make_replan_request(
                base_key="base-plan",
                confirmations=(("a", "w0", base_cell_a_w0 + 1.0),),
            ),
        )
    assert exc.value.code == "CONFIRMATION_CONFLICT"
    # 基准方案中不存在的格子。
    with pytest.raises(ConfirmationConflictError):
        service.submit_replan(
            "r-ghost",
            make_replan_request(
                base_key="base-plan", confirmations=(("a", "w1", 1.0),)
            ),
        )
    # 基准方案不存在。
    with pytest.raises(NotFoundError):
        service.submit_replan(
            "r-nobase",
            make_replan_request(base_key="missing", confirmations=()),
        )
    # 基准方案未成功（FAILED）：不允许作为重规划起点。
    service.submit("base-failed", make_request(supply=50.0, capacities=(5.0,)))
    failed = wait_for_terminal(service, "base-failed")
    assert failed.status is JobStatus.FAILED
    with pytest.raises(InvalidRequestError):
        service.submit_replan(
            "r-failedbase",
            make_replan_request(base_key="base-failed", confirmations=()),
        )
    # 同步拒绝不产生作业、不占幂等键。
    for key in ("r-over", "r-ghost", "r-nobase", "r-failedbase"):
        with pytest.raises(NotFoundError):
            service.get_result(key)
    logger.info("判定依据：四类非法重规划均同步拒绝且不产生作业")


def test_concurrent_replan_submissions_start_single_solve() -> None:
    logger.info("=== 用例：同一重规划标识并发 20 次提交只求解一次 ===")
    service = OptimizerService()
    _run_base_plan(service)
    replan = make_replan_request(
        base_key="base-plan",
        confirmations=(("a", "w0", 20.0),),
        supply=110.0,
    )
    key = "replan-race"
    n = 20
    barrier = threading.Barrier(n)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        outcome = service.submit_replan(key, replan)
        with lock:
            outcomes.append(outcome.started)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    started = sum(outcomes)
    logger.info("关键结果：%d 次重规划提交中 started=True 的次数=%d", n, started)
    assert started == 1, "并发重复提交必须只启动一次求解"

    record = wait_for_terminal(service, key)
    assert record.status is JobStatus.SUCCEEDED
    assert record.result.base_key == "base-plan"

    # 重复提交返回同一份等价结果。
    again = service.submit_replan(key, replan)
    assert again.started is False
    assert service.get_result(key).result is record.result
    logger.info("判定依据：单次求解 + 幂等复用 + 血缘 base_key=%s", record.result.base_key)


def test_low_precision_success_is_strictly_feasible() -> None:
    logger.info("=== 用例：低精度边界下成功结果严格达标 ===")
    decimals = 2
    service = OptimizerService()
    entities = (("a", 5.0, 0.005), ("b", 5.0, 0.005))
    service.submit(
        "base-strict",
        make_request(
            supply=8.0,
            capacities=(10.0,),
            entities=entities,
            quantize_decimals=decimals,
        ),
    )
    base = wait_for_terminal(service, "base-strict")
    assert base.status is JobStatus.SUCCEEDED

    service.submit_replan(
        "replan-strict",
        make_replan_request(
            base_key="base-strict",
            confirmations=(("a", "w0", 0.5),),
            supply=8.0,
            capacities=(10.0,),
            entities=entities,
            quantize_decimals=decimals,
        ),
    )
    record = wait_for_terminal(service, "replan-strict")
    assert record.status is JobStatus.SUCCEEDED
    result = record.result

    quantum = 10**-decimals
    for s in result.scenarios:
        # 严格（零容差）：保底不击穿、容量不越界、守恒精确成立。
        for e in s.entity_allocations:
            assert e.amount >= e.min_share, f"实体 {e.entity} 保底被击穿"
            assert e.amount <= e.demand + 1e-12
            # 所有量都落在量化网格上。
            assert abs(e.amount / quantum - round(e.amount / quantum)) < 1e-6
        for w in s.window_allocations:
            assert w.allocated <= w.capacity
        assert s.total_allocated == s.total_supply
        assert abs(
            sum(e.amount for e in s.entity_allocations) - s.total_supply
        ) < 1e-9
    assert _cell(result, "base", "a", "w0") >= 0.5
    logger.info(
        "判定依据：decimals=%d 下保底/容量/守恒零容差成立，确认格子保留", decimals
    )


def test_low_precision_unachievable_is_rejected_not_fake_success() -> None:
    logger.info("=== 用例：精度不足明确拒绝，不返回表面成功 ===")
    # 保底 0.5 在 0 位小数网格上必须进位为 1，超过供给 0.5 -> 参数问题拒绝。
    problem = AllocationProblem(
        entities=[EntitySpec("a", 1.0, 0.5)],
        windows=[WindowSpec("w0", 10.0)],
        scenarios=[Scenario("base", 0.5, is_baseline=True)],
    )
    with pytest.raises(InvalidRequestError) as exc:
        solve(problem, quantize_decimals=0)
    assert exc.value.code == "INVALID_REQUEST"
    logger.info("网格不可行原因=%s", exc.value.details["reasons"])

    # 确认量 0.5 在 0 位小数网格上无法精确表示 -> 同样明确拒绝。
    service = OptimizerService()
    service.submit(
        "base-grid",
        make_request(
            supply=8.0,
            capacities=(10.0,),
            entities=(("a", 5.0, 0.5), ("b", 5.0, 0.5)),
            quantize_decimals=2,
        ),
    )
    base = wait_for_terminal(service, "base-grid")
    assert base.status is JobStatus.SUCCEEDED
    service.submit_replan(
        "replan-grid",
        make_replan_request(
            base_key="base-grid",
            confirmations=(("a", "w0", 0.5),),
            supply=8.0,
            capacities=(10.0,),
            entities=(("a", 5.0, 0.5), ("b", 5.0, 0.5)),
            quantize_decimals=0,
        ),
    )
    record = wait_for_terminal(service, "replan-grid")
    assert record.status is JobStatus.FAILED
    assert record.error_code == "INVALID_REQUEST"
    assert record.result is None
    logger.info(
        "判定依据：精度不足落 FAILED/INVALID_REQUEST，无表面成功结果，原因=%s",
        record.error_details.get("reasons"),
    )


def test_replan_cancel_semantics_unchanged() -> None:
    logger.info("=== 用例：重规划作业的取消语义沿用 ===")
    import time

    service = OptimizerService()
    _run_base_plan(service)
    service.submit_replan(
        "replan-cancel",
        make_replan_request(
            base_key="base-plan",
            confirmations=(("a", "w0", 10.0),),
            solver_step_delay=0.05,
        ),
    )
    time.sleep(0.02)
    service.cancel("replan-cancel")
    record = wait_for_terminal(service, "replan-cancel")
    assert record.status is JobStatus.FAILED
    assert record.error_code == "CANCELLED"
    assert record.result is None
    logger.info("判定依据：重规划取消落 FAILED/CANCELLED，无部分结果")
