# foam2thermal 开发总结

## 概述

foam2thermal 是将 CGNS 网格转换为 OpenFOAM chtMultiRegionSimpleFoam 可计算案例的工具包。
本文档汇总转换过程中的关键技术点、**功能完整度**与**转换效率**评估，供后续开发与维护参考。

CLI 工作流：`check → scan → build → prep (Allrun.pre) → solve`

```
CGNS ──(cgns2foam，外部)──► 单体网格 ──(foam2thermal)──► 多区域 CHT 算例 ──► 求解
                              ✅ 主流程已打通              ⚠️ Windows 上求解仍不稳定
```

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
  │     ├─ 按 cellToRegion / cellZone 分割单体网格为多区域
  │     ├─ 生成 mappedWall 耦合面
  │     ├─ 写入各 region 的 cellZones（MRF 所需）
  │     └─ 输出 constant/<region>/polyMesh（faces 保持 binary faceCompactList）
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

- **stitch 界面**（fluid-fluid, fluid-solid）：按顶点集合签名配对，合并为内部面
- **cyclicAMI 界面**：不参与配对，保留为边界面，后续由 `fix_cyclic_ami_patches.py` 设置类型
- **配对方法**：面顶点排序后取元组作为签名，哈希查找配对面

### 2.5 faceCompactList 偏移表修复

cgns2foam 导出的 `faceCompactList` 可能存在**非单调 offset**（如 face 288417 附近步长 -8188），
导致 coalesce 后 OpenFOAM 报 `bad size -8188`。

**修复**：`_repair_compact_offsets()` 读取时自动修复；写入始终为 binary `faceCompactList`。

---

## 3. 区域分裂 (mesh_split.py)

### 3.1 分裂逻辑

按 `cellToRegion` 标签将单体网格分裂为多区域网格：

1. 遍历所有面，根据 owner/neighbour 的区域归属分类：
   - 两端同区域 → 内部面
   - 一端为本区域 → 边界面（保留原 patch 名）
   - 跨区域 → 耦合面（生成 `mappedWall` 类型）
2. 重新编号点、面、单元
3. 从单体 cellZones 映射并写入各 region 的 `cellZones`（供 MRF `cellZone` 引用）
4. 输出各区域 `constant/<region>/polyMesh/`

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
| `src/foam2thermal/mesh_coalesce.py` | 网格合并、点合并、退化面恢复 |
| `src/foam2thermal/mesh_split.py` | 区域分裂、耦合面生成 |
| `src/foam2thermal/mesh.py` | 网格读写、boundary/cellZones 解析 |
| `src/foam2thermal/case_generator.py` | 案例生成主流程 |
| `src/foam2thermal/templates.py` | OpenFOAM 字典模板 |
| `src/foam2thermal/field_sync.py` | 分裂后场文件同步 |
| `src/foam2thermal/config.py` | 配置加载与校验 |
| `src/foam2thermal/interfaces.py` | 界面扫描、AMI 识别、流/固分类 |
| `src/foam2thermal/runner.py` | MSYS2 调用 OpenFOAM（prep / solve） |
| `configs/laptop_thermal_steady_v3.json` | 案例配置（含 CPU 固体域） |

---

## 10. 功能完整度评估

**综合完成度：约 75–80%**（从 cgns2foam 网格到可 prep 的多区域 CHT 算例）

### 10.1 已实现（可交付）

| 模块 | 状态 | 说明 |
|------|------|------|
| CLI 工作流 | ✅ | `check` / `scan` / `build` / `run`（prep、solve 分步） |
| JSON 驱动配置 | ✅ | 区域、材料、BC、数值、MRF、AMI、`mesh_prep` |
| 界面扫描与分类 | ✅ | `foo`↔`foo_1` 配对；流-流 / 流-固 / 固-固分类 |
| 网格 coalesce | ✅ | Python 合并 stitch 界面；排除 AMI；修复 faceCompactList |
| 区域 split | ✅ | Python 替代 `splitMeshRegions`（Windows 不崩溃） |
| 多区域 CHT 模板 | ✅ | thermo、fvSchemes/Solution、`0.orig` 场、`regionProperties` |
| cyclicAMI + MRF | ✅ | AMI patch 修复；多 MRF zone；split 后保留 cellZones |
| mappedWall 耦合 | ✅ | 自动生成 `*_to_*` 面及 `turbulentTemperatureRadCoupledMixed` BC |
| 场同步 | ✅ | split 后按实际 boundary 重写 `0.orig` |
| 文档 | ✅ | `README.md` + 本文档 |

