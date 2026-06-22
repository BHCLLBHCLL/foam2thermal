"""OpenFOAM dictionary templates for chtMultiRegionSimpleFoam."""

from __future__ import annotations

from typing import Any

from .interfaces import is_ami_patch


_OF_BANNER = """/*--------------------------------*- C++ -*----------------------------------*\\
| =========                 |                                                 |
| \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |
|  \\\\    /   O peration     | Version:  v2412                                 |
|   \\\\  /    A nd           | Website:  www.openfoam.com                      |
|    \\/     M anipulation  |                                                 |
\\*---------------------------------------------------------------------------*/
"""


def foam_header(obj_class: str, obj_name: str, location: str = "", *, fmt: str = "ascii") -> str:
    loc = f'\n    location    "{location}";' if location else ""
    return (
        _OF_BANNER
        + "FoamFile\n"
        + "{\n"
        + "    version     2.0;\n"
        + f"    format      {fmt};\n"
        + '    arch        "LSB;label=32;scalar=64";\n'
        + f"    class       {obj_class};{loc}\n"
        + f"    object      {obj_name};\n"
        + "}\n"
        + "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n"
    )


def region_properties(fluid: list[str], solid: list[str]) -> str:
    fluid_s = " ".join(fluid)
    solid_s = " ".join(solid)
    return (
        foam_header("dictionary", "regionProperties")
        + f"""
regions
(
    fluid ( {fluid_s} )
    solid ( {solid_s} )
);

// ************************************************************************* //
"""
    )


def gravity_vector(g: list[float]) -> str:
    return (
        foam_header("uniformDimensionedVectorField", "g", "constant")
        + f"""
dimensions      [0 1 -2 0 0 0 0];
value           ({g[0]} {g[1]} {g[2]});
// ************************************************************************* //
"""
    )


def thermophysical_fluid(mat: dict[str, Any]) -> str:
    thermo = mat.get("thermoType", {})
    mix = mat.get("mixture", {})
    specie = mix.get("specie", {})
    thermo_d = mix.get("thermodynamics", {})
    trans = mix.get("transport", {})
    return (
        foam_header("dictionary", "thermophysicalProperties", "constant")
        + f"""
thermoType
{{
    type            {thermo.get('type', 'heRhoThermo')};
    mixture         {thermo.get('mixture', 'pureMixture')};
    transport       {thermo.get('transport', 'const')};
    thermo          {thermo.get('thermo', 'hConst')};
    equationOfState {thermo.get('equationOfState', 'perfectGas')};
    specie          {thermo.get('specie', 'specie')};
    energy          {thermo.get('energy', 'sensibleEnthalpy')};
}}

mixture
{{
    specie
    {{
        nMoles          {specie.get('nMoles', 1)};
        molWeight       {specie.get('molWeight', 28.966)};
    }}

    thermodynamics
    {{
        Cp              {thermo_d.get('Cp', 1006.43)};
        Hf              {thermo_d.get('Hf', 0)};
    }}

    transport
    {{
        mu              {trans.get('mu', 1.846e-05)};
        Pr              {trans.get('Pr', 0.706)};
    }}
}}

// ************************************************************************* //
"""
    )


def thermophysical_solid(mat: dict[str, Any]) -> str:
    thermo = mat.get("thermoType", {})
    mix = mat.get("mixture", {})
    specie = mix.get("specie", {})
    thermo_d = mix.get("thermodynamics", {})
    trans = mix.get("transport", {})
    eos = mix.get("equationOfState", {})
    return (
        foam_header("dictionary", "thermophysicalProperties", "constant")
        + f"""
thermoType
{{
    type            {thermo.get('type', 'heSolidThermo')};
    mixture         {thermo.get('mixture', 'pureMixture')};
    transport       {thermo.get('transport', 'constIso')};
    thermo          {thermo.get('thermo', 'hConst')};
    equationOfState {thermo.get('equationOfState', 'rhoConst')};
    specie          {thermo.get('specie', 'specie')};
    energy          {thermo.get('energy', 'sensibleEnthalpy')};
}}

mixture
{{
    specie
    {{
        nMoles          {specie.get('nMoles', 1)};
        molWeight       {specie.get('molWeight', 26.98)};
    }}

    thermodynamics
    {{
        Hf              {thermo_d.get('Hf', 0)};
        Sf              {thermo_d.get('Sf', 0)};
        Cp              {thermo_d.get('Cp', 871)};
    }}

    transport
    {{
        kappa           {trans.get('kappa', 202.4)};
    }}

    equationOfState
    {{
        rho             {eos.get('rho', 2719)};
    }}
}}

// ************************************************************************* //
"""
    )


