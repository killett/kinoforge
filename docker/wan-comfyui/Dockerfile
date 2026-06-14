# C28 Phase B — pre-baked image for the Wan + ComfyUI restart-loop fix.
#
# The chronic container-restart loop on the stock runpod/pytorch base
# image is driven primarily by pip / custom-node install drift during the
# kinoforge bootstrap. Baking the heavy install steps into a published
# image moves them to BUILD time (where failures are loud and obvious)
# and skips them at RUN time, eliminating the failure mode from the
# critical path.
#
# All four upstream refs are pinned via ARG so a re-build with the same
# args produces a byte-identical wheel set. KIJAI_WAN_REF / KJNODES_REF /
# VHS_REF are the SAME refs used in tests/live/cfg_c27_phase_b.yaml so
# the pre-baked image stays drift-locked to the validated C27 cfg.

FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

ARG COMFYUI_REF=v0.3.10
ARG KIJAI_WAN_REF=088128b224242e110d3906c6750e9a3a348a659b
ARG KJNODES_REF=369c8aee9ad4641823d0ffd7035076bcd297b6f2
ARG VHS_REF=4ee72c065db22c9d96c2427954dc69e7b908444b

# C28 Task 7 follow-up: pre-install awscli so the diagnostic-mode EXIT
# trap can `aws s3 cp` the boot-log snapshot. Without it, the trap runs
# silently and the Phase A capture path fails to produce evidence.
RUN pip install --no-cache-dir awscli==1.* && \
    aws --version

WORKDIR /workspace

RUN git clone --depth 1 --branch ${COMFYUI_REF} \
      https://github.com/comfyanonymous/ComfyUI /workspace/ComfyUI && \
    cd /workspace/ComfyUI && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /workspace/ComfyUI && \
    git clone https://github.com/kijai/ComfyUI-WanVideoWrapper \
      custom_nodes/ComfyUI-WanVideoWrapper && \
    cd custom_nodes/ComfyUI-WanVideoWrapper && \
    git checkout ${KIJAI_WAN_REF} && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /workspace/ComfyUI && \
    git clone https://github.com/kijai/ComfyUI-KJNodes \
      custom_nodes/ComfyUI-KJNodes && \
    cd custom_nodes/ComfyUI-KJNodes && \
    git checkout ${KJNODES_REF} && \
    pip install --no-cache-dir -r requirements.txt

RUN cd /workspace/ComfyUI && \
    git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite \
      custom_nodes/ComfyUI-VideoHelperSuite && \
    cd custom_nodes/ComfyUI-VideoHelperSuite && \
    git checkout ${VHS_REF} && \
    pip install --no-cache-dir -r requirements.txt

# Build-time import smoke — a broken pip combo OR a missing custom-node
# dependency fails `docker build` here instead of at pod-boot in prod.
RUN cd /workspace/ComfyUI && \
    python -c "import sys; sys.path.insert(0,'.'); import comfy"

# Build-time tag stamp for traceability — surfaces in the running pod via
# `printenv KINOFORGE_IMAGE_TAG` and lets the Phase B smoke verify it
# launched the expected slim image (not the stock base).
ARG IMAGE_TAG=unknown
ENV KINOFORGE_IMAGE_TAG=${IMAGE_TAG}

# Image is now ready. The kinoforge-emitted provision script (slim-mode
# branch — see render_provision §B2) skips the ComfyUI clone / pip /
# custom-node clone steps and goes straight to `exec python main.py`.
