# 设计说明：多场景资源分配模型

## 1. 问题定义

本地化（无外部求解器）为多个**场景**分配同一批**需求实体**到若干
**滚动窗口**。资源可在窗口间按实体拆分。

- 实体集 `E`：`base_demand_e`、绝对最小保障 `min_share_e`；
- 窗口集 `W`（有序）：容量 `capacity_w`；
- 场景集 `S`：总供给 `supply_s`、需求乘数 `mult_{s,e}`，其中恰有一个
  `is_baseline=true` 的基线场景。

场景 `s` 下需求：`d_{s,e} = base_demand_e * mult_{s,e}`（缺省乘数 1）。
决策变量：`x_{s,e,w} ≥ 0`。

## 2. 约束（每个场景独立且同口径成立）

```
(C1 容量)     Σ_e x_{s,e,w}            ≤ capacity_w                 ∀ w
(C2 保底)     Σ_w x_{s,e,w}            ≥ min_share_e                ∀ e
(C3 需求上限) Σ_w x_{s,e,w}            ≤ d_{s,e}                    ∀ e
(C4 守恒)     Σ_e Σ_w x_{s,e,w}        = supply_s
```

基线与扰动场景唯一的区别是 `supply_s` 与 `mult`，约束形式完全一致，
因此返回的是“一致口径”的多场景结果。

## 3. 可行性充要条件

因为实体可跨窗口拆分，C1–C4 有解当且仅当：

1. `min_share_e ≤ d_{s,e}` 对所有实体（保底落在需求可达域内）；
2. `Σ_e min_share_e ≤ supply_s`（保底总量不超供给）；
3. `supply_s ≤ Σ_e d_{s,e}`（守恒且不超发）；
4. `supply_s ≤ Σ_w capacity_w`（窗口总容量足以承接供给）。

判定见 `app/solver/validate.py::check_feasibility`，按场景逐条给出中文原因。
不满足时引擎在做任何分配之前抛出 `InfeasibleError`，因此**不存在半成品**。

## 4. 确定性两步算法

### 4.1 实体层水位填充

- 初始化 `a_e = min_share_e`，剩余 `R = supply − Σ a_e`；
- 活动集 `A = { e | a_e < d_{s,e} }`；
- 在活动集上以需求为权重重分配：本轮步长
  `step = R / Σ_{e∈A} d_{s,e}`，令最先触及需求上限的实体恰好到顶，
  其余实体同比例增长；从活动集移除到顶实体并重复，直到 `R=0`。

实现见 `app/solver/engine.py::_allocate_entities`。

### 4.2 窗口层整数网格装箱

为了让守恒/容量在输出上**精确**（而非仅在浮点容差内）成立：

- 取量子 `q = 10^{-quantize_decimals}`，全部用整数单位计算；
- 列容量 `U_w = ⌊capacity_w / q⌋`，行保底 `L_e = ⌈min_share_e / q⌉`，
  行上限 `H_e = ⌊d_{s,e} / q⌋`，供给单位 `T = round(supply / q)`；
- 浮点行目标先四舍五入为整数，再把与 `T` 的差额在 `[L_e, H_e]` 内
  按“余量最大优先”逐单位摊派，保证行合计恰好等于 `T`；
- 装箱按**列水位填充**：逐个窗口把各实体的剩余量填入，直到列满。
  由于 `T ≤ Σ U_w`（网格可行性预检保证），必然装完。

### 4.3 网格可行性预检

`_check_grid_feasibility` 在整数单位上复核第 3 节的条件。若当前精度下
（例如 `decimals=0`）容量 floor 后装不下四舍五入的供给，直接以
`INVALID_REQUEST` 拒绝并建议提高 `quantize_decimals`，而不是返回近似、
截断或伪造结果。

### 4.4 独立结果校验

`_verify` 对最终 `ScenarioResult` 独立复核 C1–C4 及实体侧/窗口侧合计一致。
任何不通过都会升级为 `SOLVER_FAILURE`（结果绝不返回给调用方）。

## 5. 取消 / 超时检查点

`_ControlGate.check(phase)` 在以下位置被调用：可行性后、每个场景开始、
实体分配前、窗口装箱前。判定优先级：

1. 取消事件被置位 → `CancelledError`；
2. `monotonic() > deadline` → `TimeoutError`；
3. 否则按 `solver_step_delay` 睡眠（仅用于本地演示/测试）。

## 6. 可复现性

- 结果只依赖：实体/窗口/场景输入、`quantize_decimals`、`SOLVER_VERSION`；
- 不依赖时间、线程调度、随机数、字典迭代顺序（全部按输入顺序处理）；
- 指纹包含 `SOLVER_VERSION`：版本变化时指纹变化，从而能识别“版本差异”
  导致的结果差异。
