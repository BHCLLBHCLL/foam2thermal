# foam2thermal

从 **cgns2foam 单体网格** + **JSON 配置** 自动生成并运行 **chtMultiRegionSimpleFoam** 多区域共轭传热（CHT）算例。

当前推荐配置：**v0.5 / BCs_fix**（`_PartSurface_*` 命名网格，默认 **不做 coalesce/stitch**，靠拓扑 + 等面数配对生成 `mappedWall` / `cyclicAMI`）。

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

> 本环境请用 `python3`（无 `python`  shim）。下文示例若写 `python`，请改为 `python3`。

---

## 命令格式

所有子命令使用统一的 **4 个位置参数**：

```bash
python3 setup_cht_case.py <command> <input_mesh> <config.json> <output_case> [options]
```

| 参数 | 含义 |
|------|------|
| `command` | `check` / `scan` / `build` / `run` |
| `input_mesh` | cgns2foam 输出的网格案例目录（含 `constant/polyMesh`） |
| `config.json` | 区域、材料、界面、数值、OpenFOAM 路径等配置 |
| `output_case` | 生成的 CHT 算例输出目录 |

查看帮助：

```bash
python3 setup_cht_case.py --help
python3 setup_cht_case.py build --help
python3 setup_cht_case.py run --help
```

---

## 子命令说明

### `check` — 校验输入网格

检查 `constant/polyMesh` 是否完整（points、faces、owner、neighbour、boundary、cellZones），并统计 patch / cellZone 数量。

```bash
python3 setup_cht_case.py check \
    tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix \
    configs/laptop_thermal_steady_v3_BCs_fix.json \
    cases/laptop_thermal_cht_v3_BCs_fix
```

输出：`cases/<output>/mesh_check.json`

---

### `scan` — 扫描界面 patch 配对与分类

识别界面 patch，并结合网格拓扑与 `constant/regionProperties` 自动分类耦合方式。

**输入来源（优先级）：**

1. **`constant/regionProperties`**（cgns2foam 网格自带）— 列出各 fluid/solid 区域名（通常与 cellZone 名一致）
2. **网格拓扑** — 读取 `owner` + `cellZones`，按 patch 面所属单元的多数 cellZone 推断 patch→region（**优先于** JSON 中过时的 `patch_regions` 手工映射）
3. **JSON `patch_regions`** — 仅填补拓扑无法覆盖的 patch；不覆盖拓扑结果

**配对规则（v0.5，两级）：**

1. **等面数配对（优先）** — 相同 `nFaces`、归属不同 region 的 patch 配对；用名称↔对端 region 的 token 打分（适配 `_PartSurface_Cu_block` ↔ `_PartSurface_air_domain_3` 这类两侧 stem 不同的 BCs_fix 命名）。`impeller*` 叶轮壁面跳过。
2. **后缀链（兜底）** — 经典 `foo`↔`foo_1`↔`foo_2`；仅当两侧面数比 ≤ `suffix_face_ratio_max`（默认 1.15），且两侧尚未被等面数配对占用时才加入（避免 Cover↔Cover_1 误配）。

**分类规则：**

| 界面类型 | 方法 (`method`) | 说明 |
|----------|-----------------|------|
| 流-流 AMI（`ami_rot*` / 名或 region 含 `rotation`） | `cyclicAMI` | prep 时 `createPatch` + `fix_cyclic_ami_patches.py` |
| 流-固 / 固-固 / 跨区流-流 | `mappedWall` | split 时生成 `*_to_*` 耦合面 |

```bash
python3 setup_cht_case.py scan \
    tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix \
    configs/laptop_thermal_steady_v3_BCs_fix.json \
    cases/laptop_thermal_cht_v3_BCs_fix
```

输出：`cases/<output>/interface_scan.json`，主要字段：

| 字段 | 内容 |
|------|------|
| `region_properties` | 从输入网格读取的 fluid/solid 区域列表 |
| `patch_regions` | 拓扑推断的 patch→region 映射 |
| `interfaces` | 每对界面的 `kind`、`method`、`region_a`、`region_b` |
| `interface_pairs` | 兼容旧版的 master/slave 列表 |

`build` 使用同一套扫描逻辑，并把推断到的 `patch_regions` 与扫描到的界面写入输出 `config.json` 的 `interfaces.explicit`（用户显式项优先保留），供 split / AMI 修复脚本使用。

---

### `build` — 生成 CHT 算例

