"""Coalesce cgns2foam zone-interface boundary face pairs into internal faces."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from .mesh import PatchInfo, parse_boundary, parse_cell_zones, write_boundary

_DATA_COUNT_RE = re.compile(rb"\n(\d+)\s*\n\(")


def _data_offset(raw: bytes) -> tuple[int, int]:
    m = _DATA_COUNT_RE.search(raw)
    if not m:
        raise ValueError("Cannot locate OpenFOAM list size in mesh file")
    return int(m.group(1)), m.end()


def _read_header_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    m = _DATA_COUNT_RE.search(raw)
    if not m:
        raise ValueError(f"Cannot locate data block in {path}")
    return raw[: m.start() + 1]


def _read_binary_vector_field(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    n, pos = _data_offset(raw)
    return np.frombuffer(raw, dtype="<f8", count=n * 3, offset=pos).reshape(n, 3).copy()


def _read_binary_label_list(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    n, pos = _data_offset(raw)
    return np.frombuffer(raw, dtype="<i4", count=n, offset=pos).copy()


def _read_ascii_face_list(path: Path) -> tuple[np.ndarray, np.ndarray]:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"(\d+)\s*\(", text)
    if not m:
        raise ValueError(f"Cannot parse face count in {path}")
    n_faces = int(m.group(1))
    offsets = np.zeros(n_faces + 1, dtype=np.int32)
    conn: list[int] = []
    idx = 0
    for fm in re.finditer(r"(\d+)\(([^)]*)\)", text[m.end() :]):
        if idx >= n_faces:
            break
        verts = [int(x) for x in fm.group(2).split() if x]
        offsets[idx] = len(conn)
        conn.extend(verts)
        idx += 1
    offsets[n_faces] = len(conn)
    if idx != n_faces:
        raise ValueError(f"Expected {n_faces} faces, parsed {idx} in {path}")
    return offsets, np.asarray(conn, dtype=np.int32)


def _faces_format(path: Path) -> str:
    raw = path.read_bytes()
    m = re.search(rb"format\s+(\w+)\s*;", raw)
    return m.group(1).decode("ascii") if m else "binary"


def _repair_compact_offsets(ofs: np.ndarray, conn_len: int) -> np.ndarray:
    """Fix non-monotonic offset entries from cgns2foam faceCompactList exports."""
    out = ofs.copy()
    i = 0
    while i < len(out) - 1:
        if int(out[i + 1]) > int(out[i]):
            i += 1
            continue
        j = i + 2
        while j < len(out) and int(out[j]) <= int(out[i]):
            j += 1
        if j >= len(out):
            break
        span = int(out[j]) - int(out[i])
        gap = j - i
        if gap <= 0 or span <= 0:
            break
        step = span // gap
        for k in range(i + 1, j):
            out[k] = int(out[i]) + step * (k - i)
        i += 1
    if int(out[-1]) != conn_len:
        out[-1] = conn_len
    return out


def _read_binary_compact_face_list(path: Path) -> tuple[np.ndarray, np.ndarray]:
    raw = path.read_bytes()
    n_ofs, pos = _data_offset(raw)
    ofs = np.frombuffer(raw, dtype="<i4", count=n_ofs, offset=pos).copy()
    pos2 = pos + 4 * n_ofs
    while pos2 < len(raw) and raw[pos2 : pos2 + 1] in b")\n\r\t ":
        pos2 += 1
    nl = raw.find(b"\n", pos2)
    n_conn = int(raw[pos2:nl].decode().strip())
    paren = raw.find(b"(", nl)
    conn = np.frombuffer(raw, dtype="<i4", count=n_conn, offset=paren + 1).copy()
    if ofs.size < 2 or not np.all(ofs[1:] >= ofs[:-1]) or int(ofs[-1]) != n_conn:
        ofs = _repair_compact_offsets(ofs, n_conn)
    return ofs, conn


def _read_faces(path: Path) -> tuple[np.ndarray, np.ndarray]:
    if _faces_format(path) == "ascii":
        return _read_ascii_face_list(path)
    return _read_binary_compact_face_list(path)


def _merge_points(points: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (merged_points, old_to_new map).

    Groups points by their rounded grid cell (``round(p/tol)``) using a
    collision-free packed key. Each group of points sharing the same grid
    cell is merged into a single representative point.

    Note: points up to ~2*tol apart can be merged if they straddle a grid
    cell boundary, but points in different grid cells are never merged.
    """
    if tol <= 0:
        return points, np.arange(len(points), dtype=np.int32)
    inv = np.round(points / tol).astype(np.int64)
    # Offset to non-negative so we can pack into a single 64-bit key without
    # collisions. Each coordinate is shifted to [0, +inf) and packed as
    # x * 2^42 + y * 2^21 + z, which is collision-free as long as each
    # coordinate fits in 21 bits (range 0..2,097,151).
    inv_offset = inv - inv.min(axis=0)
    max_val = int(inv_offset.max())
    if max_val < (1 << 21):
        keys = inv_offset[:, 0].astype(np.int64) * (1 << 42) + inv_offset[:, 1].astype(np.int64) * (1 << 21) + inv_offset[:, 2].astype(np.int64)
    else:
        # Fall back to a structured-array key for very large grids.
        dt = np.dtype([("x", np.int64), ("y", np.int64), ("z", np.int64)])
        grid = np.empty(inv_offset.shape[0], dtype=dt)
        grid["x"] = inv_offset[:, 0]
        grid["y"] = inv_offset[:, 1]
        grid["z"] = inv_offset[:, 2]
        keys = grid
    order = np.argsort(keys, kind="mergesort")
    if isinstance(keys, np.ndarray):
        keys_sorted = keys[order]
    else:
        keys_sorted = keys[order]
    _, first_idx = np.unique(keys_sorted, return_index=True)
    new_n = first_idx.size
    old_to_new = np.empty(len(points), dtype=np.int32)
    new_points = np.empty((new_n, 3), dtype=np.float64)
    for ui, start in enumerate(first_idx):
        end = first_idx[ui + 1] if ui + 1 < first_idx.size else order.size
        members = order[start:end]
        rep = int(members[0])
        new_points[ui] = points[rep]
        old_to_new[members] = ui
    return new_points, old_to_new


