"""服务层测试：超时/取消区分、失败恢复、状态迁移、结果复现。

关键输入通过 solver_step_delay 制造可控的慢求解，使超时与取消稳定复现。
"""

from __future__ import annotations

import time

from app.errors import NotFoundError
from app.logging_setup import get_logger
from app.service.idempotency import IdempotencyStore, JobStatus
from app.service.optimizer import OptimizerService
from tests.factories import make_request
from tests.test_idempotency import wait_for_terminal

logger = get_logger("tests.test_service")


def test_timeout_job_fails_with_timeout_code_and_can_retry_succeed() -> None:
    logger.info("=== 用例：超时失败(504 类)后同键重试成功 ===")
    service = OptimizerService()
    key = "k-timeout"

    # 极小超时 + 人为步进延迟，必然超时。
    slow = make_request(
        timeout_seconds=0.02,
        solver_step_delay=0.05,
        scenarios=__multi_scenarios(),
    )
    service.submit(key, slow)
    record = wait_for_terminal(service, key)
    assert record.status is JobStatus.FAILED
    assert record.error_code == "TIMEOUT"
    assert record.result is None, "失败不得写入部分结果"
    logger.info("判定依据：首次作业超时 FAILED code=TIMEOUT，无结果对象")

    # 同指纹重试但放宽超时、去掉延迟 -> 成功；证明失败未污染重试。
    fast = slow.model_copy(
        update={"timeout_seconds": None, "solver_step_delay": 0.0}
    )
    assert service.fingerprint(slow) == service.fingerprint(fast), (
        "超时/延迟是运行参数，不参与结果指纹"
    )
    outcome = service.submit(key, fast)
    assert outcome.started is True
    ok = wait_for_terminal(service, key)
    assert ok.status is JobStatus.SUCCEEDED
    assert ok.result is not None
    logger.info("重试成功 key=%s 场景数=%d", key, len(ok.result.scenarios))


def test_cancelled_job_distinct_from_timeout_and_retryable() -> None:
    logger.info("=== 用例：主动取消(499 类)后重试成功 ===")
    service = OptimizerService()
    key = "k-cancel"

    slow = make_request(solver_step_delay=0.02)
    service.submit(key, slow)
    # 立刻取消（作业尚在首个检查点之前）。
    time.sleep(0.01)
    service.cancel(key)

    record = wait_for_terminal(service, key)
    assert record.status is JobStatus.FAILED
    assert record.error_code == "CANCELLED", "取消必须与超时区分"
    assert record.result is None
    logger.info("判定依据：检测到取消事件 -> FAILED code=CANCELLED")

    fast = slow.model_copy(update={"solver_step_delay": 0.0})
    service.submit(key, fast)
    ok = wait_for_terminal(service, key)
    assert ok.status is JobStatus.SUCCEEDED
    logger.info("取消后同指纹重试成功，取消未污染后续提交")


def test_cancel_terminal_job_is_noop() -> None:
    logger.info("=== 用例：取消已终态作业不生效、不改变结果 ===")
    service = OptimizerService()
    key = "k-cancel-terminal"
    service.submit(key, make_request())
    ok = wait_for_terminal(service, key)
    assert ok.status is JobStatus.SUCCEEDED

    record = service.cancel(key)
    assert record.status is JobStatus.SUCCEEDED
    assert not record.cancel_event.is_set()
    logger.info("判定依据：终态作业取消请求被忽略，成功结果保持不变")


def test_get_unknown_key_not_found() -> None:
    logger.info("=== 用例：查询不存在的作业 ===")
    service = OptimizerService()
    try:
        service.get_result("missing")
        raise AssertionError("应抛 NotFoundError")
    except NotFoundError as exc:
        assert exc.code == "NOT_FOUND"
        logger.info("判定依据：未知键 -> NOT_FOUND")


def test_reproducible_result_across_independent_services() -> None:
    logger.info("=== 用例：跨服务实例同输入结果等价（可复现）===")
    request = make_request(
        quantize_decimals=6,
        scenarios=__multi_scenarios(),
    )
    results = []
    for i in range(2):
        service = OptimizerService(store=IdempotencyStore())
        key = f"k-repro-{i}"
        service.submit(key, request)
        record = wait_for_terminal(service, key)
        results.append(record.result)

    assert results[0] is not None and results[1] is not None
    # 只比较求解相关内容（幂等键/指纹是提交元数据，不参与结果等价性）。
    payload0 = results[0].model_dump(
        exclude={"idempotency_key", "request_fingerprint", "status"}
    )
    payload1 = results[1].model_dump(
        exclude={"idempotency_key", "request_fingerprint", "status"}
    )
    assert payload0 == payload1
    assert results[0].solver_version == results[1].solver_version
    # 指纹一致：同输入+同配置+稳定版本。
    assert results[0].request_fingerprint == results[1].request_fingerprint
    logger.info(
        "判定依据：两次独立服务的结果 JSON 完全一致 version=%s",
        results[0].solver_version,
    )


def __multi_scenarios():
    from app.models import ScenarioIn

    return [
        ScenarioIn(name="base", total_supply=120.0, is_baseline=True),
        ScenarioIn(name="s1", total_supply=100.0, demand_multipliers={"a": 1.5}),
        ScenarioIn(name="s2", total_supply=30.0),
    ]
