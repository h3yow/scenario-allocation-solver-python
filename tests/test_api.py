"""HTTP 端到端集成测试。"""
from __future__ import annotations

import threading
import time
import warnings

import pytest
from fastapi.testclient import TestClient

from sas.api import create_app
from sas.errors import SolverCancelledError
from sas.service import AllocationService

warnings.filterwarnings("ignore")


def _body(key: str = "k1", timeout: float | None = None) -> dict:
    body = {
        "idempotency_key": key,
        "entities": [
            {"id": "a", "min_guarantee": 1, "capacity": 8},
            {"id": "b", "min_guarantee": 0, "capacity": 6},
        ],
        "windows": [{"name": "w1", "total_supply": 7}],
        "scenarios": [
            {"name": "baseline", "demands": {"w1": {"a": 5, "b": 4}}},
            {"name": "shock", "demands": {"w1": {"a": 8, "b": 1}}},
        ],
    }
    if timeout is not None:
        body["config"] = {"solve_timeout": timeout}
    return body


def test_health_and_happy_path() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/api/v1/optimizations", json=_body())
    assert r.status_code == 200
    # 求解异步进行:必要时轮询至终态
    if r.json()["status"] != "SUCCEEDED":
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            r = client.get("/api/v1/optimizations/k1")
            if r.json()["status"] == "SUCCEEDED":
                break
            time.sleep(0.02)
    data = r.json()
    assert data["status"] == "SUCCEEDED"
    assert data["result"]["model_version"] == "allocation-waterfill-v1"
    # 守恒
    total = sum(data["result"]["allocations"]["baseline"]["w1"].values())
    assert total == pytest.approx(7.0, abs=1e-9)


def test_concurrent_duplicate_http_posts_share_one_result() -> None:
    client = TestClient(create_app())
    payload_hashes: set[str] = set()
    statuses: set[str] = set()
    lock = threading.Lock()

    def post() -> None:
        r = client.post("/api/v1/optimizations", json=_body("dup"))
        assert r.status_code == 200
        data = r.json()
        with lock:
            statuses.add(data["status"])
            if data["result"] is not None:
                payload_hashes.add(data["result"]["payload_hash"])

    threads = [threading.Thread(target=post) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    # 提交是异步的:部分响应可能仍是 RUNNING,轮询到终态后核对唯一结果
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        g = client.get("/api/v1/optimizations/dup")
        if g.json()["status"] == "SUCCEEDED":
            payload_hashes.add(g.json()["result"]["payload_hash"])
            break
        time.sleep(0.02)
    assert client.get("/api/v1/optimizations/dup").json()["status"] == "SUCCEEDED"
    assert len(payload_hashes) == 1
    assert statuses <= {"RUNNING", "SUCCEEDED"}


def test_error_taxonomy_over_http() -> None:
    client = TestClient(create_app())
    # 404
    assert client.get("/api/v1/optimizations/missing").status_code == 404

    # 400 业务非法(min>cap)
    bad = _body("bad")
    bad["entities"][0]["min_guarantee"] = 9
    r = client.post("/api/v1/optimizations", json=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    # 422 不可行
    inf = _body("inf")
    inf["entities"] = [{"id": "a", "min_guarantee": 9, "capacity": 9}]
    inf["scenarios"] = [{"name": "baseline", "demands": {"w1": {"a": 3}}}]
    r = client.post("/api/v1/optimizations", json=inf)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INFEASIBLE"

    # 先成功一个,再用冲突体 -> 409
    client.post("/api/v1/optimizations", json=_body("conf"))
    conflict = _body("conf")
    conflict["entities"][0]["capacity"] = 7.9
    r = client.post("/api/v1/optimizations", json=conflict)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_cancel_over_http_then_retry() -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp(scenario, window, cancel_event) -> None:
        entered.set()
        while not release.is_set():
            if cancel_event.wait(0.02):
                raise SolverCancelledError("interrupted")

    svc = AllocationService(checkpoint=cp)
    client = TestClient(create_app(svc))

    client.post("/api/v1/optimizations", json=_body("job", timeout=5.0))
    assert entered.wait(2.0)
    r = client.post("/api/v1/optimizations/job/cancel")
    assert r.status_code == 200
    # 轮询直到观察到 CANCELLED 终态(499),而不是固定 sleep
    deadline = time.monotonic() + 3.0
    seen = None
    while time.monotonic() < deadline:
        g = client.get("/api/v1/optimizations/job")
        seen = g.status_code
        if seen == 499:
            break
        time.sleep(0.02)
    assert seen == 499
    assert g.json()["error"]["code"] == "CANCELLED"
    release.set()
    time.sleep(0.1)

    # 同载荷重试成功(不带检查点)
    svc.checkpoint = None
    r2 = client.post("/api/v1/optimizations", json=_body("job"))
    assert r2.status_code == 200
    deadline = time.monotonic() + 5.0
    final = None
    while time.monotonic() < deadline:
        g = client.get("/api/v1/optimizations/job")
        if g.json()["status"] == "SUCCEEDED":
            final = g
            break
        time.sleep(0.02)
    assert final is not None and final.json()["status"] == "SUCCEEDED"


def test_timeout_over_http() -> None:
    entered = threading.Event()
    release = threading.Event()

    def cp(scenario, window, cancel_event) -> None:
        entered.set()
        while not release.is_set():
            if cancel_event.wait(0.02):
                raise SolverCancelledError("timeout interrupt")

    svc = AllocationService(checkpoint=cp)
    client = TestClient(create_app(svc))
    client.post("/api/v1/optimizations", json=_body("slow", timeout=0.2))
    assert entered.wait(2.0)
    deadline = time.monotonic() + 3.0
    status_code = None
    while time.monotonic() < deadline:
        r = client.get("/api/v1/optimizations/slow")
        status_code = r.status_code
        if status_code == 408:
            assert r.json()["error"]["code"] == "SOLVE_TIMEOUT"
            release.set()
            return
        time.sleep(0.05)
    release.set()
    pytest.fail(f"未观察到 408,最终状态码={status_code}")
