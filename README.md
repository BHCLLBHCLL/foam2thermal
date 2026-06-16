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

### `scan` — 扫描界面 patch 配对

识别 cgns2foam 导出的 `foo` / `foo_1` 界面 patch 对，以及 AMI 模式 patch。

```bash
python setup_cht_case.py scan \
    tests/laptop_thermal_steady_orig_fix_ansa \
    configs/laptop_thermal_steady.json \
    cases/laptop_thermal_cht
```

输出：`cases/laptop_thermal_cht/interface_scan.json`

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

# 2. （可选）扫描界面
python setup_cht_case.py scan   tests/laptop_thermal_steady_orig_fix_ansa configs/laptop_thermal_steady.json cases/laptop_thermal_cht

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
| `regions[].mrf` | MRF 旋转区：`cellZones`、`omega`（rad/s）、`axis`、`origin` |
| `materials` | 流体/固体热物性 |
| `interfaces` | AMI patch 配对、`ami_rotation_axis` |
| `patch_regions` | 单体网格 patch → 逻辑区域映射 |
| `boundary_conditions` | 按区域覆盖默认 BC |
| `numerics` | endTime、writeInterval、deltaT 等 |
| `mesh_prep` | coalesce、AMI、split 选项 |

**cyclicAMI 与 MRF：**

- AMI 界面（`ami_rot*`）须在**同一 fluid region** 的 polyMesh 上，因此旋转 cellZone 与 air 域合并
- 场 BC 中 AMI patch 使用 `type cyclicAMI`
- 旋转体效应由 `constant/<region>/MRFProperties` 定义（`omega` 单位为 **rad/s**）

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

## 示例（笔记本散热）

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
