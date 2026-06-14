#!/usr/bin/env bash
# C28 B1 — build + push kinoforge/wan-comfyui:<TAG> to Docker Hub.
#
# Reads DOCKERHUB_USERNAME / DOCKERHUB_TOKEN from env. Overridable tag via
# TAG=... env var; defaults match the validated refs in
# tests/live/cfg_c27_phase_b.yaml. Image is public so no registryAuthId on
# the RunPod side; downstream slim-mode boot pulls anonymously.
set -euo pipefail

TAG=${TAG:-v0.3.10-088128b2-cu124}
COMFYUI_REF=${COMFYUI_REF:-v0.3.10}
KIJAI_WAN_REF=${KIJAI_WAN_REF:-088128b224242e110d3906c6750e9a3a348a659b}
KJNODES_REF=${KJNODES_REF:-369c8aee9ad4641823d0ffd7035076bcd297b6f2}
VHS_REF=${VHS_REF:-4ee72c065db22c9d96c2427954dc69e7b908444b}

if [ -z "${DOCKERHUB_USERNAME:-}" ] || [ -z "${DOCKERHUB_TOKEN:-}" ]; then
    echo "FAIL: DOCKERHUB_USERNAME or DOCKERHUB_TOKEN missing — add to .env" >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "FAIL: docker CLI not on PATH" >&2
    exit 1
fi

echo "build: kinoforge/wan-comfyui:${TAG}"
docker build \
    --build-arg "COMFYUI_REF=${COMFYUI_REF}" \
    --build-arg "KIJAI_WAN_REF=${KIJAI_WAN_REF}" \
    --build-arg "KJNODES_REF=${KJNODES_REF}" \
    --build-arg "VHS_REF=${VHS_REF}" \
    --build-arg "IMAGE_TAG=${TAG}" \
    -t "kinoforge/wan-comfyui:${TAG}" \
    -t "kinoforge/wan-comfyui:latest" \
    docker/wan-comfyui/

echo "login: hub.docker.com (user=${DOCKERHUB_USERNAME})"
echo "${DOCKERHUB_TOKEN}" | \
    docker login --username "${DOCKERHUB_USERNAME}" --password-stdin

echo "push: kinoforge/wan-comfyui:${TAG}"
docker push "kinoforge/wan-comfyui:${TAG}"
echo "push: kinoforge/wan-comfyui:latest"
docker push "kinoforge/wan-comfyui:latest"

echo "OK: pushed kinoforge/wan-comfyui:${TAG} + :latest"
