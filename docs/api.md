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

## POST /replans（带已确认额度的重规划）

在一份**已经成功**的方案之上重规划：已确认的格子
`(scenario, entity, window, amount)` 在新方案中**原样保留**，剩余供给
只在未锁定格子上按最新输入重排。提交/查询/取消/超时/失败重试语义与
`/optimizations` 完全一致，使用同一个 `Idempotency-Key` 命名空间
（同一重规划键并发或重复提交只真正计算一次）。

请求体（在普通请求基础上增加 `based_on_key` 与 `confirmed_allocations`）：

```json
{
  "based_on_key": "demo-1",
  "entities": [{"name": "a", "base_demand": 100, "min_share": 10}],
  "windows":  [{"name": "w1", "capacity": 80}],
  "scenarios": [
    {"name": "base", "total_supply": 120, "is_baseline": true},
    {"name": "up", "total_supply": 100, "demand_multipliers": {"a": 1.5}}
  ],
  "confirmed_allocations": [
    {"scenario": "base", "entity": "a", "window": "w1", "amount": 30.0}
  ],
  "timeout_seconds": 5.0,
  "quantize_decimals": 9,
  "solver_step_delay": 0.0
}
```

字段约束：

- `based_on_key`：必填，指向**本服务内已成功**的作业；不存在 → `404`，
  存在但未成功（RUNNING/FAILED）→ `400 INVALID_REQUEST`；
- `confirmed_allocations[]`：`amount > 0`，`scenario/entity/window` 必须
  同时存在于最新输入与被接续方案；同一 `(scenario,entity,window)` 不允许
  重复；`amount` 不能超过该方案在该格子上的**实发量**（不能确认没真正
  下发过的额度）。以上结构问题均为登记前同步 `400`，不产生作业；
- `amount` 必须是 `10^-quantize_decimals` 的整数倍，否则在该精度下无法
  原样保留 → `400`，请提高 `quantize_decimals`。

响应仍是 `OptimizeResponse`，并额外回传：

- 顶层 `based_on_key`：本次重规划接续的既有方案键（谱系可见）；
- 每个场景 `confirmed_total`：该场景被原样锁定的确认额度合计。

失败分类（与普通提交共用状态机）：

- `412 CONFIRMED_CONFLICT`：确认额度与**最新输入**直接冲突——
  某窗口已装不下 / 某实体确认量超过其当前需求上限 / 确认总量超过总供给。
  `details.reasons` 逐条指出是哪个场景、哪个实体或窗口的问题；
- `422 INFEASIBLE`：输入合法、锁也不冲突，但锁定占用后残量整体无可行解
  （例如锁定把可拆分容量切碎，残量保底无处安放）；
- `400 INVALID_REQUEST`：结构/引用非法，或当前量化网格装不下；
- `TIMEOUT/CANCELLED/SOLVER_FAILURE` 与普通提交一致，可用同键同指纹重试。

**任何失败都整体发生，不返回半成品，也绝不偷偷修改确认额度来凑结果；
被接续的既有方案永不被重规划改写。**

## POST /optimizations/{key}/cancel

- `202`：作业运行中，取消信号已置位（作业在下一检查点以 `CANCELLED` 结束）；
- `200`：作业已终态，取消不生效；`404`：键不存在。

## 错误体

```json
{"error": {"code": "INFEASIBLE", "message": "…", "details": {"reasons": ["…"]}}}
```

code → HTTP 映射见 README“异常分类”一节。