def _face_vertex_key(offsets: np.ndarray, conn: np.ndarray, fi: int) -> tuple[int, ...] | None:
    s, e = int(offsets[fi]), int(offsets[fi + 1])
    idx = conn[s:e]
    if idx.size < 3:
        return None
    return tuple(sorted(int(v) for v in idx))


def _boundary_header_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"(\d+)\s*\(\s*$", text, flags=re.MULTILINE)
    if not m:
        raise ValueError(f"Cannot locate patch count in {path}")
    return text[: m.start()]


def _write_ascii_face_list(path: Path, offsets: np.ndarray, conn: np.ndarray, header: bytes) -> None:
    n_faces = offsets.size - 1
    chunks: list[str] = []
    chunk: list[str] = []
    for fi in range(n_faces):
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        verts = conn[s:e]
        chunk.append(f"{verts.size}(" + " ".join(str(int(v)) for v in verts) + ")\n")
        if len(chunk) >= 50000:
            chunks.append("".join(chunk))
            chunk.clear()
    if chunk:
        chunks.append("".join(chunk))
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(f"{n_faces}\n(\n".encode("ascii"))
        for part in chunks:
            fh.write(part.encode("ascii"))
        fh.write(b")\n")


def _write_binary_compact_face_list(
    path: Path, offsets: np.ndarray, conn: np.ndarray, header: bytes
) -> None:
    ofs = np.ascontiguousarray(offsets, dtype=np.int32)
    con = np.ascontiguousarray(conn, dtype=np.int32)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(f"{ofs.size}\n(".encode("ascii"))
        fh.write(ofs.tobytes(order="C"))
        fh.write(b")")
        fh.write(f"{con.size}\n(".encode("ascii"))
        fh.write(con.tobytes(order="C"))
        fh.write(b")\n")


def _write_binary_vector_field(path: Path, points: np.ndarray, header: bytes) -> None:
    arr = np.ascontiguousarray(points, dtype=np.float64)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(f"{arr.shape[0]}\n(".encode("ascii"))
        fh.write(arr.tobytes(order="C"))
        fh.write(b")\n")


def _write_binary_label_list(path: Path, values: np.ndarray, header: bytes) -> None:
    arr = np.ascontiguousarray(values, dtype=np.int32)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(f"{arr.size}\n(".encode("ascii"))
        fh.write(arr.tobytes(order="C"))
        fh.write(b")\n")


