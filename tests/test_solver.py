"""求解器与数据模型单测。"""
from __future__ import annotations

import threading

import pytest

from sas.errors import SolverCancelledError
from sas.models import (
    Entity,
    Problem,
    ScenarioInput,
    SolverConfig,
    Window,
    payload_digest,
)
from sas.solver import build_reports, solve, solve_window

TOL = 1e-9


def test_payload_digest_deterministic_and_order_insensitive() -> None:
    p1 = Problem(
        entities=[Entity("a", 1, 5), Entity("b", 0, 3)],
        windows=[Window("w1", 4.0)],
        scenarios=[ScenarioInput("baseline", {"w1": {"a": 3, "b": 2}})],
    )
    p2 = Problem(
        entities=[Entity("b", 0, 3), Entity("a", 1, 5)],
        windows=[Window("w1", 4.0)],
        scenarios=[ScenarioInput("baseline", {"w1": {"b": 2, "a": 3}})],
    )
    cfg = SolverConfig()
    assert payload_digest(p1, cfg) == payload_digest(p2, cfg)

    p3 = Problem(
        entities=[Entity("a", 1, 6), Entity("b", 0, 3)],
        windows=[Window("w1", 4.0)],
        scenarios=[ScenarioInput("baseline", {"w1": {"a": 3, "b": 2}})],
    )
    assert payload_digest(p1, cfg) != payload_digest(p3, cfg)


def test_window_allocation_meets_conservation_and_bounds() -> None:
    cfg = SolverConfig()
    mins = {"a": 1.0, "b": 1.0}
    caps = {"a": 10.0, "b": 10.0}

    alloc = solve_window({"a": 6.0, "b": 4.0}, mins, caps, 5.0, cfg)
    assert sum(alloc.values()) == pytest.approx(5.0, abs=TOL)
    assert alloc["a"] == pytest.approx(3.5, abs=1e-9)
    assert alloc["b"] == pytest.approx(1.5, abs=1e-9)
    assert all(v >= 1.0 - TOL for v in alloc.values())


def test_window_oversupply_fills_to_capacity_total() -> None:
    cfg = SolverConfig()
    alloc = solve_window(
        {"a": 6.0, "b": 4.0},
        {"a": 0.0, "b": 0.0},
        {"a": 10.0, "b": 10.0},
        15.0,
        cfg,
    )
    assert sum(alloc.values()) == pytest.approx(15.0, abs=TOL)
    assert alloc["a"] == pytest.approx(8.5, abs=1e-9)
    assert alloc["b"] == pytest.approx(6.5, abs=1e-9)


def test_window_degenerate_at_min_guarantees() -> None:
    cfg = SolverConfig()
    alloc = solve_window(
        {"a": 6.0, "b": 4.0},
        {"a": 1.0, "b": 1.0},
        {"a": 10.0, "b": 10.0},
        2.0,
        cfg,
    )
    assert sum(alloc.values()) == pytest.approx(2.0, abs=TOL)
    assert alloc == pytest.approx({"a": 1.0, "b": 1.0}, abs=TOL)


def test_full_problem_constraints_across_scenarios(sample_problem: Problem) -> None:
    cfg = SolverConfig()
    result = solve(sample_problem, cfg, "key", "hash")
    entity_map = {e.id: e for e in sample_problem.entities}

    for scenario in sample_problem.scenarios:
        for window in sample_problem.windows:
            alloc = result.allocations[scenario.name][window.name]
            # 总量守恒
            assert sum(alloc.values()) == pytest.approx(window.total_supply, abs=TOL)
            # 容量与最小保障
            for entity_id, value in alloc.items():
                ent = entity_map[entity_id]
                assert ent.min_guarantee - TOL <= value <= ent.capacity + TOL

    reports = build_reports(result.allocations, sample_problem)
    assert len(reports) == len(sample_problem.scenarios) * len(sample_problem.windows)
    for report in reports:
        assert report.allocated_total == pytest.approx(
            next(
                w.total_supply
                for w in sample_problem.windows
                if w.name == report.window
            ),
            abs=TOL,
        )


def test_solve_is_reproducible(sample_problem: Problem) -> None:
    cfg = SolverConfig()
    r1 = solve(sample_problem, cfg, "k", payload_digest(sample_problem, cfg))
    r2 = solve(sample_problem, cfg, "k", payload_digest(sample_problem, cfg))
    assert r1.allocations == r2.allocations
    assert r1.model_version == r2.model_version
    assert r1.config_digest == r2.config_digest


def test_solve_responds_to_cancel_event(sample_problem: Problem) -> None:
    cfg = SolverConfig()
    event = threading.Event()
    event.set()  # 进入前即取消
    with pytest.raises(SolverCancelledError):
        solve(sample_problem, cfg, "k", "h", cancel_event=event)
