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

    logger.info("输入结构校验通过")


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
