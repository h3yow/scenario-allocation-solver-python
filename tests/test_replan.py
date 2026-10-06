"""带已确认额度的重规划测试。

覆盖：
- 重规划成功：已确认格子逐格原样保留，残量重排，容量/保底/需求/守恒
  在基线与扰动场景同口径严格成立；
- 确认额度与最新输入冲突（窗口装不下 / 实体超上限 / 总量对不上）->
  CONFIRMED_CONFLICT(412)，与参数非法(400)、整体不可行(422) 区分；
- 锁定切碎容量导致残量整体不可行 -> INFEASIBLE(422)；
- 同一重规划幂等键并发/重复提交只真正计算一次，且能看出接续来源；
- 低精度紧边界下，成功结果必须严格达标，否则明确拒绝；
- 重规划不改写任何既有方案。
"""

from __future__ import annotations

import threading

import pytest

from app.errors import (
    ConfirmedConflictError,
    InfeasibleError,
    InvalidRequestError,
    NotFoundError,
)
from app.logging_setup import get_logger
from app.models import (
    ConfirmedAllocationIn,
    EntityIn,
    ReplanRequest,
    ScenarioIn,
    WindowIn,
)
from app.service.idempotency import JobStatus
from app.service.optimizer import OptimizerService
from app.solver.models import (
    AllocationProblem,
    ConfirmedCell,
    EntitySpec,
    Scenario,
    WindowSpec,
)
from app.solver.replan import replan
from tests.test_idempotency import wait_for_terminal

logger = get_logger("tests.test_replan")


# ---------------------------------------------------------------------------
# 构造助手
# ---------------------------------------------------------------------------
def _problem():
    return AllocationProblem(
        entities=[EntitySpec("a", 100, 10), EntitySpec("b", 50, 5)],
        windows=[WindowSpec("w1", 70), WindowSpec("w2", 70)],
        scenarios=[
            Scenario("base", 120, is_baseline=True),
            Scenario("up", 100, {"a": 1.5}),
        ],
    )


def _replan_request(
    *,
    based_on: str = "plan-1",
    confirmed: list[ConfirmedAllocationIn] | None = None,
    decimals: int = 6,
    supply: float = 120.0,
    capacities=(70.0, 70.0),
    multipliers=None,
):
    return ReplanRequest(
        based_on_key=based_on,
        entities=[
            EntityIn(name="a", base_demand=100, min_share=10),
            EntityIn(name="b", base_demand=50, min_share=5),
        ],
        windows=[
            WindowIn(name="w0", capacity=capacities[0]),
            WindowIn(name="w1", capacity=capacities[1]),
        ],
        scenarios=[
            ScenarioIn(name="base", total_supply=supply, is_baseline=True),
            ScenarioIn(
                name="up",
                total_supply=100.0,
                demand_multipliers=multipliers or {"a": 1.5},
            ),
        ],
        confirmed_allocations=confirmed or [],
        quantize_decimals=decimals,
    )


def _cell_map(scenario_result) -> dict[tuple[str, str], float]:
    grid = {}
    for w in scenario_result.window_allocations:
        for e, v in w.entities.items():
            grid[(e, w.window)] = v
    return grid


def _assert_strict_constraints(result, decimals: int) -> None:
    """逐场景独立核对成功结果严格满足全部约束（无浮点放水）。"""
    tol = 10**-decimals
    for s in result.scenarios:
        logger.info(
            "核对重规划场景=%s supply=%s allocated=%s confirmed=%s",
            s.scenario, s.total_supply, s.total_allocated, s.confirmed_total,
        )
        # 严格总量守恒
        assert abs(s.total_allocated - s.total_supply) <= tol
        assert (
            abs(sum(e.amount for e in s.entity_allocations) - s.total_allocated)
            <= tol
        )
        # 确认总量不超过总供给
        assert s.confirmed_total <= s.total_supply + tol
        for w in s.window_allocations:
            assert w.allocated <= w.capacity + tol, f"{w.window} 超容量"
            assert abs(sum(w.entities.values()) - w.allocated) <= tol
        for e in s.entity_allocations:
            assert e.amount + tol >= e.min_share, f"{e.entity} 破保底"
            assert e.amount <= e.demand + tol, f"{e.entity} 超需求"


