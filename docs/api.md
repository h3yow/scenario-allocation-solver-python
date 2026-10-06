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
  "base_key": null,
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

## POST /replans

带已确认额度的重规划：接着一份**已成功**的方案（`base_key`），锁定确认
格子后按最新输入重排未确认部分。幂等键、轮询、取消语义与
`POST /optimizations` 完全一致（共用同一作业存储与 `GET`/`cancel` 接口）。

请求体（在 `OptimizeRequest` 基础上增加两个字段）：

```json
{
  "base_key": "demo-1",
  "confirmations": [{"entity": "a", "window": "w1", "amount": 30.0}],
  "entities": [{"name": "a", "base_demand": 100, "min_share": 10}],
  "windows":  [{"name": "w1", "capacity": 80}],
  "scenarios": [{"name": "base", "total_supply": 100, "is_baseline": true}],
  "quantize_decimals": 9
}
```

- `confirmations[]`：已确认下发的额度格子。必须来自 `base_key` 方案
  **基线场景**中的既有分配且不超过原分配量；在重规划结果的所有场景中，
  对应格子的最终值不低于确认量（确认部分原样保留，只重排未确认部分）。

响应：

- `202 Accepted`：作业已受理（响应体带 `base_key` 表明血缘）；
- `200 OK`：该幂等键已有成功结果（重复/并发提交），返回同一份
  `OptimizeResponse`，其中 `base_key` 字段指向基准方案；
- `400 INVALID_REQUEST`：结构非法、基准方案未成功、确认量无法在当前
  精度网格上精确表示等；
- `404 NOT_FOUND`：`base_key` 指向的方案不存在；
- `409 CONFIRMATION_CONFLICT`：确认量超过基准方案对应格子的分配量，
  或确认格子在基准方案中不存在（同步拒绝，不产生作业）；
- `409 CONFLICT`：同幂等键绑定了不同指纹（语义同 `/optimizations`）。

作业终态中新增失败分类 `CONFIRMATION_CONFLICT`：确认额度与最新输入
（窗口容量、实体需求上限、场景总供给）冲突，`details.reasons` 逐条指明
场景/实体/窗口；与 `INFEASIBLE`（输入本身不可行）严格区分，且不会
返回任何半成品结果。

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
