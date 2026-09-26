"""优化问题、配置与结果的数据模型。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Entity:
    """可分配主体:min_guarantee <= 分配量 <= capacity。"""

    id: str
    min_guarantee: float
    capacity: float


@dataclass(frozen=True)
class Window:
    """滚动窗口中的一个周期:该周期内分配总量必须等于 total_supply。"""

    name: str
    total_supply: float


@dataclass(frozen=True)
class ScenarioInput:
    """一个场景(基线或扰动)在各窗口下对各主体的需求量。

    demands: window_name -> entity_id -> demand
    """

    name: str
    demands: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    """一次多场景资源分配问题的完整输入。"""

    entities: list[Entity]
    windows: list[Window]
    scenarios: list[ScenarioInput]
    baseline_name: str = "baseline"


@dataclass(frozen=True)
class SolverConfig:
    """求解配置。"""

    solve_timeout: float = 5.0
    tolerance: float = 1e-9
    max_iterations: int = 10000


@dataclass(frozen=True)
class WindowReport:
    scenario: str
    window: str
    allocated_total: float
    demand_total: float
    fill_ratio: float


@dataclass(frozen=True)
class SolveResult:
    """一次成功求解的完整、可复现结果。"""

    idempotency_key: str
    status: str
    allocations: dict[str, dict[str, dict[str, float]]]
    reports: list[WindowReport]
    payload_hash: str
    model_version: str
    config_digest: str
    elapsed_ms: float


def canonical_payload(problem: Problem, config: SolverConfig) -> str:
    """生成问题+配置的规范化 JSON(用于幂等指纹)。

    关键:字段顺序固定、float 走 repr(最短往返表示),
    保证同构输入在 Python 层稳定得到同一字符串,从而摘要可复现。
    """
    doc = {
        "problem": {
            "baseline_name": problem.baseline_name,
            # 列表按业务主键排序,使“等价但顺序不同”的请求得到同一指纹
            "entities": sorted((asdict(e) for e in problem.entities), key=lambda x: x["id"]),
            "windows": sorted((asdict(w) for w in problem.windows), key=lambda x: x["name"]),
            "scenarios": sorted(
                (asdict(s) for s in problem.scenarios), key=lambda x: x["name"]
            ),
        },
        "config": asdict(config),
    }
    return json.dumps(
        doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr
    )


def payload_digest(problem: Problem, config: SolverConfig) -> str:
    """返回规范化载荷的 sha256 摘要。"""
    return hashlib.sha256(canonical_payload(problem, config).encode("utf-8")).hexdigest()