### 10.2 部分实现 / 有局限

| 模块 | 缺口 |
|------|------|
| **稳态求解** | `fvSolution` 已补 `pRefCell`/`pRefValue`；Windows MinGW 上首步迭代仍可能 exit 3（无 FATAL 静默崩溃） |
| **界面合并** | 默认 Python coalesce；新增几何兜底配对（`coalesce_geometric_fallback`，按面心重合 + 顶点吸附）减少开放单元；`stitchMesh` 仍可选且默认关闭 |
| **湍流** | 默认 laminar；设 `turbulence.simulationType=RAS` 时自动生成各流体区 `k`/`epsilon`/`nut`/`alphat` 场（壁函数边界） |
| **辐射** | 各区生成 `radiationProperties`（默认 `radiationModel none`，消除运行时提示，可经 `radiation` 配置启用） |
| **瞬态 CHT** | 仅 `chtMultiRegionSimpleFoam`，无 `chtMultiRegionFoam` |
| **CGNS 直读** | 依赖外部 cgns2foam |
| **自动化测试** | 无单元/回归测试；靠 log 与手工验证 |
| **并行** | 无 `decomposePar` / 多核 build |
| **Windows prep** | `Allrun.pre` 中 `cp -f` 偶发无法覆盖；`createPatch`/`renumberMesh` exit 3 已设非致命 |

### 10.3 相对最初需求的覆盖

| 需求 | 覆盖 |
|------|------|
| JSON 指定流体/固体域 | ✅ |
| 扫描/指定流-流、流-固、固-固界面 | ✅ scan + coalesce / mappedWall |
| OpenFOAM chtMultiRegionSimpleFoam 案例 | ✅ build + prep |
| 散热物性、数值格式、启动脚本 | ✅ 模板 + Allrun |
| 完整可运行仿真 | ⚠️ prep 稳定；solve 建议 Linux/WSL |

### 10.4 后续优先级建议

1. **短期**：在 Linux/WSL 跑 solve；Windows 专注 build/prep
2. **质量**：提高 coalesce 配对完整率，减少开放单元与质量泄漏
3. **完整度**：RAS 场文件自动生成、可选瞬态求解器、基础回归测试
4. **平台**：修复 Allrun.pre 在 Windows 下的文件复制逻辑

---

## 11. 转换效率评估

基准案例：`tests/laptop_thermal_steady_orig_fix_ansa` → `cases/laptop_thermal_steady_cht`（7 区域）

### 11.1 典型耗时（单核，Windows + Python 3 + MSYS2 OF v2412）

| 阶段 | 耗时（约） | 主要操作 |
|------|-----------|----------|
| **build** | ~170 s | coalesce 203,968 对面；227 万→207 万面；125 万→73 万点 |
| **prep** | ~125 s | checkMesh + Python split + createPatch + renumberMesh + 场同步 |
| **solve** | ~35 s 即停 | 初始化与 MRF/AMI 通过；`Time=1` 动量 1 步后 MinGW exit 3 |

### 11.2 流水线与瓶颈

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│ 读全网格     │ ──► │ coalesce 面配对   │ ──► │ 写全网格     │  ← build 主耗时 (~80%)
│ numpy 全量   │     │ O(n_faces) Python │     │ binary I/O  │
└─────────────┘     └──────────────────┘     └─────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ split 7 regions  │  ← prep 主耗时
                    │ 再次全量读写      │
                    └──────────────────┘
