"""Coalesce cgns2foam zone-interface boundary face pairs into internal faces."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from .mesh import PatchInfo, parse_boundary, parse_cell_zones

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


def _merge_points(points: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (merged_points, old_to_new map)."""
    if tol <= 0:
        return points, np.arange(len(points), dtype=np.int32)
    inv = np.round(points / tol).astype(np.int64)
    keys = inv[:, 0] * 73856093 ^ inv[:, 1] * 19349663 ^ inv[:, 2] * 83492791
    order = np.argsort(keys, kind="mergesort")
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


def _write_boundary(path: Path, patches: list[PatchInfo], header_text: str) -> None:
    lines = [header_text, f"{len(patches)}\n(\n"]
    for p in patches:
        lines.append(
            f"\n\t{p.name}\n\t{{\n"
            f"\t\ttype {p.patch_type};\n"
            f"\t\tstartFace {p.start_face};\n"
            f"\t\tnFaces {p.n_faces};\n"
            f"\t}}\n"
        )
    lines.append(")\n")
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


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
        mid_offsets[mi] = pos
        mid_conn_parts.append(conn[s:e].copy())
        pos += e - s
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
    final_order = np.concatenate([int_order, np.asarray(bnd_order, dtype=np.int32)])

    final_owner = mid_owner[final_order]
    final_nb = mid_nb[final_order]
    n_internal = int(int_order.size)

    final_offsets = np.zeros(n_keep + 1, dtype=np.int32)
    final_conn_parts: list[np.ndarray] = []
    pos = 0
    for fi, mid_fi in enumerate(final_order):
        s, e = int(mid_offsets[mid_fi]), int(mid_offsets[mid_fi + 1])
        final_offsets[fi] = pos
        final_conn_parts.append(mid_conn[s:e])
        pos += e - s
    final_offsets[n_keep] = pos
    final_conn = np.concatenate(final_conn_parts) if final_conn_parts else np.empty(0, dtype=np.int32)

    final_patches: list[PatchInfo] = []
    cursor = n_internal
    for p in patches:
        n_pf = len(bnd_by_patch.get(p.name, []))
        if n_pf == 0:
            continue
        final_patches.append(
            PatchInfo(
                name=p.name,
                patch_type=patch_type.get(p.name, "wall"),
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
    offsets, conn = _read_ascii_face_list(faces_path)
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

    points, pt_map = _merge_points(points, point_tol)
    conn = pt_map[conn]

    excluded_faces: set[int] = set()
    for p in patches:
        if _patch_excluded(p.name, exclude_re):
            for fi in range(p.start_face, min(p.start_face + p.n_faces, n_faces)):
                excluded_faces.add(fi)

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
    _write_ascii_face_list(faces_path, final_offsets, final_conn, _read_header_bytes(faces_path))
    _write_binary_label_list(owner_path, final_owner.astype(np.int32), _read_header_bytes(owner_path))
    _write_binary_label_list(
        neighbour_path, final_nb[:n_internal].astype(np.int32), _read_header_bytes(neighbour_path)
    )
    _write_boundary(boundary_path, final_patches, _boundary_header_text(boundary_path))

    return {
        "paired_faces": paired,
        "points_before": n_points_before,
        "points_after": int(points.shape[0]),
        "faces_before": n_faces,
        "faces_after": int(final_owner.size),
        "internal_faces": n_internal,
        "boundary_patches": len(final_patches),
    }
