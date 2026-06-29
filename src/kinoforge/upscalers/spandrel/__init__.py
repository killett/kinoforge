"""SpandrelEngine — HTTP-aware UpscalerEngine impl backed by the spandrel runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} endpoints (T9).
Reuses :func:`kinoforge.engines._proxy_retry.retry_proxy_call` to absorb
RunPod proxy startup-window 404/502s.

Self-registers at module import via
``register_upscaler("spandrel", SpandrelEngine)``.
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

_DEFAULT_SERVER_PORT = "8000"


class SpandrelEngine(UpscalerEngine):
    """spandrel-based image super-resolution per-frame video upscaler."""

    name = "spandrel"
    requires_compute = True
    requires_local_weights = True
    # Empty tuple = runtime declares scale at weights-load time (spec §3.5:
    # spandrel's ModelLoader reports model.scale). Matcher pre-flight
    # short-circuits on emptiness; cfg-time validation defers to runtime.
    supported_scales: tuple[ScaleTarget, ...] = ()

    def validate_spec(self, job: UpscaleJob) -> None:
        """Refuse height-target scales (v1 deferred); accept any factor."""
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"spandrel does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale Nx"
            )

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return ``spandrel-<arch>-<precision>`` or empty string on missing keys.

        Spec §3.2: three-token slug matches the server's ``_load_model_to_gpu``
        parser. Scale is implicit in the weights — NOT in the slug.
        """
        try:
            block = cast(
                dict[str, Any], cast(dict[str, Any], cfg["upscale"])["spandrel"]
            )
            arch = str(block["arch"]).lower()
            precision = str(block["precision"])
            return f"spandrel-{arch}-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit the spandrel-only pip-install + weights-fetch fragment."""
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["spandrel"])
        model_url = str(block["model_url"])
        script = (
            'pip install "spandrel>=0.4.2" "imageio[ffmpeg]>=2.34"\n'
            f"python -m kinoforge.upscalers.spandrel._fetch_weights "
            f"--url {model_url} --dest /workspace/models/spandrel\n"
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
        """POST /upscale, poll /upscale/status/{id}, return UpscaleResult."""
        self.validate_spec(job)
        if instance is None:
            raise ValueError("SpandrelEngine requires a compute instance")
        base = self._base_url(instance)

        block = cast(
            dict[str, Any],
            cast(dict[str, Any], cfg.get("upscale", {})).get("spandrel", {}),
        )
        submit_payload = {
            "source_url": job.source.uri,
            "source_filename": job.source.uri.rsplit("/", 1)[-1] or "in.mp4",
            "scale": f"{job.scale.value:g}x",
            "engine": "spandrel",
            "spandrel": block,
        }
        submit_resp = retry_proxy_call(
            label="spandrel.submit",
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
                label="spandrel.status",
                url=f"{base}/upscale/status/{job_id}",
                fn=lambda: _http_json(
                    method="GET", url=f"{base}/upscale/status/{job_id}"
                ),
                sleep=time.sleep,
            )
            state = status["state"]
            if state == "done":
                result = status["result"]
                return UpscaleResult(
                    artifact=Artifact(
                        uri=f"{base}/artifacts/{result['filename']}",
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
                f"SpandrelEngine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")


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


try:
    registry.register_upscaler("spandrel", SpandrelEngine)
except UnknownAdapter:
    pass
