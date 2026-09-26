"""pytest 共享夹具与日志配置。

单测日志要求可见:关键输入、状态迁移、判定依据,因此这里把 sas.* 日志
接到 caplog/控制台,级别 INFO。
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from sas.models import Entity, Problem, ScenarioInput, SolverConfig, Window


@pytest.fixture(autouse=True)
def _sas_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sas")


@pytest.fixture
def sample_problem() -> Problem:
    """两主体、两窗口、三场景(基线+两种扰动)的可行示例。"""
    return Problem(
        baseline_name="baseline",
        entities=[
            Entity(id="a", min_guarantee=1.0, capacity=8.0),
            Entity(id="b", min_guarantee=0.0, capacity=6.0),
        ],
        windows=[Window("w1", 7.0), Window("w2", 5.0)],
        scenarios=[
            ScenarioInput(
                "baseline",
                {"w1": {"a": 5.0, "b": 4.0}, "w2": {"a": 3.0, "b": 4.0}},
            ),
            ScenarioInput(
                "demand_shock",
                {"w1": {"a": 8.0, "b": 1.0}, "w2": {"a": 1.0, "b": 6.0}},
            ),
            ScenarioInput(
                "supply_chain",
                {"w1": {"a": 4.0, "b": 6.0}, "w2": {"a": 5.0, "b": 2.0}},
            ),
        ],
    )


@pytest.fixture
def fast_config() -> SolverConfig:
    return SolverConfig(solve_timeout=0.3, tolerance=1e-9)


class BlockingCheckpoint:
    """测试用检查点:进入每个 (场景,窗口) 时阻塞,直到 release 或超时。

    用于稳定复现“求解进行中”的窗口,从而测试取消/超时与并发重复提交。
    """

    def __init__(self, hold_seconds: float = 5.0) -> None:
        self.hold_seconds = hold_seconds
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, scenario: str, window: str) -> None:
        logging.getLogger("sas.test").info(
            "检查点阻塞开始 | scenario=%s window=%s", scenario, window
        )
        self.entered.set()
        if not self.release.wait(timeout=self.hold_seconds):
            logging.getLogger("sas.test").info("检查点等待超时后继续")

    def wait_entered(self, timeout: float = 2.0) -> bool:
        return self.entered.wait(timeout)

    def unblock(self) -> None:
        self.release.set()


@pytest.fixture
def checkpoint_factory():
    created: list[BlockingCheckpoint] = []

    def factory(hold_seconds: float = 5.0) -> BlockingCheckpoint:
        cp = BlockingCheckpoint(hold_seconds=hold_seconds)
        created.append(cp)
        return cp

    factory.created = created
    return factory


@pytest.fixture(autouse=True)
def _assert_no_hang():
    """兜底:单个测试不应无限等待。"""
    start = time.monotonic()
    yield
    elapsed = time.monotonic() - start
    if elapsed > 20:
        pytest.fail(f"测试运行过久({elapsed:.1f}s),可能存在线程挂起")
