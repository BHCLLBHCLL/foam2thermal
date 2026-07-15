# foam2thermal 开发总结

## 概述

foam2thermal 是将 CGNS 网格转换为 OpenFOAM chtMultiRegionSimpleFoam 可计算案例的工具包。
本文档汇总转换过程中的关键技术点、**功能完整度**、**转换效率**与**已知问题分析（§13）**，供后续开发与维护参考。

CLI 工作流：`check → scan → build → prep (Allrun.pre) → solve`

```
CGNS ──(cgns2foam，外部)──► 单体网格 ──(foam2thermal)──► 多区域 CHT 算例 ──► 求解
                              ✅ 主流程已打通
                              ✅ v0.5 BCs_fix / v3：Windows 8 核可达 Time=100
                              ✅ 默认不做 coalesce/stitch（界面保留为 patch）
```

推荐配置：`configs/laptop_thermal_steady_v3_BCs_fix.json`（详见 `docs/tech_bcs_fix_interfaces.md`）。

---

## 1. 转换流程

```
CGNS 网格
  │
  ├─ 1. 读取 CGNS → OpenFOAM 单体网格 (cgns2foam 外部工具)
  │
  ├─ 2. 可选网格合并 (mesh_coalesce.py)  【v3/BCs_fix 默认关闭】
  │     ├─ 识别配对界面面并合并为内部面
  │     ├─ 合并重合点 (排除 AMI 顶点)
  │     ├─ 几何兜底配对 (coalesce_geometric_fallback)
  │     └─ 输出合并后单体 polyMesh
  │
  ├─ 3. 界面扫描 (interfaces.py) + 案例生成 (case_generator.py)
  │     ├─ 等面数配对 + 后缀链兜底 → cyclicAMI / mappedWall
  │     ├─ 将 patch_regions 与扫描界面写入输出 config.json
  │     ├─ 生成 system/ / constant.orig/ / 0.orig/
  │     └─ impeller → movingWallVelocity；AMI → cyclicAMI 场 BC
  │
  ├─ 4. 区域分裂 (mesh_split.py)
  │     ├─ 按 cellToRegion / cellZone 分割单体网格为多区域
  │     ├─ 按扫描配对生成 mappedWall（跳过 cyclicAMI / rotation）
  │     ├─ 写入各 region 的 cellZones（MRF 所需）
  │     └─ 输出 constant/<region>/polyMesh（faces 保持 binary faceCompactList）
  │
  ├─ 5. 场同步 (field_sync.py)
  │     └─ 按分裂后实际 boundary 重写 0.orig（AMI 精确名并入模式）
  │
  └─ 6. AMI 补丁修复 (fix_cyclic_ami_patches.py)
        └─ 按 config explicit / ami_patterns 将 wall 升级为 cyclicAMI
```

---

## 2. 网格合并 (mesh_coalesce.py) — 可选

> **v3 / BCs_fix 默认关闭**（`mesh_prep.coalesce_interfaces=false`）。界面保留为边界 patch，由 split 生成 mappedWall。以下算法在显式开启 coalesce 时仍适用。

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

- **stitch 界面**（fluid-fluid, fluid-solid）：按顶点集合签名配对，合并为内部面
- **cyclicAMI 界面**：不参与配对，保留为边界面，后续由 `fix_cyclic_ami_patches.py` 设置类型
- **配对方法**：面顶点排序后取元组作为签名，哈希查找配对面

### 2.5 faceCompactList 偏移表修复

cgns2foam 导出的 `faceCompactList` 可能存在**非单调 offset**（如 face 288417 附近步长 -8188），
导致 coalesce 后 OpenFOAM 报 `bad size -8188`。

**修复**：`_repair_compact_offsets()` 读取时自动修复；写入始终为 binary `faceCompactList`。

### 2.6 界面扫描与 patch→region 推断（`interfaces.py` / `mesh.py`）

cgns2foam 可能导出两种界面命名：