def _seed_base_plan(service: OptimizerService, key: str = "plan-1"):
    """提交并等待一份成功的基础方案，返回其成功响应。"""
    from tests.factories import make_request

    req = make_request(
        supply=120,
        capacities=(70, 70),
        entities=(("a", 100, 10), ("b", 50, 5)),
        scenarios=[
            ScenarioIn(name="base", total_supply=120, is_baseline=True),
            ScenarioIn(name="up", total_supply=100, demand_multipliers={"a": 1.5}),
        ],
        quantize_decimals=6,
    )
    service.submit(key, req)
    rec = wait_for_terminal(service, key)
    assert rec.status is JobStatus.SUCCEEDED
    return rec.result


# ---------------------------------------------------------------------------
# 求解器层
# ---------------------------------------------------------------------------
def test_replan_preserves_confirmed_cells_exactly_and_resatisfies_constraints():
    logger.info("=== 用例：重规划成功且确认格子逐格原样保留 ===")
    result = replan(
        _problem(),
        [
            ConfirmedCell("base", "a", "w1", 30.0),
            ConfirmedCell("base", "b", "w2", 20.0),
            ConfirmedCell("up", "a", "w1", 40.0),
        ],
        quantize_decimals=6,
    )
    _assert_strict_constraints(result, 6)

    by_name = {s.scenario: s for s in result.scenarios}
    base_grid = _cell_map(by_name["base"])
    up_grid = _cell_map(by_name["up"])

    # 已确认额度逐格原样保留（精确相等，不只是容差）。
    assert base_grid[("a", "w1")] == 30.0
    assert base_grid[("b", "w2")] == 20.0
    assert up_grid[("a", "w1")] == 40.0

    # 残量没有再流进锁定格子（锁定格子恒等于确认值）。
    assert by_name["base"].confirmed_total == 50.0
    assert by_name["up"].confirmed_total == 40.0
    logger.info(
        "判定依据：base 锁定 %s、up 锁定 %s 均逐格相等，其余约束严格成立",
        [base_grid[("a", "w1")], base_grid[("b", "w2")]],
        [up_grid[("a", "w1")]],
    )


def test_confirmed_window_overflow_is_confirmed_conflict():
    logger.info("=== 用例：确认占用超过窗口最新容量 -> 412 ===")
    p = AllocationProblem(
        entities=[EntitySpec("a", 100, 0)],
        windows=[WindowSpec("w1", 20), WindowSpec("w2", 200)],
        scenarios=[Scenario("base", 100, is_baseline=True)],
    )
    with pytest.raises(ConfirmedConflictError) as exc:
        replan(p, [ConfirmedCell("base", "a", "w1", 30.0)], quantize_decimals=6)
    reasons = exc.value.details["reasons"]
    logger.info("412 原因=%s", reasons)
    assert any("window 'w1'" in r and "装不下" in r for r in reasons)
    assert exc.value.code == "CONFIRMED_CONFLICT"
    logger.info("判定依据：窗口装不下 -> CONFIRMED_CONFLICT，区别于 400/422")


def test_confirmed_entity_over_current_demand_is_confirmed_conflict():
    logger.info("=== 用例：实体确认量超过其当前需求上限 -> 412 ===")
    # 最新输入把 a 的基础需求下调到 80，确认 90 即超上限。
    p = AllocationProblem(
        entities=[EntitySpec("a", 80, 0)],
        windows=[WindowSpec("w1", 100), WindowSpec("w2", 100)],
        scenarios=[Scenario("base", 80, is_baseline=True)],
    )
    with pytest.raises(ConfirmedConflictError) as exc:
        replan(p, [ConfirmedCell("base", "a", "w1", 81.0)], quantize_decimals=6)
    reasons = exc.value.details["reasons"]
    logger.info("412 原因=%s", reasons)
    assert any("超过其当前需求上限" in r for r in reasons)
    assert exc.value.http_status == 412


def test_confirmed_total_over_supply_is_confirmed_conflict():
    logger.info("=== 用例：确认总量超过场景供给 -> 412 ===")
    with pytest.raises(ConfirmedConflictError) as exc:
        replan(
            _problem(),
            [
                ConfirmedCell("base", "a", "w1", 70.0),
                ConfirmedCell("base", "a", "w2", 60.0),  # 合计 130 > 120
            ],
            quantize_decimals=6,
        )
    reasons = exc.value.details["reasons"]
    logger.info("412 原因=%s", reasons)
    assert any("确认总量" in r and "总供给" in r for r in reasons)