def turbulence_properties(turb: dict[str, Any]) -> str:
    sim = turb.get("simulationType", "laminar")
    if sim == "laminar":
        body = """
simulationType laminar;
"""
    else:
        model = turb.get("RASModel", "kEpsilon")
        body = f"""
simulationType RAS;

RAS
{{
    RASModel        {model};
    turbulence      on;
    printCoeffs     on;
}}
"""
    return foam_header("dictionary", "turbulenceProperties", "constant") + body + "\n// ************************************************************************* //\n"


def control_dict(numerics: dict[str, Any], solver: str) -> str:
    return (
        foam_header("dictionary", "controlDict")
        + f"""
application     {solver};

startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {numerics.get('endTime', 500)};

deltaT          {numerics.get('deltaT', 1)};

writeControl    timeStep;
writeInterval   {numerics.get('writeInterval', 50)};
purgeWrite      {numerics.get('purgeWrite', 0)};

writeFormat     binary;
writePrecision  8;
writeCompression off;

timeFormat      general;
timePrecision   8;
runTimeModifiable true;

functions
{{
}}

// ************************************************************************* //
"""
    )


def fv_schemes_fluid() -> str:
    return (
        foam_header("dictionary", "fvSchemes", "system")
        + """
ddtSchemes { default steadyState; }

gradSchemes { default Gauss linear; }

divSchemes
{
    div(phi,U)      bounded Gauss upwind;
    div(phi,h)      bounded Gauss upwind;
    div(phi,K)      bounded Gauss upwind;
    div(phi,k)      bounded Gauss upwind;
    div(phi,epsilon) bounded Gauss upwind;
    div((muEff*dev2(T(grad(U))))) Gauss linear;
    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;
    div(phid,p)     bounded Gauss upwind;
}

laplacianSchemes { default Gauss linear limited 0.333; }
interpolationSchemes { default linear; }
snGradSchemes { default limited 0.333; }

fluxRequired
{
    default no;
    pCorr;
    p_rgh;
}

wallDist { method meshWave; nRequired false; }

// ************************************************************************* //
"""
    )


def fv_schemes_solid() -> str:
    return (
        foam_header("dictionary", "fvSchemes", "system")
        + """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default Gauss linear; }
laplacianSchemes { default Gauss linear limited 0.33; }
interpolationSchemes { default linear; }
snGradSchemes { default limited 0.33; }

// ************************************************************************* //
"""
    )


def fv_solution_fluid(numerics: dict[str, Any], *, p_ref: float = 101325) -> str:
    n_nc = numerics.get("nNonOrthogonalCorrectors", 0)
    p_ref_cell = numerics.get("pRefCell", 0)
    # p_rgh internalField is initialised to 0 (gauge pressure); the pressure
    # reference must be consistent with that, otherwise the solver will try to
    # pin p_rgh to 101325 in a cell that starts at 0 and diverge.
    p_ref_value = numerics.get("pRefValue", 0)
    rho_min = numerics.get("rhoMin", 0.2)
    rho_max = numerics.get("rhoMax", 2.0)
    return (
        foam_header("dictionary", "fvSolution", "system")
        + f"""
solvers
{{
    rho
    {{
        solver          PCG;
        preconditioner  DIC;
        tolerance       1e-7;
        relTol          0;
    }}

    p_rgh
    {{
        solver           GAMG;
        smoother         GaussSeidel;
        tolerance        1e-7;
        relTol           0.01;
        maxIter          100;

        cacheAgglomeration true;
        nCellsInCoarsestLevel 200;
        agglomerator    faceAreaPair;
        mergeLevels     1;
    }}

    p_rghFinal
    {{
        $p_rgh;
        relTol           0;
    }}

    "(U|k|h|epsilon|)"
    {{
        solver           PBiCGStab;
        preconditioner   DILU;
        tolerance        1e-6;
        relTol           0.05;
    }}
}}

SIMPLE
{{
    momentumPredictor true;
    nNonOrthogonalCorrectors {n_nc};
    frozenFlow      false;
    pRefCell        {p_ref_cell};
    pRefValue       {p_ref_value};
    rhoMin          {rho_min};
    rhoMax          {rho_max};
    residualControl {{ default 1e-7; }}
}}

relaxationFactors
{{
    fields {{ p_rgh 0.7; rho 1; }}
    equations {{ U 0.4; h 0.9; k 0.7; epsilon 0.7; }}
}}

// ************************************************************************* //
"""
    )


