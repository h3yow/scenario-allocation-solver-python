# 本地化多场景资源分配求解器 (scenario-allocation-solver)

一个**进程内、无外部依赖**的多场景资源分配优化服务:同一批需求在
**正常 / 不可行 / 并发重复提交 / 求解超时 / 主动取消** 等情况下,
都能得到一致、可区分、可解释、可复现的结果。

- Web 框架:FastAPI(仅 HTTP 适配,求解器本身是纯 Python)
- Python:>= 3.14,依赖管理:uv
- 求解模型:确定性“水填充(water-filling)”分配 + 严格守恒残差修正

---

## 1. 优化模型

### 1.1 输入

| 概念 | 字段 | 含义 |
| --- | --- | --- |
| 主体 Entity | `id, min_guarantee, capacity` | 可分配对象;分配量必须落在 `[min_guarantee, capacity]` |
| 滚动窗口 Window | `name, total_supply` | 一个调度周期;该周期分配总量必须**恰好**等于供给 |
| 场景 ScenarioInput | `name, demands[window][entity]` | 基线场景或扰动场景下的需求量;**不改变**约束,只改变需求 |
| 问题 Problem | `baseline_name, entities, windows, scenarios` | 一次完整的多场景请求 |

### 1.2 决策变量与约束

对每个场景 s、窗口 w、主体 i,设分配量 x(s,w,i):

1. **容量上界**:`x_i <= capacity_i`
2. **最小保障**:`x_i >= min_guarantee_i`
3. **总量守恒**:`Σ_i x_i = total_supply_w`(严格相等,见 1.4)

### 1.3 目标口径:水填充

在满足上述约束的前提下,分配量尽量贴近需求:

- 先为所有主体保最小保障;
- 供给不足时,在同一“水位” λ 上压缩:
  `x_i = clip(demand_i + λ, min_i, capacity_i)`,
  二分搜索 λ 使 `Σ x_i = supply`;
- 供给过剩时,按容量填满。

该规则对**基线与全部扰动场景使用完全一致的口径**,因此场景之间可横向比较;
每个 `(场景, 窗口)` 独立求解,互不影响。

### 1.4 严格守恒

二分得到浮点解后,把残差 `supply − Σx_i` 量化为 `1e-12` 单位,
按主体 id 字典序逐单位摊派到仍在界内的主体。因此:

- `Σ x_i` 与 `supply` 的误差为 0(机器精确相等,测试容差 1e-9);
- 摊派顺序确定,**结果与输入顺序、线程调度无关,可复现**。

### 1.5 可行性判定(先于求解)

逐 `(场景, 窗口)` 判定可行域:

```
Σ min_guarantee <= total_supply <= Σ capacity
```

- 供给 < 最小保障合计 → `SUPPLY_BELOW_MIN_GUARANTEE`(带缺口)
- 供给 > 容量合计     → `SUPPLY_EXCEEDS_CAPACITY`(带超额)

不可行时**立即终止,不启动求解,绝不返回部分结果或伪造结果**。

### 1.6 可复现性

成功结果 `SolveResult` 携带:

- `payload_hash`:问题+配置规范化 JSON 的 sha256(实体/窗口/场景按主键排序,等价请求同指纹)
- `model_version`:当前 `allocation-waterfill-v1`(算法/口径变更必须升版本)
- `config_digest`:求解配置摘要
- `allocations` / `reports`:分配结果与每窗口守恒、满足率报表

同一输入 + 同一配置 + 同一模型版本 ⇒ 逐位相同的结果。

---

## 2. 状态机与幂等语义

### 2.1 状态

```
                 同载荷重试
 PENDING ──► RUNNING ──► SUCCEEDED        (终态,结果永久不可变)
    │           │
    │           ├──────► FAILED           (终态:INFEASIBLE / SOLVER_FAILED,可同载荷重试)
    │           ├──────► CANCELLED        (终态:主动取消,可同载荷重试)
    │           └──────► TIMEOUT          (终态:超时,可同载荷重试)
```

所有迁移在 `IdempotencyStore` 的锁内以 **CAS(expected → new)** 完成,
并记录到 `transition_log`,日志逐条可见。

### 2.2 提交规则(按 `idempotency_key`)

| 情形 | 行为 |
| --- | --- |
| 键不存在 | 校验 → 建 PENDING → 同步可行性判定 → RUNNING → 后台线程求解 |
| 键存在、**载荷相同**、执行中/成功 | 复用同一记录与结果,**绝不产生第二次求解** |
| 键存在、**载荷不同** | `409 IDEMPOTENCY_CONFLICT`,既有结果不受影响 |
| FAILED / CANCELLED / TIMEOUT + **同载荷** | 记录重置为 PENDING 后重新求解(失败恢复) |
| 上述终态 + **不同载荷** | 仍按冲突处理(键已绑定一份载荷) |
| 参数非法 / 不可行 | 非法请求**不落任何状态**;不可行落 FAILED/INFEASIBLE 后抛出 |

并发重复提交依赖“创建记录”这一原子操作:只有一个线程能创建 PENDING,
其余线程全部复用,从而保证“一次求解、一份等价结果”。

### 2.3 超时与取消如何区分

每次执行持有三个信号:

- `cancel_event`:求解器轮询的唯一中断信号(超时、取消都置位)
- `timed_out`:看门狗在 deadline 到达且任务未完成时置位
- `manual_cancel`:**仅**主动取消置位