def test_invalid_structure_is_400_distinct_from_conflict():
    logger.info("=== 用例：确认额度结构非法 -> 400（区别于 412）===")
    with pytest.raises(InvalidRequestError) as exc:
        replan(
            _problem(),
            [ConfirmedCell("base", "ghost", "w1", 1.0)],
            quantize_decimals=6,
        )
    assert exc.value.code == "INVALID_REQUEST"

    with pytest.raises(InvalidRequestError) as exc:
        # 同一 (scenario,entity,window) 不允许重复确认
        replan(
            _problem(),
            [
                ConfirmedCell("base", "a", "w1", 1.0),
                ConfirmedCell("base", "a", "w1", 2.0),
            ],
            quantize_decimals=6,
        )
    assert exc.value.code == "INVALID_REQUEST"
    logger.info("判定依据：未知引用/重复格子 -> INVALID_REQUEST，而非 412")


def test_lock_fragmentation_is_infeasible_422():
    logger.info("=== 用例：锁定切碎容量、聚合条件成立但运输不可行 -> 422 ===")
    p = AllocationProblem(
        entities=[EntitySpec("a", 100, 50), EntitySpec("b", 100, 50)],
        windows=[
            WindowSpec("w1", 40), WindowSpec("w2", 40), WindowSpec("w3", 40)
        ],
        scenarios=[Scenario("base", 120, is_baseline=True)],
    )
    locks = [
        ConfirmedCell("base", "a", "w2", 40.0),
        ConfirmedCell("base", "a", "w3", 30.0),
        ConfirmedCell("base", "b", "w1", 30.0),
    ]
    with pytest.raises(InfeasibleError) as exc:
        replan(p, locks, quantize_decimals=6)
    reasons = exc.value.details["reasons"]
    logger.info("422 原因=%s", reasons)
    assert any("不挪动确认额度" in r for r in reasons)
    logger.info("判定依据：锁本身合法且不冲突，但残量保底无处安放 -> INFEASIBLE")


def test_low_precision_success_is_strictly_feasible():
    logger.info("=== 用例：低精度(整数网格)成功结果必须严格达标 ===")
    p = AllocationProblem(
        entities=[EntitySpec("a", 10, 3), EntitySpec("b", 10, 3)],
        windows=[WindowSpec("w1", 7), WindowSpec("w2", 7)],
        scenarios=[Scenario("base", 14, is_baseline=True)],
    )
    result = replan(
        p, [ConfirmedCell("base", "a", "w1", 7)], quantize_decimals=0
    )
    s = result.scenarios[0]
    # 整数量子下精确守恒，不靠容差“看着相等”。
    assert s.total_allocated == 14
    for w in s.window_allocations:
        assert w.allocated <= w.capacity
        for v in w.entities.values():
            assert float(v).is_integer()
    for e in s.entity_allocations:
        assert e.amount >= e.min_share
    grid = _cell_map(s)
    assert grid[("a", "w1")] == 7
    logger.info("判定依据：decimals=0 下全部为整数且守恒/容量/保底严格成立")


def test_low_precision_that_cannot_be_strict_is_rejected():
    logger.info("=== 用例：低精度下无法严格达标时明确拒绝，不返回表面成功 ===")
    # 供给 10.5 在 decimals=0 网格上无法精确守恒。
    p = AllocationProblem(
        entities=[EntitySpec("a", 10, 0), EntitySpec("b", 10, 0)],
        windows=[WindowSpec("w1", 6), WindowSpec("w2", 6)],
        scenarios=[Scenario("base", 10.5, is_baseline=True)],
    )
    with pytest.raises(InvalidRequestError) as exc:
        replan(p, [ConfirmedCell("base", "a", "w1", 5)], quantize_decimals=0)
    logger.info("400 原因=%s", exc.value.details["reasons"])
    assert any("无法严格守恒" in r for r in exc.value.details["reasons"])

    # 确认额度 30.5 在整数网格上无法原样保留。
    p2 = AllocationProblem(
        entities=[EntitySpec("a", 100, 0)],
        windows=[WindowSpec("w1", 100), WindowSpec("w2", 100)],
        scenarios=[Scenario("base", 100, is_baseline=True)],
    )
    with pytest.raises(InvalidRequestError):
        replan(p2, [ConfirmedCell("base", "a", "w1", 30.5)], quantize_decimals=0)
    logger.info("判定依据：非网格对齐的供给/确认额度 -> 400 拒绝，提高精度")


def test_replan_is_deterministic():
    logger.info("=== 用例：重规划结果可复现 ===")
    locks = [ConfirmedCell("base", "a", "w1", 30.0),
             ConfirmedCell("up", "b", "w2", 10.0)]
    r1 = replan(_problem(), locks, quantize_decimals=6)
    r2 = replan(_problem(), locks, quantize_decimals=6)
    assert r1 == r2
    logger.info("判定依据：相同输入与确认额度两次求解结果完全相等")