def _compact_mesh(
    owner: np.ndarray,
    nb: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    old_patch_of: np.ndarray,
    remove: np.ndarray,
    patches: list[PatchInfo],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[PatchInfo], int]:
    n_faces = owner.size
    keep_old = np.where(~remove)[0]
    n_keep = keep_old.size

    mid_offsets = np.zeros(n_keep + 1, dtype=np.int32)
    mid_conn_parts: list[np.ndarray] = []
    pos = 0
    for mi, old_fi in enumerate(keep_old):
        s, e = int(offsets[old_fi]), int(offsets[old_fi + 1])
        part = conn[s:e].copy()
        mid_offsets[mi] = pos
        mid_conn_parts.append(part)
        pos += part.size
    mid_offsets[n_keep] = pos
    mid_conn = np.concatenate(mid_conn_parts) if mid_conn_parts else np.empty(0, dtype=np.int32)
    mid_owner = owner[keep_old]
    mid_nb = nb[keep_old]
    mid_patch = old_patch_of[keep_old]

    is_internal = mid_nb >= 0
    int_ids = np.where(is_internal)[0]
    bnd_ids = np.where(~is_internal)[0]
    sort_key = mid_owner[int_ids].astype(np.int64) * (int(mid_owner.max()) + 2) + mid_nb[int_ids]
    int_order = int_ids[np.argsort(sort_key, kind="stable")]

    patch_type = {p.name: p.patch_type for p in patches}
    bnd_by_patch: dict[str, list[int]] = {p.name: [] for p in patches}
    for mid_fi in bnd_ids:
        pname = str(mid_patch[mid_fi])
        if pname in bnd_by_patch:
            bnd_by_patch[pname].append(int(mid_fi))

    bnd_order: list[int] = []
    for p in patches:
        bnd_order.extend(bnd_by_patch.get(p.name, []))
    included = set(int_order.tolist()) | set(bnd_order)
    for mid_fi in bnd_ids:
        mid_fi = int(mid_fi)
        if mid_fi in included:
            continue
        pname = str(mid_patch[mid_fi])
        bnd_by_patch.setdefault(pname, []).append(mid_fi)
        bnd_order.append(mid_fi)
    final_order = np.concatenate([int_order, np.asarray(bnd_order, dtype=np.int32)])
    n_final = int(final_order.size)

    final_owner = mid_owner[final_order]
    final_nb = mid_nb[final_order]
    n_internal = int(int_order.size)

    final_offsets = np.zeros(n_final + 1, dtype=np.int32)
    final_conn_parts: list[np.ndarray] = []
    pos = 0
    for fi, mid_fi in enumerate(final_order):
        s, e = int(mid_offsets[mid_fi]), int(mid_offsets[mid_fi + 1])
        part = mid_conn[s:e]
        final_offsets[fi] = pos
        final_conn_parts.append(part)
        pos += part.size
    final_offsets[n_final] = pos
    final_conn = np.concatenate(final_conn_parts) if final_conn_parts else np.empty(0, dtype=np.int32)

    final_patches: list[PatchInfo] = []
    cursor = n_internal
    patch_names: list[str] = []
    for p in patches:
        if bnd_by_patch.get(p.name):
            patch_names.append(p.name)
    for pname in bnd_by_patch:
        if pname not in patch_names and bnd_by_patch[pname]:
            patch_names.append(pname)
    for pname in patch_names:
        n_pf = len(bnd_by_patch[pname])
        final_patches.append(
            PatchInfo(
                name=pname,
                patch_type=patch_type.get(pname, "wall"),
                n_faces=n_pf,
                start_face=cursor,
            )
        )
        cursor += n_pf

    return final_owner, final_nb, final_offsets, final_conn, final_patches, n_internal


def _patch_excluded(name: str, exclude_re: list[re.Pattern]) -> bool:
    return any(rx.search(name) for rx in exclude_re)


