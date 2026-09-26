"""输入校验:把结构/取值/一致性问题统一转成 InvalidRequestError。"""
from __future__ import annotations

import logging

from .errors import InvalidRequestError
from .models import Problem, SolverConfig

logger = logging.getLogger("sas.validation")


def _fail(message: str) -> None:
    logger.warning("输入校验失败 | 原因=%s", message)
    raise InvalidRequestError(message)


def validate_config(config: SolverConfig) -> None:
    """校验求解配置取值范围。"""
    logger.info(
        "校验配置 | solve_timeout=%s tolerance=%s max_iterations=%s",
        config.solve_timeout,
        config.tolerance,
        config.max_iterations,
    )
    if config.solve_timeout <= 0:
        _fail(f"solve_timeout 必须为正数,实际={config.solve_timeout}")
    if config.tolerance <= 0:
        _fail(f"tolerance 必须为正数,实际={config.tolerance}")
    if config.max_iterations <= 0:
        _fail(f"max_iterations 必须为正整数,实际={config.max_iterations}")


def validate_problem(problem: Problem) -> None:
    """校验问题结构与取值完整性。"""
    logger.info(
        "校验问题 | baseline=%s entities=%d windows=%d scenarios=%d",
        problem.baseline_name,
        len(problem.entities),
        len(problem.windows),
        len(problem.scenarios),
    )
    if not problem.entities:
        _fail("entities 不能为空:至少需要一个可分配主体")
    if not problem.windows:
        _fail("windows 不能为空:至少需要一个滚动窗口")
    if not problem.scenarios:
        _fail("scenarios 不能为空:至少需要基线场景")

    seen_ids: set[str] = set()
    for e in problem.entities:
        if not e.id:
            _fail("entity.id 不能为空")
        if e.id in seen_ids:
            _fail(f"entity.id 重复:{e.id}")
        seen_ids.add(e.id)
        if e.min_guarantee < 0:
            _fail(f"entity[{e.id}].min_guarantee 不能为负,实际={e.min_guarantee}")
        if e.capacity < 0:
            _fail(f"entity[{e.id}].capacity 不能为负,实际={e.capacity}")
        if e.min_guarantee > e.capacity + 1e-12:
            _fail(
                f"entity[{e.id}] 最小保障 {e.min_guarantee} 超过容量 {e.capacity}"
            )

    seen_windows: set[str] = set()
    for w in problem.windows:
        if not w.name:
            _fail("window.name 不能为空")
        if w.name in seen_windows:
            _fail(f"window.name 重复:{w.name}")
        seen_windows.add(w.name)
        if w.total_supply < 0:
            _fail(f"window[{w.name}].total_supply 不能为负,实际={w.total_supply}")

    scenario_names = {s.name for s in problem.scenarios}
    if problem.baseline_name not in scenario_names:
        _fail(
            f"baseline_name={problem.baseline_name} 不在场景列表 {sorted(scenario_names)} 中"
        )
    seen_scenarios: set[str] = set()
    for s in problem.scenarios:
        if not s.name:
            _fail("scenario.name 不能为空")
        if s.name in seen_scenarios:
            _fail(f"scenario.name 重复:{s.name}")
        seen_scenarios.add(s.name)
        for window_name, row in s.demands.items():
            if window_name not in seen_windows:
                _fail(
                    f"scenario[{s.name}] 引用了不存在的窗口:{window_name}"
                )
            for entity_id, demand in row.items():
                if entity_id not in seen_ids:
                    _fail(
                        f"scenario[{s.name}].window[{window_name}] "
                        f"引用了不存在的主体:{entity_id}"
                    )
                if demand < 0:
                    _fail(
                        f"scenario[{s.name}].window[{window_name}].entity[{entity_id}] "
                        f"需求量不能为负,实际={demand}"
                    )

    logger.info("输入校验通过")

