# foam2thermal

从 **cgns2foam 单体网格** + **JSON 配置** 自动生成并运行 **chtMultiRegionSimpleFoam** 多区域共轭传热（CHT）算例。

---

## 环境要求

| 组件 | 说明 |
|------|------|
| Python | 3.9+，依赖 `numpy` |
| OpenFOAM | v2412（Windows 需 MSYS2 安装，配置见 JSON `openfoam` 段） |
| 输入网格 | cgns2foam 输出的 OpenFOAM 案例目录，须含 `constant/polyMesh/` 与 `cellZones` |

```bash
pip install numpy
```

---

## 命令格式

所有子命令使用统一的 **4 个位置参数**：

```bash
python setup_cht_case.py <command> <input_mesh> <config.json> <output_case> [options]
```

| 参数 | 含义 |
|------|------|
| `command` | `check` / `scan` / `build` / `run` |
| `input_mesh` | cgns2foam 输出的网格案例目录（含 `constant/polyMesh`） |
| `config.json` | 区域、材料、界面、数值、OpenFOAM 路径等配置 |
| `output_case` | 生成的 CHT 算例输出目录 |

查看帮助：

```bash
python setup_cht_case.py --help
python setup_cht_case.py build --help
python setup_cht_case.py run --help
```

---

## 子命令说明

### `check` — 校验输入网格

检查 `constant/polyMesh` 是否完整（points、faces、owner、neighbour、boundary、cellZones），并统计 patch / cellZone 数量。

```bash
python setup_cht_case.py check \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht
```

输出：`cases/laptop_thermal_cht/mesh_check.json`

---

### `scan` — 扫描界面 patch 配对与分类

识别 cgns2foam 导出的界面 patch 链（`foo` / `foo_1` / `foo_2` …），并结合网格拓扑与 `constant/regionProperties` 自动分类耦合方式。

**输入来源（优先级）：**

1. **`constant/regionProperties`**（cgns2foam 网格自带）— 列出各 fluid/solid 区域名（通常与 cellZone 名一致）
2. **网格拓扑** — 读取 `owner` + `cellZones`，按 patch 面所属单元的多数 cellZone 推断 patch→region（**优先于** JSON 中过时的 `patch_regions` 手工映射）
3. **JSON `patch_regions`** — 仅填补拓扑无法覆盖的 patch；不覆盖拓扑结果

**配对规则：** 同一 BC 基名（去掉 `_\d+` 后缀）内，按后缀序号连续配对：`foo`↔`foo_1`↔`foo_2` …；跳过两端归属同一 region 的伪配对。

**分类规则：**

| 界面类型 | 方法 (`method`) | 说明 |
|----------|-----------------|------|
| 流-流 AMI（`ami_rot*`） | `cyclicAMI` | prep 时 `createPatch` + `fix_cyclic_ami_patches.py` |
| 流-固 / 固-固 / 跨区流-流 | `mappedWall` | split 时生成 `*_to_*` 耦合面 |

```bash
python setup_cht_case.py scan \
    tests/laptop_thermal_steady_scaled_v3_orig \
    configs/laptop_thermal_steady_v3.json \
    cases/laptop_thermal_cht_v3
```

输出：`cases/<output>/interface_scan.json`，主要字段：

| 字段 | 内容 |
|------|------|
| `region_properties` | 从输入网格读取的 fluid/solid 区域列表 |
| `patch_regions` | 拓扑推断的 patch→region 映射 |
| `interfaces` | 每对界面的 `kind`、`method`、`region_a`、`region_b` |
| `interface_pairs` | 兼容旧版的 master/slave 列表 |

`build` 使用同一套拓扑推断逻辑（并映射到 JSON 中的短 region 名，如 `air`、`CPU`）。

---

### `build` — 生成 CHT 算例

从输入网格复制并预处理单体网格，写入区域配置、场文件模板、`Allrun.pre` / `Allrun`、辅助脚本等。**不调用 OpenFOAM 求解器**。

```bash
python setup_cht_case.py build \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht
```

仅分析、不写文件：

```bash
python setup_cht_case.py build ... --dry-run
```

主要输出：

