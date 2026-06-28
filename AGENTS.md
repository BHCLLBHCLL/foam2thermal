# AGENTS.md

## Cursor Cloud specific instructions

`foam2thermal` is a **Python 3.9+ CLI tool** (no server, no database, no GUI) that
generates OpenFOAM `chtMultiRegionSimpleFoam` multi-region CHT cases from a
single-body mesh + a JSON config. Its only Python dependency is `numpy`
(installed by the update script). Usage is documented in `README.md`; the CLI
entry point is `setup_cht_case.py` and subcommands are `check / scan / build / run`.

Non-obvious caveats for working in this repo:

- **Use `python3`, not `python`.** This VM has no `python` shim; `README.md`
  examples that say `python setup_cht_case.py ...` should be run as
  `python3 setup_cht_case.py ...`.
- **`check` / `scan` / `build` need only Python + numpy** (no OpenFOAM). These
  cover the bulk of the toolkit and are the right target for local development
  and smoke testing.
- **`run` (prep/solve) requires an external OpenFOAM v2412 install** that is NOT
  present here, and the default `openfoam` paths in the JSON configs point at a
  Windows/MSYS2 layout. Treat `run`/solve as out of scope unless OpenFOAM is
  installed separately (Linux/WSL recommended per `DEV_SUMMARY.md`).
- **No input meshes are in the repo.** `tests/` (real cgns2foam meshes) and
  `cases/` (generated output) are gitignored and absent on a fresh checkout, so
  any end-to-end run must supply or synthesize a mesh. To smoke-test
  `check/scan/build` without cgns2foam, build a tiny OpenFOAM `polyMesh` using
  the project's own binary writers in `src/foam2thermal/mesh_coalesce.py`
  (`_write_binary_vector_field`, `_write_binary_compact_face_list`,
  `_write_binary_label_list`) plus `mesh.write_cell_zones_v2412`, with cellZone
  names matching the config's `regions[].cellZones`.
- **No automated test suite and no linter/formatter are configured.** For a
  basic static check use `python3 -m py_compile setup_cht_case.py src/foam2thermal/*.py scripts/*.py`.
