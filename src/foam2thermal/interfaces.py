"""Detect and classify inter-region coupling interfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .mesh import MeshInfo


class InterfaceKind(str, Enum):
    FLUID_FLUID = "fluid_fluid"
    FLUID_SOLID = "fluid_solid"
    SOLID_SOLID = "solid_solid"


class InterfaceMethod(str, Enum):
    STITCH = "stitch"          # stitchMesh – coincident patch pairs (cgns2foam)
    CYCLIC_AMI = "cyclicAMI"   # rotating / sliding fluid-fluid
    MAPPED_WALL = "mappedWall"  # split-time mappedWall coupling (cross-region)
    SKIP = "skip"              # already internal or handled elsewhere


@dataclass
class InterfacePair:
    kind: InterfaceKind
    method: InterfaceMethod
    master: str
    slave: str
    region_a: str | None = None
    region_b: str | None = None
    note: str = ""



def is_ami_patch(name: str, ami_patterns: list[str]) -> bool:
    """True if *name* matches configured AMI patch patterns."""
    base = re.sub(r"_\d+$", "", name)
    for pat in ami_patterns:
        if re.fullmatch(pat, base) or re.fullmatch(pat, name):
            return True
    return False


def _is_ami_patch(name: str, ami_patterns: list[str]) -> bool:
    return is_ami_patch(name, ami_patterns)


def scan_cgns2foam_interfaces(
    mesh: MeshInfo,
    *,
    suffix_pattern: str = r"_\d+$",
    ami_patterns: list[str] | None = None,
    exclude: list[str] | None = None,
    patch_region: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Scan cgns2foam-style duplicate BC patches (``foo`` ↔ ``foo_1`` ↔ ``foo_2`` …).

    cgns2foam appends ``_1``, ``_2``, … when the same BC name appears in a
    later CGNS zone.  Interface candidates are **consecutive** suffix pairs
    within the same base stem (``foo``/``foo_1``, ``foo_1``/``foo_2``, …).
    """
    ami_patterns = ami_patterns or [r"ami_rot\d+"]
    exclude_set = set(exclude or [])
    suffix_re = re.compile(suffix_pattern)
    names = set(mesh.patch_names) - exclude_set

    groups: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        base, idx = _patch_suffix_index(name, suffix_re)
        groups.setdefault(base, []).append((idx, name))

    pairs: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()

    for base in sorted(groups):
        items = sorted(groups[base], key=lambda x: x[0])
        for i in range(len(items) - 1):
            master, slave = items[i][1], items[i + 1][1]
            if patch_region:
                reg_m = patch_region.get(master)
                reg_s = patch_region.get(slave)
                if reg_m and reg_s and reg_m == reg_s:
                    continue
            key = frozenset({master, slave})
            if key in seen:
                continue
            seen.add(key)
            pairs.append((master, slave))

    return pairs


def _patch_suffix_index(name: str, suffix_re: re.Pattern[str]) -> tuple[str, int]:
    m = re.search(r"_(\d+)$", name)
    if m:
        return name[: m.start()], int(m.group(1))
    return suffix_re.sub("", name), 0


def classify_interface(
    master: str,
    slave: str,
    *,
    patch_region: dict[str, str],
    resolve_region_type,
    ami_patterns: list[str] | None = None,
    method_override: str | None = None,
) -> InterfacePair:
    """Classify a patch pair and choose the OpenFOAM prep method."""
    ami_patterns = ami_patterns or [r"ami_rot\d+"]

    reg_m = patch_region.get(master)
    reg_s = patch_region.get(slave)
    type_m = resolve_region_type(reg_m) if reg_m else "unknown"
    type_s = resolve_region_type(reg_s) if reg_s else "unknown"

    if _is_ami_patch(master, ami_patterns) or _is_ami_patch(slave, ami_patterns):
        kind = InterfaceKind.FLUID_FLUID
        method = InterfaceMethod.CYCLIC_AMI
    elif reg_m and reg_s and reg_m == reg_s:
        kind = InterfaceKind.FLUID_FLUID if type_m == "fluid" else InterfaceKind.SOLID_SOLID
        method = InterfaceMethod.STITCH
    elif type_m == "fluid" and type_s == "fluid":
        kind = InterfaceKind.FLUID_FLUID
        method = InterfaceMethod.MAPPED_WALL
    elif type_m == "solid" and type_s == "solid":
        kind = InterfaceKind.SOLID_SOLID
        method = InterfaceMethod.MAPPED_WALL
    elif {type_m, type_s} == {"fluid", "solid"}:
        kind = InterfaceKind.FLUID_SOLID
        method = InterfaceMethod.MAPPED_WALL
    else:
        kind = InterfaceKind.FLUID_SOLID
        method = InterfaceMethod.MAPPED_WALL

    if method_override:
        method = InterfaceMethod(method_override)

    return InterfacePair(
        kind=kind,
        method=method,
        master=master,
        slave=slave,
        region_a=reg_m,
        region_b=reg_s,
    )


def build_interface_list(
    mesh: MeshInfo,
    cfg: dict[str, Any],
    resolve_region_type,
    patch_region: dict[str, str],
) -> list[InterfacePair]:
    """Build the full interface list from JSON (explicit + optional scan)."""
    iface_cfg = cfg.get("interfaces", {})
    ami_patterns = iface_cfg.get("ami_patterns", [r"ami_rot\d+"])
    exclude = iface_cfg.get("exclude", [])
    explicit = iface_cfg.get("explicit", [])

    pairs: list[tuple[str, str, str | None]] = []
    for item in explicit:
        pairs.append((item["master"], item["slave"], item.get("method")))

    if iface_cfg.get("auto_scan", True):
        for master, slave in scan_cgns2foam_interfaces(
            mesh,
            suffix_pattern=iface_cfg.get("suffix_pattern", r"_\d+$"),
            ami_patterns=ami_patterns,
            exclude=exclude,
            patch_region=patch_region,
        ):
            if any(m == master and s == slave for m, s, _ in pairs):
                continue
            pairs.append((master, slave, None))

    result: list[InterfacePair] = []
    for master, slave, method in pairs:
        result.append(
            classify_interface(
                master,
                slave,
                patch_region=patch_region,
                resolve_region_type=resolve_region_type,
                ami_patterns=ami_patterns,
                method_override=method,
            )
        )
    return result


def scan_interfaces_report(
    mesh: MeshInfo,
    case_dir: Path,
    cfg: dict[str, Any],
    *,
    resolve_region_type,
    patch_region: dict[str, str],
    region_properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full interface scan report for the ``scan`` CLI subcommand."""
    iface_cfg = cfg.get("interfaces", {})
    interfaces = build_interface_list(
        mesh, cfg, resolve_region_type, patch_region
    )
    return {
        "patches": mesh.patch_names,
        "cell_zones": [z.name for z in mesh.cell_zones],
        "region_properties": region_properties,
        "patch_regions": patch_region,
        "interfaces": [
            {
                "master": i.master,
                "slave": i.slave,
                "kind": i.kind.value,
                "method": i.method.value,
                "region_a": i.region_a,
                "region_b": i.region_b,
            }
            for i in interfaces
        ],
        "interface_pairs": [
            {"master": i.master, "slave": i.slave} for i in interfaces
        ],
    }
