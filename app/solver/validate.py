"""输入校验：结构合法性检查与问题可行性判定分离。

- ``validate_problem`` 负责结构/取值合法性 -> InvalidRequestError（400）。
- ``check_feasibility`` 负责优化可行性 -> 返回每场景的不可行原因，由引擎
  汇总抛出 InfeasibleError（422）。两类失败严格区分。
"""

from __future__ import annotations

from app.errors import InvalidRequestError
from app.logging_setup import get_logger
from app.solver.models import AllocationProblem

logger = get_logger("app.solver.validate")

# 浮点比较容差：判定依据（是否违反约束）统一使用该容差。
EPS = 1e-9


def validate_problem(problem: AllocationProblem) -> None:
    """校验输入结构与取值合法性，非法时抛 InvalidRequestError。"""
    logger.info(
        "校验输入结构: entities=%d windows=%d scenarios=%d",
        len(problem.entities),
        len(problem.windows),
        len(problem.scenarios),
    )

    if not problem.entities:
        raise InvalidRequestError("entities 不能为空")
    if not problem.windows:
        raise InvalidRequestError("windows 不能为空")
    if not problem.scenarios:
        raise InvalidRequestError("scenarios 不能为空")

    entity_names = [e.name for e in problem.entities]
    if len(set(entity_names)) != len(entity_names):
        raise InvalidRequestError("entity 名称必须唯一", details={"names": entity_names})
    window_names = [w.name for w in problem.windows]
    if len(set(window_names)) != len(window_names):
        raise InvalidRequestError("window 名称必须唯一", details={"names": window_names})
    scenario_names = [s.name for s in problem.scenarios]
    if len(set(scenario_names)) != len(scenario_names):
        raise InvalidRequestError("scenario 名称必须唯一", details={"names": scenario_names})

    baselines = [s.name for s in problem.scenarios if s.is_baseline]
    if len(baselines) != 1:
        raise InvalidRequestError(
            "必须恰好指定一个基线场景(is_baseline=true)",
            details={"baselines": baselines},
        )

    for e in problem.entities:
        if e.base_demand < -EPS:
            raise InvalidRequestError(f"entity '{e.name}' base_demand 不能为负")
        if e.min_share < -EPS:
            raise InvalidRequestError(f"entity '{e.name}' min_share 不能为负")
        if e.min_share > e.base_demand + EPS:
            raise InvalidRequestError(
                f"entity '{e.name}' min_share={e.min_share} 不能超过 base_demand={e.base_demand}",
            )

    for w in problem.windows:
        if w.capacity < -EPS:
            raise InvalidRequestError(f"window '{w.name}' capacity 不能为负")

    known = set(entity_names)
    for s in problem.scenarios:
        if s.total_supply < -EPS:
            raise InvalidRequestError(f"scenario '{s.name}' total_supply 不能为负")
        for entity_name, mult in s.demand_multipliers.items():
            if entity_name not in known:
                raise InvalidRequestError(
                    f"scenario '{s.name}' 的需求乘数引用了未知 entity '{entity_name}'",
                )
            if mult <= 0:
                raise InvalidRequestError(
                    f"scenario '{s.name}' entity '{entity_name}' 需求乘数必须为正，实际 {mult}",
                )

    _validate_locked(problem)

    logger.info("输入结构校验通过")


def _validate_locked(problem: AllocationProblem) -> None:
    """已确认额度的结构校验（非法 -> InvalidRequestError）。

    冲突类问题（确认量与容量/需求/供给对不上）不在此处判定，
    由 ``check_locked_conflicts`` 以 CONFIRMATION_CONFLICT 单独报告。
    """
    if not problem.locked:
        return
    entity_names = {e.name for e in problem.entities}
    window_names = {w.name for w in problem.windows}
    for (entity, window), amount in problem.locked.items():
        if entity not in entity_names:
            raise InvalidRequestError(
                f"已确认额度引用了未知 entity '{entity}'",
                details={"entity": entity, "window": window},
            )
        if window not in window_names:
            raise InvalidRequestError(
                f"已确认额度引用了未知 window '{window}'",
                details={"entity": entity, "window": window},
            )
        if amount <= 0:
            raise InvalidRequestError(
                f"已确认额度必须为正: entity '{entity}' window '{window}' amount={amount}",
                details={"entity": entity, "window": window, "amount": amount},
            )


def locked_row_sums(problem: AllocationProblem) -> dict[str, float]:
    """每个实体的已确认总量（跨窗口求和）。"""
    rows: dict[str, float] = {e.name: 0.0 for e in problem.entities}
    for (entity, _window), amount in problem.locked.items():
        rows[entity] = rows.get(entity, 0.0) + amount
    return rows


def locked_col_sums(problem: AllocationProblem) -> dict[str, float]:
    """每个窗口的已确认总量（跨实体求和）。"""
    cols: dict[str, float] = {w.name: 0.0 for w in problem.windows}
    for (_entity, window), amount in problem.locked.items():
        cols[window] = cols.get(window, 0.0) + amount
    return cols


