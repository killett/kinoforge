"""SeedVR2Engine — UpscalerEngine impl for ByteDance-Seed/SeedVR2.

Talks to the FastAPI server on the pod via /upscale + /upscale/status/{id}.
Reuses :func:`kinoforge.engines._proxy_retry.retry_proxy_call` to absorb
RunPod proxy startup-window 404/502s (see project memory
``task7_comfyui_404_regression``).

Self-registers at module import via
``register_upscaler("seedvr2", SeedVR2Engine)``.
"""

from __future__ import annotations

import json as _json
import time
import urllib.request
from typing import Any, cast

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    NotYetImplementedError,
    UnknownAdapter,
    UnsupportedScaleError,
    UpscaleFailed,
)
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    RenderedProvision,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.engines._proxy_retry import retry_proxy_call

# Pinned upstream commit; bump deliberately when upstream releases a
# verified-good build. Tracked in docs/engines.md.
# IMPORTANT: Replace with a real SHA before T18 live spend — the placeholder
# string makes `pip install git+...@PLACEHOLDER` fail loudly rather than
# silently grabbing whatever upstream main happens to be that day.
_UPSTREAM_COMMIT = "PLACEHOLDER_REPLACE_BEFORE_LIVE_SPEND"
_UPSTREAM_GIT = f"git+https://github.com/ByteDance-Seed/SeedVR@{_UPSTREAM_COMMIT}"

_SUPPORTED_FACTORS: tuple[float, ...] = (2.0, 4.0)
_DEFAULT_SERVER_PORT = "8000"


class SeedVR2Engine(UpscalerEngine):
    """SeedVR2 video upscaler — submits to the diffusers-server's /upscale endpoints."""

    name = "seedvr2"
    requires_compute = True
    requires_local_weights = True
    supported_scales = tuple(
        ScaleTarget(kind="factor", value=v) for v in _SUPPORTED_FACTORS
    )

    def validate_spec(self, job: UpscaleJob) -> None:
        """Refuse height-targets (v1) and any factor outside ``supported_scales``."""
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"SeedVR2 v1 does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale Nx"
            )
        if job.scale.value not in _SUPPORTED_FACTORS:
            raise UnsupportedScaleError(scale=job.scale, engine_name=self.name)

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return sink-filename slug ``"seedvr2-{variant}-{precision}"`` or ``""``."""
        try:
            upscale_block = cast(dict[str, Any], cfg["upscale"])
            seedvr2_block = cast(dict[str, Any], upscale_block["seedvr2"])
            variant = str(seedvr2_block["variant"]).lower()
            precision = str(seedvr2_block["precision"])
            return f"seedvr2-{variant}-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit the SeedVR2-only install + weights-fetch fragment.

        The orchestrator composes this with the base diffusers provision —
        ``script`` runs additively after the server is installed, ``run_cmd``
        is empty (the diffusers server stays the process entrypoint).
        """
        upscale_block = cast(
            dict[str, Any], cfg.get("upscale", {}) if isinstance(cfg, dict) else {}
        )
        block = cast(dict[str, Any], upscale_block.get("seedvr2", {}))
        variant = block.get("variant", "3B")
        precision = block.get("precision", "fp8")
        script = (
            f'pip install --no-build-isolation "seedvr @ {_UPSTREAM_GIT}"\n'
            f"python -m kinoforge.upscalers.seedvr2._fetch_weights "
            f"--variant {variant} --precision {precision} "
            f"--dest /workspace/models/seedvr2\n"
        )
        return RenderedProvision(
            script=script,
            run_cmd=[],
            image="",
            ports=[],
            env_required=[],
        )

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """No-op — work is captured in :meth:`render_provision`."""
        del instance, cfg, cancel_token

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        """POST /upscale, poll /upscale/status/{id}, return UpscaleResult.

        Each HTTP call is wrapped in :func:`retry_proxy_call` so RunPod
        proxy startup-window 404/502s don't fail-fast a live submit.
        """
        self.validate_spec(job)
        if instance is None:
            raise ValueError("SeedVR2Engine requires a compute instance")
        base = self._base_url(instance)

        submit_payload = self._build_payload(job, cfg)
        submit_resp = retry_proxy_call(
            label="seedvr2.submit",
            url=f"{base}/upscale",
            fn=lambda: _http_json(
                method="POST", url=f"{base}/upscale", payload=submit_payload
            ),
            sleep=time.sleep,
        )
        job_id: str = submit_resp["job_id"]

        t0 = time.monotonic()
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            status = retry_proxy_call(
                label="seedvr2.status",
                url=f"{base}/upscale/status/{job_id}",
                fn=lambda: _http_json(
                    method="GET", url=f"{base}/upscale/status/{job_id}"
                ),
                sleep=time.sleep,
            )
            state = status["state"]
            if state == "done":
                result = status["result"]
                artifact_url = f"{base}/artifacts/{result['filename']}"
                return UpscaleResult(
                    artifact=Artifact(
                        uri=artifact_url,
                        sha256=result["sha256"],
                        size=result["size"],
                    ),
                    input_resolution=tuple(result["input_resolution"]),
                    output_resolution=tuple(result["output_resolution"]),
                    elapsed_s=time.monotonic() - t0,
                    engine_meta=result.get("engine_meta", {}),
                )
            if state == "error":
                raise UpscaleFailed(job_id=job_id, server_error=status.get("error", ""))
            time.sleep(2.0)

    @staticmethod
    def _base_url(instance: Instance) -> str:
        endpoints = instance.endpoints or {}
        url = endpoints.get(_DEFAULT_SERVER_PORT) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"SeedVR2Engine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")

    def _build_payload(self, job: UpscaleJob, cfg: dict[str, object]) -> dict[str, Any]:
        upscale_block = cast(
            dict[str, Any], cfg.get("upscale", {}) if isinstance(cfg, dict) else {}
        )
        return {
            "source_url": job.source.uri,
            "scale": (
                f"{job.scale.value:g}x"
                if job.scale.kind == "factor"
                else f"{int(job.scale.value)}p"
            ),
            "engine": "seedvr2",
            "seedvr2": upscale_block.get("seedvr2", {}),
        }


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        body = resp.read()
    return cast(dict[str, Any], _json.loads(body))


# Self-register on import. Duplicate-register raises UnknownAdapter; we
# absorb that single case so repeated test-suite imports stay idempotent.
try:
    registry.register_upscaler("seedvr2", SeedVR2Engine)
except UnknownAdapter:
    pass
