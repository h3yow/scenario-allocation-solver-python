"""确定性多场景资源分配求解器。

单窗口模型 (x_i 为主体 i 的分配量):
    min_i <= x_i <= cap_i,  sum_i x_i = supply,
    x_i 尽可能贴近需求 d_i(“水填充”:先保最小保障,再按需求满足,
    供给不足时等比例/同水位压缩,供给过剩时填满容量)。

实现方式:分段线性函数 F(lambda)=sum_i clip(min_i, d_i+lambda, cap_i)
关于 lambda 单调,二分求出满足 F(lambda)=supply 的水位。

守恒严格性:浮点分配后用量化为整数单位的残差做确定性修正
(按 entity id 字典序逐单位摊派),使 sum(x) 与 supply 完全相等。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from .errors import SolverCancelledError
from .models import (
    Problem,
    SolverConfig,
    SolveResult,
    WindowReport,
)

logger = logging.getLogger("sas.solver")

# 求解模型版本:算法或结果口径变化时必须升版本(参与可复现性判定)
MODEL_VERSION = "allocation-waterfill-v1"

# 残差修正的整数单位:1e-12 个供给单位,足以覆盖常见浮点误差
_QUANTUM = 1e-12


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def solve_window(
    demand_by_entity: dict[str, float],
    min_guarantee: dict[str, float],
    capacity: dict[str, float],
    supply: float,
    config: SolverConfig,
) -> dict[str, float]:
    """单窗口的水填充式分配(确定性)。"""
    ids = sorted(demand_by_entity)
    lo_bounds = [min_guarantee[i] for i in ids]
    hi_bounds = [capacity[i] for i in ids]
    demands = [demand_by_entity[i] for i in ids]

    # 水位 lambda 的二分搜索:F(lambda) = sum clip(min, d+lambda, cap)
    lam_low = min(lo_bounds[i] - demands[i] for i in range(len(ids))) - 1.0
    lam_high = max(hi_bounds[i] - demands[i] for i in range(len(ids))) + 1.0

    def water_level(lam: float) -> float:
        return sum(
            _clip(demands[i] + lam, lo_bounds[i], hi_bounds[i])
            for i in range(len(ids))
        )

    iterations = 0
    lam = (lam_low + lam_high) / 2.0
    for iterations in range(1, config.max_iterations + 1):
        if water_level(lam_low) >= supply:
            # 下界水位即可覆盖供给:精确解落在退化解区间,统一采用 lam_low
            lam = lam_low
            break
        if water_level(lam_high) <= supply:
            lam = lam_high
            break
        mid = (lam_low + lam_high) / 2.0
        if water_level(mid) < supply:
            lam_low = mid
        else:
            lam_high = mid
        if lam_high - lam_low <= config.tolerance:
            lam = (lam_low + lam_high) / 2.0
            break
    else:
        lam = (lam_low + lam_high) / 2.0
        logger.warning("水位二分达到 max_iterations=%d,采用当前水位", config.max_iterations)

    alloc = [
        _clip(demands[i] + lam, lo_bounds[i], hi_bounds[i]) for i in range(len(ids))
    ]

    # 确定性残差修正:量化为整数单位后按 id 顺序逐单位摊派
    result = {ids[i]: alloc[i] for i in range(len(ids))}
    residual_units = round((supply - sum(alloc)) / _QUANTUM)
    step = 1 if residual_units > 0 else -1
    correction_moves = 0
    while residual_units != 0:
        moved = False
        for i in range(len(ids)):
            if residual_units == 0:
                break
            candidate = alloc[i] + step * _QUANTUM
            if lo_bounds[i] - config.tolerance <= candidate <= hi_bounds[i] + config.tolerance:
                alloc[i] = candidate
                result[ids[i]] = candidate
                residual_units -= step
                moved = True
                correction_moves += 1
        if not moved:
            # 理论上不会发生(可行性已先行保证)
            logger.error("残差修正异常终止 residual_units=%d", residual_units)
            break

    logger.info(
        "单窗口求解 | supply=%.6f 分配合计=%.6f 二分迭代=%d 残差修正单位=%d",
        supply,
        sum(result.values()),
        iterations,
        correction_moves,
    )
    return result


def build_reports(
    allocations: dict[str, dict[str, dict[str, float]]],
    problem: Problem,
) -> list[WindowReport]:
    """由分配结果构造守恒/满足率报表。"""
    reports: list[WindowReport] = []
    for scenario in problem.scenarios:
        alloc_by_window = allocations[scenario.name]
        for window in problem.windows:
            row = alloc_by_window[window.name]
            demand_row = scenario.demands.get(window.name, {})
            allocated_total = sum(row.values())
            demand_total = sum(demand_row.get(e.id, 0.0) for e in problem.entities)
            fill_ratio = (
                allocated_total / demand_total if demand_total > 0 else 1.0
            )
            reports.append(
                WindowReport(
                    scenario=scenario.name,
                    window=window.name,
                    allocated_total=allocated_total,
                    demand_total=demand_total,
                    fill_ratio=fill_ratio,
                )
            )
    return reports


def _config_digest(config: SolverConfig) -> str:
    return hashlib.sha256(repr(asdict(config)).encode("utf-8")).hexdigest()[:16]


def solve(
    problem: Problem,
    config: SolverConfig,
    idempotency_key: str,
    payload_hash: str,
    elapsed_ms: float = 0.0,
    cancel_event: threading.Event | None = None,
    checkpoint: "Callable[[str, str, threading.Event], None] | None" = None,
) -> SolveResult:
    """求解全部场景×窗口。

    调用前应已通过 validate_* 与 check_feasibility。
    cancel_event 置位时在任意场景/窗口边界抛 SolverCancelledError,不产出结果。
    checkpoint: 每个 (场景, 窗口) 开解前调用的钩子
                (scenario_name, window_name, cancel_event),
                生产环境为 None;测试可注入可被取消信号中断的阻塞点。
    """
    started = time.perf_counter()
    allocations: dict[str, dict[str, dict[str, float]]] = {}
    for scenario in problem.scenarios:
        allocations[scenario.name] = {}
        for window in problem.windows:
            if cancel_event is not None and cancel_event.is_set():
                logger.warning(
                    "求解被取消 | key=%s 场景=%s 窗口=%s(不产出部分结果)",
                    idempotency_key,
                    scenario.name,
                    window.name,
                )
                raise SolverCancelledError(
                    f"求解在场景[{scenario.name}]窗口[{window.name}]前被取消"
                )
            if checkpoint is not None:
                checkpoint(scenario.name, window.name, cancel_event)
            demand_row = {
                e.id: scenario.demands.get(window.name, {}).get(e.id, 0.0)
                for e in problem.entities
            }
            min_map = {e.id: e.min_guarantee for e in problem.entities}
            cap_map = {e.id: e.capacity for e in problem.entities}
            alloc = solve_window(
                demand_row, min_map, cap_map, window.total_supply, config
            )
            allocations[scenario.name][window.name] = alloc
            logger.info(
                "状态推进 | key=%s 场景=%s 窗口=%s 分配=%s",
                idempotency_key,
                scenario.name,
                window.name,
                {k: round(v, 6) for k, v in alloc.items()},
            )

    reports = build_reports(allocations, problem)
    result = SolveResult(
        idempotency_key=idempotency_key,
        status="SUCCEEDED",
        allocations=allocations,
        reports=reports,
        payload_hash=payload_hash,
        model_version=MODEL_VERSION,
        config_digest=_config_digest(config),
        elapsed_ms=elapsed_ms if elapsed_ms else (time.perf_counter() - started) * 1000,
    )
    logger.info("求解完成 | key=%s elapsed_ms=%.3f", idempotency_key, result.elapsed_ms)
    return result