def fv_solution_solid() -> str:
    return (
        foam_header("dictionary", "fvSolution", "system")
        + """
solvers
{
    h
    {
        solver          PCG;
        preconditioner  DIC;
        nSweeps         2;
        tolerance       1e-8;
        relTol          0.05;
    }
}

SIMPLE
{
    residualControl { default 1e-20; }
}

relaxationFactors
{
    equations { h 1; }
}

// ************************************************************************* //
"""
    )


def _bc_block(name: str, spec: dict[str, Any], field: str) -> str:
    btype = spec.get("type", "zeroGradient")
    lines = [f"    {name}", "    {", f"        type            {btype};"]
    for key, val in spec.items():
        if key == "type":
            continue
        if isinstance(val, str):
            lines.append(f"        {key:<16} {val};")
        elif isinstance(val, (int, float)):
            lines.append(f"        {key:<16} uniform {val};")
        elif isinstance(val, list):
            lines.append(f"        {key:<16} uniform ({' '.join(str(v) for v in val)});")
        else:
            lines.append(f"        {key:<16} uniform {val};")
    if btype in ("fixedValue", "inletOutlet", "externalWallHeatFluxTemperature"):
        if "value" not in spec:
            lines.append("        value           $internalField;")
    lines.append("    }")
    return "\n".join(lines)


def mrf_properties(
    cell_zones: list[str],
    origins: list[tuple[float, float, float]],
    axis: list[float],
    omega: float,
    non_rotating: list[str],
) -> str:
    if non_rotating:
        nr = " ".join(non_rotating)
        nr_block = f"nonRotatingPatches ( {nr} );"
    else:
        nr_block = "nonRotatingPatches ();"
    ax, ay, az = axis
    blocks: list[str] = []
    for i, zone in enumerate(cell_zones):
        ox, oy, oz = origins[i] if i < len(origins) else origins[0]
        name = f"MRF{i + 1}" if len(cell_zones) > 1 else "MRF"
        blocks.append(
            f"""{name}
{{
    cellZone            {zone};
    active              yes;
    {nr_block}
    origin              ({ox} {oy} {oz});
    axis                ({ax} {ay} {az});
    omega               {omega};
}}"""
        )
    return (
        foam_header("dictionary", "MRFProperties", "constant")
        + "\n\n"
        + "\n\n".join(blocks)
        + "\n\n// ************************************************************************* //\n"
    )


def _cyclic_ami_bc(field: str) -> str:
    return f"""    {{
        type            cyclicAMI;
    }}"""


def field_T(
    region_type: str,
    patches: list[str],
    bc_cfg: dict[str, Any],
    T0: float,
    *,
    ami_patterns: list[str] | None = None,
) -> str:
    """Temperature field with coupled BC auto-detection."""
    blocks = ['     #includeEtc "caseDicts/setConstraintTypes"']
    kappa = "fluidThermo" if region_type == "fluid" else "solidThermo"

    ami_patterns = ami_patterns or [r"ami_rot\d+"]

    for p in patches:
        if p in bc_cfg:
            blocks.append(_bc_block(p, bc_cfg[p], "T"))
        elif is_ami_patch(p, ami_patterns):
            blocks.append(f"    {p}\n{_cyclic_ami_bc('T')}")
        elif "_to_" in p:
            blocks.append(
                f"""    {p}
    {{
        type            compressible::turbulentTemperatureRadCoupledMixed;
        Tnbr            T;
        kappaMethod     {kappa};
        useImplicit     false;
        qrNbr           none;
        qr              none;
        value           $internalField;
    }}"""
            )
        else:
            blocks.append(
                f"""    {p}
    {{
        type            zeroGradient;
    }}"""
            )

    body = "\n\n".join(blocks)
    return (
        foam_header("volScalarField", "T", "0")
        + f"""
dimensions      [0 0 0 1 0 0 0];
internalField   uniform {T0};

boundaryField
{{
{body}
}}

// ************************************************************************* //
"""
    )


