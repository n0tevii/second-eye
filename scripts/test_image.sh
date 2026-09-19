#!/usr/bin/env bash
set -euo pipefail
image=$1
version=$2
# Test-only dependencies live in a disposable derived image, never the published image.
cat > image-output/Dockerfile.test <<EOF
FROM $image
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==9.1.1 pluggy==1.6.0 iniconfig==2.3.0 packaging==26.3 pygments==2.21.0
EOF
docker build -f image-output/Dockerfile.test -t second-eye-ci-tests release-source
mounts=(-v "$PWD/release-source/tests:/tests:ro")
for directory in scripts deploy; do
    if [[ -d "release-source/$directory" ]]; then
        mounts+=(-v "$PWD/release-source/$directory:/$directory:ro")
    fi
done
docker run --rm --network none "${mounts[@]}" --entrypoint python second-eye-ci-tests -m pytest /tests -q | tee image-output/linux-tests.txt
container=$(docker run -d --network none -e ADMIN_USERNAME=ci-admin -e ADMIN_PASSWORD=ci-only-test-password-1234 -e ENABLE_NOVNC=0 "$image")
trap 'docker rm -f "$container" >/dev/null' EXIT
docker exec -i "$container" python - "$version" < scripts/container_smoke.py | tee image-output/runtime-smoke.txt