| 风格 | 示例 | 配对策略 |
|------|------|----------|
| 后缀链 | `case2_s` / `case2_s_1` / … | 同基名连续后缀（兜底） |
| BCs_fix / PartSurface | `_PartSurface_Cu_block` ↔ `_PartSurface_air_domain_3` | **等面数 + 名称↔region token 打分（优先）** |

`scan` 与 `build` 共用以下逻辑（`build` 另将 cellZone 名映射为 JSON 短 region 名，并把结果写入输出 `config.json`）。

#### 2.6.1 读取 `constant/regionProperties`

输入网格若含 `constant/regionProperties`（cgns2foam 新增），直接解析 fluid/solid 区域列表：

```
regions
(
    fluid  ( laptop_3d_geom.air.air_domain FPHPARTS.rotation1 FPHPARTS.rotation2 )
    solid  ( laptop_3d_geom.fan1.case1 ... solid_region.fin_2 )
);
```

`parse_region_properties()` / `load_region_properties()` 提供 `region → type` 映射，用于界面分类。

#### 2.6.2 拓扑推断 patch→region

`infer_patch_regions_from_topology()` 读取 `owner` + `cellZones`：

1. 建立 cell → cellZone 索引
2. 对每个 patch，取其面的 owner cell 所属 zone 的**众数**作为 patch 归属 region
3. 优先级：**拓扑 > JSON `patch_regions`（仅补缺）> 名称启发式**

#### 2.6.3 两级界面配对（v0.5）

`scan_cgns2foam_interfaces()`：

1. **`scan_face_count_interfaces`**（有 `patch_region` 时优先）  
   相同 `nFaces`、不同 region；`_pair_name_score` 选最优对；跳过 `impeller*`。
2. **`scan_suffix_interfaces`**（兜底）  
   `foo`↔`foo_1`↔…；跳过同 region；面数比须 ≤ `suffix_face_ratio_max`（默认 1.15）；已 claimed 的 patch 不再加入。

#### 2.6.4 界面分类与方法

`classify_interface()` 输出 `kind` + `method`。AMI 命中条件：

- `ami_patterns`（默认 `ami_rot\d+`、`.*[Rr]otation\d*`）
- 或 patch / 归属 region 名含 `rotation`

| kind | method | 后续处理 |
|------|--------|----------|
| `fluid_fluid`（AMI） | `cyclicAMI` | `createPatch` + `fix_cyclic_ami_patches.py` |
| `fluid_fluid`（非 AMI，跨区） | `mappedWall` | split 生成 `*_to_*` |
| `fluid_solid` | `mappedWall` | split 生成 `*_to_*` |
| `solid_solid` | `mappedWall` | split 生成 `*_to_*` |

`scan` 报告写入 `interface_scan.json`。`build` 将扫描结果合并进输出 `config.json` 的 `interfaces.explicit`（用户显式优先）。

专题说明：`docs/tech_bcs_fix_interfaces.md`。

---

## 3. 区域分裂 (mesh_split.py)

### 3.1 分裂逻辑

按 `cellToRegion` 标签将单体网格分裂为多区域网格：

1. 遍历所有面，根据 owner/neighbour 的区域归属分类：
   - 两端同区域 → 内部面
   - 一端为本区域 → 边界面（保留原 patch 名）
   - 跨区域且在扫描配对中 → 耦合面（生成 `mappedWall`）
2. **跳过** `interfaces.explicit` 中 `cyclicAMI` 对，以及名含 `rotation` 的 patch（留给 createPatch）
3. 重新编号点、面、单元
4. 从单体 cellZones 映射并写入各 region 的 `cellZones`（供 MRF `cellZone` 引用）
5. 输出各区域 `constant/<region>/polyMesh/`

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

通过 `ami_patterns` 正则匹配（默认 `ami_rot\d+`、`.*[Rr]otation\d*`）自动识别 AMI 补丁。
`field_sync._effective_ami_patterns` 另将 `interfaces.explicit` 中 cyclicAMI 双方的**精确名**并入模式，
确保 BCs_fix 中 `_PartSurface_air_domain_7` 等不含 `rotation` 的一侧也写 `cyclicAMI` BC。

