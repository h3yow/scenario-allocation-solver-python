"""API 传输层 DTO（Pydantic），与求解器领域模型解耦。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ScenarioIn(BaseModel):
    name: str
    total_supply: float = Field(ge=0)
    demand_multipliers: dict[str, float] = Field(default_factory=dict)
    is_baseline: bool = False


class EntityIn(BaseModel):
    name: str
    base_demand: float = Field(ge=0)
    min_share: float = Field(default=0.0, ge=0)


class WindowIn(BaseModel):
    name: str
    capacity: float = Field(ge=0)


class OptimizeRequest(BaseModel):
    entities: list[EntityIn]
    windows: list[WindowIn]
    scenarios: list[ScenarioIn]
    timeout_seconds: float | None = Field(default=None, gt=0)
    quantize_decimals: int = Field(default=9, ge=0, le=12)
    solver_step_delay: float = Field(default=0.0, ge=0)


class ConfirmedAllocationIn(BaseModel):
    """一条已确认（锁定、不可挪动）的额度：某场景下实体在某窗口上的量。"""

    scenario: str
    entity: str
    window: str
    amount: float = Field(gt=0)


class ReplanRequest(BaseModel):
    """带已确认额度的重规划：接着 ``based_on_key`` 的成功方案继续做。"""

    based_on_key: str = Field(min_length=1)
    entities: list[EntityIn]
    windows: list[WindowIn]
    scenarios: list[ScenarioIn]
    confirmed_allocations: list[ConfirmedAllocationIn] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0)
    quantize_decimals: int = Field(default=9, ge=0, le=12)
    solver_step_delay: float = Field(default=0.0, ge=0)


class EntityOut(BaseModel):
    entity: str
    demand: float
    min_share: float
    amount: float


class WindowOut(BaseModel):
    window: str
    allocated: float
    capacity: float
    entities: dict[str, float]


class ScenarioOut(BaseModel):
    scenario: str
    is_baseline: bool
    total_supply: float
    total_demand: float
    total_allocated: float
    confirmed_total: float = 0.0
    feasible: bool
    infeasibility_reasons: list[str]
    entity_allocations: list[EntityOut]
    window_allocations: list[WindowOut]


class OptimizeResponse(BaseModel):
    idempotency_key: str
    status: str
    solver_version: str
    request_fingerprint: str
    quantize_decimals: int
    based_on_key: str | None = None
    scenarios: list[ScenarioOut]