从输入网格复制并预处理单体网格，写入区域配置、场文件模板、`Allrun.pre` / `Allrun`、辅助脚本等。**不调用 OpenFOAM 求解器**。

```bash
python3 setup_cht_case.py build \
    tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix \
    configs/laptop_thermal_steady_v3_BCs_fix.json \
    cases/laptop_thermal_cht_v3_BCs_fix
```

仅分析、不写文件：

```bash
python3 setup_cht_case.py build ... --dry-run
```

主要输出：

| 路径 | 内容 |
|------|------|
| `constant/polyMesh/` | 单体网格（BCs_fix 默认不 coalesce；prep 前） |
| `constant.orig/<region>/` | 各区域物性、MRF 等（prep 后部署） |
| `system.orig/<region>/` | 各区域 fvSchemes / fvSolution |
| `0.orig/<region>/` | 各区域初始场（prep 后同步到 `0/`） |
| `config.json` | 配置副本（含扫描写入的 `patch_regions` / `interfaces.explicit`） |
| `setup_report.json` | 构建报告（界面、coalesce 统计等） |
| `Allrun.pre` | 网格拆分与场同步脚本 |
| `Allrun` | 完整流程（prep + 求解器） |
| `scripts/` | split、fix AMI、fix mappedWall、sync fields 等 |

---

### `run` — 执行 OpenFOAM 流程

通过 MSYS2 `bash` 调用 OpenFOAM 工具（路径在 JSON `openfoam` 段配置）。

```bash
# prep + 求解器（默认）
python3 setup_cht_case.py run \
    tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix \
    configs/laptop_thermal_steady_v3_BCs_fix.json \
    cases/laptop_thermal_cht_v3_BCs_fix

# 仅网格预处理
python3 setup_cht_case.py run ... --step prep

# 仅求解器（须已完成 prep）
python3 setup_cht_case.py run ... --step solve
```

| `--step` | 行为 |
|----------|------|
| `all`（默认） | `Allrun.pre` + `chtMultiRegionSimpleFoam` |
| `prep` | 仅 `Allrun.pre` |
| `solve` | 仅求解器（**不**重跑 prep） |

> **注意**：案例目录下的 `./Allrun` 会先执行 `./Allrun.pre` 再跑求解器。若 prep 已完成，建议用 CLI `--step solve`，避免重复 prep。

---

## 推荐工作流

### 首次完整流程（BCs_fix / v0.5）

```bash
MESH=tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix
CFG=configs/laptop_thermal_steady_v3_BCs_fix.json
OUT=cases/laptop_thermal_cht_v3_BCs_fix

python3 setup_cht_case.py check  "$MESH" "$CFG" "$OUT"
python3 setup_cht_case.py scan   "$MESH" "$CFG" "$OUT"
python3 setup_cht_case.py build  "$MESH" "$CFG" "$OUT"
python3 setup_cht_case.py run    "$MESH" "$CFG" "$OUT" --step prep
python3 setup_cht_case.py run    "$MESH" "$CFG" "$OUT" --step solve --parallel
```

### 修改 JSON 配置后

- 改了区域、材料、BC、MRF 等 → 重新 **`build`**，再 **`run --step prep`**
- 只改了数值（endTime 等）→ 可手动改 `system/controlDict`，或 rebuild 后只跑 solve

### prep 已完成，仅重跑求解器

```bash
# 若存在旧 log，OpenFOAM 可能跳过运行；需先删除
rm cases/laptop_thermal_cht_v3_BCs_fix/log.chtMultiRegionSimpleFoam

python3 setup_cht_case.py run ... --step solve
```

### 在 MSYS2 OpenFOAM 环境中手动运行

```bash
cd cases/laptop_thermal_cht_v3_BCs_fix_rebuild
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
→ fix_mapped_wall_patches.py   # mappedWall + open→patch + MRF nonRotating
→ verifyRegions.sh
→ 部署 constant.orig / system.orig
→ fix_mapped_wall_patches.py   # 再次合并 nonRotating（防 cp 覆盖）
→ sync_region_fields.py
→ renumberMesh -allRegions
→ restore0Dir -allRegions
```

prep 完成后：

- 单体 `constant/polyMesh` 被删除
- 各区域网格位于 `constant/<region>/polyMesh/`
- 初始场位于 `0/<region>/`
- `open*` 网格类型为 **`patch`**（cgns2foam 常误标为 `wall`，由 split/`fix_mapped_wall` 纠正）

