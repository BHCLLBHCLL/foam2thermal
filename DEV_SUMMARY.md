# foam2thermal 开发总结

## 概述

foam2thermal 是将 CGNS 网格转换为 OpenFOAM chtMultiRegionSimpleFoam 可计算案例的工具包。
本文档汇总转换过程中的关键技术点，供后续开发与维护参考。

---

## 1. 转换流程

```
CGNS 网格
  │
  ├─ 1. 读取 CGNS → OpenFOAM 单体网格 (cgns2foam 外部工具)
  │
  ├─ 2. 网格合并 (mesh_coalesce.py)
  │     ├─ 识别配对界面面 (stitch / cyclicAMI)
  │     ├─ 合并重合点 (排除 AMI 顶点)
  │     ├─ 消除退化面 (重复顶点恢复)
  │     └─ 输出合并后单体 polyMesh
  │
  ├─ 3. 案例生成 (case_generator.py)
  │     ├─ 生成 system/ (controlDict, fvSchemes, fvSolution)
  │     ├─ 生成 constant/ (thermophysicalProperties, MRFProperties, g)
  │     └─ 生成 0.orig/ (T, U, p, p_rgh 初始场)
  │
  ├─ 4. 区域分裂 (mesh_split.py)
  │     ├─ 按 cellZone 分割单体网格为多区域
  │     ├─ 生成 mappedWall 耦合面
  │     └─ 输出 constant/<region>/polyMesh
  │
  ├─ 5. 场同步 (field_sync.py)
  │     └─ 根据分裂后实际边界 patch 重新生成 0.orig 场文件
  │
  └─ 6. AMI 补丁修复 (fix_cyclic_ami_patches.py)
        └─ 将 AMI 边界类型从 wall 改为 cyclicAMI
```

---

## 2. 网格合并 (mesh_coalesce.py)

### 2.1 点合并算法

使用空间哈希进行点合并，核心函数 `_merge_points`：

- **哈希键**：`round(p / tol)` 取整后打包为 64 位整数键
  `key = x * 2^42 + y * 2^21 + z`（每维 21 位，范围 0~2,097,151）
- **大网格回退**：当坐标范围超过 21 位时，回退为结构化数组键
- **排除集**：AMI 面顶点不参与合并，保留原始坐标

```python
def _merge_points(points, tol, *, exclude=None):
    # exclude 集合中的点保留原始坐标，追加到合并后点数组末尾
    # 其余点按空间哈希分组合并
```

### 2.2 AMI 顶点排除（关键修复）

**问题**：全局点合并会将 AMI 圆柱面顶点与其他网格块的邻近点合并，
导致圆柱面产生凸起，破坏 AMI 匹配精度。

**修复**：收集所有 AMI 面引用的顶点 ID，传入 `_merge_points` 的 `exclude` 参数：

```python
excluded_vert_ids: set[int] = set()
for fi in excluded_faces:  # AMI 面集合
    s, e = int(offsets[fi]), int(offsets[fi + 1])
    excluded_vert_ids.update(int(v) for v in conn[s:e])

points, pt_map = _merge_points(points, point_tol, exclude=excluded_vert_ids)
```

**验证**：AMI 边界 412,644 个面的顶点坐标与原始网格完全一致（0 不匹配）。

### 2.3 退化面恢复

点合并可能导致同一面的多个顶点被合并为同一点（退化面），
产生零面积面，导致求解器 NaN 残差。

**修复**：保存所有面的原始顶点，合并后检测退化面并恢复原始顶点：

```python
all_face_orig_verts = [conn[s:e].copy() for fi in range(n_faces)]
# 合并后检测：len(current_verts) != len(set(current_verts))
# 恢复：重新添加原始点坐标
```

### 2.4 界面面配对

- **stitch 界面**（fluid-solid, fluid-fluid）：按顶点集合签名配对，合并为内部面
- **cyclicAMI 界面**：不参与配对，保留为边界面，后续由 `fix_cyclic_ami_patches.py` 设置类型
- **配对方法**：面顶点排序后取元组作为签名，哈希查找配对面

