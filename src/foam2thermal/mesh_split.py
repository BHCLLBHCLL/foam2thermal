"""Split a monolithic polyMesh into per-region meshes (Windows-safe fallback)."""

from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np

from .mesh import (
    CellZoneInfo,
    PatchInfo,
    mapped_wall_patch,
    parse_boundary,
    parse_cell_zones,
    write_boundary,
    write_cell_zones_v2412,
)
from .mesh_coalesce import (
    _read_faces,
    _read_binary_label_list,
    _read_binary_vector_field,
    _write_binary_compact_face_list,
    _write_binary_label_list,
    _write_binary_vector_field,
)

_LABEL_IO_COUNT = re.compile(rb"\n(\d+)\s*\n\(")


def _default_header(obj_class: str, obj_name: str, *, fmt: str = "binary") -> bytes:
    return (
        f"FoamFile\n{{\n    version 2.0;\n    format {fmt};\n"
        f"    class {obj_class};\n    object {obj_name};\n}}\n\n"
    ).encode("ascii")


def _default_boundary_header() -> str:
    return (
        "FoamFile\n{\n    version 2.0;\n    format binary;\n"
        "    class polyBoundaryMesh;\n    object boundary;\n}\n\n"
    )


def _read_label_io_list(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    m = _LABEL_IO_COUNT.search(raw)
    if not m:
        raise ValueError(f"Cannot parse label list in {path}")
    n = int(m.group(1))
    return np.frombuffer(raw, dtype="<i4", count=n, offset=m.end()).copy()


def _region_names_from_properties(case_dir: Path) -> list[str]:
    for rel in ("constant/regionProperties", "system/regionProperties"):
        rp = case_dir / rel
        if not rp.is_file():
            continue
        text = rp.read_text(encoding="utf-8", errors="replace")
        names: list[str] = []
        for block in ("fluid", "solid"):
            m = re.search(rf"{block}\s*\(\s*([^)]+)\)", text, flags=re.DOTALL)
            if not m:
                continue
            chunk = m.group(1).strip()
            quoted = re.findall(r'"([^"]+)"', chunk)
            names.extend(quoted if quoted else chunk.split())
        if names:
            return names
    raise FileNotFoundError("regionProperties not found")


def _cell_region_map(case_dir: Path, poly_dir: Path, n_cells: int) -> tuple[np.ndarray, list[str]]:
    ctr = case_dir / "constant" / "cellToRegion"
    if ctr.is_file():
        labels = _read_label_io_list(ctr)
        if labels.size != n_cells:
            raise ValueError(f"cellToRegion size {labels.size} != nCells {n_cells}")
        return labels.astype(np.int32), _region_names_from_properties(case_dir)

    zones = parse_cell_zones(poly_dir / "cellZones")
    if not zones:
        raise ValueError("cellZones empty or unreadable")
    cell_region = np.full(n_cells, -1, dtype=np.int32)
    names = [z.name for z in zones]
    for ri, z in enumerate(zones):
        labels = np.asarray(z.cell_labels, dtype=np.int64)
        valid = (labels >= 0) & (labels < n_cells)
        cell_region[labels[valid]] = ri
    if np.any(cell_region < 0):
        raise ValueError("Some cells are not in any cellZone")
    return cell_region, names


def _face_patch_map(patches: list[PatchInfo], n_faces: int) -> np.ndarray:
    out = np.full(n_faces, -1, dtype=np.int32)
    for pi, p in enumerate(patches):
        end = min(p.start_face + p.n_faces, n_faces)
        if end > p.start_face:
            out[p.start_face:end] = pi
    return out


def _region_cell_zones(
    orig_zones: list[CellZoneInfo],
    region_id: int,
    cell_region: np.ndarray,
    cell_map: dict[int, int],
) -> list[CellZoneInfo]:
    out: list[CellZoneInfo] = []
    for z in orig_zones:
        labels = sorted(
            cell_map[int(c)]
            for c in z.cell_labels
            if 0 <= int(c) < cell_region.size
            and int(cell_region[int(c)]) == region_id
            and int(c) in cell_map
        )
        if labels:
            out.append(CellZoneInfo(name=z.name, cell_labels=labels))
    return out


def _build_patch_pairs(case_dir: Path, patches: list[PatchInfo]) -> dict[str, tuple[str, str]]:
    """Map each named coupling patch to (paired_patch, remote_region).

    Reads ``config.json`` (written by build) for ``patch_regions`` and the
    ``interfaces`` config (explicit + auto-scan), then pairs cgns2foam-style
    ``foo`` ↔ ``foo_1`` boundary patches by name.  AMI pairs are excluded
    (handled by createPatch/fix_cyclic_ami).  Returns ``{}`` when config.json
    is absent (backward-compatible: splitter falls back to internal-face-only
    coupling, as before).
    """
    import json

    from .interfaces import is_ami_patch

    cfg_path = case_dir / "config.json"
    if not cfg_path.is_file():
        return {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    patch_regions = cfg.get("patch_regions", {})
    iface_cfg = cfg.get("interfaces", {})
    ami_patterns = iface_cfg.get("ami_patterns", [r"ami_rot\d+"])

    name_set = {p.name for p in patches}

    # Collect (master, slave) pairs from explicit list + auto-scan (_1 suffix)
    pairs: list[tuple[str, str]] = []
    for item in iface_cfg.get("explicit", []):
        pairs.append((item["master"], item["slave"]))
    if iface_cfg.get("auto_scan", True):
        for name in sorted(name_set):
            m = re.match(r"^(.+)_1$", name)
            if m and m.group(1) in name_set:
                pairs.append((m.group(1), name))

    patch_pairs: dict[str, tuple[str, str, str]] = {}
    for master, slave in pairs:
        if is_ami_patch(master, ami_patterns) or is_ami_patch(slave, ami_patterns):
            continue
        reg_m = patch_regions.get(master)
        reg_s = patch_regions.get(slave)
        if reg_m and reg_s and reg_m != reg_s:
            # Store (paired_patch, this_patch_config_region, other_patch_config_region).
            # The actual face owner may differ from config (cgns2foam _1 suffix
            # convention is not always "other side"), so we keep both regions
            # and pick the non-local one at split time.
            patch_pairs[master] = (slave, reg_m, reg_s)
            patch_pairs[slave] = (master, reg_s, reg_m)
    return patch_pairs


def _extract_region_mesh(
    region_id: int,
    region_name: str,
    region_names: list[str],
    points: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    owner: np.ndarray,
    nb: np.ndarray,
    patches: list[PatchInfo],
    face_patch: np.ndarray,
    cell_region: np.ndarray,
    orig_cell_zones: list[CellZoneInfo],
    patch_pairs: dict[str, tuple[str, str]] | None = None,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    list[PatchInfo],
    int,
    list[CellZoneInfo],
]:
    """Vectorised region extraction.

    Replaces the original 22M-face Python loop (which allocated one numpy
    array per face and exhausted memory on the air region) with numpy
    boolean masking + slice assignment.  Face vertex data is gathered via
    the ``np.repeat`` + ``np.arange`` trick so no per-face array allocation
    is needed.
    """
    import sys as _sys
    import time as _time
    _t0 = _time.time()
    n_faces = owner.size
    cell_mask = cell_region == region_id
    cells = np.where(cell_mask)[0]
    n_cells_region = cells.size
    print(f"[split] {region_name}: {n_cells_region} cells, {n_faces} faces", file=_sys.stderr, flush=True)

    # cell_map_arr[old_cell] -> new_cell_id (-1 if not in region)
    cell_map_arr = np.full(cell_region.size, -1, dtype=np.int64)
    cell_map_arr[cells] = np.arange(n_cells_region, dtype=np.int64)

    # Classify faces (vectorised).  owner always exists; nb may be -1.
    own_in = cell_mask[owner]
    valid_nb = nb >= 0
    nb_safe = np.where(valid_nb, nb, 0)
    nb_in = np.zeros(n_faces, dtype=bool)
    nb_in[valid_nb] = cell_mask[nb_safe[valid_nb]]

    internal_mask = own_in & valid_nb & nb_in
    boundary_mask = own_in & ~valid_nb
    coupling_out_mask = own_in & valid_nb & ~nb_in   # owner in, neighbour out
    coupling_in_mask = ~own_in & valid_nb & nb_in    # owner out, neighbour in

    # --- internal faces: order own < nei, flip verts if needed ---
    int_idx = np.where(internal_mask)[0]
    n_internal = int_idx.size
    int_own = owner[int_idx]
    int_nei = nb[int_idx]
    flip_int = int_own > int_nei
    int_own_o = np.where(flip_int, int_nei, int_own).astype(np.int64)
    int_nei_o = np.where(flip_int, int_own, int_nei).astype(np.int64)
    # Sort by (own, nei)
    sort_key = int_own_o * np.int64(cell_region.size + 1) + int_nei_o
    sort_order = np.argsort(sort_key, kind="mergesort")
    int_idx = int_idx[sort_order]
    int_own_o = int_own_o[sort_order]
    int_nei_o = int_nei_o[sort_order]
    flip_int = flip_int[sort_order]

    # --- boundary faces: group by patch (preserve patch order) ---
    # Paired boundary patches (cgns2foam foo/foo_1 naming) are routed to
    # named coupling groups -> mappedWall, NOT regular boundary patches.
    pp = patch_pairs or {}
    bnd_face_idx = np.where(boundary_mask)[0]
    bnd_patch_id = face_patch[bnd_face_idx]
    bnd_groups: list[tuple[str, np.ndarray]] = []
    named_coup_faces: dict[str, np.ndarray] = {}
    for pi, p in enumerate(patches):
        sel = bnd_face_idx[bnd_patch_id == pi]
        if not sel.size:
            continue
        if p.name in pp:
            # patch_pairs[p.name] = (paired_patch, this_config_region, other_config_region).
            # Pick remote = the region that is NOT the local region. This handles
            # the case where actual face ownership differs from the config mapping.
            _, reg_a, reg_b = pp[p.name]
            if region_name == reg_a:
                remote_region = reg_b
            elif region_name == reg_b:
                remote_region = reg_a
            else:
                # Local region is neither of the config regions — fall back to other.
                remote_region = reg_b
            key = f"{region_name}_to_{remote_region}"
            named_coup_faces[key] = (
                np.concatenate([named_coup_faces[key], sel])
                if key in named_coup_faces
                else sel
            )
        else:
            bnd_groups.append((p.name, sel))

    # --- coupling faces: group by remote region name (sorted for determinism) ---
    coup_out_idx = np.where(coupling_out_mask)[0]
    coup_out_remote = cell_region[nb[coup_out_idx]]
    coup_in_idx = np.where(coupling_in_mask)[0]
    coup_in_remote = cell_region[owner[coup_in_idx]]

    coup_groups: dict[str, np.ndarray] = {}  # key -> positions in all_coup arrays
    # Combine out + in (in faces need vert flip)
    all_coup_remote = np.concatenate([coup_out_remote, coup_in_remote])
    all_coup_idx = np.concatenate([coup_out_idx, coup_in_idx])
    coup_flip = np.zeros(all_coup_idx.size, dtype=bool)
    coup_flip[coup_out_idx.size:] = True  # coupling-in faces need flip
    # Append named coupling (pre-paired boundary patches, no mesh merge).
    # These are boundary faces: owner=owner[fi], nb=-1, no flip needed.
    import sys as _sys
    print(f"[split] {region_name}: named_coup_faces keys={list(named_coup_faces.keys())}", file=_sys.stderr, flush=True)
    for key, sel in named_coup_faces.items():
        remote_name = key[len(region_name) + 4 :]  # strip "<region>_to_"
        print(f"[split] {region_name}: named key={key} remote_name={remote_name!r} in_names={remote_name in region_names}", file=_sys.stderr, flush=True)
        if remote_name not in region_names:
            continue
        remote_id = region_names.index(remote_name)
        all_coup_remote = np.concatenate([
            all_coup_remote,
            np.full(sel.size, remote_id, dtype=np.int32),
        ])
        all_coup_idx = np.concatenate([all_coup_idx, sel])
        coup_flip = np.concatenate([coup_flip, np.zeros(sel.size, dtype=bool)])
    for rid in np.unique(all_coup_remote):
        if rid < 0 or rid >= len(region_names):
            continue
        rname = region_names[int(rid)]
        key = f"{region_name}_to_{rname}"
        coup_groups[key] = np.where(all_coup_remote == rid)[0]  # positions
    # Coupling face owner = the cell in THIS region
    # For coup_out: owner is owner[fi]; for coup_in: owner is nb[fi]
    coup_owner = np.where(
        coup_flip,
        nb[all_coup_idx],
        owner[all_coup_idx],
    )

    # --- Build ordered face index list ---
    # Order: [internal_sorted, boundary_per_patch, coupling_sorted]
    bnd_idx_concat = np.concatenate([sel for _, sel in bnd_groups]) if bnd_groups else np.empty(0, dtype=np.int64)
    coup_pos_concat = np.concatenate([coup_groups[k] for k in sorted(coup_groups.keys())]) if coup_groups else np.empty(0, dtype=np.int64)
    coup_idx_concat = all_coup_idx[coup_pos_concat] if coup_pos_concat.size else np.empty(0, dtype=np.int64)
    coup_flip_in_order = coup_flip[coup_pos_concat] if coup_pos_concat.size else np.empty(0, dtype=bool)
    coup_owner_in_order = coup_owner[coup_pos_concat] if coup_pos_concat.size else np.empty(0, dtype=np.int64)
    # Flip flags per ordered face
    flip_flags_int = flip_int  # bool[n_internal]
    flip_flags_bnd = np.zeros(bnd_idx_concat.size, dtype=bool)

    face_order = np.concatenate([int_idx, bnd_idx_concat, coup_idx_concat])
    n_out = face_order.size
    flip_all = np.concatenate([flip_flags_int, flip_flags_bnd, coup_flip_in_order])

    # --- Build new_owner / new_nb ---
    new_owner = np.empty(n_out, dtype=np.int32)
    new_nb = np.full(n_out, -1, dtype=np.int32)
    # Internal: own/nei from int_own_o/int_nei_o
    new_owner[:n_internal] = cell_map_arr[int_own_o]
    new_nb[:n_internal] = cell_map_arr[int_nei_o]
    # Boundary: owner = original owner, remapped
    if bnd_idx_concat.size:
        new_owner[n_internal:n_internal + bnd_idx_concat.size] = cell_map_arr[owner[bnd_idx_concat]]
    # Coupling: owner from coup_owner_in_order
    if coup_idx_concat.size:
        new_owner[n_internal + bnd_idx_concat.size:] = cell_map_arr[coup_owner_in_order]

    print(f"[split] {region_name}: classified {n_internal} internal, {bnd_idx_concat.size} boundary, {coup_idx_concat.size} coupling faces ({_time.time()-_t0:.1f}s)", file=_sys.stderr, flush=True)

    # --- Build new_conn (vectorised via repeat+arange trick) ---
    fo = face_order
    starts = offsets[fo].astype(np.int64)
    ends = offsets[fo + 1].astype(np.int64)
    sizes = ends - starts
    new_offsets = np.zeros(n_out + 1, dtype=np.int32)
    np.cumsum(sizes, out=new_offsets[1:])
    total_verts = int(new_offsets[-1])
    # Repeat per-face values to per-vertex
    starts_per_vert = np.repeat(starts, sizes)
    sizes_per_vert = np.repeat(sizes, sizes)
    flip_per_vert = np.repeat(flip_all, sizes)
    new_off_per_vert = np.repeat(new_offsets[:-1].astype(np.int64), sizes)
    # Within-face position: 0, 1, ..., sizes[i]-1
    within = np.arange(total_verts, dtype=np.int64) - new_off_per_vert
    # For flipped faces, reverse the within-face order
    within_final = np.where(flip_per_vert, sizes_per_vert - 1 - within, within)
    src_indices = starts_per_vert + within_final
    # Gather raw vertices
    raw_verts = conn[src_indices]

    # --- Build point map (vectorised) ---
    used_pts = np.unique(raw_verts)
    pt_map_arr = np.full(int(raw_verts.max()) + 1 if raw_verts.size else 1, -1, dtype=np.int32)
    pt_map_arr[used_pts] = np.arange(used_pts.size, dtype=np.int32)
    new_conn = pt_map_arr[raw_verts].astype(np.int32)
    new_points = points[used_pts]

    print(f"[split] {region_name}: built conn ({total_verts} verts, {used_pts.size} unique pts) ({_time.time()-_t0:.1f}s)", file=_sys.stderr, flush=True)

    # --- Build final_patches list ---
    final_patches: list[PatchInfo] = []
    cursor = n_internal
    for p in patches:
        sel = next((s for name, s in bnd_groups if name == p.name), None)
        if sel is None:
            continue
        final_patches.append(
            PatchInfo(
                name=p.name,
                patch_type=p.patch_type,
                n_faces=sel.size,
                start_face=cursor,
                sample_mode=p.sample_mode,
                sample_region=p.sample_region,
                sample_patch=p.sample_patch,
                neighbour_patch=p.neighbour_patch,
                rotation_axis=p.rotation_axis,
                match_tolerance=p.match_tolerance,
                transform=p.transform,
            )
        )
        cursor += sel.size
    for pname in sorted(coup_groups.keys()):
        sel = coup_groups[pname]
        remote = pname[len(region_name) + 4 :]
        final_patches.append(
            mapped_wall_patch(region_name, remote, n_faces=sel.size, start_face=cursor)
        )
        cursor += sel.size

    # cell_map dict for _region_cell_zones (zones are small, dict is fine)
    cell_map_dict = {int(c): i for i, c in enumerate(cells.tolist())}
    print(f"[split] {region_name}: building cellZones ({_time.time()-_t0:.1f}s)", file=_sys.stderr, flush=True)
    region_zones = _region_cell_zones(orig_cell_zones, region_id, cell_region, cell_map_dict)
    print(f"[split] {region_name}: {len(region_zones)} cellZones, done ({_time.time()-_t0:.1f}s)", file=_sys.stderr, flush=True)
    return new_points, new_owner, new_nb, new_offsets, new_conn, final_patches, n_internal, region_zones


def _write_region_poly(
    poly_dir: Path,
    points: np.ndarray,
    owner: np.ndarray,
    nb: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    patches: list[PatchInfo],
    n_internal: int,
    cell_zones: list[CellZoneInfo] | None = None,
) -> None:
    poly_dir.mkdir(parents=True, exist_ok=True)
    _write_binary_vector_field(poly_dir / "points", points, _default_header("vectorField", "points"))
    _write_binary_compact_face_list(
        poly_dir / "faces", offsets, conn, _default_header("faceCompactList", "faces")
    )
    _write_binary_label_list(poly_dir / "owner", owner.astype(np.int32), _default_header("labelList", "owner"))
    _write_binary_label_list(
        poly_dir / "neighbour",
        nb[:n_internal].astype(np.int32),
        _default_header("labelList", "neighbour"),
    )
    write_boundary(poly_dir / "boundary", patches, _default_boundary_header())
    if cell_zones:
        write_cell_zones_v2412(poly_dir / "cellZones", cell_zones)


def interface_neighbors(case_dir: Path) -> dict[str, list[str]]:
    """Map each region name to coupled neighbour region names (from monolithic mesh).

    Vectorised: uses numpy boolean indexing instead of a Python loop over all
    faces (O(nFaces) ~ 22M), which previously made ``build`` appear to hang.
    """
    poly = case_dir / "constant" / "polyMesh"
    if not poly.is_dir():
        return {}
    owner = _read_binary_label_list(poly / "owner")
    nb_raw = _read_binary_label_list(poly / "neighbour")
    n_cells = int(owner.max()) + 1
    cell_region, names = _cell_region_map(case_dir, poly, n_cells)
    nb = np.full(owner.size, -1, dtype=np.int32)
    nb[: nb_raw.size] = nb_raw

    # Vectorised: select only internal faces (nb >= 0) and compute region pairs.
    valid = nb >= 0
    o_arr = owner[valid]
    n_arr = nb[valid]
    ro = cell_region[o_arr]
    rn = cell_region[n_arr]
    # Keep only cross-region pairs with both regions valid.
    keep = (ro != rn) & (ro >= 0) & (rn >= 0)
    ro = ro[keep]
    rn = rn[keep]
    if ro.size > 0:
        pairs = np.stack([np.minimum(ro, rn), np.maximum(ro, rn)], axis=1)
        unique_pairs = np.unique(pairs, axis=0)
    else:
        unique_pairs = np.empty((0, 2), dtype=np.int32)

    neighbors: dict[str, set[str]] = {n: set() for n in names}
    for a, b in unique_pairs.tolist():
        neighbors[names[int(a)]].add(names[int(b)])
        neighbors[names[int(b)]].add(names[int(a)])
    return {k: sorted(v) for k, v in neighbors.items()}


def field_patches_for_region(
    region_foam_name: str,
    *,
    config_name: str,
    monolithic_patch_names: list[str],
    patch_region: dict[str, str],
    neighbors: dict[str, list[str]],
) -> list[str]:
    """Patch names for 0/ fields after region split."""
    ext = [p for p in monolithic_patch_names if patch_region.get(p) == config_name]
    coupled = [f"{region_foam_name}_to_{nbr}" for nbr in neighbors.get(region_foam_name, [])]
    return ext + sorted(coupled)


def split_mesh_regions(case_dir: Path, region_names: list[str] | None = None) -> dict:
    """Write constant/<region>/polyMesh for each region; remove monolithic polyMesh."""
    poly = case_dir / "constant" / "polyMesh"
    points = _read_binary_vector_field(poly / "points")
    offsets, conn = _read_faces(poly / "faces")
    owner = _read_binary_label_list(poly / "owner")
    nb_raw = _read_binary_label_list(poly / "neighbour")
    patches = parse_boundary(poly / "boundary")
    n_faces = owner.size
    nb = np.full(n_faces, -1, dtype=np.int32)
    nb[: nb_raw.size] = nb_raw
    face_patch = _face_patch_map(patches, n_faces)

    n_cells = int(owner.max()) + 1
    cell_region, names = _cell_region_map(case_dir, poly, n_cells)
    orig_cell_zones = parse_cell_zones(poly / "cellZones")
    patch_pairs = _build_patch_pairs(case_dir, patches)
    import sys as _sys
    print(f"[split] n_cells={n_cells}, n_zones={len(orig_cell_zones)}, patch_pairs={len(patch_pairs)}", file=_sys.stderr, flush=True)
    for z in orig_cell_zones:
        print(f"[split]   zone '{z.name}': {len(z.cell_labels)} cells", file=_sys.stderr, flush=True)
    for pn, (qn, ra, rb) in sorted(patch_pairs.items()):
        print(f"[split]   named pair: {pn} <-> {qn} (config_regions={ra},{rb})", file=_sys.stderr, flush=True)
    if region_names:
        names = list(region_names)

    report: dict = {"regions": {}, "method": "python_split", "named_pairs": len(patch_pairs)}
    for ri, rname in enumerate(names):
        mesh = _extract_region_mesh(
            ri,
            rname,
            names,
            points,
            offsets,
            conn,
            owner,
            nb,
            patches,
            face_patch,
            cell_region,
            orig_cell_zones,
            patch_pairs=patch_pairs,
        )
        out_poly = case_dir / "constant" / rname / "polyMesh"
        if out_poly.exists():
            shutil.rmtree(out_poly)
        _write_region_poly(out_poly, *mesh[:7], mesh[7])
        report["regions"][rname] = {
            "cells": int(np.sum(cell_region == ri)),
            "faces": int(mesh[1].size),
            "internal_faces": mesh[6],
            "patches": len(mesh[5]),
            "cell_zones": len(mesh[7]),
        }

    shutil.rmtree(poly)
    return report
