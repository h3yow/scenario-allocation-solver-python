# HTTP 接口契约

所有 POST 提交通过请求头携带幂等键：`Idempotency-Key: <string>`。

## POST /optimizations

请求体：

```json
{
  "entities": [{"name": "a", "base_demand": 100, "min_share": 10}],
  "windows":  [{"name": "w1", "capacity": 80}],
  "scenarios": [
    {"name": "base", "total_supply": 120, "is_baseline": true},
    {"name": "up", "total_supply": 100, "demand_multipliers": {"a": 1.5}}
  ],
  "timeout_seconds": 5.0,
  "quantize_decimals": 9,
  "solver_step_delay": 0.0
}
```

响应：

- `202 Accepted`：作业已受理（RUNNING）；
- `200 OK`：该幂等键已有成功结果（重复提交），返回同一份 `OptimizeResponse`；
- `400`：参数非法；`409`：同键不同指纹。

`OptimizeResponse`：

```json
{
  "idempotency_key": "demo-1",
  "status": "SUCCEEDED",
  "solver_version": "solver-1.0.0",
  "request_fingerprint": "sha256-hex",
  "quantize_decimals": 9,
  "scenarios": [
    {
      "scenario": "base",
      "is_baseline": true,
      "total_supply": 120.0,
      "total_demand": 150.0,
      "total_allocated": 120.0,
      "feasible": true,
      "infeasibility_reasons": [],
      "entity_allocations": [
        {"entity": "a", "demand": 100.0, "min_share": 10.0, "amount": 80.0}
      ],
      "window_allocations": [
        {"window": "w1", "allocated": 80.0, "capacity": 80.0,
         "entities": {"a": 80.0}}
      ]
    }
  ]
}
```

## GET /optimizations/{key}

- `200` + `OptimizeResponse`：成功；
- `202` + `{status:"RUNNING",...}`：仍在求解（可轮询）；
- `200` + `{status:"FAILED", error:{...}}`：失败，`error.code` 区分
  `INFEASIBLE/TIMEOUT/CANCELLED/SOLVER_FAILURE`；
- `404`：键不存在。

## POST /optimizations/{key}/cancel

- `202`：作业运行中，取消信号已置位（作业在下一检查点以 `CANCELLED` 结束）；
- `200`：作业已终态，取消不生效；`404`：键不存在。

## 错误体

```json
{"error": {"code": "INFEASIBLE", "message": "…", "details": {"reasons": ["…"]}}}
```

code → HTTP 映射见 README“异常分类”一节。
