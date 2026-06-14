#!/bin/sh
# Verify regional polyMesh/boundary exists for every region in regionProperties.

set -e
cd "${0%/*}/.." || exit 1

if [ -f system/regionProperties ] && [ ! -f constant/regionProperties ]; then
    cp system/regionProperties constant/regionProperties
fi

regions=""
if [ -f constant/regionProperties ]; then
    regions=$(sed -n '/^fluid/,/^);/p;/^solid/,/^);/p' constant/regionProperties \
        | grep -o '"[^"]*"' | tr -d '"')
fi
if [ -z "$regions" ]; then
    regions=$(foamListRegions 2>/dev/null || true)
fi

missing=""
for region in $regions; do
    region=$(printf '%s' "$region" | tr -d '\r')
    if [ ! -f "constant/${region}/polyMesh/boundary" ]; then
        missing="${missing} ${region}"
    fi
done

if [ -n "$missing" ]; then
    echo "ERROR: missing regional polyMesh for:${missing}"
    exit 1
fi

n=$(echo "$regions" | wc -w)
echo "OK: all ${n} region meshes present"