`fix_cyclic_ami_patches.py` 按同一套 explicit / patterns 把 wall 升级为 cyclicAMI（含 `transform rotational` 与 `ami_rotation_axis`）。

### 4.4 叶轮壁面 `movingWallVelocity`（关键修复）

**问题**：MRF 下叶轮 patch 使用 `noSlip`（绝对 U=0）与 MRF 源项对抗，叶片附近出现虚假数百 m/s 射流。

**修复**：`templates.field_U` 对名含 `impeller` 的 patch 默认写：

```
type            movingWallVelocity;
value           uniform (0 0 0);
```

壁面绝对速度由 MRF 按 ω×r 给出。配置中应将叶轮列入 `interfaces.exclude`，避免被当成耦合界面。

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

### 6.1 MRFProperties（OpenFOAM v2412 格式）

每个旋转 cellZone 单独一个 MRF 子字典，使用 `cellZone`（单数，非 `cellZones`）：

```
MRF1
{
    cellZone            FPHPARTS.rotation1;
    active              yes;
    nonRotatingPatches  ( ami_rot1 ami_rot1_1 ami_rot2 ami_rot2_1 open open_1 );
    origin              (-67.8 -2.999 80.986);
    axis                (0 1 0);
    omega               100.0;
}

MRF2
{
    cellZone            FPHPARTS.rotation2;
    ...
    axis                (0 -1 0);
```

- `nonRotatingPatches` 必须包含所有 AMI 面和开放边界
- 默认转轴：`rotation1` → `(0 1 0)`，`rotation2` → `(0 -1 0)`；其他 cellZone 回退 `(0 0 1)`
- `origin` 默认按各 rotation cellZone 几何中心分别计算

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
| `src/foam2thermal/mesh_coalesce.py` | 可选网格合并、点合并、几何兜底配对 |
| `src/foam2thermal/mesh_split.py` | 区域分裂、mappedWall；跳过 AMI/rotation |
| `src/foam2thermal/mesh.py` | 网格读写、boundary/cellZones、`regionProperties`、拓扑 patch→region |
| `src/foam2thermal/case_generator.py` | 案例生成；扫描结果写入 `config.json` |
| `src/foam2thermal/templates.py` | OpenFOAM 字典模板；impeller → `movingWallVelocity` |
| `src/foam2thermal/field_sync.py` | 分裂后场同步；effective AMI 精确名模式 |
| `src/foam2thermal/config.py` | 配置加载与校验 |
| `src/foam2thermal/interfaces.py` | 等面数/后缀扫描、AMI 识别、流/固分类 |
| `src/foam2thermal/runner.py` | MSYS2 调用 OpenFOAM（prep / solve） |
| `scripts/fix_cyclic_ami_patches.py` | 按配置升级 cyclicAMI |
| `configs/laptop_thermal_steady_v3_BCs_fix.json` | **推荐** BCs_fix 配置（无 coalesce/stitch） |
| `configs/laptop_thermal_steady_v3.json` | v3 经典后缀命名配置 |
| `docs/tech_bcs_fix_interfaces.md` | BCs_fix 界面与叶轮专题 |
| `docs/tech_h_initial_divergence_fix.md` | 焓值 h 初始发散修正 |

---

## 10. 功能完整度评估

**综合完成度：约 85%**（从 cgns2foam 网格到可 prep/solve 的多区域 CHT 算例；v0.5 BCs_fix 已验证）

### 10.1 已实现（可交付）

