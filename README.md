# Scenario Allocation Solver

本地化的**多场景滚动窗口资源分配优化**服务。同一批需求在
正常 / 不可行 / 并发重复提交 / 求解超时 / 主动取消等情况下，都得到
一致、可复现、可解释的结果；方案下发后可锁定已确认额度做**重规划**，
只重排未确认部分。纯本地运行，不依赖外部优化求解器。

- Web 框架：FastAPI（Pydantic v2）
- Python：>=3.14.4，依赖管理：uv
- 求解：确定性两步法（实体水位填充 + 窗口整数网格装箱），无第三方求解器

---

## 1. 优化模型

### 1.1 输入

| 概念 | 字段 | 含义 |
| --- | --- | --- |
| 需求实体 | `entities[]` | `name`、`base_demand`（基线需求）、`min_share`（最小保障量，绝对值） |
| 滚动窗口 | `windows[]` | `name`、`capacity`（该窗口容量上限） |
| 场景 | `scenarios[]` | `name`、`total_supply`（该场景总供给）、`demand_multipliers`（实体需求扰动乘数）、`is_baseline`（**恰好一个**基线场景） |

某场景下实体 `e` 的扰动后需求为：

```
demand_s(e) = base_demand(e) * demand_multipliers_s.get(e, 1.0)
```

### 1.2 决策变量与约束（所有场景同一口径）

`x[s][e][w]` = 场景 `s` 下实体 `e` 分配到窗口 `w` 的资源量。

1. **容量约束**：每个场景、每个窗口
   `Σ_e x[s][e][w] ≤ capacity(w)`
2. **最小保障**：每个场景、每个实体
   `Σ_w x[s][e][w] ≥ min_share(e)`
3. **需求上限（不超发）**：
   `Σ_w x[s][e][w] ≤ demand_s(e)`
4. **总量守恒**：每个场景恰好把供给分完
   `Σ_e Σ_w x[s][e][w] = total_supply(s)`

基线场景与扰动场景使用完全相同的约束口径，区别仅在总供给与需求乘数。

### 1.3 可行性判据（实体可跨窗口拆分）

每个场景必须同时满足，否则该请求整体判为**不可行**（不返回任何部分结果）：

- 每个实体 `min_share(e) ≤ demand_s(e)`（保底本身可达）；
- 最小保障总量 `Σ min_share(e) ≤ total_supply(s)`；
- `total_supply(s) ≤ Σ demand_s(e)`（不超发才能守恒）；
- 窗口容量总量 `Σ capacity(w) ≥ total_supply(s)`。

不可行原因按场景逐条写入响应，保证可解释。

### 1.4 求解算法（确定性）

1. **实体层 · 水位填充（water-filling）**：先给每个实体发 `min_share`，
   剩余供给在「未触需求上限」的实体间按需求量加权分配，逐批触顶直到发完。
2. **窗口层 · 整数网格装箱**：在量化精度 `quantize_decimals` 下把所有量
   表示为量子 `q = 10^-decimals` 的非负整数倍；行目标对齐量化后供给，
   按列水位填充装箱。结果经过独立的容量/保底/守恒校验后才返回。

确定性排序 + 固定量化规则 ⇒ **相同输入与配置、相同 `solver_version`
结果逐字节一致**（见 `app/solver/engine.py::SOLVER_VERSION`）。

### 1.5 量化与网格可行性

浮点可行不等于选定小数位下的整数网格可行。求解前做网格级预检：
若在当前 `quantize_decimals` 下容量边界被舍入吞掉、保底被击穿等，
直接返回 `INVALID_REQUEST` 并提示提高小数位（而不是中途失败或伪造结果）。

---

## 2. 带已确认额度的重规划

方案下发后，运营上常先确认其中一部分额度（已对外承诺、不可再动），之后
需求或窗口容量变动时只重排未确认部分。`POST /replans` 在一份**已成功**
方案的基础上完成这件事：

- 请求在常规输入之上增加 `base_key`（延续的既有方案）与
  `confirmations[]`（`(entity, window, amount)` 确认格子，必须来自基准
  方案基线场景的既有分配且不超过原分配量）；