def field_U(
    patches: list[str],
    bc_cfg: dict[str, Any],
    U0: list[float],
    *,
    ami_patterns: list[str] | None = None,
) -> str:
    blocks = ['     #includeEtc "caseDicts/setConstraintTypes"']
    ux, uy, uz = U0
    ami_patterns = ami_patterns or [r"ami_rot\d+"]
    for p in patches:
        if p in bc_cfg:
            blocks.append(_bc_block(p, bc_cfg[p], "U"))
        elif is_ami_patch(p, ami_patterns):
            blocks.append(f"    {p}\n{_cyclic_ami_bc('U')}")
        elif p.endswith("_1") and p.startswith("open"):
            # Outlet -> pressureInletOutletVelocity lets flow leave freely and
            # reverts to internalField on backflow.
            blocks.append(
                f"""    {p}
    {{
        type            pressureInletOutletVelocity;
        value           $internalField;
    }}"""
            )
        elif "_to_" in p:
            blocks.append(
                f"""    {p}
    {{
        type            noSlip;
    }}"""
            )
        else:
            blocks.append(
                f"""    {p}
    {{
        type            noSlip;
    }}"""
            )
    return (
        foam_header("volVectorField", "U", "0")
        + f"""
dimensions      [0 1 -1 0 0 0 0];
internalField   uniform ({ux} {uy} {uz});

boundaryField
{{
{chr(10).join(blocks)}
}}

// ************************************************************************* //
"""
    )


def field_p(
    patches: list[str],
    p0: float,
    *,
    ami_patterns: list[str] | None = None,
) -> str:
    blocks = ['     #includeEtc "caseDicts/setConstraintTypes"']
    ami_patterns = ami_patterns or [r"ami_rot\d+"]
    for p in patches:
        if is_ami_patch(p, ami_patterns):
            blocks.append(f"    {p}\n{_cyclic_ami_bc('p')}")
        elif "_to_" in p:
            blocks.append(
                f"""    {p}
    {{
        type            calculated;
        value           $internalField;
    }}"""
            )
        else:
            blocks.append(
                f"""    {p}
    {{
        type            calculated;
        value           $internalField;
    }}"""
            )
    return (
        foam_header("volScalarField", "p", "0")
        + f"""
dimensions      [1 -1 -2 0 0 0 0];
internalField   uniform {p0};

boundaryField
{{
{chr(10).join(blocks)}
}}

// ************************************************************************* //
"""
    )


def field_p_rgh(
    patches: list[str],
    p0: float,
    *,
    bc_cfg: dict[str, Any] | None = None,
    ami_patterns: list[str] | None = None,
) -> str:
    blocks = ['     #includeEtc "caseDicts/setConstraintTypes"']
    ami_patterns = ami_patterns or [r"ami_rot\d+"]
    bc_cfg = bc_cfg or {}
    for p in patches:
        if p in bc_cfg:
            blocks.append(_bc_block(p, bc_cfg[p], "p_rgh"))
        elif is_ami_patch(p, ami_patterns):
            blocks.append(f"    {p}\n{_cyclic_ami_bc('p_rgh')}")
        elif p == "open":
            # Inlet with fixed velocity -> fixedFluxPressure so the pressure
            # floats and the mass flux is driven by the U boundary.
            blocks.append(
                f"""    {p}
    {{
        type            fixedFluxPressure;
        value           $internalField;
    }}"""
            )
        elif p.endswith("_1") and p.startswith("open"):
            # Outlet -> fixedValue pins the pressure reference (gauge = 0).
            blocks.append(
                f"""    {p}
    {{
        type            fixedValue;
        value           $internalField;
    }}"""
            )
        else:
            blocks.append(
                f"""    {p}
    {{
        type            fixedFluxPressure;
        value           $internalField;
    }}"""
            )
    return (
        foam_header("volScalarField", "p_rgh", "0")
        + f"""
dimensions      [1 -1 -2 0 0 0 0];
internalField   uniform {p0};

boundaryField
{{
{chr(10).join(blocks)}
}}

// ************************************************************************* //
"""
    )