| 路径 | 内容 |
|------|------|
| `constant/polyMesh/` | 合并界面后的单体网格（prep 前） |
| `constant.orig/<region>/` | 各区域物性、MRF 等（prep 后部署） |
| `system.orig/<region>/` | 各区域 fvSchemes / fvSolution |
| `0.orig/<region>/` | 各区域初始场（prep 后同步到 `0/`） |
| `config.json` | 配置副本（含 `_meta.source_mesh`） |
| `setup_report.json` | 构建报告（界面、coalesce 统计等） |
| `Allrun.pre` | 网格拆分与场同步脚本 |
| `Allrun` | 完整流程（prep + 求解器） |
| `scripts/` | split、fix AMI、fix mappedWall、sync fields 等 |

---

### `run` — 执行 OpenFOAM 流程

通过 MSYS2 `bash` 调用 OpenFOAM 工具（路径在 JSON `openfoam` 段配置）。

```bash
# prep + 求解器（默认）
python setup_cht_case.py run \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht

# 仅网格预处理
python setup_cht_case.py run ... --step prep

# 仅求解器（须已完成 prep）
python setup_cht_case.py run ... --step solve
```

| `--step` | 行为 |
|----------|------|
| `all`（默认） | `Allrun.pre` + `chtMultiRegionSimpleFoam` |
| `prep` | 仅 `Allrun.pre` |
| `solve` | 仅求解器（**不**重跑 prep） |

> **注意**：案例目录下的 `./Allrun` 会先执行 `./Allrun.pre` 再跑求解器。若 prep 已完成，建议用 CLI `--step solve`，避免重复 prep。

---

## 推荐工作流

### 首次完整流程

```bash
# 1. 校验网格
python setup_cht_case.py check  tests/laptop_thermal_steady_orig_fix_ansa configs/laptop_thermal_steady.json cases/laptop_thermal_cht

# 2. （推荐）扫描界面：读 regionProperties + 拓扑，输出 cyclicAMI / mappedWall 分类
python setup_cht_case.py scan   tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3

# 3. 生成算例
python setup_cht_case.py build  tests/laptop_thermal_steady_orig_fix_ansa configs/laptop_thermal_steady.json cases/laptop_thermal_cht

# 4. 网格预处理（split、AMI、MRF、场同步）
python setup_cht_case.py run    tests/laptop_thermal_steady_orig_fix_ansa configs/laptop_thermal_steady.json cases/laptop_thermal_cht --step prep

# 5. 运行求解器
python setup_cht_case.py run    tests/laptop_thermal_steady_orig_fix_ansa configs/laptop_thermal_steady.json cases/laptop_thermal_cht --step solve
```

### 修改 JSON 配置后

- 改了区域、材料、BC、MRF 等 → 重新 **`build`**，再 **`run --step prep`**
- 只改了数值（endTime 等）→ 可手动改 `system/controlDict`，或 rebuild 后只跑 solve

### prep 已完成，仅重跑求解器

```bash
# 若存在旧 log，OpenFOAM 可能跳过运行；需先删除
rm cases/laptop_thermal_cht/log.chtMultiRegionSimpleFoam

python setup_cht_case.py run ... --step solve
```

### 在 MSYS2 OpenFOAM 环境中手动运行

```bash
cd cases/laptop_thermal_cht
./Allrun.pre    # 网格 prep
./Allrun        # prep + 求解器（等价于 --step all）
./Allclean      # 清理时间目录
```

---

## `Allrun.pre` 流程概要

```
[有 constant/polyMesh 时]
  checkMesh → topoSet → createPatch(AMI) → split_regions.py
[已 split 时]
  跳过上述步骤
→ fix_cyclic_ami_patches.py
→ fix_mapped_wall_patches.py
→ verifyRegions.sh
→ 部署 constant.orig / system.orig
→ sync_region_fields.py
→ renumberMesh -allRegions
→ restore0Dir -allRegions
```

prep 完成后：

- 单体 `constant/polyMesh` 被删除
- 各区域网格位于 `constant/<region>/polyMesh/`
- 初始场位于 `0/<region>/`

---

## 配置文件要点

示例：`configs/laptop_thermal_steady.json`

