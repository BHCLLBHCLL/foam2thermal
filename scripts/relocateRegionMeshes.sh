#!/bin/sh
# splitMeshRegions -overwrite may leave one region mesh in constant/polyMesh.
# Move it to constant/<first_missing_region>/polyMesh when needed.

set -e
cd "${0%/*}/.." || exit 1

if [ ! -f constant/polyMesh/boundary ]; then
    exit 0
fi

if [ ! -f system/regionProperties ]; then
    exit 0
fi

for region in $(foamListRegions 2>/dev/null); do
    if [ ! -f "constant/${region}/polyMesh/boundary" ]; then
        echo "  relocating constant/polyMesh -> constant/${region}/polyMesh"
        mkdir -p "constant/${region}"
        mv constant/polyMesh "constant/${region}/polyMesh"
        exit 0
    fi
done

echo "WARNING: constant/polyMesh exists but all region meshes seem present"
exit 0
