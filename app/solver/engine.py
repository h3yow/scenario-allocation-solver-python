"""确定性多场景分配求解引擎（无外部求解器依赖）。

两步法（每个场景同一口径）：
1. 实体层：先满足最小保障，再按扰动后需求加权做水位填充（water-filling），
   上限为该实体需求，剩余供给恰好全部分配（总量守恒-实体侧）。
2. 窗口层：把实体分配量按固定顺序贪心装箱到滚动窗口，形成 entity×window
   分配矩阵；量化到固定小数位后把残差修补回矩阵，保证守恒精确成立。

取消与超时通过 ``_ControlGate`` 周期性检查区分：
- 取消优先：取消信号置位时抛 CancelledError；
- 否则超时抛 TimeoutError。
结果只依赖输入、配置与 SOLVER_VERSION，保证可复现。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from app.errors import (
    CancelledError,
    InfeasibleError,
    InvalidRequestError,
    SolverFailureError,
    TimeoutError,
)
from app.logging_setup import get_logger
from app.solver.models import (
    AllocationProblem,
    EntityAllocation,
    ScenarioResult,
    SolveResult,
    WindowAllocation,
)
from app.solver.validate import check_feasibility, scenario_demand, validate_problem

logger = get_logger("app.solver.engine")

SOLVER_VERSION = "solver-1.0.0"
DEFAULT_DECIMALS = 9
_EPS = 1e-9

CancelCheck = Callable[[], bool]


class _ControlGate:
    """统一的取消/超时检查点。取消判定优先于超时。"""

    def __init__(
        self,
        timeout_seconds: float | None,
        is_cancelled: CancelCheck | None,
        step_delay: float,
    ) -> None:
        self._deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds else None
        )
        self._is_cancelled = is_cancelled
        self.step_delay = step_delay

    def check(self, phase: str) -> None:
        if self._is_cancelled is not None and self._is_cancelled():
            logger.info("检查点 phase=%s 命中取消信号", phase)
            raise CancelledError("优化作业被主动取消")
        if self._deadline is not None and time.monotonic() > self._deadline:
            logger.info("检查点 phase=%s 命中求解超时", phase)
            raise TimeoutError("求解超时")
        if self.step_delay:
            time.sleep(self.step_delay)


def solve(
    problem: AllocationProblem,
    *,
    timeout_seconds: float | None = None,
    is_cancelled: CancelCheck | None = None,
    step_delay: float = 0.0,
    quantize_decimals: int = DEFAULT_DECIMALS,
) -> SolveResult:
    """求解全部场景；不可行时抛 InfeasibleError，不返回任何部分结果。"""
    logger.info(
        "开始求解: scenarios=%d timeout=%s delay=%.4f decimals=%d version=%s",
        len(problem.scenarios),
        timeout_seconds,
        step_delay,
        quantize_decimals,
        SOLVER_VERSION,
    )
    validate_problem(problem)
    gate = _ControlGate(timeout_seconds, is_cancelled, step_delay)

    infeasible = check_feasibility(problem)
    if infeasible:
        # 不可行：直接失败，绝不返回/伪造部分结果。
        logger.warning("问题不可行，拒绝求解，共 %d 条原因", len(infeasible))
        raise InfeasibleError(
            "分配问题不可行，无法给出满足全部约束的结果",
            details={"reasons": infeasible},
        )

    grid_issues = _check_grid_feasibility(problem, quantize_decimals)
    if grid_issues:
        logger.warning("量化网格不可行（decimals=%d）: %s", quantize_decimals, grid_issues)
        raise InvalidRequestError(
            f"在 {quantize_decimals} 位小数的量化网格上无法同时满足容量/保底/守恒，"
            "请提高 quantize_decimals",
            details={"reasons": grid_issues, "quantize_decimals": quantize_decimals},
        )

    gate.check("feasibility")

    results: list[ScenarioResult] = []
    for scenario in problem.scenarios:
        gate.check(f"scenario:{scenario.name}")
        results.append(
            _solve_scenario(
                problem,
                scenario,
                decimals=quantize_decimals,
                gate=gate,
            )
        )

    logger.info("求解完成，场景数=%d", len(results))
    return SolveResult(
        scenarios=results,
        solver_version=SOLVER_VERSION,
        quantize_decimals=quantize_decimals,
    )


def _check_grid_feasibility(problem: AllocationProblem, decimals: int) -> list[str]:
    """量化网格级可行性预检（单位：量子的整数倍）。

    在选定小数位下，容量 floor、保底 ceil、供给/需求 round 后必须仍存在
    合法整数方案，否则应在求解前明确拒绝，而不是产生半成品。
    """
    scale = 10**decimals
    cap_units = [int(w.capacity * scale + 1e-6) for w in problem.windows]
    total_cap = sum(cap_units)
    issues: list[str] = []

    for s in problem.scenarios:
        demands = scenario_demand(problem, s)
        supply_u = int(round(s.total_supply * scale))
        ceil_min = sum(_ceil_units(e.min_share, scale) for e in problem.entities)
        floor_demand = sum(int(demands[e.name] * scale + 1e-6) for e in problem.entities)

        if supply_u > total_cap:
            issues.append(
                f"scenario '{s.name}': 量化后供给 {supply_u} 单位超过窗口容量合计 "
                f"{total_cap} 单位（容量边界被四舍五入吞掉）"
            )
        if ceil_min > supply_u:
            issues.append(
                f"scenario '{s.name}': 量化后保底合计 {ceil_min} 单位超过供给 "
                f"{supply_u} 单位"
            )
        if supply_u > floor_demand:
            issues.append(
                f"scenario '{s.name}': 量化后供给 {supply_u} 单位超过需求上限合计 "
                f"{floor_demand} 单位"
            )
        for e in problem.entities:
            ceil_min_e = _ceil_units(e.min_share, scale)
            floor_dem_e = int(demands[e.name] * scale + 1e-6)
            if ceil_min_e > floor_dem_e:
                issues.append(
                    f"scenario '{s.name}': entity '{e.name}' 量化后保底 "
                    f"{ceil_min_e} 单位高于扰动需求上限 {floor_dem_e} 单位"
                )
    return issues


def _ceil_units(x: float, scale: int) -> int:
    import math

    return max(0, math.ceil(x * scale - 1e-6))


def _solve_scenario(problem, scenario, *, decimals: int, gate: _ControlGate) -> ScenarioResult:
    demands = scenario_demand(problem, scenario)
    supply = scenario.total_supply
    logger.info(
        "求解场景 scenario=%s baseline=%s supply=%.6f demands=%s",
        scenario.name,
        scenario.is_baseline,
        supply,
        {k: round(v, 6) for k, v in demands.items()},
    )

    # ---- 第一步：实体层水位填充 ----
    gate.check(f"entities:{scenario.name}")
    entity_amounts = _allocate_entities(
        names=[e.name for e in problem.entities],
        demands=demands,
        min_shares={e.name: e.min_share for e in problem.entities},
        supply=supply,
    )
    logger.info(
        "实体层分配完成 scenario=%s %s",
        scenario.name,
        {k: round(v, 6) for k, v in entity_amounts.items()},
    )

    # ---- 第二步：窗口层整数网格装箱（含量化与守恒修补）----
    gate.check(f"windows:{scenario.name}")
    matrix = _quantize_and_repair(
        names_e=[e.name for e in problem.entities],
        names_w=[w.name for w in problem.windows],
        capacities={w.name: w.capacity for w in problem.windows},
        target_totals=entity_amounts,
        bounds={e.name: (e.min_share, demands[e.name]) for e in problem.entities},
        supply=supply,
        decimals=decimals,
    )

    result = _assemble(
        scenario_name=scenario.name,
        is_baseline=scenario.is_baseline,
        supply=supply,
        decimals=decimals,
        demands=demands,
        min_shares={e.name: e.min_share for e in problem.entities},
        capacities={w.name: w.capacity for w in problem.windows},
        names_e=[e.name for e in problem.entities],
        names_w=[w.name for w in problem.windows],
        matrix=matrix,
    )

    violations = _verify(result, decimals)
    if violations:
        raise SolverFailureError(
            f"场景 '{scenario.name}' 求解结果未通过内部约束校验",
            details={"violations": violations},
        )
    logger.info("场景 scenario=%s 约束校验通过（容量/最小保障/守恒均成立）", scenario.name)
    return result


def _allocate_entities(
    *,
    names: list[str],
    demands: dict[str, float],
    min_shares: dict[str, float],
    supply: float,
) -> dict[str, float]:
    """先保底、后水位填充，输出各实体分配量（浮点）。"""
    amounts = {n: min_shares[n] for n in names}
    remaining = supply - sum(amounts.values())
    if remaining < -_EPS:  # 已由可行性检查拦截，此处为防御
        raise SolverFailureError("保底后剩余供给为负")

    # 水位填充：活动集合按需求量加权分配剩余，直到全部触顶或剩余耗尽。
    active = {n for n in names if amounts[n] < demands[n] - _EPS}
    while remaining > _EPS and active:
        weight = sum(demands[n] for n in active)
        if weight <= _EPS:
            raise SolverFailureError("活动实体需求权重为正但计算为零")
        step = remaining / weight
        capped: list[str] = []
        for n in active:
            headroom = demands[n] - amounts[n]
            increment = demands[n] * step
            if increment >= headroom - _EPS:
                capped.append(n)
        if not capped:
            for n in active:
                amounts[n] += demands[n] * step
            remaining = 0.0
            break
        # 本轮最大可行步长：让最先触顶的实体恰好到顶。
        feasible_step = min((demands[n] - amounts[n]) / demands[n] for n in capped)
        for n in active:
            amounts[n] += demands[n] * feasible_step
        remaining -= feasible_step * weight
        active = {n for n in active if amounts[n] < demands[n] - _EPS}

    # 消除浮点抖动。
    for n in names:
        if abs(amounts[n]) < _EPS:
            amounts[n] = 0.0
    return amounts


def _quantize_and_repair(
    *,
    names_e: list[str],
    names_w: list[str],
    capacities: dict[str, float],
    target_totals: dict[str, float],
    bounds: dict[str, tuple[float, float]],
    supply: float,
    decimals: int,
) -> dict[tuple[str, str], float]:
    """纯整数网格量化与修补。

    所有格子取值均为量子 q=10**-decimals 的非负整数倍：
    - 列容量上限 U_w = floor(capacity_w / q)；
    - 行目标 T_e（行单位合计对齐 round(supply/q)，保证总量守恒）；
    - 行下限 L_e = floor(min_share_e / q)（保底，绝不向下击穿）。
    逐行贪心分配（实体外层、窗口内层），可行性已保证存在合法整数方案。
    """
    quantum = 10**-decimals
    scale = 10**decimals

    col_cap = {w: int(capacities[w] * scale + 1e-6) for w in names_w}
    floor_units = {e: int(bounds[e][0] * scale + 1e-6) for e in names_e}
    ceil_units = {e: int(bounds[e][1] * scale + 1e-6) for e in names_e}

    # 行目标（整数单位），随后把合计对齐到供给的整数单位。
    targets = {e: int(round(target_totals[e] / quantum)) for e in names_e}
    supply_units = int(round(supply / quantum))

    # 缺口摊派严格落在 [保底, 需求上限] 内：
    # 正缺口 -> 需求余量最大的实体；负缺口 -> 保底余量最大的实体。
    def _adjust(delta: int) -> None:
        while delta > 0:
            cand = [e for e in names_e if targets[e] < ceil_units[e]]
            if not cand:
                raise SolverFailureError("量化网格下无法在需求上限内守恒")
            e = max(cand, key=lambda x: ceil_units[x] - targets[x])
            targets[e] += 1
            delta -= 1
        while delta < 0:
            cand = [e for e in names_e if targets[e] > floor_units[e]]
            if not cand:
                raise SolverFailureError("量化网格下无法在保底之上守恒")
            e = max(cand, key=lambda x: targets[x] - floor_units[x])
            targets[e] -= 1
            delta += 1

    _adjust(supply_units - sum(targets.values()))

    if sum(targets.values()) != supply_units:
        raise SolverFailureError("量化精度不足：最小保障在该小数位下无法守恒")
    if any(targets[e] < floor_units[e] for e in names_e):
        raise SolverFailureError("量化精度不足：最小保障在该小数位下被击穿")

    col_used = {w: 0 for w in names_w}
    units: dict[tuple[str, str], int] = {}
    # 列水位填充：外层窗口（逐列填满到 floor 容量上限），内层实体按固定顺序。
    # 总供给 <= 列容量合计（可行性已保证），因此必定能装完且不产生碎片失败。
    row_left = dict(targets)
    for w in names_w:
        room = col_cap[w]
        for e in names_e:
            if room == 0:
                break
            if row_left[e] <= 0:
                continue
            take = min(row_left[e], room)
            if take > 0:
                units[(e, w)] = take
                row_left[e] -= take
                room -= take
        col_used[w] = col_cap[w] - room

    leftover = sum(row_left.values())
    if leftover != 0:
        raise SolverFailureError(
            f"量化网格下无足够窗口容量（剩余 {leftover * quantum}）"
        )

    return {k: round(v * quantum, decimals) for k, v in units.items()}


def _assemble(
    *,
    scenario_name: str,
    is_baseline: bool,
    supply: float,
    decimals: int,
    demands: dict[str, float],
    min_shares: dict[str, float],
    capacities: dict[str, float],
    names_e: list[str],
    names_w: list[str],
    matrix: dict[tuple[str, str], float],
) -> ScenarioResult:
    entity_allocations = [
        EntityAllocation(
            entity=e,
            demand=demands[e],
            min_share=min_shares[e],
            amount=round(sum(matrix.get((e, w), 0.0) for w in names_w), decimals),
        )
        for e in names_e
    ]
    window_allocations = [
        WindowAllocation(
            window=w,
            allocated=round(sum(matrix.get((e, w), 0.0) for e in names_e), decimals),
            capacity=capacities[w],
            entities={
                e: matrix[(e, w)] for e in names_e if (e, w) in matrix and matrix[(e, w)] != 0.0
            },
        )
        for w in names_w
    ]
    total_allocated = round(sum(wa.allocated for wa in window_allocations), decimals)
    return ScenarioResult(
        scenario=scenario_name,
        is_baseline=is_baseline,
        total_supply=round(supply, decimals),
        total_demand=round(sum(demands.values()), decimals),
        total_allocated=total_allocated,
        entity_allocations=entity_allocations,
        window_allocations=window_allocations,
        feasible=True,
        infeasibility_reasons=[],
    )


def _verify(result: ScenarioResult, decimals: int) -> list[str]:
    """对最终结果做独立约束校验（容量/最小保障/总量守恒）。"""
    tol = 2 * 10**-decimals
    violations: list[str] = []

    for wa in result.window_allocations:
        if wa.allocated > wa.capacity + tol:
            violations.append(
                f"窗口 '{wa.window}' 超容量: {wa.allocated} > {wa.capacity}"
            )

    for ea in result.entity_allocations:
        if ea.amount < ea.min_share - tol:
            violations.append(
                f"实体 '{ea.entity}' 低于最小保障: {ea.amount} < {ea.min_share}"
            )
        if ea.amount > ea.demand + tol:
            violations.append(
                f"实体 '{ea.entity}' 超过需求上限: {ea.amount} > {ea.demand}"
            )

    if abs(result.total_allocated - result.total_supply) > tol:
        violations.append(
            f"总量不守恒: 分配 {result.total_allocated} != 供给 {result.total_supply}"
        )

    entity_sum = round(sum(ea.amount for ea in result.entity_allocations), 12)
    window_sum = round(sum(wa.allocated for wa in result.window_allocations), 12)
    if abs(entity_sum - window_sum) > tol:
        violations.append(f"实体侧与窗口侧合计不一致: {entity_sum} != {window_sum}")

    return violations
