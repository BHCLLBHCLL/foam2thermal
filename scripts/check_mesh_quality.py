"""Check original mesh quality to diagnose coalesce point merging issues."""
import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from foam2thermal.mesh_coalesce import (
    _read_binary_vector_field,
    _read_faces,
    _read_binary_label_list,
    _faces_format,
)


def check_mesh(poly_dir: Path, label: str) -> None:
    print(f"\n=== {label} ===")
    points = _read_binary_vector_field(poly_dir / "points")
    offsets, conn = _read_faces(poly_dir / "faces")
    owner = _read_binary_label_list(poly_dir / "owner")
    nb_raw = _read_binary_label_list(poly_dir / "neighbour")

    n_faces = owner.size
    nb = np.full(n_faces, -1, dtype=np.int32)
    if nb_raw.size == n_faces:
        nb[:] = nb_raw
    else:
        nb[: nb_raw.size] = nb_raw

    n_cells = int(owner.max()) + 1 if owner.size else 0
    print(f"points: {points.shape[0]}, faces: {n_faces}, cells: {n_cells}")

    # Check for degenerate faces (repeated vertices)
    degenerate_faces = 0
    for fi in range(n_faces):
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        verts = conn[s:e]
        if len(set(int(v) for v in verts)) < len(verts):
            degenerate_faces += 1
    print(f"degenerate faces (repeated verts): {degenerate_faces}")

    # Check for zero area faces
    zero_area = 0
    min_area = float("inf")
    for fi in range(n_faces):
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        verts = conn[s:e]
        if len(verts) < 3:
            zero_area += 1
            continue
        pts = points[verts]
        # Compute face area using cross product
        area = np.zeros(3)
        center = pts.mean(axis=0)
        for i in range(len(pts)):
            v1 = pts[i] - center
            v2 = pts[(i + 1) % len(pts)] - center
            area += 0.5 * np.cross(v1, v2)
        a = np.linalg.norm(area)
        if a < min_area:
            min_area = a
        if a < 1e-15:
            zero_area += 1
    print(f"zero area faces: {zero_area}, min area: {min_area:.6e}")

    # Check minimum edge length
    min_edge = float("inf")
    sample_count = 0
    for fi in range(min(n_faces, 100000)):
        s, e = int(offsets[fi]), int(offsets[fi + 1])
        verts = conn[s:e]
        pts = points[verts]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = np.linalg.norm(pts[i] - pts[j])
                if d > 0 and d < min_edge:
                    min_edge = d
        sample_count += 1
    print(f"min edge length (sampled {sample_count} faces): {min_edge:.6e}")

    # Check point distances - find closest pairs
    print("checking closest point pairs (sampled)...")
    # Sample 10000 points and find closest pairs
    idx = np.random.choice(points.shape[0], min(10000, points.shape[0]), replace=False)
    sample = points[idx]
    # Use KDTree for efficiency
    from scipy.spatial import cKDTree

    tree = cKDTree(sample)
    dists, _ = tree.query(sample, k=2)
    nn_dists = dists[:, 1]
    nn_dists = nn_dists[nn_dists > 0]
    if len(nn_dists) > 0:
        print(
            f"nearest neighbor distances: min={nn_dists.min():.6e}, "
            f"median={np.median(nn_dists):.6e}, max={nn_dists.max():.6e}"
        )
        print(f"points with NN < 1e-4: {(nn_dists < 1e-4).sum()}")
        print(f"points with NN < 1e-6: {(nn_dists < 1e-6).sum()}")
        print(f"points with NN < 1e-8: {(nn_dists < 1e-8).sum()}")


if __name__ == "__main__":
    # Check original mesh
    orig = Path("tests/laptop_thermal_steady_scaled_v3_orig/constant/polyMesh")
    check_mesh(orig, "ORIGINAL MESH")

    # Check coalesced mesh
    coal = Path("cases/laptop_thermal_cht_v3/constant/polyMesh")
    if coal.exists():
        check_mesh(coal, "COALESCED MESH (before split)")