| 模块 | 状态 | 说明 |
|------|------|------|
| CLI 工作流 | ✅ | `check` / `scan` / `build` / `run`（prep、solve 分步） |
| JSON 驱动配置 | ✅ | 区域、材料、BC、数值、MRF、AMI、`mesh_prep` |
| 界面扫描与分类 | ✅ | 等面数配对（BCs_fix）+ 后缀链兜底；`regionProperties` + 拓扑；AMI/`rotation` → `cyclicAMI`，其余 → `mappedWall` |
| 配置落盘 | ✅ | `build` 写回 `patch_regions` 与扫描 `interfaces.explicit` |
| 网格 coalesce | ✅ 可选 | 默认关（v3/BCs_fix）；开启时排除 AMI；几何兜底配对 |
| 区域 split | ✅ | Python 替代 `splitMeshRegions`；跳过 AMI/rotation |
| 多区域 CHT 模板 | ✅ | thermo、fvSchemes/Solution、`0.orig`、`regionProperties`、`radiationProperties` |
| cyclicAMI + MRF | ✅ | 扩展 `ami_patterns`；精确名场 BC；多 MRF zone |
| 叶轮壁面 | ✅ | `impeller*` → `movingWallVelocity` |
| mappedWall 耦合 | ✅ | 自动生成 `*_to_*` 及耦合 T BC |
| 场同步 | ✅ | split 后按实际 boundary 重写 `0.orig` |
| 文档 | ✅ | `README.md` + 本文档 + `docs/` 专题 |

### 10.2 部分实现 / 有局限

| 模块 | 缺口 |
|------|------|
| **稳态求解** | v0.5 BCs_fix 与 v3 已在 Windows 8 核验证 Time=100；早期 v1 仍可能 MinGW exit 3 |
| **界面合并** | 推荐路径不 coalesce/stitch；旧网格仍可开 `coalesce_interfaces` + 几何兜底 |
| **湍流** | 默认 laminar；RAS 时自动生成 `k`/`epsilon`/`nut`/`alphat` |
| **辐射** | 各区 `radiationProperties`（默认 `none`） |
| **瞬态 CHT** | 仅 `chtMultiRegionSimpleFoam` |
| **CGNS 直读** | 依赖外部 cgns2foam |
| **自动化测试** | 无单元/回归测试；靠 log 与手工验证 |
| **并行** | build 无并行；solve 支持 `nProcs` / `--parallel` |
| **Windows prep** | `Allrun.pre` 中 `cp -f` 偶发无法覆盖；`createPatch`/`renumberMesh` exit 3 已设非致命 |

### 10.3 相对最初需求的覆盖

| 需求 | 覆盖 |
|------|------|
| JSON 指定流体/固体域 | ✅ |
| 扫描/指定流-流、流-固、固-固界面 | ✅ 等面数 + 拓扑 + split `mappedWall` / AMI `cyclicAMI` |
| OpenFOAM chtMultiRegionSimpleFoam 案例 | ✅ build + prep |
| 散热物性、数值格式、启动脚本 | ✅ 模板 + Allrun |
| 完整可运行仿真 | ✅ BCs_fix / v3：Windows 8 核 Time=100 |

### 10.4 后续优先级建议

详见 **§13 转换代码问题分析**。摘要：

1. **正确性**：统一 scan/build 区域类型源；配对失败与耦合面丢弃显式告警
2. **物理 BC**：叶轮 `movingWallVelocity` 配置化；AMI 轴与 MRF 轴校验
3. **工程**：对齐 `coalesce_interfaces` 默认值；补最小回归测试
4. **平台**：修复 Allrun.pre 在 Windows 下的文件复制；长期 solve 建议 Linux/WSL

---

## 11. 转换效率评估

### 11.1 v0.5 BCs_fix 基准（2026-07，Windows + Python 3 + MSYS2 OF v2412）

案例：`tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix` → `cases/laptop_thermal_cht_v3_BCs_fix`  
配置：`configs/laptop_thermal_steady_v3_BCs_fix.json`（`coalesce_interfaces=false`）

| 阶段 | 结果 |
|------|------|
| **scan / build** | ~22 界面（2× cyclicAMI + 20× mappedWall）；`mesh_coalesce.paired_faces=0` |
| **prep** | 8 区域 split + AMI/mappedWall 修复 + 场同步 |
| **solve** | 8 核并行达 Time=100；reconstructPar 完成 |

### 11.2 v3 基准（历史，2026-07）

案例：`tests/laptop_thermal_steady_scaled_v3_orig` → `cases/laptop_thermal_cht_v3`（8 区域）

