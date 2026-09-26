"""多场景与滚动窗口约束一致性单测。

口径:同一批主体/窗口约束(min、capacity、total_supply)对所有场景一致生效;
每个 (场景, 窗口) 独立满足守恒,且基线与扰动场景使用同一套约束参数。
"""
from __future__ import annotations

import logging

import pytest

from sas.models import Entity, Problem, ScenarioInput, SolverConfig, Window
from sas.service import AllocationService
from sas.store import Status

logger = logging.getLogger("sas.test.scenario")

TOL = 1e-9


def _multi_window_problem() -> Problem:
    return Problem(
        baseline_name="baseline",
        entities=[
            Entity("a", min_guarantee=2.0, capacity=6.0),
            Entity("b", min_guarantee=1.0, capacity=5.0),
            Entity("c", min_guarantee=0.0, capacity=4.0),
        ],
        windows=[Window("mon", 8.0), Window("tue", 6.0), Window("wed", 9.0)],
        scenarios=[
            ScenarioInput(
                "baseline",
                {
                    "mon": {"a": 5, "b": 4, "c": 2},
                    "tue": {"a": 3, "b": 3, "c": 2},
                    "wed": {"a": 6, "b": 2, "c": 3},
                },
            ),
            ScenarioInput(
                "demand_up_20pct",
                {
                    "mon": {"a": 6, "b": 4.8, "c": 2.4},
                    "tue": {"a": 3.6, "b": 3.6, "c": 2.4},
                    "wed": {"a": 7.2, "b": 2.4, "c": 3.6},
                },
            ),
            ScenarioInput(
                "demand_shift",
                {
                    "mon": {"a": 2, "b": 6, "c": 3},
                    "tue": {"a": 1, "b": 4, "c": 4},
                    "wed": {"a": 4, "b": 1, "c": 4},
                },
            ),
        ],
    )


def test_constraints_hold_in_every_scenario_and_window() -> None:
    problem = _multi_window_problem()
    svc = AllocationService()
    svc.submit("multi", problem)
    assert svc.wait_until_terminal("multi", 5)
    status, result = svc.get_result("multi")
    assert status is Status.SUCCEEDED

    entity_map = {e.id: e for e in problem.entities}
    window_map = {w.name: w.total_supply for w in problem.windows}

    for scenario in problem.scenarios:
        for window_name, supply in window_map.items():
            alloc = result.allocations[scenario.name][window_name]
            # 1) 总量守恒:分配合计 == 窗口供给(严格)
            assert sum(alloc.values()) == pytest.approx(supply, abs=TOL), (
                scenario.name,
                window_name,
            )
            # 2) 每个主体满足最小保障与容量
            for entity_id, value in alloc.items():
                ent = entity_map[entity_id]
                assert ent.min_guarantee - TOL <= value <= ent.capacity + TOL
            # 3) 所有主体都被显式分配(口径一致,不允许缺项)
            assert set(alloc) == set(entity_map)

    # 4) 报表口径:allocated_total 与分配合计一致
    for report in result.reports:
        assert report.allocated_total == pytest.approx(
            window_map[report.window], abs=TOL
        )
        assert 0.0 <= report.fill_ratio


def test_rolling_windows_independent_but_same_rules() -> None:
    """不同窗口供给不同,但 min/cap 约束集合相同——结果随窗口变化、规则不变。"""
    problem = _multi_window_problem()
    cfg = SolverConfig()
    svc = AllocationService(default_config=cfg)
    svc.submit("roll", problem)
    assert svc.wait_until_terminal("roll", 5)
    _, result = svc.get_result("roll")

    baseline = result.allocations["baseline"]
    # 各窗口合计等于各自供给(滚动窗口守恒)
    assert sum(baseline["mon"].values()) == pytest.approx(8.0, abs=TOL)
    assert sum(baseline["tue"].values()) == pytest.approx(6.0, abs=TOL)
    assert sum(baseline["wed"].values()) == pytest.approx(9.0, abs=TOL)
    # 不同窗口结果一般不同(供给变了),但主体集合一致
    assert baseline["mon"] != baseline["tue"]
    assert set(baseline["mon"]) == set(baseline["tue"]) == set(baseline["wed"])


def test_scenario_results_share_same_entities_and_windows() -> None:
    problem = _multi_window_problem()
    svc = AllocationService()
    svc.submit("shape", problem)
    assert svc.wait_until_terminal("shape", 5)
    _, result = svc.get_result("shape")

    for scenario in problem.scenarios:
        assert set(result.allocations[scenario.name]) == {"mon", "tue", "wed"}
        for window_name in ("mon", "tue", "wed"):
            assert set(result.allocations[scenario.name][window_name]) == {
                "a",
                "b",
                "c",
            }

