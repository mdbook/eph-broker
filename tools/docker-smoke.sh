#!/bin/sh
# Exercise the real image without sharing a running broker's loopback interface.
set -eu

root=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
image="eph-broker-smoke:$$"
built=
container=
volume=

cleanup() {
    result=$?
    trap - 0
    if [ "$result" -ne 0 ] && [ -n "$container" ]; then
        docker logs "$container" >&2 || true
    fi
    if [ -n "$container" ]; then docker rm -f "$container" >/dev/null || true; fi
    if [ -n "$volume" ]; then docker volume rm "$volume" >/dev/null || true; fi
    if [ -n "$built" ]; then docker image rm "$image" >/dev/null || true; fi
    exit "$result"
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM

docker build --tag "$image" "$root"
built=1
volume=$(docker volume create)
# Match compose.yaml's hardening and health check, but give this test its own
# network namespace with no egress or published ports. Only its own loopback is used.
container=$(docker create \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:mode=1777 \
    --mount "type=volume,source=$volume,target=/var/lib/eph-broker" \
    --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).read()\"" \
    --health-interval 10s --health-timeout 5s --health-retries 3 --health-start-period 10s \
    "$image")
docker start "$container" >/dev/null

attempt=0
while :; do
    status=$(docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' "$container")
    case "$status" in
        'running healthy') break ;;
        'running starting') ;;
        *) echo "Smoke container failed: $status" >&2; exit 1 ;;
    esac
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo 'Smoke container did not become healthy within 60 seconds' >&2
        exit 1
    fi
    sleep 1
done

docker exec "$container" python -c '
import json
import urllib.request

for path in ("/health", "/"):
    with urllib.request.urlopen("http://127.0.0.1:8787" + path, timeout=3) as response:
        assert response.status == 200, (path, response.status)
        body = response.read()
        if path == "/health":
            assert json.loads(body)["ok"] is True
        else:
            # urllib follows the existing / -> /dash.html redirect.
            assert b"<html" in body.lower()
    print("GET", path, "200")
'
echo 'Docker smoke passed'