def test_replan_forbidden_cells_dont_produce_false_solver_failure(caplog):
    logger.info("=== 用例：禁格下水填充行目标不可路由时弹性运输仍给严格可行解 ===")
    # 整数网格：e0 需求100、e1 需求7；w0=10,w1=8；供给15；锁 e0@w0=5。
    # 水位填充把残量 10 几乎全给 e0（行目标圆整 e0=10,e1=0），但 e0 禁 w0、
    # w1 仅剩 8，精确行目标流不通；弹性边界下 e0=8(w1)、e1=2(w0) 可行。
    # 求解器必须弹性成功（绝不误报 SOLVER_FAILURE）且结果严格达标。
    p = AllocationProblem(
        entities=[EntitySpec("e0", 100, 0), EntitySpec("e1", 7, 0)],
        windows=[WindowSpec("w0", 10), WindowSpec("w1", 8)],
        scenarios=[Scenario("base", 15, is_baseline=True)],
    )
    result = replan(
        p, [ConfirmedCell("base", "e0", "w0", 5.0)], quantize_decimals=0
    )
    s = result.scenarios[0]
    grid = _cell_map(s)
    assert grid[("e0", "w0")] == 5.0          # 锁定逐格保留
    assert grid[("e0", "w1")] == 8.0          # e0 受禁格+列容量限制
    assert grid[("e1", "w0")] == 2.0          # 残量改由 e1 承接
    assert s.total_allocated == 15
    for w in s.window_allocations:
        assert w.allocated <= w.capacity
    logger.info("判定依据：禁格紧结构 grid=%s 严格可行，未误报 SOLVER_FAILURE",
                grid)
    # 确认确实走到了弹性兜底路径（而非恰好精确可行）。
    assert any("弹性边界确定性运输" in rec.getMessage()
               for rec in caplog.records), "应记录精确行目标不可路由后的弹性回退"


# ---------------------------------------------------------------------------
# 服务层：谱系、幂等/并发、原方案不被改写
# ---------------------------------------------------------------------------
def test_service_replan_references_base_and_keeps_it_unchanged():
    logger.info("=== 用例：重规划接续既有成功方案，且不改写既有方案 ===")
    service = OptimizerService()
    base = _seed_base_plan(service, "plan-1")
    base_before = base.model_dump()

    req = _replan_request(
        based_on="plan-1",
        confirmed=[ConfirmedAllocationIn(scenario="base", entity="a",
                                         window="w0", amount=30.0)],
    )
    out = service.submit_replan("replan-1", req)
    assert out.started is True
    rec = wait_for_terminal(service, "replan-1")
    assert rec.status is JobStatus.SUCCEEDED
    assert rec.result.based_on_key == "plan-1"
    logger.info("判定依据：重规划结果带 based_on_key=plan-1，谱系可见")

    s = rec.result.scenarios[0]
    grid = _cell_map(s)
    assert grid[("a", "w0")] == 30.0
    _assert_strict_constraints(__from_response(rec.result), 6)

    # 既有方案逐字节不变。
    base_after = service.get_result("plan-1").result.model_dump()
    assert base_after == base_before
    logger.info("判定依据：重规划前后 plan-1 响应完全一致，未被新请求改写")


def test_service_replan_based_on_missing_or_not_succeeded():
    logger.info("=== 用例：接续目标缺失/未成功的处理 ===")
    service = OptimizerService()
    req = _replan_request(based_on="missing")
    with pytest.raises(NotFoundError):
        service.submit_replan("r-x", req)
    logger.info("判定依据：接续不存在的方案 -> NOT_FOUND(404)")

    # 存在但还在 RUNNING / FAILED -> 400
    from tests.factories import make_request

    bad = make_request(supply=50, capacities=(5,))
    service.submit("plan-fail", bad)
    fail_rec = wait_for_terminal(service, "plan-fail")
    assert fail_rec.status is JobStatus.FAILED
    req2 = _replan_request(based_on="plan-fail")
    with pytest.raises(InvalidRequestError) as exc:
        service.submit_replan("r-y", req2)
    assert "尚未成功" in exc.value.message
    logger.info("判定依据：接续未成功方案 -> INVALID_REQUEST(400)")