| 阶段 | 耗时（约） | 结果 |
|------|-----------|------|
| **scan** | ~7 s | 界面扫描 → `interface_scan.json` |
| **build** | ~33 s | 界面写入 `setup_report.json`（无 coalesce 时更快） |
| **prep** | ~3.2 min | split 8 区域 + AMI/mappedWall + 场同步 |
| **solve** | ~82 min | 8 核并行达 Time=100 |

### 11.3 v1 基准（历史参考）

案例：`tests/laptop_thermal_steady_orig_fix_ansa` → `cases/laptop_thermal_steady_cht`（7 区域，含 coalesce）

| 阶段 | 耗时（约） | 主要操作 |
|------|-----------|----------|
| **build** | ~170 s | coalesce 203,968 对面；227 万→207 万面；125 万→73 万点 |
| **prep** | ~125 s | checkMesh + Python split + createPatch + renumberMesh + 场同步 |
| **solve** | ~35 s 即停 | 初始化与 MRF/AMI 通过；`Time=1` 动量 1 步后 MinGW exit 3 |

### 11.4 流水线与瓶颈

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│ 读全网格     │ ──► │ 界面扫描(轻量)    │ ──► │ 写案例文件   │  ← BCs_fix build 主路径
│ numpy 全量   │     │ 等面数 / 后缀     │     │             │
└─────────────┘     └──────────────────┘     └─────────────┘
         │（仅当 coalesce_interfaces=true）
         ▼
