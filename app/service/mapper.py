"""DTO <-> 领域模型转换，以及 SolveResult -> OptimizeResponse 装配。"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.models import (
    EntityOut,
    OptimizeRequest,
    OptimizeResponse,
    ReplanRequest,
    ScenarioOut,
    WindowOut,
)
from app.solver.models import (
    AllocationProblem,
    ConfirmedCell,
    EntitySpec,
    Scenario,
    SolveResult,
    WindowSpec,
)

logger = get_logger("app.service.mapper")


def _problem_parts(request) -> tuple[list[EntitySpec], list[WindowSpec], list[Scenario]]:
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
    return entities, windows, scenarios


def request_to_problem(request: OptimizeRequest) -> AllocationProblem:
    """把 API 请求转换为求解器问题（确定性字段顺序）。"""
    entities, windows, scenarios = _problem_parts(request)
    problem = AllocationProblem(entities=entities, windows=windows, scenarios=scenarios)
    logger.info(
        "请求转换为求解问题: %d 实体 / %d 窗口 / %d 场景",
        len(entities),
        len(windows),
        len(scenarios),
    )
    return problem


def replan_request_to_parts(
    request: ReplanRequest,
) -> tuple[AllocationProblem, list[ConfirmedCell]]:
    """重规划请求 -> (最新问题, 已确认格子列表，顺序保持)。"""
    entities, windows, scenarios = _problem_parts(request)
    problem = AllocationProblem(entities=entities, windows=windows, scenarios=scenarios)
    confirmed = [
        ConfirmedCell(
            scenario=c.scenario,
            entity=c.entity,
            window=c.window,
            amount=c.amount,
        )
        for c in request.confirmed_allocations
    ]
    logger.info(
        "重规划请求转换: %d 实体 / %d 窗口 / %d 场景 / %d 确认格子 based_on=%s",
        len(entities),
        len(windows),
        len(scenarios),
        len(confirmed),
        request.based_on_key,
    )
    return problem, confirmed


def build_response(
    key: str,
    fingerprint: str,
    result: SolveResult,
    based_on_key: str | None = None,
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
                confirmed_total=s.confirmed_total,
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
        based_on_key=based_on_key,
        scenarios=scenarios,
    )
    logger.info(
        "装配响应 key=%s 场景数=%d based_on=%s",
        key, len(scenarios), based_on_key,
    )
    return response
