"""Fix faces with duplicate vertices by removing the duplicates.

The cgns2foam output contains faces with duplicate vertex labels (e.g.,
[267491, 284471, 267320, 284471] where 284471 appears twice).  These
degenerate faces produce zero-area triangles during fan triangulation and
can crash the pressure equation assembly in chtMultiRegionSimpleFoam.

This script rewrites the binary ``faces`` file so every face keeps only
its unique vertex labels while preserving the original ordering.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from foam2thermal.mesh_coalesce import (
    _read_faces,
    _write_binary_compact_face_list,
    _faces_format,
    _read_header_bytes,
)


def _dedup_vertices(verts: np.ndarray) -> np.ndarray:
    """Return vertex labels with consecutive duplicates removed.

    Non-consecutive duplicates are also removed – only the first
    occurrence of each label is kept.
    """
    seen: set[int] = set()
    out: list[int] = []
    for v in verts:
        iv = int(v)
        if iv in seen:
            continue
        seen.add(iv)
        out.append(iv)
    return np.asarray(out, dtype=np.int32)


def main() -> None:
    case_dir = Path(__file__).resolve().parent.parent / "cases" / "laptop_cht"
    mesh_dir = case_dir / "constant" / "air" / "polyMesh"
    faces_path = mesh_dir / "faces"

    backup = faces_path.with_suffix(".faces.bak")
    if not backup.exists():
        shutil.copy2(faces_path, backup)
        print(f"Backed up original faces -> {backup}")

    print("Reading faces...")
    offsets, conn = _read_faces(faces_path)
    n_faces = offsets.shape[0] - 1
    print(f"  nFaces = {n_faces}")

    header = _read_header_bytes(faces_path)
    is_binary = _faces_format(faces_path) == "binary"

    fixed_count = 0
    dropped_count = 0
    new_offsets = np.zeros(n_faces + 1, dtype=np.int32)
    new_conn: list[int] = []

    for fi in range(n_faces):
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        verts = conn[s:e]
        unique = _dedup_vertices(verts)
        if unique.size < 3:
            # Keep at least 3 vertices by padding with the first vertex.
            # This is a degenerate face, but we keep it to avoid breaking
            # the mesh topology.  The face will have zero area.
            pad = np.full(3 - unique.size, unique[0] if unique.size else 0, dtype=np.int32)
            unique = np.concatenate([unique, pad])
            dropped_count += 1
        elif unique.size < verts.size:
            fixed_count += 1
        new_offsets[fi] = len(new_conn)
        new_conn.extend(int(v) for v in unique)
    new_offsets[n_faces] = len(new_conn)

    new_conn_arr = np.asarray(new_conn, dtype=np.int32)
    print(f"  Fixed (removed duplicates): {fixed_count}")
    print(f"  Degenerate (< 3 unique):   {dropped_count}")
    print(f"  Old conn size: {conn.size}")
    print(f"  New conn size: {new_conn_arr.size}")

    print("Writing fixed faces...")
    if is_binary:
        _write_binary_compact_face_list(faces_path, new_offsets, new_conn_arr, header)
    else:
        from foam2thermal.mesh_coalesce import _write_ascii_face_list
        _write_ascii_face_list(faces_path, new_offsets, new_conn_arr, header)
    print("Done.")


if __name__ == "__main__":
    main()