┌──────────────────┐
│ coalesce 面配对   │  ← 旧路径 build 主耗时 (~80%)
│ O(n_faces) Python │
└──────────────────┘
```

| 瓶颈 | 影响 | 改进方向 |
|------|------|----------|
| coalesce 面配对（旧路径） | build 占绝大部分时间 | 保持默认关闭；或分块/Numba |
| 多次全量加载 | 内存数 GB 级 | coalesce + split 合并 Pass |
| zone 几何查询 | 单次 centroid 可达 ~70 s | 缓存、按需计算 |
| OpenFOAM 工具 | renumberMesh ~4 s | 可接受 |

### 11.5 效率优点

- **binary 全程**：`faceCompactList` / `labelList` / `vectorField`
- **Python split**：规避 Windows 上 `splitMeshRegions` 崩溃
- **默认跳过 coalesce/stitch**：BCs_fix / v3 build 显著快于 v1
- **prep 幂等**：已 split 案例跳过单体网格步骤

### 11.6 效率评级（laptop 级）

| 维度 | 评分 | 说明 |
|------|------|------|
| I/O 格式 | ★★★★☆ | binary compact |
| build 速度 | ★★★★☆ | 无 coalesce 时数十秒级 |
| prep 速度 | ★★★☆☆ | split 为主 |
| 内存 | ★★☆☆☆ | 全网格常驻 |
| 可扩展性 | ★★☆☆☆ | build 无并行 |
| 端到端成功率 | ★★★★☆ | BCs_fix / v3：8 核 Time=100 已验证 |

---

## 12. 已知遗留问题

### 已修复（历史问题，重建案例后生效）

| 问题 | 处理 |
|------|------|
| `bad size -8188`（faceCompactList） | `_repair_compact_offsets()` + binary 写回 |
| `pRefCell` 缺失导致求解器 FATAL | `fvSolution` 增加 `pRefCell`/`pRefValue`/`rhoMin`/`rhoMax` |
| MRF `cellZones` 格式错误 | 改为每 zone 一条 `cellZone`（MRF1/MRF2） |
| split 后 cellZones 为空 | `mesh_split` 写入 region cellZones |
| `useImplicit true` + MinGW | 耦合 T BC 改为 `useImplicit false` |
| BCs_fix 界面 stem 不同无法配对 | 等面数 + 名称↔region token 打分（v0.5） |
| AMI 仅识别 `ami_rot*` | 扩展 `rotation*` / `_looks_like_rotation` + explicit 精确名 |
| 叶轮 `noSlip` 虚假射流 | `impeller*` → `movingWallVelocity` |

### 仍待解决

1. **网格开放单元（旧 coalesce 路径）**：未配对界面面会产生质量泄漏。几何兜底（`coalesce_geometric_fallback`）可补配；BCs_fix 默认不 coalesce，依赖 mappedWall 耦合而非合并内部面。

2. **Windows 求解器 exit 3**（v1 历史）：v3 / BCs_fix 配置已在 8 核跑通 Time=100。更大网格仍建议 **WSL2/Linux**。

3. **Allrun.pre 复制失败**：MSYS2 bash 中 `cp -f constant.orig/*` 偶发无法覆盖。

4. **momentumPredictor / 松弛**：BCs_fix 用 `momentumPredictor false` 与较低松弛（0.2）保初期稳定；稳定后可酌情提高。

5. **高非正交网格**：最大非正交性可达 ~84°；可增大 `nNonOrthogonalCorrectors`。

6. **等面数歧义**：多个 patch 同 `nFaces` 时依赖名称打分；极端命名仍可能需 `interfaces.explicit` 手工指定。

更完整的严重度分级与问题地图见 **§13**。

---

## 13. 转换代码问题分析（整体评估）

> 评估基准：2026-07，面向 laptop CHT（v3 / BCs_fix）已端到端打通，但正确性仍依赖启发式与配置约定。换命名、跳过 prep 步骤或误用 `regionProperties` 类型时，容易出现 *silent wrong*。

### 13.1 总判

| 维度 | 评价 |
|------|------|
| laptop 网格族可用性 | ✅ scan → build → prep → solve（8 核 Time=100）可复现 |
| 通用性 | ⚠️ 强绑定 `case1`/`ami`/`impeller`/`_PartSurface_*` 等命名 |
| 正确性护栏 | ❌ 无自动化测试；配对/丢面/BC 错误常静默 |
| 文档与代码一致性 | ⚠️ 部分滞后（见 §13.5） |

### 13.2 问题地图

```
CGNS ──► scan 配对/分类 ──► build 模板+config ──► prep(split/AMI/sync) ──► solve
              │                    │                      │                  │
              ├ RP vs JSON 类型    ├ coalesce 默认 True   ├ remote 名不匹配   ├ 数值分叉
              ├ 等面数/后缀启发式  ├ neighbors 空         ├ 叶轮/AMI BC       └ 发散/喷流
              └ kind 误判          └ 0.orig 缺 *_to_*     └ 耦合面丢弃
```

### 13.3 正确性 / 物理（高）

#### 1. MRF 叶轮边界易错

叶轮曾用 `noSlip`，与 MRF 源项冲突，导致 `|U|_max` 达数百 m/s（物理叶尖 `ωR≈3 m/s`，`omega=100 rad/s`，`R≈30 mm`）。  
已改为 `templates.field_U` 中 `*impeller*` → `movingWallVelocity`。残留风险：

- 依赖 patch 名含 `impeller`
- JSON `boundary_conditions` 若写死 `noSlip` 会覆盖模板
- 旧算例未从 Time=0 重跑则结果仍错

#### 2. 界面配对不完备 → 漏耦 / 假耦

| 机制 | 风险 |
|------|------|
| 等面数配对（`scan_face_count_interfaces`） | 两侧 `nFaces` 不等（如 air↔Cu 925 vs 24523）无法配对，留作普通 wall |
| 后缀链 `foo↔foo_1↔foo_2` | 可能产生非物理假界面 |
| 名称↔region token 打分 | BCs_fix 可用，换命名易失效 |

`coalesce_interfaces: false` 时，未配对界面不会并成内部面，质量泄漏风险更大。工具链对 mappedWall **不校验**面数对称（OpenFOAM 允许非共形，但大比例靠 `nearestPatchFace`，热流精度差）。

#### 3. scan 与 build 区域类型可能不一致

输入 `constant/regionProperties` 常把 `case1/case2` 标成 **solid**，JSON 配置为 **fluid**。  
`scan`（`_resolve_region_type`）优先用 RP；`build` 用配置 `resolve_region_type`。报告中的 `kind` 可能误导。

#### 4. split 时命名耦合面可能被丢弃

`mesh_split._extract_region_mesh`：若 `remote_name` 不在 `region_names`（zone 长名 vs 短 region 名错位），`named_coup_faces` 直接 `continue`，面从区域网格消失 → 开孔/拓扑洞。无显式报错。

### 13.4 流程 / 工程（高–中）

#### 5. `coalesce` 默认值与文档/配置相反

代码：`case_generator._copy_mesh` 中 `coalesce_interfaces` **默认 True**；v3 / BCs_fix 配置与文档多为 **false**。漏写该键会静默走慢路径。

#### 6. `interface_neighbors` 在 coalesce 关闭时几乎为空

只统计跨区**内部面**。cgns2foam 界面为边界双 patch 时，build 阶段邻居为空，依赖 prep 的 `sync_region_fields`；跳过 sync → `0.orig` 缺 `*_to_*`。

#### 7. AMI 场 BC 曾漏配

`_PartSurface_air_domain_7/8` 不匹配 `*rotation*`，`p` 写成 `calculated` → FATAL。现靠 `field_sync._effective_ami_patterns` 并入显式 AMI 名；fix 脚本与模板默认模式仍易不同步。

#### 8. AMI 轴 vs MRF 轴

全局一个 `interfaces.ami_rotation_axis`；MRF 按 zone 启发式（`rotation*`→+Y）。轴不一致时无自动校验（BCs_fix 已手工对齐 `[0,1,0]`）。

#### 9. 数值设置分叉

| 配置 | 特征 | 结果 |
|------|------|------|
| BCs_fix | `momentumPredictor false`、松弛 0.2、`limitT [200,500]` | 可跑到 Time=100 |
| v3 | 曾用 `momentumPredictor true`、松弛 0.5 等 | 更易发散 |

`limitTemperature` 仅挂在流体区；固体区无同等钳制。

### 13.5 可维护性 / 通用性（中–低）

| # | 问题 | 证据 / 影响 |
|---|------|-------------|
| 10 | 强绑定 laptop 命名 | `_infer_patch_region`、`open`/`impeller` 特判；换项目需改代码或大量 JSON |
| 11 | `parse_boundary` 不完整 | 正则止于 `nFaces/startFace`；其后的 `sampleRegion`/`samplePatch` 可能读不到 |
| 12 | Windows prep 脆弱 | `Allrun.pre` 仍 `cp -f`；`createPatch`/`checkMesh` 非致命，依赖 Python 补丁 |
| 13 | 无自动化测试 | 配对、split、AMI sync、叶轮 BC 无回归护栏 |
| 14 | 文档滞后 | AMI `transform` 文档曾写 `rotational`、代码为 `noOrdering`；README 混用 `python`/`python3` |
| 15 | 其它气味 | `_build_patch_pairs` 类型标注与三元组不符；`fix_mapped_wall` 写死 `air/MRFProperties`；`omega` 无单位校验；`p_rgh` build 写 0、sync 写绝对压 |

### 13.6 严重度速查

| 级别 | 条目 |
|------|------|
| **Critical** | split 耦合面因 remote 名丢弃；scan/build 区域类型源不一致 |
| **High** | 等面数漏耦 + coalesce 关；`interface_neighbors` 空；coalesce 默认 True；AMI/MRF 轴；数值分叉；叶轮 BC 可被覆盖 |
| **Medium** | 假配对；`classify` 对 AMI 名强制 cyclicAMI；`parse_boundary`；omega 无校验；limitT 仅流体 |
| **Low** | laptop 启发式；类型标注；MRF 写死 air；无测试；文档不一致 |

### 13.7 优先改进建议

1. **统一 region 类型源**：scan/build 都只信 JSON（或强制校正输入 RP）
2. **配对/丢面显式告警**：未配界面、面数比过大、`named_coup` 丢弃不得静默
3. **叶轮 BC 配置化**：`mrf.wallPatches` 或强制 `movingWallVelocity`，不单靠名字
4. **对齐 `coalesce_interfaces` 默认值**与文档；coalesce 关时检查 mappedWall 覆盖率
5. **最小回归**：小网格测配对、split 面数、AMI 场类型、叶轮 BC
6. **文档与代码同步**：本文件、`README.md`、`docs/tech_bcs_fix_interfaces.md`
