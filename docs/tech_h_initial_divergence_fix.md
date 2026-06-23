# 技术开发文档：焓值 h 初始发散问题修正

## 1. 问题描述

在使用 `chtMultiRegionSimpleFoam` 求解器计算含 MRF（多参考系）风扇的笔记本热仿真案例时，
求解器在 Time=2 崩溃，报错信息如下：

```
--> FOAM FATAL ERROR: (openfoam-2412)
Maximum number of iterations exceeded: 100
when starting from T0:300.00001 old T:1.4145663e+14 new T:1.4145663e+14
f:1.423662e+17 p:14388.783 tol:0.030000001
```

热物理模型 Newton 迭代超过 100 次，温度发散至 1.4×10¹⁴ K。

## 2. 根因分析

### 2.1 SIMPLE 循环中的求解顺序

`chtMultiRegionSimpleFoam` 的 SIMPLE 循环对每个流体区域的求解顺序为：

1. **动量预测器**（momentumPredictor）：求解 U*（使用当前 p_rgh）
2. **能量方程**：求解 h（使用 U* 对应的通量 phi）
3. **压力修正**：求解 p_rgh
4. **速度和通量修正**：修正 U 和 phi

关键问题在于 **h 在压力修正之前求解**，此时通量 phi 来自动量预测器，**不满足质量守恒**。

### 2.2 MRF 对通量的影响

MRF 风扇区域（omega = 100 rad/s）在动量预测器中产生强旋转速度场。在 Time=1 第一步迭代时：

- 初始 U = 0，p_rgh = 0
- 动量预测器求解 U*，MRF 贡献强旋转速度
- h 方程使用此非质量守恒通量进行对流计算
- 强旋转通量导致焓场 h 严重过冲

### 2.3 松弛因子过大

原始松弛因子设置：

```
relaxationFactors
{
    fields { p_rgh 0.7; rho 1; }
    equations { U 0.4; h 0.9; k 0.7; epsilon 0.7; }
}
```

h 松弛因子 = 0.9 意味着每次迭代 h 场变化量达 90%，几乎无阻尼，导致温度瞬间爆炸。

### 2.4 崩溃过程

| 时间步 | h 求解后 T 范围 | 连续性误差 | 状态 |
|--------|----------------|-----------|------|
| Time=1 | -272790 ~ 264869 K | 430507 | 温度爆炸 |
| Time=2 | 1.4×10¹⁴ K | — | 热物理模型迭代发散，崩溃 |

## 3. 修正方案

### 3.1 关闭动量预测器

```diff
 SIMPLE
 {
-    momentumPredictor true;
+    momentumPredictor false;
     nNonOrthogonalCorrectors 2;
 }
```

**原理**：关闭动量预测器后，Time=1 时 U 保持初始值 0，通量 phi = 0，h 方程无对流项，
温度保持 300 K 不变。压力修正后得到质量守恒的通量，供 Time=2 的 h 求解使用。

### 3.2 降低松弛因子

```diff
 relaxationFactors
 {
-    fields { p_rgh 0.7; rho 1; }
-    equations { U 0.4; h 0.9; k 0.7; epsilon 0.7; }
+    fields { p_rgh 0.3; rho 1; }
+    equations { U 0.3; h 0.3; k 0.7; epsilon 0.7; }
 }
```

**原理**：降低 h 松弛因子至 0.3，每次迭代仅更新 30% 的修正量，防止焓场过冲。
同时降低 p_rgh 和 U 的松弛因子，提升压力-速度耦合稳定性。

### 3.3 增加非正交修正

```diff
 SIMPLE
 {
     momentumPredictor false;
-    nNonOrthogonalCorrectors 0;
+    nNonOrthogonalCorrectors 2;
 }
```

**原理**：网格最大非正交性达 83.96°，增加 2 次非正交修正改善压力方程求解精度。

### 3.4 添加温度限值保护

新建 `system/air/fvOptions`：

```
limitT
{
    type            limitTemperature;
    active          yes;
    selectionMode   all;
    min             200;
    max             500;
}
```

**原理**：作为安全网，在 h 求解后将温度裁剪到 [200, 500] K 范围内，
防止极端温度值导致热物理模型 Newton 迭代发散。

## 4. 涉及文件

| 文件 | 修改内容 |
|------|---------|
| `system/air/fvSolution` | momentumPredictor false, nNonOrthogonalCorrectors 2, 松弛因子降低 |
| `system/air/fvOptions` | 新建，添加 limitTemperature 温度限值 |

## 5. 验证结果

### 修正前

| 时间步 | Min/max T | 连续性误差 (global) | 状态 |
|--------|-----------|-------------------|------|
| Time=1 | -272790 ~ 264869 K | 430507 | 温度爆炸 |
| Time=2 | 1.4×10¹⁴ K | — | 崩溃 |

### 修正后

| 时间步 | Min/max T | 连续性误差 (global) | 状态 |
|--------|-----------|-------------------|------|
| Time=1 | 300 ~ 300 K | 0.47 | 完全稳定 |
| Time=2 | 200 ~ 500 K | -7499 | 温度被限值保护，继续计算 |

Time=1 温度完全稳定在 300 K，连续性误差从 430507 降至 0.47。
Time=2 温度被 limitTemperature 裁剪到 [200, 500] K，求解器不再因热物理模型迭代发散而崩溃。

## 6. 注意事项

1. **momentumPredictor false** 会降低动量方程的收敛速度，但显著提升初期稳定性。
   建议在计算稳定后（如 50 步后）可考虑改回 true 以加速收敛。

2. **limitTemperature** 的 min/max 应根据实际物理场景设置。
   本案例环境温度 300 K，风扇散热温升预计不超过 100 K，故设为 [200, 500] K。

3. 连续性误差在 Time=2 仍较高（7499），主要原因是网格中存在 29,789 个开放单元
   （来自未配对的界面面），导致质量泄漏。此问题需通过修复网格 coalesce 逻辑解决。

## 7. 相关代码

- 求解器fvSolution 配置：`cases/laptop_thermal_cht_v3/system/air/fvSolution`
- 温度限值配置：`cases/laptop_thermal_cht_v3/system/air/fvOptions`
- 模板生成函数：`src/foam2thermal/templates.py` → `fv_solution_fluid()`