def create_patch_ami(
    pairs: list[tuple[str, str]],
    rot_axis: list[float] | None = None,
    match_tolerance: float = 0.001,
) -> str:
    """createPatchDict: convert cgns2foam AMI wall pairs to cyclicAMI (pre-split)."""
    axis = rot_axis or [0, 0, 1]
    blocks = []
    for m, s in pairs:
        blocks.append(
            f"""    {{
        name            {m};
        patchInfo
        {{
            type            cyclicAMI;
            matchTolerance  {match_tolerance};
            neighbourPatch  {s};
            transform       noOrdering;
            rotationAxis    ({axis[0]} {axis[1]} {axis[2]});
        }}
        constructFrom     patches;
        patches         ({m});
    }}

    {{
        name            {s};
        patchInfo
        {{
            type            cyclicAMI;
            matchTolerance  {match_tolerance};
            neighbourPatch  {m};
            transform       noOrdering;
            rotationAxis    ({axis[0]} {axis[1]} {axis[2]});
        }}
        constructFrom     patches;
        patches         ({s});
    }}"""
        )
    inner = "\n\n".join(blocks) if blocks else ""
    return (
        foam_header("dictionary", "createPatchDict", "system")
        + f"""
pointSync false;

patches
(
{inner}
);

// ************************************************************************* //
"""
    )


def tolerance_dict(
    point_merge_tol: float = 0.1,
    edge_merge_tol: float = 0.05,
) -> str:
    return (
        foam_header("dictionary", "toleranceDict", "system")
        + f"""
pointMergeTol            {point_merge_tol};
edgeMergeTol             {edge_merge_tol};
nFacesPerSlaveEdge       5;
edgeFaceEscapeLimit      10;
integralAdjTol           {point_merge_tol};
edgeMasterCatchFraction  0.4;
edgeCoPlanarTol          0.8;
edgeEndCutoffTol         0.0001;

// ************************************************************************* //
"""
    )


def stitch_mesh_dict(entries: list[tuple[str, str, str]]) -> str:
    """stitchMeshDict: list of (name, master, slave, match mode)."""
    blocks = []
    for name, master, slave, mode in entries:
        blocks.append(
            f"""{name}
{{
    match   {mode};
    master  {master};
    slave   {slave};
}}"""
        )
    inner = "\n\n".join(blocks)
    return (
        foam_header("dictionary", "stitchMeshDict", "system")
        + f"""
{inner}

// ************************************************************************* //
"""
    )


def topo_set_cell_zones(zone_names: list[str]) -> str:
    """topoSetDict: rebuild cellZones from existing zone labels (OpenFOAM CHT prep)."""
    actions = []
    for zname in zone_names:
        set_name = f"{zname}_cells"
        actions.append(
            f"""    {{
        name    {set_name};
        type    cellSet;
        action  new;
        source  zoneToCell;
        zone    {zname};
    }}
    {{
        name    {zname};
        type    cellZoneSet;
        action  new;
        source  setToCellZone;
        set     {set_name};
    }}"""
        )
    inner = "\n\n".join(actions)
    return (
        foam_header("dictionary", "topoSetDict", "system")
        + f"""
actions
(
{inner}
);

// ************************************************************************* //
"""
    )


def combine_cell_zones_dict(combine: dict[str, list[str]]) -> str:
    """Write a cellZones replacement mapping multiple zones → one region zone."""
    lines = []
    for new_name, sources in combine.items():
        lines.append(f"    {new_name} {{ type cellZone; cellLabels List<label> 0(); }}")
    return "\n".join(lines)
