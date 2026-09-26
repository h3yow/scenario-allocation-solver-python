"""关键输入、状态迁移、判定依据必须出现在单测日志中。"""
from __future__ import annotations

import logging

import pytest

from sas.service import AllocationService


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def collector() -> _Collector:
    handler = _Collector()
    logger = logging.getLogger("sas")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def test_key_events_are_logged(sample_problem, collector: _Collector) -> None:
    svc = AllocationService()
    svc.submit("logged", sample_problem)
    assert svc.wait_until_terminal("logged", 5)

    joined = "\n".join(collector.messages)
    # 关键输入
    assert "校验问题" in joined
    assert "可行性判定" in joined
    # 判定依据
    assert "判定依据" in joined
    # 状态迁移
    assert "(init) -> PENDING" in joined
    assert "PENDING -> RUNNING" in joined
    assert "RUNNING -> SUCCEEDED" in joined
    # 结果提交
    assert any("结果已提交" in m for m in collector.messages)


def test_infeasible_reason_is_logged(collector: _Collector) -> None:
    from sas.models import Entity, Problem, ScenarioInput, Window
    from sas.errors import InfeasibleError

    svc = AllocationService()
    p = Problem(
        entities=[Entity("a", 9.0, 9.0)],
        windows=[Window("w", 3.0)],
        scenarios=[ScenarioInput("baseline", {"w": {"a": 3.0}})],
    )
    with pytest.raises(InfeasibleError):
        svc.submit("inf-log", p)
    joined = "\n".join(collector.messages)
    assert "SUPPLY_BELOW_MIN_GUARANTEE" in joined
