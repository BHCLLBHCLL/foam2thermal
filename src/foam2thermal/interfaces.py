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


def _patch_stem(name: str, suffix_re: re.Pattern[str]) -> str:
    return suffix_re.sub("", name)


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
) -> list[tuple[str, str]]:
    """Scan cgns2foam-style duplicate BC patches (``foo`` ↔ ``foo_1``).

    cgns2foam appends ``_1``, ``_2``, … when the same BC name appears in a
    later CGNS zone.  Interface candidates are pairs where exactly one side
    ends with ``_1`` and the other side is the stripped base name.
    """
    ami_patterns = ami_patterns or [r"ami_rot\d+"]
    exclude_set = set(exclude or [])
    suffix_re = re.compile(suffix_pattern)
    names = set(mesh.patch_names) - exclude_set

    pairs: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()

    for name in sorted(names):
        m = re.match(r"^(.+)_1$", name)
        if not m:
            continue
        base = m.group(1)
        if base not in names:
            continue
        key = frozenset({base, name})
        if key in seen:
            continue
        seen.add(key)
        pairs.append((base, name))

    return pairs


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
    elif type_m == "fluid" and type_s == "fluid":
        kind = InterfaceKind.FLUID_FLUID
        method = InterfaceMethod.STITCH
    elif type_m == "solid" and type_s == "solid":
        kind = InterfaceKind.SOLID_SOLID
        method = InterfaceMethod.STITCH
    elif {type_m, type_s} == {"fluid", "solid"}:
        kind = InterfaceKind.FLUID_SOLID
        method = InterfaceMethod.STITCH
    else:
        kind = InterfaceKind.FLUID_SOLID
        method = InterfaceMethod.STITCH

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