BCs_fix 配置默认 **关闭** `coalesce_interfaces` / `stitch_interfaces`：界面以边界 patch 形式保留，由 split 生成 `mappedWall`，AMI 由 `createPatch` + Python 修复。

---

## 配置文件要点

| 配置 | 网格命名 | 说明 |
|------|----------|------|
| `configs/laptop_thermal_steady_v3_BCs_fix.json` | `_PartSurface_*` | **推荐**；等面数扫描；coalesce/stitch 关；叶轮 `movingWallVelocity`；`open` 用 `prghTotalPressure` |
| `configs/laptop_thermal_steady_v3.json` | `case*_s` / `CPU_s` 等 | 经典后缀链；同样默认不 coalesce/stitch；`open` BC 与 BCs_fix 对齐 |

| 段 | 作用 |
|----|------|
| `openfoam.root` / `bash` | OpenFOAM 安装路径、MSYS2 bash |
| `openfoam.solver` | 求解器名，默认 `chtMultiRegionSimpleFoam` |
| `regions` | 区域名、类型（fluid/solid）、cellZones、材料 |
| `regions[].mrf` | MRF 旋转区：`cellZones`、`omega`（rad/s）、`axis`/`axes`、`origin` |
| `materials` | 流体/固体热物性 |
| `interfaces` | `auto_scan`、`ami_patterns`、`ami_rotation_axis`、`explicit`、`exclude` |
| `patch_regions` | 可选：单体网格 patch→region 补充映射；**拓扑推断优先**，仅作兜底 |
| `boundary_conditions` | 按区域覆盖默认 BC（含 `k`/`epsilon`/`nut`/`alphat`） |
| `turbulence` | `simulationType laminar`（默认）或 `RAS` + `RASModel`；设 RAS 时自动生成各流体区 `k`/`epsilon`/`nut`/`alphat` 场 |
| `radiation` | 可选；字符串模型名或 `{ "default": "none", "<region>": "fvDOM" }`，缺省各区写 `radiationModel none` |
| `numerics` | `endTime`/`writeInterval`/`deltaT`/`relaxation`；`frozenFlow`、`limitTemperature`、`limitVelocity`、`momentumPredictor` |
| `mesh_prep` | coalesce / stitch / AMI / split；v3 与 BCs_fix 默认 `coalesce_interfaces=false`、`stitch_interfaces=false` |

**cyclicAMI 与 MRF：**

- AMI 界面须在**同一 fluid region** 的 polyMesh 上，因此旋转 cellZone 与 air 域合并
- 默认 `ami_patterns`：`ami_rot\d+`、`.*[Rr]otation\d*`；亦可按 patch/region 名含 `rotation` 识别
- BCs_fix 显式示例：`_PartSurface_rotation1` ↔ `_PartSurface_air_domain_7`
- `ami_rotation_axis` 应与风扇转轴一致（BCs_fix 为 `(0 1 0)`）
- 场 BC 中 AMI patch 使用 `type cyclicAMI`；`field_sync` 会把 explicit 对的**精确名**并入 AMI 模式，避免仅一侧匹配 `*rotation*`
- `nonRotatingPatches` 须含 **AMI 两侧**、全部 `open*`、以及 split 后的 `air_to_*`（由 `fix_mapped_wall_patches` 在部署 `constant.orig` 后再次合并）

**开放边界 `open`（须为 `patch`）：**

- 网格类型必须是 **`patch`**，不能是 `wall`（cgns2foam 常误标；`resolve_open_patch_type` / split / fix 脚本自动纠正）
- 推荐场 BC：`U`=`pressureInletOutletVelocity`，`p_rgh`=**`prghTotalPressure`**（带 `p0`/`U`/`phi`/`rho`），`T`=`inletOutlet`
- **不要**对 `p_rgh` 用静态场的 `totalPressure`（会破坏 `heRhoThermo` / rho 限幅）
- 可选 `numerics.limitVelocity.max`（如 `4`）压制箱角数值尖峰；叶尖量级约 `ωR`

**叶轮壁面（MRF）：**

- patch 名含 `impeller` 时，`U` 默认写 `movingWallVelocity`（`value uniform (0 0 0)`），由 MRF 给出绝对壁速 ω×r
- 若误用 `noSlip`（绝对 U=0），会与 MRF 源项对抗，产生数百 m/s 的虚假射流
- 建议在 `interfaces.exclude` 中排除叶轮 patch，避免被当成耦合界面

---

## 日志与排错

日志文件位于输出案例目录：`log.<utility>.<utility>`