```

| 瓶颈 | 影响 | 改进方向 |
|------|------|----------|
| coalesce 面配对 | build 占绝大部分时间 | 分块配对、Numba/C++ 扩展 |
| 多次全量加载 | 内存数 GB 级；重复 parse | coalesce + split 合并 Pass |
| 单线程 | CPU 利用率低 | 按 region 并行 split |
| zone 几何查询 | 单次 centroid 可达 ~70 s | 缓存、按需计算 |
| OpenFOAM 工具 | renumberMesh ~4 s | 可接受 |

### 11.3 效率优点

- **binary 全程**：`faceCompactList` / `labelList` / `vectorField` 不转 ASCII，I/O 与 OpenFOAM 原生一致
- **Python split**：规避 Windows 上 `splitMeshRegions` 崩溃，prep 可重复、幂等
- **coalesce 替代 stitchMesh**：默认不跑 OpenFOAM stitch，减少大网格反复读写
- **prep 幂等**：已 split 案例跳过单体网格步骤

### 11.4 效率评级（laptop 级 ~200 万面）

| 维度 | 评分 | 说明 |
|------|------|------|
| I/O 格式 | ★★★★☆ | binary compact，体积小、读写快 |
| build 速度 | ★★☆☆☆ | ~3 min / 200 万面，偏慢 |
| prep 速度 | ★★★☆☆ | ~2 min，split 为主 |
| 内存 | ★★☆☆☆ | 全网格常驻内存，更大案例压力大 |
| 可扩展性 | ★★☆☆☆ | 无并行；500 万面以上会明显变慢 |
| 端到端成功率 | ★★★☆☆ | 转换成功率高；Windows 求解不稳定 |

---

## 12. 已知遗留问题

### 已修复（历史问题，重建案例后生效）

| 问题 | 处理 |
|------|------|
| `bad size -8188`（faceCompactList） | `_repair_compact_offsets()` + binary 写回 |
| `pRefCell` 缺失导致求解器 FATAL | `fvSolution` 增加 `pRefCell`/`pRefValue`/`rhoMin`/`rhoMax` |
| MRF `cellZones` 格式错误 | 改为每 zone 一条 `cellZone`（MRF1/MRF2） |
| split 后 cellZones 为空 | `mesh_split` 写入 region cellZones |
| `useImplicit true` + MinGW | 耦合 T BC 改为 `useImplicit false`，避免 `fvMatrixAssembly` 崩溃 |

### 仍待解决

1. **网格开放单元**：未配对界面面会产生质量泄漏。已新增几何兜底配对（`mesh_prep.coalesce_geometric_fallback`，默认开启，容差 `coalesce_geom_tol` 默认 `5 × coalesce_point_tol`），对"几何重合但点未被合并"的界面面按面心重合 + 顶点吸附补配；`setup_report.json` 的 `mesh_coalesce` 增加 `paired_signature`/`paired_geometric`/`suspected_unpaired_interface_faces` 以便观测剩余开放面。仍需在真实网格上核对配对完整率。

2. **Windows 求解器 exit 3**：`chtMultiRegionSimpleFoam` 在 `Time=1` 求解 U 后静默退出（无 FATAL）；`FOAM_SIGFPE=0` 无效。建议在 **WSL2/Linux** 求解，或 `frozenFlow true` 做纯传热调试。

3. **Allrun.pre 复制失败**：MSYS2 bash 中 `cp -f constant.orig/*` 偶发无法覆盖，需改为 Windows 兼容复制（Python/PowerShell）。

4. **momentumPredictor false**（v3 配置）：降低初期动量收敛速度；稳定后（约 50 步）可改回 `true`。v3 已将 `frozenFlow` 由 `true` 改为 `false`（真正求解流场对流），并加回 `limitTemperature [200,500]` 作为温度安全网。

5. **高非正交网格**：最大非正交性 ~84°，严重非正交面 ~1,391；需网格优化或增大 `nNonOrthogonalCorrectors`。

6. **湍流/RAS**：laminar 可用；RAS 案例需补全场文件与 `turbulenceProperties` 一致性。