def test_service_confirm_more_than_issued_rejected_before_registration():
    logger.info("=== 用例：确认量超过既有方案实发量，登记前 400 且不占键 ===")
    service = OptimizerService()
    _seed_base_plan(service, "plan-1")
    req = _replan_request(
        based_on="plan-1",
        confirmed=[ConfirmedAllocationIn(scenario="base", entity="a",
                                         window="w0", amount=999.0)],
    )
    with pytest.raises(InvalidRequestError) as exc:
        service.submit_replan("r-over", req)
    violations = exc.value.details["violations"]
    logger.info("超发确认明细=%s", violations)
    assert violations and violations[0]["issued_in_based_on"] == 70.0
    # 未登记作业：查询应 NOT_FOUND，不占用幂等键。
    with pytest.raises(NotFoundError):
        service.get_result("r-over")
    logger.info("判定依据：超发确认 400 且无作业落库，实发量 70 被准确指出")


def test_concurrent_duplicate_replans_compute_once():
    logger.info("=== 用例：同一重规划键 20 路并发只计算一次 ===")
    service = OptimizerService()
    _seed_base_plan(service, "plan-1")
    req = _replan_request(
        based_on="plan-1",
        confirmed=[ConfirmedAllocationIn(scenario="base", entity="a",
                                         window="w0", amount=30.0)],
    )
    key = "replan-race"
    n = 20
    barrier = threading.Barrier(n)
    started: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        out = service.submit_replan(key, req)
        with lock:
            started.append(out.started)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(started) == 1, "并发重规划必须只真正计算一次"
    logger.info("关键结果：%d 次并发重规划 started=True 次数=%d", n, sum(started))

    rec = wait_for_terminal(service, key)
    assert rec.status is JobStatus.SUCCEEDED
    assert rec.result.based_on_key == "plan-1"

    # 成功后重复提交：返回同一份固化对象。
    again = service.submit_replan(key, req)
    assert again.started is False
    assert service.get_result(key).result is rec.result
    logger.info("判定依据：重复提交 started=False 且返回同一结果对象")


def test_replan_timeout_and_cancel_semantics_unchanged():
    logger.info("=== 用例：重规划沿用超时/取消语义，且可同指纹重试成功 ===")
    service = OptimizerService()
    _seed_base_plan(service, "plan-1")

    slow = _replan_request(
        confirmed=[ConfirmedAllocationIn(scenario="base", entity="a",
                                         window="w0", amount=30.0)],
    )
    slow = slow.model_copy(update={"timeout_seconds": 0.02,
                                   "solver_step_delay": 0.05})
    service.submit_replan("r-slow", slow)
    rec = wait_for_terminal(service, "r-slow")
    assert rec.status is JobStatus.FAILED
    assert rec.error_code == "TIMEOUT"
    assert rec.result is None
    logger.info("判定依据：重规划超时落 FAILED/TIMEOUT，无部分结果")

    fast = slow.model_copy(update={"timeout_seconds": None,
                                   "solver_step_delay": 0.0})
    assert service.replan_fingerprint(slow) == service.replan_fingerprint(fast)
    service.submit_replan("r-slow", fast)
    ok = wait_for_terminal(service, "r-slow")
    assert ok.status is JobStatus.SUCCEEDED
    assert ok.result.based_on_key == "plan-1"
    logger.info("判定依据：超时后同指纹重规划重试成功，旧失败不污染")


# ---------------------------------------------------------------------------
# 把成功响应还原成求解器 SolveResult 形状以复用严格校验（仅测试用）。
# ---------------------------------------------------------------------------
def __from_response(response):
    from app.service.mapper import build_response  # noqa: F401
    from app.solver.models import (
        EntityAllocation,
        ScenarioResult,
        SolveResult,
        WindowAllocation,
    )

    scenarios = []
    for s in response.scenarios:
        scenarios.append(
            ScenarioResult(
                scenario=s.scenario,
                is_baseline=s.is_baseline,
                total_supply=s.total_supply,
                total_demand=s.total_demand,
                total_allocated=s.total_allocated,
                entity_allocations=[
                    EntityAllocation(
                        entity=e.entity, demand=e.demand,
                        min_share=e.min_share, amount=e.amount,
                    )
                    for e in s.entity_allocations
                ],
                window_allocations=[
                    WindowAllocation(
                        window=w.window, allocated=w.allocated,
                        capacity=w.capacity, entities=dict(w.entities),
                    )
                    for w in s.window_allocations
                ],
                feasible=True,
                infeasibility_reasons=[],
                confirmed_total=s.confirmed_total,
            )
        )
    return SolveResult(
        scenarios=scenarios,
        solver_version=response.solver_version,
        quantize_decimals=response.quantize_decimals,
    )