| 现象 | 原因 / 处理 |
|------|-------------|
| `bad size -8188` in checkMesh/topoSet | coalesce 后 `faceCompactList` 偏移表不一致（已修复）；需重新 **`build`** |
| `Cannot find file "points" in polyMesh` | 在已 split 的案例上重跑 prep → 用最新 `Allrun.pre`，或先 **`build`** |
| `createPatch` / `renumberMesh` exit 3 | Windows MinGW 已知问题，脚本已设为非致命；AMI 由 Python 脚本补全 |
| 叶轮附近虚假高速射流 | 检查 impeller patch 的 `U` 是否为 `movingWallVelocity`（非 `noSlip`） |
| 出口/`open` 附近 \|U\| 达数百 m/s | 查 `open` 是否为 `patch`；`p_rgh` 是否为 `prghTotalPressure`；MRF `nonRotatingPatches` 是否含 AMI 两侧与 `air_to_*` |
| `open` 仍是 `wall` | 重跑 prep（`fix_mapped_wall_patches`）或确认 `mesh.resolve_open_patch_type` / split 路径 |
| AMI 一侧仍是 wall | 确认 `interfaces.explicit` / `ami_patterns`；重跑 prep 使 `fix_cyclic_ami_patches.py` 升级双方 |
| `Illegal neighbourPatch name None` | `parse_boundary` 曾截断导致重写丢 AMI 元数据；需完整块解析后重跑 `fix_cyclic_ami` |
| `Solver run failed` 但 log 很短 | 可能是 MinGW 崩溃或磁盘满（`decomposePar` IO ERROR）；检查磁盘与 `log.*` |
| 求解器提示 already run | 删除 `log.chtMultiRegionSimpleFoam` 后重跑 |
| prep 报无网格 | 先执行 **`build`** |

---

## 目录结构

```
foam2thermal/
├── setup_cht_case.py       # CLI 入口
├── configs/                # JSON 配置（v3 / BCs_fix）
├── docs/                   # 技术专题文档
├── tests/                  # 测试用 cgns2foam 网格（gitignore）
├── cases/                  # 生成的算例（gitignore）
├── scripts/                # 案例内复制的辅助脚本
│   ├── split_regions.py
│   ├── fix_cyclic_ami_patches.py
│   ├── fix_mapped_wall_patches.py
│   ├── sync_region_fields.py
│   └── verifyRegions.sh
└── src/foam2thermal/       # Python 包
```

技术文档：`DEV_SUMMARY.md`（总览）· `docs/tech_h_initial_divergence_fix.md`（焓发散）· `docs/tech_bcs_fix_interfaces.md`（BCs_fix 界面与叶轮）

---

## 示例（笔记本散热 v0.5 / BCs_fix）

网格：`tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix`（`_PartSurface_*` 命名）  
配置：`configs/laptop_thermal_steady_v3_BCs_fix.json`  
输出：`cases/laptop_thermal_cht_v3_BCs_fix_rebuild`

```bash
MESH=tests/laptop_thermal_steady_scaled_v3_orig_BCs_fix
CFG=configs/laptop_thermal_steady_v3_BCs_fix.json
OUT=cases/laptop_thermal_cht_v3_BCs_fix_rebuild

python3 setup_cht_case.py scan  "$MESH" "$CFG" "$OUT"
python3 setup_cht_case.py build "$MESH" "$CFG" "$OUT"
python3 setup_cht_case.py run   "$MESH" "$CFG" "$OUT" --step prep
python3 setup_cht_case.py run   "$MESH" "$CFG" "$OUT" --step solve --parallel
```

生成算例含 **8 个区域**（air 含旋转区 + case1/case2 为 solid + CPU/Cu/Cover/fin1/fin2），约 **22 对界面**（2× `cyclicAMI` + 20× `mappedWall`），叶轮为 `movingWallVelocity`，`open` 为 `patch` + `prghTotalPressure`。Windows 8 核并行已验证求解至 Time=100（`|U|_max≈ωR`）。

---

## 示例（笔记本散热 v3，经典后缀命名）

```bash
python3 setup_cht_case.py scan  tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3
python3 setup_cht_case.py build tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3
python3 setup_cht_case.py run   tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3 --step prep
python3 setup_cht_case.py run   tests/laptop_thermal_steady_scaled_v3_orig configs/laptop_thermal_steady_v3.json cases/laptop_thermal_cht_v3 --step solve --parallel
```

同样默认关闭 coalesce/stitch；界面依赖后缀链 + 拓扑分类。
