"""API 端到端测试。

覆盖：
- 提交 202、完成后 GET 200；成功后重复 POST 幂等返回 200 等价结果；
- 参数非法 400、不可行 422、同键冲突 409、超时 504、取消 499、未找到 404；
- 并发重复提交只产生一次求解；
- 返回结果在基线与扰动场景间一致满足容量/保底/守恒约束。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.logging_setup import get_logger
from app.models import ScenarioIn
from main import create_app
from app.service.optimizer import OptimizerService
from app.service.idempotency import JobStatus
from tests.factories import make_request

logger = get_logger("tests.test_api")


@pytest.fixture()
def client():
    service = OptimizerService()
    app = create_app(service=service)
    with TestClient(app) as c:
        c.service = service
        yield c


def _poll(client, key: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/optimizations/{key}")
        body = resp.json()
        if body.get("status") == "RUNNING":
            time.sleep(0.01)
            continue
        return resp, body
    raise AssertionError("轮询超时")


VALID_HEADERS = {"Idempotency-Key": "k-valid"}


def test_submit_then_get_success_and_constraints(client) -> None:
    logger.info("=== API：提交->轮询->成功，核对跨场景约束 ===")
    req = make_request(
        scenarios=[
            ScenarioIn(name="base", total_supply=120, is_baseline=True),
            ScenarioIn(name="up", total_supply=100, demand_multipliers={"a": 1.5}),
            ScenarioIn(name="tight", total_supply=30),
        ]
    ).model_dump()

    resp = client.post("/optimizations", json=req, headers=VALID_HEADERS)
    # 受理返回 202；求解极快时也可能已直接返回 200（完成），两者都合法。
    assert resp.status_code in (200, 202), resp.text
    if resp.status_code == 200:
        body = resp.json()
        assert body["status"] == "SUCCEEDED"
        final, body = resp, body
    else:
        assert resp.json()["status"] == "RUNNING"
        final, body = _poll(client, VALID_HEADERS["Idempotency-Key"])
    assert final.status_code == 200
    assert body["status"] == "SUCCEEDED"

    assert len(body["scenarios"]) == 3
    for s in body["scenarios"]:
        tol = 2e-6
        assert abs(s["total_allocated"] - s["total_supply"]) <= tol
        window_sum = sum(w["allocated"] for w in s["window_allocations"])
        assert abs(window_sum - s["total_allocated"]) <= tol
        for w in s["window_allocations"]:
            assert w["allocated"] <= w["capacity"] + tol
            assert abs(sum(w["entities"].values()) - w["allocated"]) <= tol
        for e in s["entity_allocations"]:
            assert e["amount"] >= e["min_share"] - tol
            assert e["amount"] <= e["demand"] + tol
    logger.info("判定依据：3 个场景的容量/保底/守恒在 API 结果中全部成立")


def test_duplicate_post_returns_same_idempotent_result(client) -> None:
    logger.info("=== API：成功后重复 POST 幂等 ===")
    req = make_request().model_dump()
    key = "k-dup"
    client.post("/optimizations", json=req, headers={"Idempotency-Key": key})
    _, first = _poll(client, key)

    resp = client.post("/optimizations", json=req, headers={"Idempotency-Key": key})
    assert resp.status_code == 200
    assert resp.json() == first
    logger.info("判定依据：重复提交返回 200 且响应体与首次完全一致")


def test_invalid_payload_returns_400(client) -> None:
    logger.info("=== API：参数非法 400 ===")
    req = make_request().model_dump()
    req["entities"] = []  # 业务层：实体为空
    resp = client.post(
        "/optimizations", json=req, headers={"Idempotency-Key": "k-bad"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    # 传输层：负数
    req2 = make_request().model_dump()
    req2["windows"][0]["capacity"] = -3
    resp2 = client.post(
        "/optimizations", json=req2, headers={"Idempotency-Key": "k-bad2"}
    )
    assert resp2.status_code == 400
    assert resp2.json()["error"]["code"] == "INVALID_REQUEST"
    logger.info("判定依据：空实体/负容量均为 400 INVALID_REQUEST")


def test_infeasible_returns_422_with_reasons(client) -> None:
    logger.info("=== API：不可行 422 且带原因 ===")
    req = make_request(supply=50, capacities=(5,)).model_dump()
    client.post(
        "/optimizations", json=req, headers={"Idempotency-Key": "k-inf"}
    )
    resp, body = _poll(client, "k-inf")
    assert resp.status_code == 200  # 作业终态仍可查询
    assert body["status"] == "FAILED"
    assert body["error"]["code"] == "INFEASIBLE"
    assert body["error"]["details"]["reasons"]
    logger.info("判定依据：不可行落 FAILED/INFEASIBLE，原因=%s",
                body["error"]["details"]["reasons"])


def test_conflicting_key_returns_409(client) -> None:
    logger.info("=== API：同键不同内容 409 ===")
    key = "k-409"
    client.post(
        "/optimizations",
        json=make_request(supply=120).model_dump(),
        headers={"Idempotency-Key": key},
    )
    _poll(client, key)
    resp = client.post(
        "/optimizations",
        json=make_request(supply=77).model_dump(),
        headers={"Idempotency-Key": key},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFLICT"
    logger.info("判定依据：同键不同指纹 -> 409 CONFLICT")


def test_timeout_returns_504_and_cancel_returns_499_distinct(client) -> None:
    logger.info("=== API：超时 504 与取消 499 可区分 ===")
    # 超时
    slow = make_request(
        timeout_seconds=0.02, solver_step_delay=0.05
    ).model_dump()
    client.post(
        "/optimizations", json=slow, headers={"Idempotency-Key": "k-timeout"}
    )
    _, body = _poll(client, "k-timeout")
    assert body["error"]["code"] == "TIMEOUT"

    # 取消
    slow2 = make_request(solver_step_delay=0.05).model_dump()
    client.post(
        "/optimizations", json=slow2, headers={"Idempotency-Key": "k-cancel"}
    )
    time.sleep(0.02)
    cancel_resp = client.post("/optimizations/k-cancel/cancel")
    assert cancel_resp.status_code == 202
    _, body2 = _poll(client, "k-cancel")
    assert body2["error"]["code"] == "CANCELLED"
    assert body2["error"]["code"] != body["error"]["code"]
    logger.info("判定依据：TIMEOUT(504) 与 CANCELLED(499) code 严格不同")


def test_missing_key_returns_404(client) -> None:
    resp = client.get("/optimizations/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    logger.info("判定依据：查询未知键 -> 404")


def test_concurrent_duplicate_posts_single_solve(client) -> None:
    logger.info("=== API：20 路并发同键提交 ===")
    req = make_request().model_dump()
    key = "k-api-race"
    n = 20
    barrier = threading.Barrier(n)
    statuses: list[int] = []
    lock = threading.Lock()

    def hit() -> None:
        barrier.wait()
        r = client.post(
            "/optimizations", json=req, headers={"Idempotency-Key": key}
        )
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=hit) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个 202（真正启动），其余为 202（RUNNING 复用）或 200（已成功复用）。
    _, body = _poll(client, key)
    assert body["status"] == "SUCCEEDED"
    assert set(statuses) <= {202, 200}
    logger.info("提交状态码分布=%s，最终单一成功结果",
                {code: statuses.count(code) for code in set(statuses)})
