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


def _merge_points(
    points: np.ndarray,
    tol: float,
    *,
    exclude: set[int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (merged_points, old_to_new map).

    Groups points by their rounded grid cell (``round(p/tol)``) using a
    collision-free packed key. Each group of points sharing the same grid
    cell is merged into a single representative point.

    Points in the ``exclude`` set are never merged with any other point;
    they keep their own coordinates and are appended after the merged
    points in the output array.

    Note: points up to ~2*tol apart can be merged if they straddle a grid
    cell boundary, but points in different grid cells are never merged.
    """
    if tol <= 0:
        return points, np.arange(len(points), dtype=np.int32)

    n = len(points)

    # Determine which points are eligible for merging.
    if exclude:
        merge_mask = np.ones(n, dtype=bool)
        for idx in exclude:
            if 0 <= idx < n:
                merge_mask[idx] = False
        merge_indices = np.where(merge_mask)[0]
        keep_indices = np.where(~merge_mask)[0]
    else:
        merge_mask = None
        merge_indices = np.arange(n, dtype=np.int32)
        keep_indices = np.empty(0, dtype=np.int32)

    if merge_indices.size == 0:
        # Nothing to merge – return original points unchanged.
        return points, np.arange(n, dtype=np.int32)

    # Merge only the eligible subset.
    merge_pts = points[merge_indices]
    inv = np.round(merge_pts / tol).astype(np.int64)
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
    merged_points = np.empty((new_n, 3), dtype=np.float64)
    merge_to_new = np.empty(merge_indices.size, dtype=np.int32)
    for ui, start in enumerate(first_idx):
        end = first_idx[ui + 1] if ui + 1 < first_idx.size else order.size
        members = order[start:end]
        rep = int(members[0])
        merged_points[ui] = merge_pts[rep]
        merge_to_new[members] = ui

    # Build the final points array: [merged_points, kept_points].
    final_n = new_n + keep_indices.size
    final_points = np.empty((final_n, 3), dtype=np.float64)
    final_old_to_new = np.empty(n, dtype=np.int32)

    final_points[:new_n] = merged_points
    for i, old_idx in enumerate(merge_indices):
        final_old_to_new[old_idx] = merge_to_new[i]

    if keep_indices.size > 0:
        final_points[new_n:] = points[keep_indices]
        for i, old_idx in enumerate(keep_indices):
            final_old_to_new[old_idx] = new_n + i

    return final_points, final_old_to_new


def _face_vertex_key(offsets: np.ndarray, conn: np.ndarray, fi: int) -> tuple[int, ...] | None:
    s, e = int(offsets[fi]), int(offsets[fi + 1])
    idx = conn[s:e]
    if idx.size < 3:
        return None
    return tuple(sorted(int(v) for v in idx))


def _face_centroid(points: np.ndarray, verts: np.ndarray) -> np.ndarray:
    return points[verts].mean(axis=0)


def _face_area(points: np.ndarray, verts: np.ndarray) -> float:
    """Polygon area via fan triangulation about the face centroid."""
    v = points[verts]
    c = v.mean(axis=0)
    rel = v - c
    nxt = np.roll(rel, -1, axis=0)
    cross = np.cross(rel, nxt)
    return float(0.5 * np.linalg.norm(cross, axis=1).sum())


def _geometric_interface_pairs(
    points: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    owner: np.ndarray,
    nb: np.ndarray,
    remove: np.ndarray,
    cell_zone: np.ndarray,
    excluded_faces: set[int],
    geom_tol: float,
    *,
    area_rel_tol: float = 0.10,
) -> tuple[list[tuple[int, int]], dict[int, int], int]:
    """Pair geometrically coincident inter-zone boundary faces missed by the
    exact vertex-signature pass (e.g. when their points were just outside the
    point-merge tolerance and therefore never merged).

    Returns ``(pairs, vert_remap, n_suspected_unpaired)`` where *pairs* is a
    list of ``(face_a, face_b)`` to merge, *vert_remap* snaps face-b vertices
    onto the coincident face-a vertices (so the two faces share a vertex set
    and the cells stay watertight), and *n_suspected_unpaired* counts faces
    that remain unpaired after both the signature and geometric passes –
    including faces with no opposite-zone candidate at all (isolated
    interface faces that previously went unreported).
    """
    if geom_tol <= 0:
        return [], {}, 0

    n_faces = owner.size
    cands: list[int] = []
    centroids: dict[int, np.ndarray] = {}
    vcount: dict[int, int] = {}
    areas: dict[int, float] = {}
    for fi in range(n_faces):
        if nb[fi] >= 0 or remove[fi] or fi in excluded_faces:
            continue
        z = int(cell_zone[int(owner[fi])])
        if z < 0:
            continue
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        if e - s < 3:
            continue
        verts = conn[s:e]
        cands.append(fi)
        centroids[fi] = _face_centroid(points, verts)
        vcount[fi] = int(e - s)
        areas[fi] = _face_area(points, verts)

    if not cands:
        return [], {}, 0

    # Spatial hash on centroid grid cell; search the 27-cell neighbourhood.
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for fi in cands:
        gc = tuple(int(round(v / geom_tol)) for v in centroids[fi])
        buckets.setdefault(gc, []).append(fi)

    neighbour_offsets = [
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
    ]

    # Collect ALL candidate pairs first, then greedily match in ascending
    # distance order.  This avoids order-sensitive greedy matching where an
    # early face steals a partner that would fit a later face better.
    candidate_pairs: list[tuple[float, int, int]] = []
    cand_set = set(cands)
    for fa in cands:
        za = int(cell_zone[int(owner[fa])])
        ca = centroids[fa]
        gc = tuple(int(round(v / geom_tol)) for v in ca)
        for off in neighbour_offsets:
            cell = (gc[0] + off[0], gc[1] + off[1], gc[2] + off[2])
            for fb in buckets.get(cell, ()):
                if fb == fa or fb not in cand_set:
                    continue
                if int(cell_zone[int(owner[fb])]) == za:
                    continue
                # Allow vertex-count mismatch of ±1: cgns2foam may split one
                # quad on side A into two triangles on side B (or vice versa),
                # which previously caused all such pairs to be skipped.
                if abs(vcount[fb] - vcount[fa]) > 1:
                    continue
                d = float(np.linalg.norm(centroids[fb] - ca))
                if d > geom_tol:
                    continue
                amax = max(areas[fa], areas[fb], 1e-300)
                if abs(areas[fa] - areas[fb]) / amax > area_rel_tol:
                    continue
                # Deterministic ordering: pair (lower face id first) so that
                # sorting by distance is stable for equal distances.
                a, b = (fa, fb) if fa < fb else (fb, fa)
                candidate_pairs.append((d, a, b))

    # Sort by distance ascending, then by face id for determinism.
    candidate_pairs.sort(key=lambda t: (t[0], t[1], t[2]))

    matched: set[int] = set()
    pairs: list[tuple[int, int]] = []
    vert_remap: dict[int, int] = {}
    loose_tol = 2.0 * geom_tol  # second-pass tolerance for vertex snapping

    for d, fa, fb in candidate_pairs:
        if fa in matched or fb in matched:
            continue
        remap = _vertex_correspondence(points, offsets, conn, fa, fb, geom_tol)
        if remap is None:
            # Retry with a looser tolerance: vertices may sit just outside
            # geom_tol when the two sides of the interface were meshed
            # independently with slightly different node spacing.
            remap = _vertex_correspondence(
                points, offsets, conn, fa, fb, loose_tol
            )
            if remap is None:
                continue
        matched.add(fa)
        matched.add(fb)
        pairs.append((fa, fb))
        vert_remap.update(remap)

    # Count ALL remaining unpaired candidate faces, not just those that had
    # an opposite-zone neighbour.  Isolated interface faces (no candidate
    # within geom_tol) are the primary source of "open cells" reported by
    # checkMesh and were previously invisible in the report.
    suspected = sum(1 for fi in cands if fi not in matched)

    return pairs, vert_remap, suspected


def _vertex_correspondence(
    points: np.ndarray,
    offsets: np.ndarray,
    conn: np.ndarray,
    fa: int,
    fb: int,
    geom_tol: float,
) -> dict[int, int] | None:
    """Map each vertex of the *smaller* face onto its nearest partner on the
    *larger* face and return a ``{fb_vert: fa_vert}`` remap (only entries
    where the two differ).

    Returns ``None`` if any vertex of the smaller face has no unique partner
    on the larger face within *geom_tol*.

    When the two faces have different vertex counts (e.g. quad vs triangle
    split), the smaller face is mapped onto the larger one and excess
    vertices on the larger face are simply left unmapped – the faces are
    still considered pairable as long as every vertex of the *smaller* face
    finds a partner.  The returned dict always uses the convention
    ``{fb_vert: fa_vert}`` regardless of which face is larger.
    """
    sa, ea = int(offsets[fa]), int(offsets[fa + 1])
    sb, eb = int(offsets[fb]), int(offsets[fb + 1])
    verts_a = conn[sa:ea]  # fa vertices
    verts_b = conn[sb:eb]  # fb vertices

    if verts_a.size >= verts_b.size:
        # fa has at least as many vertices as fb: map each fb vertex to its
        # nearest fa vertex.  Result direction is {fb_vert: fa_vert}.
        larger, smaller = verts_a, verts_b
        larger_pts = points[larger]
        remap: dict[int, int] = {}
        used: set[int] = set()
        for v_sm in smaller:
            d = np.linalg.norm(larger_pts - points[int(v_sm)], axis=1)
            order = np.argsort(d)
            chosen = -1
            for k in order:
                if d[k] > geom_tol:
                    break
                if int(k) in used:
                    continue
                chosen = int(k)
                break
            if chosen < 0:
                return None
            used.add(chosen)
            target = int(larger[chosen])
            if int(v_sm) != target:
                remap[int(v_sm)] = target
        if len(used) != len(smaller):
            return None
        return remap
    else:
        # fb has more vertices than fa: map each fa vertex to its nearest fb
        # vertex, then flip the mapping to {fb_vert: fa_vert}.
        larger, smaller = verts_b, verts_a
        larger_pts = points[larger]
        reverse_remap: dict[int, int] = {}  # {fa_vert: fb_vert}
        used = set()
        for v_sm in smaller:
            d = np.linalg.norm(larger_pts - points[int(v_sm)], axis=1)
            order = np.argsort(d)
            chosen = -1
            for k in order:
                if d[k] > geom_tol:
                    break
                if int(k) in used:
                    continue
                chosen = int(k)
                break
            if chosen < 0:
                return None
            used.add(chosen)
            reverse_remap[int(v_sm)] = int(larger[chosen])
        if len(used) != len(smaller):
            return None
        # Flip to {fb_vert: fa_vert}; only keep entries where they differ.
        remap = {
            fb_v: fa_v for fa_v, fb_v in reverse_remap.items() if fb_v != fa_v
        }
        return remap


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


def _resolve_patch_type(name: str, fallback: str = "wall") -> str:
    """Compatibility wrapper – prefer ``mesh.resolve_open_patch_type``."""
    from .mesh import resolve_open_patch_type

    return resolve_open_patch_type(name, fallback)


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
                patch_type=_resolve_patch_type(pname, patch_type.get(pname, "wall")),
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
    geometric_fallback: bool = True,
    geom_tol: float | None = None,
) -> dict:
    """Merge coincident inter-zone boundary faces into internal faces.

    The exact vertex-signature pass relies on coincident interface points
    having been merged by ``_merge_points``.  When the two sides of an
    interface sit just outside ``point_tol`` they are never merged and the
    faces stay on the boundary as "open" interface faces (mass leakage).
    The optional geometric fallback (``geometric_fallback``) pairs such faces
    by face-centroid coincidence within ``geom_tol`` (default ``5 * point_tol``)
    and snaps their vertices so the cells stay watertight.
    """
    import sys, time
    def _clog(msg: str) -> None:
        print(f"[coalesce] {msg}", file=sys.stderr, flush=True)

    exclude_re = [re.compile(p) for p in (exclude_patterns or [r"ami_rot"])]
    if geom_tol is None:
        geom_tol = 5.0 * point_tol
    points_path = poly_dir / "points"
    faces_path = poly_dir / "faces"
    owner_path = poly_dir / "owner"
    neighbour_path = poly_dir / "neighbour"
    boundary_path = poly_dir / "boundary"

    _clog("read points ...")
    t0 = time.time()
    n_points_before = _read_binary_vector_field(points_path).shape[0]
    points = _read_binary_vector_field(points_path)
    _clog(f"  points: {n_points_before} ({time.time()-t0:.1f}s)")
    _clog("read faces/owner/neighbour ...")
    t0 = time.time()
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
    _clog(f"  faces/owner/neighbour done ({time.time()-t0:.1f}s)")

    n_faces = owner.size
    nb = np.full(n_faces, -1, dtype=np.int32)
    if neighbour_raw.size == n_faces:
        nb[:] = neighbour_raw
    else:
        nb[: neighbour_raw.size] = neighbour_raw

    n_cells = int(owner.max()) + 1 if owner.size else 0
    cell_zone = np.full(max(n_cells, 1), -1, dtype=np.int32)
    for zi, z in enumerate(zones):
        labels = np.asarray(z.cell_labels, dtype=np.int64)
        valid = (labels >= 0) & (labels < n_cells)
        cell_zone[labels[valid]] = zi

    # Identify excluded (e.g. AMI) patch faces before point merging so we
    # can restore their original vertices if global point merging collapses
    # two vertices of the same face into one (creating a degenerate face).
    _clog("collect excluded (AMI) faces ...")
    t0 = time.time()
    excluded_faces: set[int] = set()
    for p in patches:
        if _patch_excluded(p.name, exclude_re):
            for fi in range(p.start_face, min(p.start_face + p.n_faces, n_faces)):
                excluded_faces.add(fi)
    _clog(f"  {len(excluded_faces)} excluded faces ({time.time()-t0:.1f}s)")

    # Collect all vertex indices referenced by excluded (AMI) faces.
    # These vertices are excluded from the global point merge so that the
    # smooth cylindrical AMI surface is preserved.  Only non-AMI vertices
    # (interface faces, internal faces) participate in the merge.
    excluded_vert_ids: set[int] = set()
    for fi in excluded_faces:
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        excluded_vert_ids.update(int(v) for v in conn[s:e])

    # Save original connectivity (single array copy, NOT per-face copies).
    # The previous implementation allocated a numpy array for each of the
    # ~22M faces, which was the dominant build-time bottleneck.
    original_conn = conn.copy()
    excluded_face_orig_verts: dict[int, np.ndarray] = {}
    for fi in excluded_faces:
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        excluded_face_orig_verts[fi] = original_conn[s:e].copy()

    _clog("merge points ...")
    t0 = time.time()
    original_points = points.copy()
    points, pt_map = _merge_points(points, point_tol, exclude=excluded_vert_ids)
    conn = pt_map[conn]
    _clog(f"  merge done ({time.time()-t0:.1f}s)")

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

    # Also restore any other face that became degenerate after point merging.
    # This catches internal faces and non-excluded boundary faces whose
    # vertices were collapsed by the global point merge.  Without this,
    # zero-area faces produce NaN residuals in the solver.
    _clog("check degenerate faces ...")
    t0 = time.time()
    for fi in range(n_faces):
        if fi in excluded_face_orig_verts:
            continue  # already handled above
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        current_verts = conn[s:e]
        if len(current_verts) != len(set(int(v) for v in current_verts)):
            orig_verts = original_conn[s:e]
            new_verts: list[int] = []
            for ov in orig_verts:
                new_idx = len(points) + len(extra_points)
                extra_points.append(original_points[int(ov)])
                new_verts.append(new_idx)
            conn[s:e] = np.array(new_verts, dtype=conn.dtype)

    if extra_points:
        points = np.vstack([points, np.array(extra_points, dtype=np.float64)])

    _clog(f"degenerate check done ({time.time()-t0:.1f}s)")
    _clog("build vertex signature keys ...")
    t0 = time.time()
    key_to_faces: dict[tuple[int, ...], list[int]] = {}
    for fi in range(n_faces):
        if nb[fi] >= 0 or fi in excluded_faces:
            continue
        key = _face_vertex_key(offsets, conn, fi)
        if key:
            key_to_faces.setdefault(key, []).append(fi)
    _clog(f"  {len(key_to_faces)} signature buckets ({time.time()-t0:.1f}s)")

    _clog("signature pairing ...")
    t0 = time.time()
    remove = np.zeros(n_faces, dtype=bool)
    paired = 0
    for faces in key_to_faces.values():
        # A signature bucket may contain >2 faces when three or more zones
        # meet along the same edge (T-junctions) or when cgns2foam emits
        # duplicate interface faces.  Pair them greedily as long as we can
        # find two faces from different zones with different owners.
        remaining = [f for f in faces if not remove[f]]
        i = 0
        while i < len(remaining):
            f0 = remaining[i]
            c0 = int(owner[f0])
            z0 = int(cell_zone[c0])
            paired_here = False
            for j in range(i + 1, len(remaining)):
                f1 = remaining[j]
                c1 = int(owner[f1])
                if c0 == c1:
                    continue
                z1 = int(cell_zone[c1])
                if z0 < 0 or z1 < 0 or z0 == z1:
                    continue
                own, nei = min(c0, c1), max(c0, c1)
                keep = f0 if c0 == own else f1
                drop = f1 if keep == f0 else f0
                owner[keep] = own
                nb[keep] = nei
                remove[drop] = True
                paired += 1
                paired_here = True
                remaining.pop(j)
                break
            if paired_here:
                remaining.pop(i)
            else:
                i += 1

    paired_signature = paired
    _clog(f"signature pairing done: {paired_signature} paired ({time.time()-t0:.1f}s)")
    paired_geometric = 0
    suspected_unpaired = 0
    if geometric_fallback:
        _clog("geometric fallback pairing ...")
        t0 = time.time()
        geo_pairs, vert_remap, suspected_unpaired = _geometric_interface_pairs(
            points, offsets, conn, owner, nb, remove, cell_zone, excluded_faces, geom_tol
        )
        if vert_remap:
            remap_arr = np.arange(points.shape[0], dtype=conn.dtype)
            for old, new in vert_remap.items():
                if 0 <= old < remap_arr.size:
                    remap_arr[old] = new
            conn = remap_arr[conn]
        for fa, fb in geo_pairs:
            c0, c1 = int(owner[fa]), int(owner[fb])
            if c0 == c1:
                continue
            own, nei = min(c0, c1), max(c0, c1)
            keep = fa if c0 == own else fb
            drop = fb if keep == fa else fa
            owner[keep] = own
            nb[keep] = nei
            remove[drop] = True
            paired += 1
            paired_geometric += 1
        _clog(f"geometric pairing done: +{paired_geometric} paired ({time.time()-t0:.1f}s)")

    if paired == 0:
        # Build patch distribution of remaining unpaired interface faces
        # so the user can see which interface leaks mass.
        unpaired_by_patch: dict[str, int] = {}
        for fi in range(n_faces):
            if nb[fi] >= 0 or remove[fi] or fi in excluded_faces:
                continue
            z = int(cell_zone[int(owner[fi])])
            if z < 0:
                continue
            pname = "<unknown>"
            for p in patches:
                if p.start_face <= fi < p.start_face + p.n_faces:
                    pname = p.name
                    break
            unpaired_by_patch[pname] = unpaired_by_patch.get(pname, 0) + 1
        return {
            "paired_faces": 0,
            "paired_signature": 0,
            "paired_geometric": 0,
            "suspected_unpaired_interface_faces": suspected_unpaired,
            "unpaired_by_patch": unpaired_by_patch,
            "points_before": n_points_before,
            "points_after": points.shape[0],
        }

    old_patch_of = np.full(n_faces, "", dtype=object)
    for p in patches:
        end = min(p.start_face + p.n_faces, n_faces)
        if end > p.start_face:
            old_patch_of[p.start_face:end] = p.name

    _clog("compact mesh ...")
    t0 = time.time()
    final_owner, final_nb, final_offsets, final_conn, final_patches, n_internal = _compact_mesh(
        owner, nb, offsets, conn, old_patch_of, remove, patches
    )
    _clog(f"  compact done ({time.time()-t0:.1f}s)")

    _clog("write mesh files ...")
    t0 = time.time()
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
    _clog(f"  write done ({time.time()-t0:.1f}s)")

    # Build patch distribution of remaining unpaired interface faces so the
    # user can see which interface leaks mass after coalescing.
    unpaired_by_patch = {}
    for fi in range(n_faces):
        if nb[fi] >= 0 or remove[fi] or fi in excluded_faces:
            continue
        z = int(cell_zone[int(owner[fi])])
        if z < 0:
            continue
        pname = str(old_patch_of[fi]) or "<unknown>"
        unpaired_by_patch[pname] = unpaired_by_patch.get(pname, 0) + 1

    return {
        "paired_faces": paired,
        "paired_signature": paired_signature,
        "paired_geometric": paired_geometric,
        "suspected_unpaired_interface_faces": suspected_unpaired,
        "unpaired_by_patch": unpaired_by_patch,
        "points_before": n_points_before,
        "points_after": int(points.shape[0]),
        "faces_before": n_faces,
        "faces_after": int(final_owner.size),
        "internal_faces": n_internal,
        "boundary_patches": len(final_patches),
    }