---

## 3. 区域分裂 (mesh_split.py)

### 3.1 分裂逻辑

按 `cellToRegion` 标签将单体网格分裂为多区域网格：

1. 遍历所有面，根据 owner/neighbour 的区域归属分类：
   - 两端同区域 → 内部面
   - 一端为本区域 → 边界面（保留原 patch 名）
   - 跨区域 → 耦合面（生成 `mappedWall` 类型）
2. 重新编号点、面、单元
3. 输出各区域 `constant/<region>/polyMesh/`

### 3.2 耦合面命名

跨区域面自动命名为 `{region}_to_{neighbor}`，例如 `air_to_CPU`。
对应的 `mappedWall` 补丁配置：

```
air_to_CPU
{
    type            mappedWall;
    sampleMode      nearestPatchFace;
    sampleRegion    CPU;
    samplePatch     CPU_to_air;
}
```

---

## 4. 边界条件配置

### 4.1 p_rgh 边界条件（关键修复）

**问题**：`open` 补丁使用 `fixedFluxPressure` 时，`adjustPhi` 不将其视为可调节出口，
导致 "Continuity error cannot be removed by adjusting the outflow" 错误。

**修复**：将 `open` 补丁的 p_rgh 改为 `fixedValue`：

```json
"p_rgh": {
    "open": { "type": "fixedValue", "value": "uniform 0" }
}
```

`fixedValue` 使 `pPatch.fixesValue()` 返回 true，被 `adjustPhi` 识别为可调节出口。

### 4.2 场同步 (field_sync.py)

**问题**：`field_sync.py` 重新生成 0.orig 场时未传递 `bc_cfg` 参数，
导致配置文件中的 p_rgh 边界条件覆盖被忽略。

**修复**：

```python
(odir / "p_rgh").write_text(
    field_p_rgh(patches, 0, bc_cfg=rbc.get("p_rgh", {}), ami_patterns=ami_pats),
    ...
)
```

### 4.3 AMI 补丁自动识别

通过 `ami_patterns` 正则匹配（默认 `ami_rot\d+`）自动识别 AMI 补丁，
生成 `cyclicAMI` 边界条件：

```python
def is_ami_patch(name, patterns):
    return any(re.search(p, name) for p in patterns)
```

---

## 5. 焓值 h 初始发散修正（关键修复）

### 5.1 问题

`chtMultiRegionSimpleFoam` 在 Time=2 崩溃，温度发散至 1.4×10¹⁴ K。

### 5.2 根因

SIMPLE 循环中 h 方程在压力修正前求解，使用 MRF 强旋转产生的非质量守恒通量。
h 松弛因子 0.9 过高，Time=1 温度爆炸至 ±27 万 K。

### 5.3 修正

| 修改项 | 修正前 | 修正后 | 作用 |
|--------|--------|--------|------|
| momentumPredictor | true | false | Time=1 时 U=0，h 无对流 |
| h 松弛因子 | 0.9 | 0.3 | 防止焓场过冲 |
| p_rgh 松弛因子 | 0.7 | 0.3 | 提升压力-速度耦合稳定性 |
| U 松弛因子 | 0.4 | 0.3 | 降低动量过冲 |
| nNonOrthogonalCorrectors | 0 | 2 | 改善高非正交网格压力求解 |
| limitTemperature | 无 | [200, 500] K | 温度安全网 |

### 5.4 验证

| 时间步 | 修正前 T | 修正后 T | 修正前连续性误差 | 修正后连续性误差 |
|--------|---------|---------|----------------|----------------|
| Time=1 | -272790 ~ 264869 K | 300 ~ 300 K | 430507 | 0.47 |
| Time=2 | 1.4×10¹⁴ K（崩溃） | 200 ~ 500 K | — | -7499 |

---

## 6. MRF 配置

### 6.1 MRFProperties

