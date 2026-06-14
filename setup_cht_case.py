#!/usr/bin/env python3
"""foam2thermal CLI – standard 4-argument interface.

Usage::

    python setup_cht_case.py <command> <input_mesh> <config.json> <output_case> [options]

Commands:
    check   validate input mesh
    scan    scan interface patch pairs → output_case/interface_scan.json
    build   generate CHT case into output_case
    run     execute Allrun.pre / solver in output_case

Example::

    python setup_cht_case.py build \\
        tests/laptop_thermal_steady_orig_fix \\
        configs/laptop_thermal_steady.json \\
        cases/laptop_thermal_cht
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from foam2thermal.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