def check_locked_conflicts(problem: AllocationProblem) -> list[str]:
    """已确认额度与最新输入的冲突检测；为空表示无冲突。

    逐条指明场景/实体/窗口，与 INFEASIBLE（输入本身不可行）严格区分：
    只有在去掉确认额度后问题本身可行、但保留确认额度后不可行时，
    才会走到这里报告冲突。
    """
    if not problem.locked:
        return []
    reasons: list[str] = []
    rows = locked_row_sums(problem)
    cols = locked_col_sums(problem)
    total_locked = sum(problem.locked.values())

    # 窗口容量是跨场景共享的：确认量超过容量在所有场景都装不下。
    for w in problem.windows:
        if cols[w.name] > w.capacity + EPS:
            reason = (
                f"window '{w.name}': 已确认量 {_fmt(cols[w.name])} 超过窗口容量 "
                f"{_fmt(w.capacity)}（所有场景均装不下）"
            )
            logger.info("确认额度冲突: %s", reason)
            reasons.append(reason)

    for s in problem.scenarios:
        demands = scenario_demand(problem, s)
        for e in problem.entities:
            locked_row = rows[e.name]
            if locked_row > demands[e.name] + EPS:
                reason = (
                    f"scenario '{s.name}': entity '{e.name}' 已确认量 "
                    f"{_fmt(locked_row)} 超过其当前需求上限 {_fmt(demands[e.name])}"
                )
                logger.info("确认额度冲突: %s", reason)
                reasons.append(reason)

        # 每个实体至少要拿 max(保底, 已确认行合计)，合计不得超过场景供给。
        min_required = sum(
            max(e.min_share, rows[e.name]) for e in problem.entities
        )
        if min_required > s.total_supply + EPS:
            reason = (
                f"scenario '{s.name}': 保底与已确认量合计 {_fmt(min_required)} "
                f"超过总供给 {_fmt(s.total_supply)}，确认额度无法原样保留"
            )
            logger.info("确认额度冲突: %s", reason)
            reasons.append(reason)

        if total_locked > s.total_supply + EPS:
            reason = (
                f"scenario '{s.name}': 已确认总量 {_fmt(total_locked)} "
                f"超过总供给 {_fmt(s.total_supply)}，总量守恒无法成立"
            )
            logger.info("确认额度冲突: %s", reason)
            reasons.append(reason)

    return reasons


def scenario_demand(problem: AllocationProblem, scenario) -> dict[str, float]:
    """计算某场景下各实体的扰动后需求。"""
    demands: dict[str, float] = {}
    for e in problem.entities:
        mult = scenario.demand_multipliers.get(e.name, 1.0)
        demands[e.name] = e.base_demand * mult
    return demands


def check_feasibility(problem: AllocationProblem) -> list[str]:
    """返回不可行原因列表；为空表示可行。

    每个场景的可行性充要条件（实体可在窗口间拆分）：
    1. 每个实体的最小保障 <= 其扰动后需求（保底本身必须可达）；
    2. 最小保障总量 <= 场景总供给；
    3. 场景总供给 <= 扰动后总需求（分配以需求为上限，不得超发）；
    4. 窗口容量总量 >= 场景总供给（总量守恒要求恰好分配完供给）。
    """
    reasons: list[str] = []
    total_capacity = sum(w.capacity for w in problem.windows)
    total_min = sum(e.min_share for e in problem.entities)

    for s in problem.scenarios:
        demands = scenario_demand(problem, s)
        total_demand = sum(demands.values())

        for name, demand in demands.items():
            min_share = next(
                e.min_share for e in problem.entities if e.name == name
            )
            if min_share > demand + EPS:
                reason = (
                    f"scenario '{s.name}': entity '{name}' 的最小保障 "
                    f"{_fmt(min_share)} 超过扰动后需求 {_fmt(demand)}"
                )
                logger.info("判定不可行: %s", reason)
                reasons.append(reason)

        logger.info(
            "可行性判定 scenario=%s supply=%.6f total_demand=%.6f "
            "total_min_share=%.6f total_capacity=%.6f",
            s.name,
            s.total_supply,
            total_demand,
            total_min,
            total_capacity,
        )

        if total_min > s.total_supply + EPS:
            reason = (
                f"scenario '{s.name}': 最小保障总量 {_fmt(total_min)} "
                f"超过总供给 {_fmt(s.total_supply)}"
            )
            logger.info("判定不可行: %s", reason)
            reasons.append(reason)

        if s.total_supply > total_demand + EPS:
            reason = (
                f"scenario '{s.name}': 总供给 {_fmt(s.total_supply)} "
                f"超过扰动后总需求 {_fmt(total_demand)}，无法在不超发的情况下守恒"
            )
            logger.info("判定不可行: %s", reason)
            reasons.append(reason)

        if total_capacity + EPS < s.total_supply:
            reason = (
                f"scenario '{s.name}': 窗口容量总量 {_fmt(total_capacity)} "
                f"小于总供给 {_fmt(s.total_supply)}，总量守恒无法成立"
            )
            logger.info("判定不可行: %s", reason)
            reasons.append(reason)

    if not reasons:
        logger.info("全部场景判定可行")
    return reasons


def _fmt(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".")
