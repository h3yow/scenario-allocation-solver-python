"""带已确认额度的重规划求解（在既有成功方案之上只重排未确认部分）。

与普通求解同一口径（容量 / 最小保障 / 需求上限 / 总量守恒，基线与扰动
场景完全一致），区别在于：调用方给出的已确认格子
``(scenario, entity, window, amount)`` 在新方案中必须**原样保留**——
残量供给不允许再流入这些格子（既不会被重新分配、挪走，也不会少算）。

失败严格分类，且在做任何分配之前逐场景汇总，绝不返回半成品：

- ``INVALID_REQUEST``(400)：重规划请求结构非法（未知引用、重复格子、
  非正确量），或当前 ``quantize_decimals`` 的整数网格本身装不下；
- ``CONFIRMED_CONFLICT``(412)：已确认额度与**最新输入**直接冲突
  （窗口装不下 / 实体确认量超过其当前上限 / 确认总量超过供给）；
- ``INFEASIBLE``(422)：输入合法、锁也不冲突，但锁占用后残量问题
  整体无可行解（锁定把可拆分的容量切碎，残量保底无处安放）。
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

from app.errors import (
    CancelledError,
    ConfirmedConflictError,
    InfeasibleError,
    InvalidRequestError,
    SolverFailureError,
    TimeoutError,
)
from app.logging_setup import get_logger
from app.solver.engine import (
    DEFAULT_DECIMALS,
    SOLVER_VERSION,
    _allocate_entities,
    _assemble,
    _ceil_units,
    _verify,
)
from app.solver.models import (
    AllocationProblem,
    ConfirmedCell,
    ScenarioResult,
    SolveResult,
)
from app.solver.validate import check_feasibility, scenario_demand, validate_problem

logger = get_logger("app.solver.replan")

_EPS = 1e-9
CancelCheck = Callable[[], bool]


class _Gate:
    """取消/超时检查点，与主引擎同一语义（取消优先）。"""

    def __init__(self, timeout_seconds, is_cancelled, step_delay):
        self._deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds else None
        )
        self._is_cancelled = is_cancelled
        self.step_delay = step_delay

    def check(self, phase: str) -> None:
        if self._is_cancelled is not None and self._is_cancelled():
            logger.info("检查点 phase=%s 命中取消信号", phase)
            raise CancelledError("重规划作业被主动取消")
        if self._deadline is not None and time.monotonic() > self._deadline:
            logger.info("检查点 phase=%s 命中求解超时", phase)
            raise TimeoutError("重规划求解超时")
        if self.step_delay:
            time.sleep(self.step_delay)


# ---------------------------------------------------------------------------
# 结构校验（登记前可同步调用）
# ---------------------------------------------------------------------------
def _cell_dict(c: ConfirmedCell) -> dict:
    return {"scenario": c.scenario, "entity": c.entity,
            "window": c.window, "amount": c.amount}


def _fmt(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".")


def validate_replan_structure(
    problem: AllocationProblem, confirmed: list[ConfirmedCell]
) -> None:
    """结构非法 -> InvalidRequestError（400），与优化可行性无关。"""
    validate_problem(problem)
    scenario_names = {s.name for s in problem.scenarios}
    entity_names = {e.name for e in problem.entities}
    window_names = {w.name for w in problem.windows}

    seen: set[tuple[str, str, str]] = set()
    for c in confirmed:
        if c.scenario not in scenario_names:
            raise InvalidRequestError(
                f"确认额度引用了未知 scenario '{c.scenario}'",
                details={"cell": _cell_dict(c)},
            )
        if c.entity not in entity_names:
            raise InvalidRequestError(
                f"确认额度引用了未知 entity '{c.entity}'",
                details={"cell": _cell_dict(c)},
            )
        if c.window not in window_names:
            raise InvalidRequestError(
                f"确认额度引用了未知 window '{c.window}'",
                details={"cell": _cell_dict(c)},
            )
        if c.amount <= 0:
            raise InvalidRequestError(
                "确认额度必须为正数（零额度无需确认）",
                details={"cell": _cell_dict(c)},
            )
        key = (c.scenario, c.entity, c.window)
        if key in seen:
            raise InvalidRequestError(
                f"确认额度重复: scenario='{c.scenario}' entity='{c.entity}' "
                f"window='{c.window}' 出现多次",
                details={"cell": _cell_dict(c)},
            )
        seen.add(key)
    logger.info("重规划结构校验通过：确认格子 %d 个", len(confirmed))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def replan(
    problem: AllocationProblem,
    confirmed: list[ConfirmedCell],
    *,
    timeout_seconds: float | None = None,
    is_cancelled: CancelCheck | None = None,
    step_delay: float = 0.0,
    quantize_decimals: int = DEFAULT_DECIMALS,
) -> SolveResult:
    """在锁定已确认格子的前提下重排未确认部分；失败整体抛出，无半成品。"""
    logger.info(
        "开始重规划: scenarios=%d confirmed_cells=%d timeout=%s delay=%.4f "
        "decimals=%d version=%s",
        len(problem.scenarios), len(confirmed), timeout_seconds,
        step_delay, quantize_decimals, SOLVER_VERSION,
    )
    validate_replan_structure(problem, confirmed)
    gate = _Gate(timeout_seconds, is_cancelled, step_delay)

    base_infeasible = check_feasibility(problem)
    if base_infeasible:
        logger.warning("最新输入本身不可行，拒绝重规划，共 %d 条原因",
                       len(base_infeasible))
        raise InfeasibleError(
            "重规划所依据的最新输入不可行，无法给出满足全部约束的结果",
            details={"reasons": base_infeasible},
        )

    gate.check("feasibility")

    locks_by_scenario: dict[str, list[ConfirmedCell]] = {
        s.name: [] for s in problem.scenarios
    }
    for c in confirmed:
        locks_by_scenario[c.scenario].append(c)
        logger.info("锁定确认额度 scenario=%s entity=%s window=%s amount=%s",
                    c.scenario, c.entity, c.window, _fmt(c.amount))

    # 第一轮：逐场景完成全部失败判定（任何分配之前）。
    conflict_reasons: list[str] = []
    infeasible_reasons: list[str] = []
    grid_issues: list[str] = []
    plans: dict[str, dict] = {}
    for s in problem.scenarios:
        gate.check(f"check:{s.name}")
        plan = _prepare_scenario(problem, s, locks_by_scenario[s.name],
                                 quantize_decimals)
        plans[s.name] = plan
        conflict_reasons.extend(plan["conflicts"])
        infeasible_reasons.extend(plan["infeasible"])
        grid_issues.extend(plan["grid_issues"])

    if conflict_reasons:
        logger.warning("确认额度与最新输入冲突，共 %d 条", len(conflict_reasons))
        raise ConfirmedConflictError(
            "已确认额度与最新输入冲突，无法在原样保留确认额度的前提下重规划",
            details={"reasons": conflict_reasons},
        )
    if grid_issues:
        logger.warning("量化网格不可行（decimals=%d）: %s",
                       quantize_decimals, grid_issues)
        raise InvalidRequestError(
            f"在 {quantize_decimals} 位小数的量化网格上无法同时满足容量/保底/守恒"
            "（含已确认额度），请提高 quantize_decimals",
            details={"reasons": grid_issues,
                     "quantize_decimals": quantize_decimals},
        )
    if infeasible_reasons:
        logger.warning("锁定后残量问题整体不可行，共 %d 条",
                       len(infeasible_reasons))
        raise InfeasibleError(
            "已确认额度虽不与最新输入冲突，但锁定后残量重排无可行解",
            details={"reasons": infeasible_reasons},
        )

    # 第二轮：逐场景求解（此时所有场景已确认可行）。
    results: list[ScenarioResult] = []
    for s in problem.scenarios:
        gate.check(f"solve:{s.name}")
        results.append(
            _solve_scenario(problem, s, locks_by_scenario[s.name],
                            plans[s.name], quantize_decimals, gate)
        )

    logger.info("重规划完成，场景数=%d", len(results))
    return SolveResult(
        scenarios=results,
        solver_version=SOLVER_VERSION,
        quantize_decimals=quantize_decimals,
    )


# ---------------------------------------------------------------------------
# 单场景判定
# ---------------------------------------------------------------------------
def _prepare_scenario(problem, scenario, locks, decimals):
    scale = 10**decimals
    names_e = [e.name for e in problem.entities]
    names_w = [w.name for w in problem.windows]
    demands = scenario_demand(problem, scenario)
    min_shares = {e.name: e.min_share for e in problem.entities}
    capacities = {w.name: w.capacity for w in problem.windows}
    supply = scenario.total_supply

    conflicts: list[str] = []
    infeasible: list[str] = []
    grid_issues: list[str] = []

    # 严格守恒的前提：总供给必须恰好落在量子网格上。否则在该精度下无论如何
    # 分配都无法既“分完真实供给”又只用量子整数倍，必须明确拒绝（400），
    # 而不是四舍五入后返回一个看似守恒、实际少分/多分的“成功”。
    supply_exact_u = int(round(supply * scale))
    if abs(supply - supply_exact_u / scale) > 1e-12:
        grid_issues.append(
            f"scenario '{scenario.name}': 总供给 {_fmt(supply)} 不是 "
            f"1e-{decimals} 的整数倍，该精度下无法严格守恒，请提高 "
            "quantize_decimals"
        )

    lock_units: dict[tuple[str, str], int] = {}
    misaligned: list[str] = []
    for c in locks:
        exact = c.amount * scale
        units = int(round(exact))
        # 严格网格对齐：确认额度必须是量子的精确整数倍（不做四舍五入吞掉）。
        if abs(c.amount - units / scale) > 1e-12:
            misaligned.append(
                f"scenario '{scenario.name}': entity '{c.entity}' 在窗口 "
                f"'{c.window}' 的确认额度 {_fmt(c.amount)} 不是 "
                f"1e-{decimals} 的整数倍，无法在当前小数精度下原样保留"
            )
            continue
        lock_units[(c.entity, c.window)] = (
            lock_units.get((c.entity, c.window), 0) + units
        )
    if misaligned:
        grid_issues.extend(misaligned)

    forbidden = set(lock_units)
    row_lock = {e: sum(lock_units.get((e, w), 0) for w in names_w)
                for e in names_e}
    col_lock = {w: sum(lock_units.get((e, w), 0) for e in names_e)
                for w in names_w}
    total_lock = sum(row_lock.values())
    row_lock_f = {e: row_lock[e] / scale for e in names_e}
    col_lock_f = {w: col_lock[w] / scale for w in names_w}
    total_lock_f = total_lock / scale

    logger.info(
        "场景=%s 锁定汇总: 锁定总量=%s 供给=%s 行锁=%s 列锁=%s 禁格=%d",
        scenario.name, _fmt(total_lock_f), _fmt(supply),
        {e: _fmt(row_lock_f[e]) for e in names_e if row_lock_f[e] > 0},
        {w: _fmt(col_lock_f[w]) for w in names_w if col_lock_f[w] > 0},
        len(forbidden),
    )

    # 412：锁与最新输入的直接冲突（浮点层面）。
    for e in names_e:
        if row_lock_f[e] > demands[e] + _EPS:
            conflicts.append(
                f"scenario '{scenario.name}': entity '{e}' 已确认量 "
                f"{_fmt(row_lock_f[e])} 超过其当前需求上限 "
                f"{_fmt(demands[e])}"
            )
    for w in names_w:
        if col_lock_f[w] > capacities[w] + _EPS:
            conflicts.append(
                f"scenario '{scenario.name}': window '{w}' 已确认占用 "
                f"{_fmt(col_lock_f[w])}，超过该窗口当前容量 "
                f"{_fmt(capacities[w])}，窗口已装不下"
            )
    if total_lock_f > supply + _EPS:
        conflicts.append(
            f"scenario '{scenario.name}': 确认总量 {_fmt(total_lock_f)} "
            f"超过场景总供给 {_fmt(supply)}，供给总量对不上"
        )

    # 整数单位层面的同类冲突（紧边界被舍入吞掉时补一条网格口径）。
    supply_u = supply_exact_u
    col_cap_u = {w: int(capacities[w] * scale + 1e-6) for w in names_w}
    dem_u = {e: int(demands[e] * scale + 1e-6) for e in names_e}
    for e in names_e:
        if row_lock[e] > dem_u[e] and row_lock_f[e] <= demands[e] + _EPS:
            conflicts.append(
                f"scenario '{scenario.name}': entity '{e}' 确认量 "
                f"{row_lock[e]} 单位在量化网格上高于其需求上限 "
                f"{dem_u[e]} 单位（边界被舍入吞掉）"
            )
    for w in names_w:
        if col_lock[w] > col_cap_u[w] and col_lock_f[w] <= capacities[w] + _EPS:
            conflicts.append(
                f"scenario '{scenario.name}': window '{w}' 确认占用 "
                f"{col_lock[w]} 单位在量化网格上高于窗口容量 "
                f"{col_cap_u[w]} 单位（边界被舍入吞掉）"
            )
    if total_lock > supply_u and total_lock_f <= supply + _EPS:
        conflicts.append(
            f"scenario '{scenario.name}': 确认总量 {total_lock} 单位在量化网格上"
            f"高于供给 {supply_u} 单位（边界被舍入吞掉）"
        )

    # 422：锁定后残量的整体可行性（聚合条件）。
    residual_supply = supply - total_lock_f
    residual_floor = sum(
        max(0.0, min_shares[e] - row_lock_f[e]) for e in names_e
    )
    residual_demand_cap = sum(
        max(0.0, demands[e] - row_lock_f[e]) for e in names_e
    )
    if residual_floor > residual_supply + _EPS:
        infeasible.append(
            f"scenario '{scenario.name}': 锁定后各实体尚需的最小保障合计 "
            f"{_fmt(residual_floor)} 超过剩余供给 {_fmt(residual_supply)}"
        )
    if residual_supply > residual_demand_cap + _EPS:
        infeasible.append(
            f"scenario '{scenario.name}': 剩余供给 {_fmt(residual_supply)} "
            f"超过各实体未锁定的需求上限合计 {_fmt(residual_demand_cap)}，"
            "无法在不超发的情况下守恒"
        )

    floor_e_u = {
        e: max(0, _ceil_units(min_shares[e], scale) - row_lock[e])
        for e in names_e
    }
    ceil_e_u = {e: max(0, dem_u[e] - row_lock[e]) for e in names_e}
    cap_w_u = {w: max(0, col_cap_u[w] - col_lock[w]) for w in names_w}
    total_u = supply_u - total_lock

    if total_u >= 0 and not misaligned and not conflicts:
        strict_ok = _flow_feasible(
            names_e, names_w, floor_e_u, ceil_e_u, cap_w_u, forbidden, total_u
        )
        if strict_ok:
            logger.info("场景=%s 残量网格可行（严格口径）剩余供给=%d 单位",
                        scenario.name, total_u)
        else:
            loose_floor = {
                e: max(0, int(max(0.0, min_shares[e] - row_lock_f[e]) * scale
                              + 1e-6))
                for e in names_e
            }
            loose_ceil = {
                e: max(0, math.ceil(max(0.0, demands[e] - row_lock_f[e]) * scale
                                    - 1e-6))
                for e in names_e
            }
            loose_cap = {
                w: max(0, math.ceil(max(0.0, capacities[w] - col_lock_f[w])
                                    * scale - 1e-6))
                for w in names_w
            }
            loose_ok = _flow_feasible(
                names_e, names_w, loose_floor, loose_ceil, loose_cap,
                forbidden, total_u,
            )
            if loose_ok:
                grid_issues.append(
                    f"scenario '{scenario.name}': 锁定后的残量运输在 "
                    f"{decimals} 位小数的严格整数网格上无解（放宽舍入后可行），"
                    "请提高 quantize_decimals"
                )
            else:
                infeasible.append(
                    f"scenario '{scenario.name}': 已确认额度占用后，剩余供给 "
                    f"{total_u} 单位无法在不挪动确认额度的情况下分配"
                    "（未锁定格子的容量/上限组合不足以承接残量保底与守恒）"
                )

    return {
        "demands": demands,
        "min_shares": min_shares,
        "capacities": capacities,
        "supply": supply,
        "forbidden": forbidden,
        "lock_units": lock_units,
        "row_lock": row_lock,
        "col_lock": col_lock,
        "total_lock": total_lock,
        "floor_e_u": floor_e_u,
        "ceil_e_u": ceil_e_u,
        "cap_w_u": cap_w_u,
        "total_u": total_u,
        "conflicts": conflicts,
        "infeasible": infeasible,
        "grid_issues": grid_issues,
    }


# ---------------------------------------------------------------------------
# 单场景求解
# ---------------------------------------------------------------------------
def _solve_scenario(problem, scenario, locks, plan, decimals, gate):
    scale = 10**decimals
    quantum = 10**-decimals
    names_e = [e.name for e in problem.entities]
    names_w = [w.name for w in problem.windows]
    demands = plan["demands"]
    min_shares = plan["min_shares"]
    capacities = plan["capacities"]
    supply = plan["supply"]
    lock_units = plan["lock_units"]
    row_lock = plan["row_lock"]
    total_lock = plan["total_lock"]

    # 实体层：在残量需求上限内做水位填充，目标为残量供给。
    gate.check(f"entities:{scenario.name}")
    residual_demands = {e: max(0.0, demands[e] - row_lock[e] / scale)
                        for e in names_e}
    residual_supply = max(0.0, supply - total_lock / scale)
    residual_min = {
        e: max(0.0, min_shares[e] - row_lock[e] / scale) for e in names_e
    }
    row_targets_f = _allocate_entities(
        names=names_e,
        demands=residual_demands,
        min_shares=residual_min,
        supply=residual_supply,
    )
    row_targets = {
        e: int(round(row_targets_f[e] / quantum)) for e in names_e
    }
    # 对齐残量总供给单位（缺口摊派落在残量 [保底, 上限] 内）。
    delta = plan["total_u"] - sum(row_targets.values())

    def _adjust(d: int) -> None:
        while d > 0:
            cand = [e for e in names_e if row_targets[e] < plan["ceil_e_u"][e]]
            if not cand:
                raise SolverFailureError("重规划：量化网格下无法在需求上限内守恒")
            e = max(cand, key=lambda x: plan["ceil_e_u"][x] - row_targets[x])
            row_targets[e] += 1
            d -= 1
        while d < 0:
            cand = [e for e in names_e if row_targets[e] > plan["floor_e_u"][e]]
            if not cand:
                raise SolverFailureError("重规划：量化网格下无法在保底之上守恒")
            e = max(cand, key=lambda x: row_targets[x] - plan["floor_e_u"][x])
            row_targets[e] -= 1
            d += 1

    _adjust(delta)
    logger.info(
        "场景=%s 残量实体层目标=%s 锁=%s",
        scenario.name,
        {e: row_targets[e] for e in names_e if row_targets[e] > 0},
        {e: row_lock[e] for e in names_e if row_lock[e] > 0},
    )

    # 窗口层：下界最大流保证禁格下可行。优先按水位填充的精确行目标运输；
    # 若该组行目标在禁格下不可路由（但 [保底,上限] 内存在别的分配，预检
    # 已保证可行），退化为弹性边界取一个确定性的可行整数流。
    gate.check(f"windows:{scenario.name}")
    residual = _flow_allocate(
        names_e, names_w,
        row_targets, row_targets, plan["cap_w_u"], plan["forbidden"],
        plan["total_u"],
    )
    if residual is None:
        logger.info(
            "场景=%s 精确行目标在禁格下不可路由，改用弹性边界确定性运输",
            scenario.name,
        )
        residual = _flow_allocate(
            names_e, names_w,
            plan["floor_e_u"], plan["ceil_e_u"], plan["cap_w_u"],
            plan["forbidden"], plan["total_u"],
        )
    if residual is None:
        # 预检已保证可行；走到这里属于内部错误而非不可行。
        raise SolverFailureError(
            f"场景 '{scenario.name}' 重规划残量运输意外失败（预检应已拦截）"
        )

    # 合并锁定格子（锁定格子绝不接收残量流）。
    units = dict(lock_units)
    for key, v in residual.items():
        if v > 0:
            units[key] = units.get(key, 0) + v
    matrix = {k: round(v * quantum, decimals) for k, v in units.items()}

    result = _assemble(
        scenario_name=scenario.name,
        is_baseline=scenario.is_baseline,
        supply=supply,
        decimals=decimals,
        demands=demands,
        min_shares=min_shares,
        capacities=capacities,
        names_e=names_e,
        names_w=names_w,
        matrix=matrix,
    )
    confirmed_cells = tuple(sorted(locks, key=lambda c: (names_e.index(c.entity),
                                                         names_w.index(c.window))))
    result = _attach_confirmed(result, total_lock / scale, confirmed_cells,
                               decimals)

    violations = _verify(result, decimals)
    violations.extend(_verify_locks(result, locks, decimals))
    if violations:
        raise SolverFailureError(
            f"场景 '{scenario.name}' 重规划结果未通过内部约束校验",
            details={"violations": violations},
        )
    logger.info(
        "场景=%s 重规划约束校验通过（锁定 %s，容量/保底/守恒严格成立）",
        scenario.name, _fmt(total_lock / scale),
    )
    return result


def _attach_confirmed(result: ScenarioResult, confirmed_total: float,
                      confirmed_cells, decimals: int) -> ScenarioResult:
    import dataclasses

    return dataclasses.replace(
        result,
        confirmed_total=round(confirmed_total, decimals),
        confirmed_cells=tuple(confirmed_cells),
    )


def _verify_locks(result: ScenarioResult, locks, decimals: int) -> list[str]:
    """严格核对：每个锁定格子的量必须原样保留。

    允许至多半个量子的浮点抖动（锁定量在预检时已强制网格对齐，真实偏差
    只能是 0 或整数量子级，半量子阈值等价于“必须落在同一网格点”，不放水）。
    """
    tol = 0.5 * 10**-decimals
    cell_value: dict[tuple[str, str], float] = {}
    for wa in result.window_allocations:
        for e, v in wa.entities.items():
            cell_value[(e, wa.window)] = v
    violations: list[str] = []
    for c in locks:
        actual = cell_value.get((c.entity, c.window), 0.0)
        if abs(actual - c.amount) > tol:
            violations.append(
                f"确认额度被破坏: scenario='{c.scenario}' entity='{c.entity}' "
                f"window='{c.window}' 应={c.amount} 实={actual}"
            )
    return violations


# ---------------------------------------------------------------------------
# 整数下界最大流（Edmonds-Karp，边顺序固定 => 确定性）
# ---------------------------------------------------------------------------
def _flow_feasible(names_e, names_w, row_lo, row_hi, col_cap, forbidden,
                   total) -> bool:
    return _build_and_flow(
        names_e, names_w, row_lo, row_hi, col_cap, forbidden, total
    )[0]


def _flow_allocate(names_e, names_w, row_lo, row_hi, col_cap, forbidden,
                   total):
    ok, alloc = _build_and_flow(
        names_e, names_w, row_lo, row_hi, col_cap, forbidden, total
    )
    return alloc if ok else None


def _build_and_flow(names_e, names_w, row_lo, row_hi, col_cap, forbidden,
                    total):
    ss, tt, src, snk = 0, 1, 2, 3
    e_base = 4
    w_base = e_base + len(names_e)
    n_nodes = w_base + len(names_w)
    mf = _MaxFlow(n_nodes)
    balance = [0] * n_nodes
    # 每项: (entity, window, 边尾u, 边在 g[u] 中的下标, lower, 原始可增广容量)
    cell_refs: list[tuple[str, str, int, int, int, int]] = []

    def _lb_edge(u, v, lower, upper):
        span = upper - lower
        idx = mf.add_edge(u, v, span)
        balance[v] += lower
        balance[u] -= lower
        return idx, lower, span

    for i, e in enumerate(names_e):
        _lb_edge(src, e_base + i, row_lo[e], row_hi[e])
    for i, e in enumerate(names_e):
        for j, w in enumerate(names_w):
            if (e, w) in forbidden:
                continue
            idx, lower, span = _lb_edge(e_base + i, w_base + j, 0, col_cap[w])
            cell_refs.append((e, w, e_base + i, idx, lower, span))
    for j in range(len(names_w)):
        _lb_edge(w_base + j, snk, 0, col_cap[names_w[j]])
    _lb_edge(snk, src, total, total)

    required = 0
    for v, b in enumerate(balance):
        if b > 0:
            mf.add_edge(ss, v, b)
            required += b
        elif b < 0:
            mf.add_edge(v, tt, -b)

    if mf.max_flow(ss, tt) != required:
        return False, None

    alloc: dict[tuple[str, str], int] = {}
    for e, w, u, idx, lower, span in cell_refs:
        residual = mf.g[u][idx][2]
        flow = lower + (span - residual)
        if flow > 0:
            alloc[(e, w)] = flow
    return True, alloc


class _MaxFlow:
    def __init__(self, n: int) -> None:
        self.n = n
        self.g: list[list[list[int]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int) -> int:
        idx = len(self.g[u])
        self.g[u].append([v, len(self.g[v]), cap])
        self.g[v].append([u, idx, 0])
        return idx

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        while True:
            parent_v = [-1] * self.n
            parent_e = [-1] * self.n
            parent_v[s] = s
            dq = deque([s])
            while dq and parent_v[t] == -1:
                u = dq.popleft()
                for i, (v, _rev, cap) in enumerate(self.g[u]):
                    if cap > 0 and parent_v[v] == -1:
                        parent_v[v] = u
                        parent_e[v] = i
                        dq.append(v)
            if parent_v[t] == -1:
                break
            add = 10**30
            v = t
            while v != s:
                u = parent_v[v]
                add = min(add, self.g[u][parent_e[v]][2])
                v = u
            v = t
            while v != s:
                u = parent_v[v]
                edge = self.g[u][parent_e[v]]
                edge[2] -= add
                self.g[v][edge[1]][2] += add
                v = u
            flow += add
        return flow
