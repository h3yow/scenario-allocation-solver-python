"""可行性判定与解释。

对每个 (场景, 窗口), 分配量 x_i 必须同时满足:
  1) 容量上界:    x_i <= capacity_i
  2) 最小保障:    x_i >= min_guarantee_i
  3) 总量守恒:    sum_i x_i = total_supply

故可行域非空 当且仅当  sum(min_i) <= total_supply <= sum(cap_i)。
场景只改变需求,不改变 min/cap/supply,因此各场景可行性结论天然一致;
仍逐场景核对,保证报表与日志口径统一。
"""
from __future__ import annotations

import logging

from .errors import InfeasibleError
from .models import Problem

logger = logging.getLogger("sas.feasibility")


def check_feasibility(problem: Problem, tolerance: float = 1e-9) -> None:
    """对每个 (场景, 窗口) 检查可行域;不可行则抛 InfeasibleError 并附带判定依据。"""
    min_sum = sum(e.min_guarantee for e in problem.entities)
    cap_sum = sum(e.capacity for e in problem.entities)
    logger.info(
        "可行性判定 | 主体数=%d sum(min_guarantee)=%.6f sum(capacity)=%.6f tol=%.0e",
        len(problem.entities),
        min_sum,
        cap_sum,
        tolerance,
    )

    for scenario in problem.scenarios:
        for window in problem.windows:
            supply = window.total_supply
            row = scenario.demands.get(window.name, {})
            demand_total = sum(row.get(e.id, 0.0) for e in problem.entities)

            logger.info(
                "判定依据 | 场景=%s 窗口=%s supply=%.6f 需求合计=%.6f "
                "下界合计=%.6f 上界合计=%.6f",
                scenario.name,
                window.name,
                supply,
                demand_total,
                min_sum,
                cap_sum,
            )

            if supply < min_sum - tolerance:
                details = {
                    "scenario": scenario.name,
                    "window": window.name,
                    "reason": "SUPPLY_BELOW_MIN_GUARANTEE",
                    "total_supply": supply,
                    "sum_min_guarantee": min_sum,
                    "shortfall": min_sum - supply,
                }
                logger.warning("不可行 | %s | details=%s", details["reason"], details)
                raise InfeasibleError(
                    f"场景[{scenario.name}]窗口[{window.name}]供给 {supply} "
                    f"小于最小保障合计 {min_sum}(缺口 {min_sum - supply:.6f})",
                    details,
                )
            if supply > cap_sum + tolerance:
                details = {
                    "scenario": scenario.name,
                    "window": window.name,
                    "reason": "SUPPLY_EXCEEDS_CAPACITY",
                    "total_supply": supply,
                    "sum_capacity": cap_sum,
                    "excess": supply - cap_sum,
                }
                logger.warning("不可行 | %s | details=%s", details["reason"], details)
                raise InfeasibleError(
                    f"场景[{scenario.name}]窗口[{window.name}]供给 {supply} "
                    f"超过容量合计 {cap_sum}(超出 {supply - cap_sum:.6f})",
                    details,
                )

    logger.info("可行性判定通过:全部场景×窗口可行")
