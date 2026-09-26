"""输入校验与可行性判定单测。"""
from __future__ import annotations

import pytest

from sas.errors import InfeasibleError, InvalidRequestError
from sas.feasibility import check_feasibility
from sas.models import Entity, Problem, ScenarioInput, SolverConfig, Window
from sas.validation import validate_config, validate_problem


def _feasible_single() -> Problem:
    return Problem(
        entities=[Entity("a", 1.0, 5.0)],
        windows=[Window("w", 3.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 2.0}})],
    )


@pytest.mark.parametrize(
    "config",
    [
        SolverConfig(solve_timeout=0),
        SolverConfig(tolerance=-1.0),
        SolverConfig(max_iterations=0),
    ],
)
def test_invalid_config_rejected(config: SolverConfig) -> None:
    with pytest.raises(InvalidRequestError):
        validate_config(config)


def test_valid_config_accepted() -> None:
    validate_config(SolverConfig())


def test_empty_entities_rejected() -> None:
    p = Problem(
        entities=[],
        windows=[Window("w", 1.0)],
        scenarios=[ScenarioInput("baseline", {})],
    )
    with pytest.raises(InvalidRequestError, match="entities"):
        validate_problem(p)


def test_duplicate_entity_id_rejected() -> None:
    p = Problem(
        entities=[Entity("a", 0, 1), Entity("a", 0, 1)],
        windows=[Window("w", 1.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 1}})],
    )
    with pytest.raises(InvalidRequestError, match="重复"):
        validate_problem(p)


def test_min_above_capacity_rejected() -> None:
    p = Problem(
        entities=[Entity("a", 5.0, 1.0)],
        windows=[Window("w", 3.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 2.0}})],
    )
    with pytest.raises(InvalidRequestError, match="最小保障"):
        validate_problem(p)


def test_missing_baseline_rejected() -> None:
    p = Problem(
        baseline_name="baseline",
        entities=[Entity("a", 0, 5)],
        windows=[Window("w", 3.0)],
        scenarios=[ScenarioInput("other", {"w": {"a": 2}})],
    )
    with pytest.raises(InvalidRequestError, match="baseline"):
        validate_problem(p)


def test_unknown_entity_reference_rejected() -> None:
    p = Problem(
        entities=[Entity("a", 0, 5)],
        windows=[Window("w", 3.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"ghost": 2.0}})],
    )
    with pytest.raises(InvalidRequestError, match="ghost"):
        validate_problem(p)


def test_negative_supply_rejected() -> None:
    p = Problem(
        entities=[Entity("a", 0, 5)],
        windows=[Window("w", -1.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 2.0}})],
    )
    with pytest.raises(InvalidRequestError, match="total_supply"):
        validate_problem(p)


def test_valid_problem_passes(sample_problem: Problem) -> None:
    validate_problem(sample_problem)


def test_infeasible_supply_below_min() -> None:
    p = Problem(
        entities=[Entity("a", 5.0, 9.0)],
        windows=[Window("w", 3.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 3.0}})],
    )
    with pytest.raises(InfeasibleError) as exc:
        check_feasibility(p)
    assert exc.value.details["reason"] == "SUPPLY_BELOW_MIN_GUARANTEE"
    assert exc.value.details["shortfall"] == pytest.approx(2.0)


def test_infeasible_supply_exceeds_capacity() -> None:
    p = Problem(
        entities=[Entity("a", 0.0, 5.0)],
        windows=[Window("w", 8.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 3.0}})],
    )
    with pytest.raises(InfeasibleError) as exc:
        check_feasibility(p)
    assert exc.value.details["reason"] == "SUPPLY_EXCEEDS_CAPACITY"
    assert exc.value.details["excess"] == pytest.approx(3.0)


def test_feasible_passes() -> None:
    check_feasibility(_feasible_single())