def coalesce_zone_interfaces(
    poly_dir: Path,
    *,
    point_tol: float = 1e-4,
    exclude_patterns: list[str] | None = None,
) -> dict:
    """Merge coincident inter-zone boundary faces into internal faces."""
    exclude_re = [re.compile(p) for p in (exclude_patterns or [r"ami_rot"])]
    points_path = poly_dir / "points"
    faces_path = poly_dir / "faces"
    owner_path = poly_dir / "owner"
    neighbour_path = poly_dir / "neighbour"
    boundary_path = poly_dir / "boundary"

    n_points_before = _read_binary_vector_field(points_path).shape[0]
    points = _read_binary_vector_field(points_path)
    faces_fmt = _faces_format(faces_path)
    offsets, conn = _read_faces(faces_path)
    owner = _read_binary_label_list(owner_path).copy()
    neighbour_raw = _read_binary_label_list(neighbour_path)
    patches = parse_boundary(boundary_path)
    zones = (
        parse_cell_zones(poly_dir / "cellZones")
        if (poly_dir / "cellZones").is_file()
        else []
    )

    n_faces = owner.size
    nb = np.full(n_faces, -1, dtype=np.int32)
    if neighbour_raw.size == n_faces:
        nb[:] = neighbour_raw
    else:
        nb[: neighbour_raw.size] = neighbour_raw

    n_cells = int(owner.max()) + 1 if owner.size else 0
    cell_zone = np.full(max(n_cells, 1), -1, dtype=np.int32)
    for zi, z in enumerate(zones):
        for c in z.cell_labels:
            if 0 <= c < n_cells:
                cell_zone[c] = zi

    # Identify excluded (e.g. AMI) patch faces before point merging so we
    # can restore their original vertices if global point merging collapses
    # two vertices of the same face into one (creating a degenerate face).
    excluded_faces: set[int] = set()
    excluded_face_orig_verts: dict[int, np.ndarray] = {}
    for p in patches:
        if _patch_excluded(p.name, exclude_re):
            for fi in range(p.start_face, min(p.start_face + p.n_faces, n_faces)):
                excluded_faces.add(fi)
                s, e = int(offsets[fi]), int(offsets[fi + 1])
                excluded_face_orig_verts[fi] = conn[s:e].copy()

    original_points = points.copy()
    points, pt_map = _merge_points(points, point_tol)
    conn = pt_map[conn]

    # Restore degenerate faces on excluded patches by re-adding the original
    # (pre-merge) points so each face keeps its distinct vertices.
    extra_points: list[np.ndarray] = []
    for fi, orig_verts in excluded_face_orig_verts.items():
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        current_verts = conn[s:e]
        if len(current_verts) != len(set(int(v) for v in current_verts)):
            new_verts: list[int] = []
            for ov in orig_verts:
                new_idx = len(points) + len(extra_points)
                extra_points.append(original_points[int(ov)])
                new_verts.append(new_idx)
            conn[s:e] = np.array(new_verts, dtype=conn.dtype)

    if extra_points:
        points = np.vstack([points, np.array(extra_points, dtype=np.float64)])

    key_to_faces: dict[tuple[int, ...], list[int]] = {}
    for fi in range(n_faces):
        if nb[fi] >= 0 or fi in excluded_faces:
            continue
        key = _face_vertex_key(offsets, conn, fi)
        if key:
            key_to_faces.setdefault(key, []).append(fi)

    remove = np.zeros(n_faces, dtype=bool)
    paired = 0
    for faces in key_to_faces.values():
        if len(faces) != 2:
            continue
        f0, f1 = faces
        c0, c1 = int(owner[f0]), int(owner[f1])
        if c0 == c1:
            continue
        z0, z1 = int(cell_zone[c0]), int(cell_zone[c1])
        if z0 < 0 or z1 < 0 or z0 == z1:
            continue
        own, nei = min(c0, c1), max(c0, c1)
        keep = f0 if int(owner[f0]) == own else f1
        drop = f1 if keep == f0 else f0
        owner[keep] = own
        nb[keep] = nei
        remove[drop] = True
        paired += 1

    if paired == 0:
        return {"paired_faces": 0, "points_before": n_points_before, "points_after": points.shape[0]}

    old_patch_of = np.full(n_faces, "", dtype=object)
    for p in patches:
        for fi in range(p.start_face, p.start_face + p.n_faces):
            if fi < n_faces:
                old_patch_of[fi] = p.name

    final_owner, final_nb, final_offsets, final_conn, final_patches, n_internal = _compact_mesh(
        owner, nb, offsets, conn, old_patch_of, remove, patches
    )

    _write_binary_vector_field(points_path, points, _read_header_bytes(points_path))
    faces_header = _read_header_bytes(faces_path)
    if faces_fmt == "binary":
        _write_binary_compact_face_list(faces_path, final_offsets, final_conn, faces_header)
    else:
        _write_ascii_face_list(faces_path, final_offsets, final_conn, faces_header)
    _write_binary_label_list(owner_path, final_owner.astype(np.int32), _read_header_bytes(owner_path))
    _write_binary_label_list(
        neighbour_path, final_nb[:n_internal].astype(np.int32), _read_header_bytes(neighbour_path)
    )
    write_boundary(boundary_path, final_patches, _boundary_header_text(boundary_path))

    return {
        "paired_faces": paired,
        "points_before": n_points_before,
        "points_after": int(points.shape[0]),
        "faces_before": n_faces,
        "faces_after": int(final_owner.size),
        "internal_faces": n_internal,
        "boundary_patches": len(final_patches),
    }
