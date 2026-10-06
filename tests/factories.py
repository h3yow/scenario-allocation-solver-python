"""测试构造工厂：快速构造问题与请求。"""

from __future__ import annotations

from app.models import (
    ConfirmationIn,
    EntityIn,
    OptimizeRequest,
    ReplanRequest,
    ScenarioIn,
    WindowIn,
)
from app.solver.models import AllocationProblem, EntitySpec, Scenario, WindowSpec


def make_problem(
    *,
    supply: float = 120.0,
    capacities: tuple[float, ...] = (70.0, 90.0),
    entities: tuple[tuple[str, float, float], ...] = (
        ("a", 100.0, 10.0),
        ("b", 50.0, 5.0),
        ("c", 30.0, 0.0),
    ),
    extra_scenarios: tuple[Scenario, ...] = (),
) -> AllocationProblem:
    return AllocationProblem(
        entities=[EntitySpec(n, d, m) for n, d, m in entities],
        windows=[WindowSpec(f"w{i}", c) for i, c in enumerate(capacities)],
        scenarios=[
            Scenario("base", supply, is_baseline=True),
            *extra_scenarios,
        ],
    )


def make_request(
    *,
    supply: float = 120.0,
    capacities: tuple[float, ...] = (70.0, 90.0),
    entities: tuple[tuple[str, float, float], ...] = (
        ("a", 100.0, 10.0),
        ("b", 50.0, 5.0),
        ("c", 30.0, 0.0),
    ),
    scenarios: list[ScenarioIn] | None = None,
    timeout_seconds: float | None = None,
    quantize_decimals: int = 6,
    solver_step_delay: float = 0.0,
) -> OptimizeRequest:
    return OptimizeRequest(
        entities=[EntityIn(name=n, base_demand=d, min_share=m) for n, d, m in entities],
        windows=[WindowIn(name=f"w{i}", capacity=c) for i, c in enumerate(capacities)],
        scenarios=scenarios
        or [ScenarioIn(name="base", total_supply=supply, is_baseline=True)],
        timeout_seconds=timeout_seconds,
        quantize_decimals=quantize_decimals,
        solver_step_delay=solver_step_delay,
    )


def make_replan_request(
    *,
    base_key: str,
    confirmations: tuple[tuple[str, str, float], ...] = (),
    supply: float = 120.0,
    capacities: tuple[float, ...] = (70.0, 90.0),
    entities: tuple[tuple[str, float, float], ...] = (
        ("a", 100.0, 10.0),
        ("b", 50.0, 5.0),
        ("c", 30.0, 0.0),
    ),
    scenarios: list[ScenarioIn] | None = None,
    timeout_seconds: float | None = None,
    quantize_decimals: int = 6,
    solver_step_delay: float = 0.0,
) -> ReplanRequest:
    return ReplanRequest(
        base_key=base_key,
        confirmations=[
            ConfirmationIn(entity=e, window=w, amount=v) for e, w, v in confirmations
        ],
        entities=[EntityIn(name=n, base_demand=d, min_share=m) for n, d, m in entities],
        windows=[WindowIn(name=f"w{i}", capacity=c) for i, c in enumerate(capacities)],
        scenarios=scenarios
        or [ScenarioIn(name="base", total_supply=supply, is_baseline=True)],
        timeout_seconds=timeout_seconds,
        quantize_decimals=quantize_decimals,
        solver_step_delay=solver_step_delay,
    )