- 重规划结果的**所有场景**（基线与扰动同口径）中，确认格子最终值不低于
  确认量——确认部分不会被重新分配、挪走或变少，未确认部分按最新需求、
  窗口容量与总供给重排；容量上限、最小保障、需求上限、总量守恒继续严格
  成立；
- 响应带 `base_key` 血缘字段；重规划指纹绑定基准方案与确认额度，同一
  重规划标识并发/重复提交只真正计算一次，返回同一份等价结果；
- 确认额度与最新输入冲突（窗口装不下、确认量超过实体当前需求上限、
  确认总量超过供给等）时整体失败为 `CONFIRMATION_CONFLICT`，
  `details.reasons` 逐条指明场景/实体/窗口，与 `INVALID_REQUEST`、
  `INFEASIBLE` 严格区分；绝不悄悄改动确认额度来凑出"能用"的结果；
- 成功结果严格达标：保底/确认量按 `ceil` 上网格（零容差不击穿），
  精度不足时明确按 `INVALID_REQUEST`/`INFEASIBLE` 拒绝，不返回表面成功；
- 基准方案（SUCCEEDED）永久固化，重规划不会修改它；查询、取消、超时、
  失败重试语义与普通优化完全一致（共用同一作业存储与接口）。

模型细节见 `docs/model.md` 第 7 节，接口契约见 `docs/api.md`。

---

## 3. 状态机与幂等语义

### 2.1 作业状态

```
            首次/失败后同指纹重试
   (不存在) ───────────────────────► RUNNING
                                     ├──► SUCCEEDED   （结果永久固化）
                                     └──► FAILED       （可同指纹重试）
```

- `RUNNING`：后台守护线程求解中；
- `SUCCEEDED`：终态，结果随幂等键永久保留；
- `FAILED`：终态，记录失败分类与原因；**允许用相同幂等键 + 相同指纹重试**，
  重试会创建全新记录与全新取消事件（旧失败/旧取消信号不污染新执行）。

### 2.2 幂等键规则（`Idempotency-Key` 请求头）

- 指纹 = 对「实体/窗口/场景/量化精度 + `SOLVER_VERSION`」做 canonical-JSON
  后 SHA-256。**运行参数（超时、步进延迟）不参与指纹。**
- 同键 + 同指纹，无论并发还是重复提交：**只启动一次求解**，最终一份等价结果；
  成功后再提交直接返回同一份固化结果（HTTP 200）。
- 同键 + 不同指纹：`409 CONFLICT`，既有作业与结果不受影响（作业处于
  RUNNING/SUCCEEDED/FAILED 任一状态都一样）。
- 并发安全由 `IdempotencyStore` 的单一锁保证：登记与状态迁移在同一临界区。

---

## 4. 异常分类

| code | HTTP | 触发条件 | 可重试 |
| --- | --- | --- | --- |
| `INVALID_REQUEST` | 400 | 结构/取值非法（空实体、重名、非唯一基线、负值、未知实体引用、量化网格不可行等） | 修改输入 |
| `INFEASIBLE` | 422 | 输入合法但优化不可行，`details.reasons` 列出每条原因 | 修改输入 |
| `CONFLICT` | 409 | 同幂等键绑定了不同请求指纹 | 更换键或保持同体 |
| `CONFIRMATION_CONFLICT` | 409 | 重规划中已确认额度与基准方案或最新输入（容量/需求上限/供给）冲突，`details.reasons` 指明场景/实体/窗口 | 调整确认额度或输入 |
| `TIMEOUT` | 504 | 求解超过 `timeout_seconds` | 同键同指纹可重试 |
| `CANCELLED` | 499 | 作业在运行中被主动取消 | 同键同指纹可重试 |
| `SOLVER_FAILURE` | 500 | 求解器内部错误（含结果未通过内部约束校验） | 同键同指纹可重试 |
| `NOT_FOUND` | 404 | 查询/取消不存在的作业 | — |

- **参数非法（400）在登记前同步拒绝**，不产生作业、不占用幂等键；
- **不可行（422）属于合法输入的优化结果**，作业落 FAILED 并附原因；
- **超时与主动取消严格区分**：检查点上若取消信号置位则判 CANCELLED
  （取消优先），否则越过截止时间判 TIMEOUT；
