# 技术开发文档：BCs_fix 界面扫描与叶轮壁面（v0.5）

## 1. 背景

cgns2foam 新网格（`laptop_thermal_steady_scaled_v3_orig_BCs_fix`）采用 `_PartSurface_*` 命名：

- 界面两侧 **stem 往往不同**，例如空气侧 `_PartSurface_Cu_block` 对固体侧 `_PartSurface_air_domain_3`
- 旋转交界面名为 `_PartSurface_rotation1` / `_PartSurface_air_domain_7`（不再是 `ami_rot*`）
- 叶轮叶片为 `_PartSurface_impeller1` / `impeller2`（壁面，非耦合界面）

仅依赖「同基名后缀链」`foo`↔`foo_1` 会误配或漏配。v0.5 改为 **等面数 + 名称打分** 为主，后缀链为辅，并默认 **不做 coalesce/stitch**。

## 2. 转换策略变化

| 项目 | 旧路径（stitch/coalesce） | BCs_fix / v0.5 |
|------|---------------------------|-----------------|
| 界面重合面 | Python coalesce 合并为内部面 | **保留为边界 patch** |
| OpenFOAM stitchMesh | 可选 | 默认关闭 |
| 流固/固固耦合 | split 时部分来自 coalesce 后拓扑 | split 按扫描配对生成 `*_to_*` mappedWall |
| AMI | `ami_rot*` + createPatch | `rotation*` 名 / explicit 对 + createPatch + Python 修复 |
| 配置 | 手工 `patch_regions` 为主 | 拓扑推断写入 `config.json`；扫描结果并入 `interfaces.explicit` |

相关 JSON（`mesh_prep`）：

```json
"coalesce_interfaces": false,
"coalesce_geometric_fallback": false,
"stitch_interfaces": false
```

## 3. 界面配对算法（`interfaces.py`）

### 3.1 等面数配对 — `scan_face_count_interfaces`

1. 按 `nFaces` 分组 patch
2. 跳过 `impeller*`（叶轮壁面）
3. 仅允许 **不同 region** 的候选
4. 用 `_pair_name_score`：patch 名 token 与对端 region 名 token 交叉匹配（+2/+2，双向再 +2）
5. 贪心取最高分对；score≤0 则停止（避免两叶轮同面数误配）

示例（BCs_fix）：

```
_PartSurface_Cu_block  (owned by air)  ↔  _PartSurface_air_domain_3  (owned by Cu)
```

### 3.2 后缀链 — `scan_suffix_interfaces`（兜底）

经典 `foo`/`foo_1`/… 连续配对；`scan_cgns2foam_interfaces` 仅在以下条件加入：

- 两侧尚未被等面数配对占用
- 面数比 ≤ `suffix_face_ratio_max`（默认 1.15）

避免 Cover↔Cover_1 等「同名不同面」假链。

### 3.3 AMI 分类 — `classify_interface`

命中任一即倾向 `cyclicAMI`：

- `ami_patterns`（默认 `ami_rot\d+`、`.*[Rr]otation\d*`）
- patch 或归属 region 名含 `rotation`（`_looks_like_rotation`）

流-流且 AMI 命中 → `cyclicAMI`；其余跨区 → `mappedWall`。

### 3.4 配置落盘（`case_generator.py`）

`build` 将：

- 拓扑 `patch_regions` 写回输出 `config.json`
- 扫描界面追加到 `interfaces.explicit`（用户显式项优先）

供 `split_regions.py`、`fix_cyclic_ami_patches.py`、`field_sync` 在 prep 阶段读取。

## 4. split / AMI / 场同步配套

### 4.1 `mesh_split.py`

- `interfaces.explicit` 中 `method=cyclicAMI` 的对 **不** 转成 mappedWall 配对
- patch 名含 `rotation` 的同样跳过 mappedWall 路径，留给 createPatch

### 4.2 `fix_cyclic_ami_patches.py`

- 从 `config.json` 读 `explicit` cyclicAMI 对与 `ami_patterns`
- 升级条件：配置对中的 wall，或名匹配 `ami_patterns` 的 wall（不再硬编码仅 `ami_rot*`）

### 4.3 `field_sync.py` — `_effective_ami_patterns`

把 explicit AMI 双方 **精确名**（`re.escape`）并入模式列表，使 `_PartSurface_air_domain_7` 这类不含 `rotation` 的一侧也得到 `cyclicAMI` 场 BC。

## 5. 叶轮 `movingWallVelocity`（关键修复）

### 5.1 问题

MRF 下叶轮 patch 若用 `noSlip`（绝对速度 U=0），与 MRF 源项（相对系）冲突，在叶片附近产生虚假高速射流（数百 m/s）。

### 5.2 修正

`templates.field_U`：patch 名含 `impeller` 时默认：

```
type            movingWallVelocity;
value           uniform (0 0 0);
```

物理含义：壁面绝对速度由 MRF 按 ω×r 给出；`value` 为初始猜测。

### 5.3 配置建议

```json
"interfaces": {
  "exclude": ["_PartSurface_impeller1", "_PartSurface_impeller2"],
  "ami_rotation_axis": [0, 1, 0]
}
```

`ami_rotation_axis` 须与风扇转轴一致（本案例 Y 轴）。

## 6. 验证（BCs_fix）

| 阶段 | 结果 |
|------|------|
| scan / build | ~22 界面（2× cyclicAMI + 20× mappedWall）；coalesce paired_faces=0 |
| prep | 8 区域 split；AMI 双方升级；mappedWall `*_to_*` 齐全 |
| solve | Windows 8 核并行至 Time=100 |

配置：`configs/laptop_thermal_steady_v3_BCs_fix.json`  
案例：`cases/laptop_thermal_cht_v3_BCs_fix`

## 7. 相关代码

| 文件 | 职责 |
|------|------|
| `src/foam2thermal/interfaces.py` | 等面数/后缀扫描、AMI 分类 |
| `src/foam2thermal/case_generator.py` | 扫描结果写入 `config.json` |
| `src/foam2thermal/mesh_split.py` | 跳过 AMI/rotation 的 mappedWall 配对 |
| `src/foam2thermal/field_sync.py` | effective AMI 模式（含精确名） |
| `src/foam2thermal/templates.py` | impeller → `movingWallVelocity` |
| `scripts/fix_cyclic_ami_patches.py` | 按配置对升级 cyclicAMI |