```
MRF1
{
    cellZone            FPHPARTS.rotation1;
    active              yes;
    nonRotatingPatches  ( ami_rot1 ami_rot1_1 ami_rot2 ami_rot2_1 open );
    origin              (-0.0678 -0.00298 0.08098);
    axis                (0 0 1);
    omega               100.0;
}
```

- `nonRotatingPatches` 必须包含所有 AMI 面和开放边界
- `axis` 必须与风扇旋转轴一致（本案例为 z 轴）

### 6.2 Allrun.pre 复制问题（Windows）

**问题**：MSYS2 bash 中 `cp -f constant.orig/"${region}"/*` 无法正确覆盖文件。

**临时修复**：手动用 PowerShell `Copy-Item -Force` 复制。
**待修复**：Allrun.pre 的复制逻辑需改为 Windows 兼容方式。

---

## 7. 二进制网格格式

### 7.1 读写函数

| 函数 | 用途 |
|------|------|
| `_read_binary_vector_field` | 读取 points（binary vectorField） |
| `_read_binary_label_list` | 读取 owner/neighbour（binary labelList） |
| `_read_faces` | 读取 faces（binary faceCompactList） |
| `_write_binary_vector_field` | 写入 points |
| `_write_binary_label_list` | 写入 owner/neighbour |
| `_write_binary_compact_face_list` | 写入 faces |

### 7.2 cellZones 解析

使用二进制字节搜索解析 cellZones 文件，避免正则匹配大文件性能问题：

```python
def parse_cell_zones(path):
    raw = path.read_bytes()
    # 二进制搜索 zone 名称和 cell 标签
```

---

## 8. Windows OpenFOAM 环境

### 8.1 环境变量设置

```powershell
$env:WM_PROJECT_DIR = "C:\OF\v2412\msys64\home\ofuser\OpenFOAM\OpenFOAM-v2412"
$env:FOAM_ETC = "$env:WM_PROJECT_DIR\etc"
$env:PATH = "C:\OF\v2412\msys64\mingw64\bin;C:\OF\v2412\msys64\home\ofuser\OpenFOAM\OpenFOAM-v2412\platforms\win64MingwDPInt32Opt\bin;" + $env:PATH
$env:LD_LIBRARY_PATH = "C:\OF\v2412\msys64\home\ofuser\OpenFOAM\OpenFOAM-v2412\platforms\win64MingwDPInt32Opt\lib"
$env:HOME = "C:\OF\v2412\msys64\home\ofuser"
```

### 8.2 求解器执行

```powershell
chtMultiRegionSimpleFoam.exe > log.chtMultiRegionSimpleFoam 2>&1
```

---

## 9. 关键文件索引

| 文件 | 职责 |
|------|------|
| `src/foam2thermal/mesh_coalesce.py` | 网格合并、点合并、退化面恢复 |
| `src/foam2thermal/mesh_split.py` | 区域分裂、耦合面生成 |
| `src/foam2thermal/mesh.py` | 网格读写、boundary/cellZones 解析 |
| `src/foam2thermal/case_generator.py` | 案例生成主流程 |
| `src/foam2thermal/templates.py` | OpenFOAM 字典模板 |
| `src/foam2thermal/field_sync.py` | 分裂后场文件同步 |
| `src/foam2thermal/config.py` | 配置加载与校验 |
| `src/foam2thermal/interfaces.py` | AMI 补丁识别 |
| `configs/laptop_thermal_steady_v3.json` | 案例配置 |

---

## 10. 已知遗留问题

1. **网格开放单元**：29,789 个开放单元（未配对界面面导致），产生质量泄漏，
   连续性误差在 Time=2 仍较高（7499）。需修复 coalesce 界面配对逻辑。

2. **Allrun.pre 复制失败**：MSYS2 bash 中 `cp -f` 无法覆盖文件，
   需改为 Windows 兼容的复制方式。

3. **momentumPredictor false**：降低初期动量收敛速度，
   建议计算稳定后（约 50 步）改回 true 加速收敛。

4. **高非正交网格**：最大非正交性 83.96°，1,391 个严重非正交面，
   需考虑网格优化或增加非正交修正次数。