- 取消/失败**绝不写入部分结果**，也不影响后续同指纹重试。

错误体统一形如：

```json
{"error": {"code": "INFEASIBLE", "message": "...", "details": {"reasons": ["..."]}}}
```

---

## 5. 配置项

| 配置 | 位置 | 默认 | 说明 |
| --- | --- | --- | --- |
| `timeout_seconds` | 请求体 | `null`（不限） | 单次求解截止时间（秒） |
| `quantize_decimals` | 请求体 | `9` | 量化小数位（0–12） |
| `solver_step_delay` | 请求体 | `0.0` | 每个检查点人为延迟（秒），用于本地演示/测试超时与取消 |
| `default_timeout_seconds` | `ServiceConfig` | `null` | 服务级默认超时 |
| `max_timeout_seconds` | `ServiceConfig` | `null` | 允许的超时上限，超出会被钳制 |
| `solver_step_delay` | `ServiceConfig` | `0.0` | 服务级默认步进延迟 |

---

## 6. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/optimizations`（头 `Idempotency-Key`） | 提交；首次 202，已完成的重复提交 200 |
| POST | `/replans`（头 `Idempotency-Key`） | 带已确认额度的重规划；语义同上，结果带 `base_key` 血缘 |
| GET | `/optimizations/{key}` | 查询；RUNNING→202，SUCCEEDED→200，FAILED→200 带 error |
| POST | `/optimizations/{key}/cancel` | 请求取消；运行中 202，否则 200 |

详见 `docs/api.md`，交互式文档：服务启动后访问 `/docs`。

---

## 7. 本地验证

```bash
# 安装依赖（首次会创建 .venv）
uv sync

# 运行全部单测（含关键输入、状态迁移、判定依据日志）
uv run pytest -q
uv run pytest -q --log-cli-level=INFO   # 查看详细判定日志

# 启动服务
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

提交示例：

```bash
curl -s -X POST http://127.0.0.1:8000/optimizations \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-1' \
  -d '{
    "entities":[{"name":"a","base_demand":100,"min_share":10},
                {"name":"b","base_demand":50,"min_share":5}],
    "windows":[{"name":"w1","capacity":80},{"name":"w2","capacity":80}],
    "scenarios":[{"name":"base","total_supply":120,"is_baseline":true},
                 {"name":"up","total_supply":100,"demand_multipliers":{"a":1.5}}]
  }'
```

重规划示例（接着上面的 `demo-1`，确认 `a` 在 `w1` 的 30 单位后按新供给重排）：

```bash
curl -s -X POST http://127.0.0.1:8000/replans \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: replan-1' \
  -d '{
    "base_key": "demo-1",
    "confirmations": [{"entity":"a","window":"w1","amount":30}],
    "entities":[{"name":"a","base_demand":100,"min_share":10},
                {"name":"b","base_demand":50,"min_share":5}],
    "windows":[{"name":"w1","capacity":80},{"name":"w2","capacity":80}],
    "scenarios":[{"name":"base","total_supply":100,"is_baseline":true},
                 {"name":"up","total_supply":90,"demand_multipliers":{"a":1.5}}]
  }'
# 查询/取消与 /optimizations 相同：GET /optimizations/replan-1
```

### 测试覆盖

- `tests/test_solver.py`：可行性判定、容量/保底/守恒、超时 vs 取消、可复现；
- `tests/test_idempotency.py`：20 路并发重复提交只求解一次、冲突、失败后重试使用全新取消事件；
- `tests/test_service.py`：超时/取消后的重试不被污染、跨服务结果复现；
- `tests/test_api.py`：端到端状态码分类、幂等、不可行原因、并发提交、跨场景约束一致性、重规划端到端与错误分类；
- `tests/test_replan.py`：确认格子保留与未确认部分重排、确认额度冲突分类、
  基准方案校验、并发重规划只求解一次、低精度严格达标/明确拒绝、取消语义沿用。

更多设计说明见 `docs/model.md`。
