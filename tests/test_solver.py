"""求解器单测：可行性判定、分配口径、取消/超时、可复现性。

覆盖：
- 正常多场景：容量 / 最小保障 / 总量守恒在所有场景成立；
- 不可行输入：区分结构非法(400 类)与优化不可行(422 类)，且无部分结果；
- 超时 vs 主动取消：两个异常类型严格区分；
- 同输入结果可复现（SOLVER_VERSION + 确定性算法）。
"""

from __future__ import annotations

import threading

import pytest

from app.errors import CancelledError, InfeasibleError, InvalidRequestError, TimeoutError
from app.logging_setup import get_logger
from app.solver.engine import SOLVER_VERSION, solve
from app.solver.models import AllocationProblem, EntitySpec, Scenario, WindowSpec
from tests.factories import make_problem

logger = get_logger("tests.test_solver")


def assert_constraints(result, decimals: int = 6) -> None:
    """独立核对每个场景的容量、最小保障、需求上限与总量守恒。"""
    tol = 2 * 10**-decimals
    for s in result.scenarios:
        logger.info(
            "核对场景=%s supply=%s allocated=%s windows=%d",
            s.scenario,
            s.total_supply,
            s.total_allocated,
            len(s.window_allocations),
        )
        # 总量守恒：所有窗口分配合计 == 供给。
        assert abs(s.total_allocated - s.total_supply) <= tol
        # 实体侧与窗口侧合计一致。
        assert (
            abs(sum(e.amount for e in s.entity_allocations) - s.total_allocated)
            <= tol
        )
        for w in s.window_allocations:
            assert w.allocated <= w.capacity + tol, f"窗口 {w.window} 超容量"
            assert abs(sum(w.entities.values()) - w.allocated) <= tol
        for e in s.entity_allocations:
            assert e.amount >= e.min_share - tol, f"实体 {e.entity} 破保底"
            assert e.amount <= e.demand + tol, f"实体 {e.entity} 超需求"


def test_baseline_and_perturbed_scenarios_share_consistent_rules() -> None:
    logger.info("=== 用例：基线+扰动场景一致口径 ===")
    problem = make_problem(
        extra_scenarios=(
            Scenario("demand_up", 100.0, {"a": 1.5, "c": 0.5}),
            Scenario("supply_tight", 30.0),
        )
    )
    result = solve(problem, quantize_decimals=6)

    assert len(result.scenarios) == 3
    names = [s.scenario for s in result.scenarios]
    assert names == ["base", "demand_up", "supply_tight"]
    assert result.scenarios[0].is_baseline is True
    assert result.scenarios[1].is_baseline is False
    assert_constraints(result)
    logger.info("判定依据：三场景守恒/容量/保底均在容差内，用例通过")


def test_infeasible_capacity_shortfall_raises_and_no_partial_result() -> None:
    logger.info("=== 用例：容量不足不可行 ===")
    problem = AllocationProblem(
        entities=[EntitySpec("a", 100, 10)],
        windows=[WindowSpec("w1", 5)],
        scenarios=[Scenario("base", 50, is_baseline=True)],
    )
    with pytest.raises(InfeasibleError) as exc:
        solve(problem)
    reasons = exc.value.details["reasons"]
    logger.info("不可行原因=%s", reasons)
    assert any("窗口容量总量" in r for r in reasons)
    # 不可行必须抛错，不返回伪造/部分结果。
    assert exc.value.code == "INFEASIBLE"


def test_infeasible_min_share_exceeds_supply() -> None:
    logger.info("=== 用例：保底总量超过供给不可行 ===")
    problem = AllocationProblem(
        entities=[EntitySpec("a", 100, 40), EntitySpec("b", 100, 40)],
        windows=[WindowSpec("w1", 1000)],
        scenarios=[Scenario("base", 50, is_baseline=True)],
    )
    with pytest.raises(InfeasibleError) as exc:
        solve(problem)
    logger.info("不可行原因=%s", exc.value.details["reasons"])
    assert any("最小保障总量" in r for r in exc.value.details["reasons"])


def test_infeasible_min_share_above_perturbed_demand() -> None:
    logger.info("=== 用例：扰动后需求低于保底不可行 ===")
    problem = AllocationProblem(
        entities=[EntitySpec("a", 100, 30)],
        windows=[WindowSpec("w1", 1000)],
        scenarios=[
            Scenario("base", 50, is_baseline=True),
            Scenario("crash", 50, {"a": 0.1}),  # 需求跌到 10 < 保底 30
        ],
    )
    with pytest.raises(InfeasibleError) as exc:
        solve(problem)
    assert any("扰动后需求" in r for r in exc.value.details["reasons"])
    logger.info("判定依据：扰动场景保底不可达 -> INFEASIBLE")


def test_invalid_structure_is_distinct_from_infeasible() -> None:
    logger.info("=== 用例：结构非法与不可行区分 ===")
    no_entity = AllocationProblem(
        entities=[],
        windows=[WindowSpec("w1", 1)],
        scenarios=[Scenario("base", 0, is_baseline=True)],
    )
    with pytest.raises(InvalidRequestError) as exc:
        solve(no_entity)
    assert exc.value.code == "INVALID_REQUEST"

    two_baseline = AllocationProblem(
        entities=[EntitySpec("a", 10, 0)],
        windows=[WindowSpec("w1", 10)],
        scenarios=[
            Scenario("b1", 5, is_baseline=True),
            Scenario("b2", 5, is_baseline=True),
        ],
    )
    with pytest.raises(InvalidRequestError):
        solve(two_baseline)
    logger.info("判定依据：结构问题 -> INVALID_REQUEST，区别于 INFEASIBLE")


def test_timeout_raises_timeout_error() -> None:
    logger.info("=== 用例：求解超时 ===")
    problem = make_problem(
        extra_scenarios=(
            Scenario("s1", 100.0),
            Scenario("s2", 100.0),
            Scenario("s3", 100.0),
        )
    )
    with pytest.raises(TimeoutError) as exc:
        solve(problem, timeout_seconds=0.01, step_delay=0.05)
    assert exc.value.code == "TIMEOUT"
    logger.info("判定依据：越过 deadline 且无取消信号 -> TIMEOUT")


def test_cancellation_is_distinct_from_timeout() -> None:
    logger.info("=== 用例：主动取消优先于超时 ===")
    event = threading.Event()
    event.set()  # 预先取消
    problem = make_problem()
    with pytest.raises(CancelledError) as exc:
        solve(
            problem,
            is_cancelled=event.is_set,
            timeout_seconds=0.001,  # 即使同时超时
            step_delay=0.01,
        )
    assert exc.value.code == "CANCELLED"
    logger.info("判定依据：取消信号置位优先 -> CANCELLED，而非 TIMEOUT")


def test_same_input_reproduces_identical_result() -> None:
    logger.info("=== 用例：同输入同版本结果可复现 ===")
    problem = make_problem(
        extra_scenarios=(Scenario("up", 100.0, {"a": 1.5}),)
    )
    r1 = solve(problem, quantize_decimals=6)
    r2 = solve(problem, quantize_decimals=6)
    assert r1 == r2
    assert r1.solver_version == SOLVER_VERSION
    logger.info("判定依据：两次求解冻结数据类完全相等，version=%s", SOLVER_VERSION)
