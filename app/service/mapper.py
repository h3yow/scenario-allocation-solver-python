"""DTO <-> 领域模型转换，以及 SolveResult -> OptimizeResponse 装配。"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.models import (
    EntityOut,
    OptimizeRequest,
    OptimizeResponse,
    ScenarioOut,
    WindowOut,
)
from app.solver.models import (
    AllocationProblem,
    EntitySpec,
    Scenario,
    SolveResult,
    WindowSpec,
)

logger = get_logger("app.service.mapper")


def request_to_problem(request: OptimizeRequest) -> AllocationProblem:
    """把 API 请求转换为求解器问题（确定性字段顺序）。"""
    entities = [
        EntitySpec(name=e.name, base_demand=e.base_demand, min_share=e.min_share)
        for e in request.entities
    ]
    windows = [WindowSpec(name=w.name, capacity=w.capacity) for w in request.windows]
    scenarios = [
        Scenario(
            name=s.name,
            total_supply=s.total_supply,
            demand_multipliers=dict(s.demand_multipliers),
            is_baseline=s.is_baseline,
        )
        for s in request.scenarios
    ]
    problem = AllocationProblem(entities=entities, windows=windows, scenarios=scenarios)
    logger.info(
        "请求转换为求解问题: %d 实体 / %d 窗口 / %d 场景",
        len(entities),
        len(windows),
        len(scenarios),
    )
    return problem


def build_response(
    key: str,
    fingerprint: str,
    result: SolveResult,
) -> OptimizeResponse:
    """把求解结果装配为幂等固化的响应（不含易变的时间/线程信息）。"""
    scenarios: list[ScenarioOut] = []
    for s in result.scenarios:
        scenarios.append(
            ScenarioOut(
                scenario=s.scenario,
                is_baseline=s.is_baseline,
                total_supply=s.total_supply,
                total_demand=s.total_demand,
                total_allocated=s.total_allocated,
                feasible=s.feasible,
                infeasibility_reasons=list(s.infeasibility_reasons),
                entity_allocations=[
                    EntityOut(
                        entity=e.entity,
                        demand=e.demand,
                        min_share=e.min_share,
                        amount=e.amount,
                    )
                    for e in s.entity_allocations
                ],
                window_allocations=[
                    WindowOut(
                        window=w.window,
                        allocated=w.allocated,
                        capacity=w.capacity,
                        entities=dict(w.entities),
                    )
                    for w in s.window_allocations
                ],
            )
        )
    response = OptimizeResponse(
        idempotency_key=key,
        status="SUCCEEDED",
        solver_version=result.solver_version,
        request_fingerprint=fingerprint,
        quantize_decimals=result.quantize_decimals,
        scenarios=scenarios,
    )
    logger.info("装配响应 key=%s 场景数=%d", key, len(scenarios))
    return response
