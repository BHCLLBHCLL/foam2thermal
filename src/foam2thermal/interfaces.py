"""Detect and classify inter-region coupling interfaces."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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


def _looks_like_rotation(name: str | None) -> bool:
    if not name:
        return False
    return "rotation" in name.lower()


def _patch_suffix_index(name: str, suffix_re: re.Pattern[str]) -> tuple[str, int]:
    m = re.search(r"_(\d+)$", name)
    if m:
        return name[: m.start()], int(m.group(1))
    return suffix_re.sub("", name), 0


def scan_suffix_interfaces(
    mesh: MeshInfo,
    *,
    suffix_pattern: str = r"_\d+$",
    exclude: list[str] | None = None,
    patch_region: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Pair ``foo`` ↔ ``foo_1`` ↔ ``foo_2`` … by consecutive suffix index."""
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


_NOISE_TOKENS = frozenset(
    {
        "partsurface",
        "laptop",
        "3d",
        "geom",
        "solid",
        "region",
        "fphparts",
        "domain",
    }
)


def _name_tokens(name: str) -> set[str]:
    parts = re.split(r"[._]+", name.lower())
    return {p for p in parts if p and not p.isdigit() and p not in _NOISE_TOKENS}


def _pair_name_score(a: str, b: str, ra: str, rb: str) -> int:
    """Score how likely *a*/*b* form a physical interface by name↔region tokens.

    BCs_fix naming puts the *remote* zone stem in the patch name, e.g.
    ``_PartSurface_case2`` (owned by air) faces ``_PartSurface_air_domain``
    (owned by case2).
    """
    na, nb = _name_tokens(a), _name_tokens(b)
    ta, tb = _name_tokens(ra), _name_tokens(rb)
    score = 0
    if na & tb:
        score += 2
    if nb & ta:
        score += 2
    if (na & tb) and (nb & ta):
        score += 2
    return score


def scan_face_count_interfaces(
    mesh: MeshInfo,
    *,
    patch_region: dict[str, str],
    exclude: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Pair patches with equal ``nFaces`` owned by different regions.

    Handles BCs_fix-style naming where sides of the same interface have
    unrelated stems (``_PartSurface_Cu_block`` ↔ ``_PartSurface_air_domain_3``).
    When more than two patches share a face count, name↔region token scoring
    picks the most likely pairs and skips zero-score matches (e.g. dual
    impellers with equal face counts).
    """
    exclude_set = set(exclude or [])
    by_count: dict[int, list[str]] = defaultdict(list)
    for p in mesh.patches:
        if p.name in exclude_set or p.n_faces <= 0:
            continue
        # Impeller blades are walls, not coupling interfaces.
        if "impeller" in p.name.lower():
            continue
        by_count[p.n_faces].append(p.name)

    pairs: list[tuple[str, str]] = []
    for n_faces in sorted(by_count):
        names = by_count[n_faces]
        if len(names) < 2:
            continue
        remaining = set(names)
        while len(remaining) >= 2:
            best: tuple[int, str, str] | None = None
            for a in remaining:
                ra = patch_region.get(a)
                if not ra:
                    continue
                for b in remaining:
                    if a >= b:
                        continue
                    rb = patch_region.get(b)
                    if not rb or ra == rb:
                        continue
                    score = _pair_name_score(a, b, ra, rb)
                    if best is None or score > best[0]:
                        best = (score, a, b)
            if best is None or best[0] <= 0:
                break
            _, a, b = best
            master, slave = (a, b) if a < b else (b, a)
            pairs.append((master, slave))
            remaining.discard(a)
            remaining.discard(b)
    return pairs


def scan_cgns2foam_interfaces(
    mesh: MeshInfo,
    *,
    suffix_pattern: str = r"_\d+$",
    ami_patterns: list[str] | None = None,
    exclude: list[str] | None = None,
    patch_region: dict[str, str] | None = None,
    suffix_face_ratio_max: float = 1.15,
) -> list[tuple[str, str]]:
    """Scan interface patch pairs (equal-nFaces topology + filtered suffix).

    1. **Face-count** (preferred when ``patch_region`` is available) – equal
       ``nFaces`` + name↔region token scoring (BCs_fix / PartSurface naming).
    2. **Suffix chain** – classic ``foo`` / ``foo_1``; only added when face
       counts are within ``suffix_face_ratio_max`` and neither side is already
       claimed by a face-count pair (avoids false Cover↔Cover_1 chains).
    """
    _ = ami_patterns  # kept for API compatibility with callers
    patch_by_name = {p.name: p for p in mesh.patches}
    pairs: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    claimed: set[str] = set()

    if patch_region:
        for master, slave in scan_face_count_interfaces(
            mesh, patch_region=patch_region, exclude=exclude
        ):
            key = frozenset({master, slave})
            if key in seen:
                continue
            seen.add(key)
            claimed.add(master)
            claimed.add(slave)
            pairs.append((master, slave))

    for master, slave in scan_suffix_interfaces(
        mesh,
        suffix_pattern=suffix_pattern,
        exclude=exclude,
        patch_region=patch_region,
    ):
        key = frozenset({master, slave})
        if key in seen:
            continue
        if master in claimed or slave in claimed:
            continue
        pa, pb = patch_by_name.get(master), patch_by_name.get(slave)
        if pa and pb and min(pa.n_faces, pb.n_faces) > 0:
            ratio = max(pa.n_faces, pb.n_faces) / min(pa.n_faces, pb.n_faces)
            if ratio > suffix_face_ratio_max:
                continue
        seen.add(key)
        claimed.add(master)
        claimed.add(slave)
        pairs.append((master, slave))
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
    ami_patterns = ami_patterns or [r"ami_rot\d+", r".*[Rr]otation\d*"]

    reg_m = patch_region.get(master)
    reg_s = patch_region.get(slave)
    type_m = resolve_region_type(reg_m) if reg_m else "unknown"
    type_s = resolve_region_type(reg_s) if reg_s else "unknown"

    ami_hit = (
        _is_ami_patch(master, ami_patterns)
        or _is_ami_patch(slave, ami_patterns)
        or _looks_like_rotation(master)
        or _looks_like_rotation(slave)
        or _looks_like_rotation(reg_m)
        or _looks_like_rotation(reg_s)
    )

    if ami_hit and type_m == "fluid" and type_s == "fluid":
        kind = InterfaceKind.FLUID_FLUID
        method = InterfaceMethod.CYCLIC_AMI
    elif ami_hit:
        # Name looks like AMI but region types incomplete – still treat as AMI.
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
    ami_patterns = iface_cfg.get(
        "ami_patterns", [r"ami_rot\d+", r".*[Rr]otation\d*"]
    )
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
            if any(
                {m, s} == {master, slave} for m, s, _ in pairs
            ):
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