工作线程在每个 `(场景, 窗口)` 边界检查信号,被中断后按信号来源落
TIMEOUT 或 CANCELLED。两者是不同终态、不同错误码、不同 HTTP 状态。
看门狗在任务正常结束(`finished`)后不会再误判超时。

---

## 3. 异常分类

| 异常 | error_code | HTTP | 含义 |
| --- | --- | --- | --- |
| `InvalidRequestError` | `INVALID_REQUEST` | 400 | 业务取值/一致性非法(如 min>cap、超时配置<=0、引用不存在的主体) |
| (Pydantic 结构错误) | — | 422 | 字段类型/取值违反 schema(如负数、缺字段) |
| `InfeasibleError` | `INFEASIBLE` | 422 | 可行域为空,`details` 含场景/窗口/原因/缺口或超额 |
| `ConflictError` | `IDEMPOTENCY_CONFLICT` | 409 | 同一幂等键绑定了不同请求体 |
| `SolveTimeoutError` | `SOLVE_TIMEOUT` | 408 | 超过 `solve_timeout` 预算 |
| `SolverCancelledError` | `CANCELLED` | 499 | 主动取消 |
| `SolverFailedError` | `SOLVER_FAILED` | 500 | 求解器内部错误 |
| 未知幂等键 | `NOT_FOUND` | 404 | 查询/取消不存在的键 |

提交即进行同步校验与可行性判定,所以不可行/非法在 POST 时直接返回;
超时/取消发生在异步执行期间,通过 GET 结果观察到对应状态码。

---

## 4. 配置项

`SolverConfig`(可在请求体 `config` 中按请求覆盖):

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `solve_timeout` | `5.0` 秒 | 求解时间预算;>0。超时落 TIMEOUT |
| `tolerance` | `1e-9` | 水位二分收敛容差与界判定容差;>0 |
| `max_iterations` | `10000` | 单窗口水位二分最大迭代数;正整数 |

配置参与 `payload_digest` 与 `config_digest`——不同配置产生不同指纹与摘要。

---

## 5. HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /api/v1/optimizations` | 提交(或复用)优化请求 |
| `GET /api/v1/optimizations/{key}` | 查询状态与结果 |
| `POST /api/v1/optimizations/{key}/cancel` | 主动取消 |

请求示例:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/optimizations \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "demo-1",
    "entities": [
      {"id": "a", "min_guarantee": 1, "capacity": 8},
      {"id": "b", "min_guarantee": 0, "capacity": 6}
    ],
    "windows": [{"name": "w1", "total_supply": 7}],
    "scenarios": [
      {"name": "baseline", "demands": {"w1": {"a": 5, "b": 4}}},
      {"name": "shock",    "demands": {"w1": {"a": 8, "b": 1}}}
    ],
    "config": {"solve_timeout": 5.0, "tolerance": 1e-9}
  }'
```

异步语义:POST 返回当前状态(`PENDING/RUNNING/SUCCEEDED` 或直接抛错);
客户端随后 GET 轮询。本地小问题通常在 POST 返回前就已完成。

---

## 6. 本地验证方法

```bash
# 1) 安装依赖(uv)
uv sync

# 2) 运行全部单测(含关键输入/状态迁移/判定依据日志)
uv run python -m pytest -q

# 只看某类场景
uv run python -m pytest tests/test_service_idempotency.py -q      # 并发重复提交/冲突
uv run python -m pytest tests/test_timeout_cancel.py -q          # 超时/取消/重试
uv run python -m pytest tests/test_scenario_consistency.py -q    # 场景间约束一致性
uv run python -m pytest tests/test_validation.py -q              # 非法输入/不可行

# 3) 起真实服务并用 curl 验证
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

测试覆盖(44 条):

- **并发重复提交**:8 线程同键并发只产生一次 RUNNING、一份结果对象
- **等价请求复用**:重复提交返回同一结果,成功结果不可被重复求解
- **冲突请求**:同键不同载荷 409,既有结果不被覆盖
- **参数非法**:结构层 422、业务层 400、空键拒绝、非法配置拒绝、不落脏状态
- **不可行**:两类不可行原因 + 详情,落 FAILED/INFEASIBLE,无结果
- **超时 vs 取消**:0.3s 预算下的 TIMEOUT(408)与主动 CANCELLED(499)可区分
- **失败恢复**:取消/超时后同载荷重试成功,迁移日志保留恢复轨迹
- **场景一致性**:三场景×三窗口下守恒、界约束、主体/窗口口径齐全
- **可复现**:两个独立服务实例对同一输入给出逐位相同结果
- **可观测**:`sas.*` 日志含关键输入、可行性判定依据与全部状态迁移

---

## 7. 目录结构

```
sas/
  models.py       数据模型 + 规范化载荷/指纹
  validation.py   配置与问题校验(INVALID_REQUEST)
  feasibility.py  可行域判定与解释(INFEASIBLE)
  solver.py       确定性水填充求解器 + 报表
  store.py        线程安全幂等记录 + CAS 状态机
  worker.py       工作线程、看门狗(超时)、取消信号
  service.py      编排:提交/查询/取消/重试
  api.py          FastAPI 路由与异常→HTTP 映射
  errors.py       异常分类
tests/            上述场景的单测/集成测试
main.py           uvicorn 入口
```
