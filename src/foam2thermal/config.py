"""Load and validate JSON configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RegionDef:
    name: str
    type: str  # "fluid" | "solid"
    cell_zones: list[str] = field(default_factory=list)
    material: str | None = None

    @property
    def foam_name(self) -> str:
        """OpenFOAM region name after splitMeshRegions.

        Uses the configured region name (self.name) so that region directories,
        regionProperties and coupling patch names stay short and dot-free.
        The Python splitter (split_regions.py) reads region names from
        regionProperties, so this name propagates consistently.
        """
        return self.name


@dataclass
class CaseConfig:
    raw: dict[str, Any]
    source_case: Path
    output_case: Path
    openfoam_root: Path
    bash_exe: Path
    solver: str
    regions: list[RegionDef]
    region_materials: dict[str, str]
    materials: dict[str, Any]
    initial: dict[str, Any]
    boundary_conditions: dict[str, Any]
    numerics: dict[str, Any]
    interfaces: dict[str, Any]
    mesh_prep: dict[str, Any]
    patch_regions: dict[str, str]
    gravity: list[float]
    turbulence: dict[str, Any]

    @property
    def region_types(self) -> dict[str, str]:
        return {r.foam_name: r.type for r in self.regions}

    @property
    def fluid_regions(self) -> list[str]:
        return [r.foam_name for r in self.regions if r.type == "fluid"]

    @property
    def solid_regions(self) -> list[str]:
        return [r.foam_name for r in self.regions if r.type == "solid"]

    def resolve_region_type(self, name: str | None) -> str:
        if not name:
            return "unknown"
        for r in self.regions:
            if name in (r.name, r.foam_name):
                return r.type
        return "unknown"

    def material_for(self, region: str) -> dict[str, Any]:
        for r in self.regions:
            if r.foam_name == region or r.name == region:
                key = self.region_materials.get(r.name) or self.region_materials.get(r.foam_name)
                if key and key in self.materials:
                    return self.materials[key]
        raise KeyError(f"No material mapping for region '{region}'")


def load_config(
    config_path: Path,
    input_mesh: Path,
    output_case: Path,
) -> CaseConfig:
    """Load JSON config; *input_mesh* and *output_case* come from CLI positional args."""
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    of = raw.get("openfoam", {})

    regions = [
        RegionDef(
            name=r["name"],
            type=r["type"],
            cell_zones=r.get("cellZones", []),
            material=r.get("material"),
        )
        for r in raw["regions"]
    ]

    region_materials = raw.get("region_materials", {})
    for r in regions:
        if r.material:
            region_materials.setdefault(r.name, r.material)

    return CaseConfig(
        raw=raw,
        source_case=input_mesh.resolve(),
        output_case=output_case.resolve(),
        openfoam_root=Path(of["root"]),
        bash_exe=Path(of.get("bash", r"C:\OF\v2412\msys64\usr\bin\bash.exe")),
        solver=of.get("solver", "chtMultiRegionSimpleFoam"),
        regions=regions,
        region_materials=region_materials,
        materials=raw.get("materials", {}),
        initial=raw.get("initial_conditions", {}),
        boundary_conditions=raw.get("boundary_conditions", {}),
        numerics=raw.get("numerics", {}),
        interfaces=raw.get("interfaces", {}),
        mesh_prep=raw.get("mesh_prep", {}),
        patch_regions=raw.get("patch_regions", {}),
        gravity=raw.get("gravity", [0, 0, -9.81]),
        turbulence=raw.get("turbulence", {}),
    )