| 段 | 作用 |
|----|------|
| `openfoam.root` / `bash` | OpenFOAM 安装路径、MSYS2 bash |
| `openfoam.solver` | 求解器名，默认 `chtMultiRegionSimpleFoam` |
| `regions` | 区域名、类型（fluid/solid）、cellZones、材料 |
| `regions[].mrf` | MRF 旋转区：`cellZones`、`omega`（rad/s）、`axis`/`axes`、`origin` |
| `materials` | 流体/固体热物性 |
| `interfaces` | AMI 显式配对、`ami_rotation_axis`、`auto_scan`（默认 true） |
| `patch_regions` | 可选：单体网格 patch→region 补充映射；**拓扑推断优先**，仅作兜底 |
| `boundary_conditions` | 按区域覆盖默认 BC（含 `k`/`epsilon`/`nut`/`alphat`） |
| `turbulence` | `simulationType laminar`（默认）或 `RAS` + `RASModel`；设 RAS 时自动生成各流体区 `k`/`epsilon`/`nut`/`alphat` 场 |
| `radiation` | 可选；字符串模型名或 `{ "default": "none", "<region>": "fvDOM" }`，缺省各区写 `radiationModel none` |
| `numerics` | `endTime`/`writeInterval`/`deltaT`/`relaxation`；`frozenFlow`（冻结流场只解能量）、`limitTemperature {min,max}`（生成 `fvOptions` 温度限值） |
| `mesh_prep` | coalesce、AMI、split 选项；`coalesce_geometric_fallback`（默认 true）+ `coalesce_geom_tol`（默认 `5×coalesce_point_tol`）几何兜底配对，减少开放单元 |

**cyclicAMI 与 MRF：**

- AMI 界面（`ami_rot*`）须在**同一 fluid region** 的 polyMesh 上，因此旋转 cellZone 与 air 域合并
- 场 BC 中 AMI patch 使用 `type cyclicAMI`
- 旋转体效应由 `constant/<region>/MRFProperties` 定义（`omega` 单位为 **rad/s**）
- 默认转轴：`rotation1` → `(0 1 0)`，`rotation2` → `(0 -1 0)`（按 cellZone 名匹配）；可用 `mrf.axis`（统一）或 `mrf.axes`（按 zone 覆盖）

---

## 日志与排错

日志文件位于输出案例目录：`log.<utility>.<utility>`

| 现象 | 原因 / 处理 |
|------|-------------|
| `bad size -8188` in checkMesh/topoSet | coalesce 后 `faceCompactList` 偏移表末尾与 connectivity 长度不一致（已修复）；需重新 **`build`** |
| `Cannot find file "points" in polyMesh` | 在已 split 的案例上重跑 prep → 用最新 `Allrun.pre`，或先 **`build`** |
| `createPatch` / `renumberMesh` exit 3 | Windows MinGW 已知问题，脚本已设为非致命；AMI 由 Python 脚本补全 |
| `Solver run failed` 但 log 很短 | 可能是 MinGW 崩溃；检查 `log.chtMultiRegionSimpleFoam` |
| 求解器提示 already run | 删除 `log.chtMultiRegionSimpleFoam` 后重跑 |
| prep 报无网格 | 先执行 **`build`** |

---

## 目录结构

```
foam2thermal/
├── setup_cht_case.py       # CLI 入口
├── configs/                # JSON 配置
├── tests/                  # 测试用 cgns2foam 网格
├── cases/                  # 生成的算例（gitignore）
├── scripts/                # 案例内复制的辅助脚本
│   ├── split_regions.py
│   ├── fix_cyclic_ami_patches.py
│   ├── fix_mapped_wall_patches.py
│   ├── sync_region_fields.py
│   └── verifyRegions.sh
└── src/foam2thermal/       # Python 包
```

---

## 示例（笔记本散热 v3）

v3 算例：`tests/laptop_thermal_steady_scaled_v3_orig`（含 `constant/regionProperties`）→ `cases/laptop_thermal_cht_v3`（8 区域 + CPU 固体域）。

```bash
# 完整流程
python setup_cht_case.py scan  tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3
python setup_cht_case.py build tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3
python setup_cht_case.py run   tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3 --step prep
python setup_cht_case.py run   tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3 --step solve --parallel

# 或一键 build + prep + 8 核求解（不含 scan）
python scripts/run_cht_parallel_test.py
```

生成算例含 **8 个区域**（air 含旋转区 + case1 + case2 + CPU/Cu/Cover/fin1/fin2），**cyclicAMI** 旋转界面 + **mappedWall** 流固/固固耦合 + **MRF** 风扇区。

---

## 示例（笔记本散热 v1）

```bash
python setup_cht_case.py build \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht

python setup_cht_case.py run \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht --step prep

python setup_cht_case.py run \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht --step solve
```

生成算例含 **7 个区域**（air 含旋转区 + fan1 + fan2 + 4 固体），**cyclicAMI** 旋转界面 + **MRF** 风扇区。
