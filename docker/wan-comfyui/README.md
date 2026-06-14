# kinoforge/wan-comfyui — pre-baked Wan 2.1 + ComfyUI image

This Dockerfile builds a slim, pre-baked container image used by
kinoforge's C28 restart-loop fix. All ComfyUI + custom-node clone +
pip install work is moved to build time, eliminating the most common
boot-time failure modes that drove the chronic container-restart loop
on the stock `runpod/pytorch:2.4.0-...` base image.

## Layers

| Layer | What |
|---|---|
| Base | `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` |
| `awscli` | pre-installed so the C28 A2 diagnostic-mode EXIT trap can `aws s3 cp` boot logs |
| ComfyUI | cloned at `COMFYUI_REF`, `pip install -r requirements.txt` |
| Custom nodes | Kijai Wan, KJNodes, VHS — each cloned, checked out at the pinned ref, pip-installed |
| Smoke | build-time `import comfy` — fails the build (loud) instead of pod boot (silent + restart-loop) |

## Build (local)

```bash
TAG=v0.3.10-088128b2-cu124 \
  docker build \
    --build-arg COMFYUI_REF=v0.3.10 \
    --build-arg KIJAI_WAN_REF=088128b224242e110d3906c6750e9a3a348a659b \
    --build-arg KJNODES_REF=369c8aee9ad4641823d0ffd7035076bcd297b6f2 \
    --build-arg VHS_REF=4ee72c065db22c9d96c2427954dc69e7b908444b \
    --build-arg IMAGE_TAG=${TAG} \
    -t kinoforge/wan-comfyui:${TAG} \
    -t kinoforge/wan-comfyui:latest \
    docker/wan-comfyui/
```

A `pixi run build-image-wan-comfyui` task wraps the same invocation
with `docker login` + `docker push` for one-shot rebuilds.

## Build (CI)

The `.github/workflows/build-wan-comfyui-image.yml` workflow exposes
`workflow_dispatch` only (NOT push-triggered — image rebuilds are
explicit operator actions). It uses `DOCKERHUB_USERNAME` +
`DOCKERHUB_TOKEN` secrets from the repo's Actions settings.

## Ref pinning policy

The four `ARG` refs MUST stay in sync with
`tests/live/cfg_c27_phase_b.yaml`. Updating either side requires
re-building the image AND re-running the Phase B acceptance smoke.

## Slim-mode boot interaction

When the kinoforge cfg's `compute.image` starts with
`kinoforge/wan-comfyui:`, `render_provision` takes the slim-mode branch
(see `src/kinoforge/engines/comfyui/__init__.py`) and emits a provision
script that skips the ComfyUI clone + pip + custom-node clone steps —
the pre-baked image already has them. Selfterm + `exec python main.py`
still emit unchanged.
