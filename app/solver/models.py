"""求解器领域模型（与传输层 DTO 解耦）。

口径说明（所有场景一致）：
- 容量约束：每个场景、每个窗口上的分配量 <= 窗口容量。
- 最小保障：每个场景、每个需求实体分配量 >= 其最小保障量（若适用）。
- 总量守恒：每个场景下所有窗口分配量之和 == 该场景的总供给量。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    name: str
    total_supply: float
    demand_multipliers: dict[str, float] = field(default_factory=dict)
    is_baseline: bool = False


@dataclass(frozen=True)
class EntitySpec:
    name: str
    base_demand: float
    min_share: float = 0.0


@dataclass(frozen=True)
class WindowSpec:
    name: str
    capacity: float


@dataclass(frozen=True)
class AllocationProblem:
    entities: list[EntitySpec]
    windows: list[WindowSpec]
    scenarios: list[Scenario]


@dataclass(frozen=True)
class EntityAllocation:
    entity: str
    demand: float
    min_share: float
    amount: float


@dataclass(frozen=True)
class WindowAllocation:
    window: str
    allocated: float
    capacity: float
    entities: dict[str, float]


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    is_baseline: bool
    total_supply: float
    total_demand: float
    total_allocated: float
    entity_allocations: list[EntityAllocation]
    window_allocations: list[WindowAllocation]
    feasible: bool
    infeasibility_reasons: list[str]


@dataclass(frozen=True)
class SolveResult:
    scenarios: list[ScenarioResult]
    solver_version: str
    quantize_decimals: int
