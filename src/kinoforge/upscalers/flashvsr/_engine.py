"""FlashVSREngine — HTTP-aware UpscalerEngine impl backed by FlashVSR runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} + /upload endpoints.
Reuses :func:`kinoforge.engines._proxy_retry.retry_proxy_call` for RunPod
proxy startup-window 404/502 tolerance.
"""

from __future__ import annotations

import hashlib
import json as _json
import time
import urllib.request
from pathlib import Path
from typing import IO, Any, cast
from urllib.error import HTTPError

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    NotYetImplementedError,
    UploadIntegrityError,
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


class FlashVSREngine(UpscalerEngine):
    """FlashVSR v1.1 streaming diffusion video upscaler."""

    name = "flashvsr"
    requires_compute = True
    requires_local_weights = True
    # Empty tuple = runtime declares scale at weights-load time (spec §3.5);
    # StreamingDMDPipeline's `.scale` attribute reports it after from_pretrained.
    supported_scales: tuple[ScaleTarget, ...] = ()

    def validate_spec(self, job: UpscaleJob) -> None:
        """Refuse height-target scales (spec §2 non-goal)."""
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale Nx"
            )

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return ``flashvsr-wan21-<precision>`` slug for the server LRU."""
        try:
            block = cast(
                dict[str, Any], cast(dict[str, Any], cfg["upscale"])["flashvsr"]
            )
            precision = str(block["precision"])
            return f"flashvsr-wan21-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit BSA compile + FlashVSR install + weights fetch + hermetic flip."""
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["flashvsr"])
        bundle = str(block["weights_bundle"])
        long_video = "1" if block.get("long_video_mode") else "0"
        script = "".join(
            [
                "set -euo pipefail\n",
                'python -c "import torch; '
                "assert torch.cuda.get_device_capability()[0] >= 8, "
                "f'flashvsr: BSA needs SM80+, got {torch.cuda.get_device_capability()}'"
                '" || exit 87\n',
                "export TORCH_EXTENSIONS_DIR=/workspace/.cache/bsa\n",
                "export MAX_JOBS=4\n",
                'mkdir -p "$TORCH_EXTENSIONS_DIR"\n',
                # Pin BSA to commit 3453bbb1 (Feb 2025) — the last version
                # before the Blackwell (compute_100/110/120) `-gencode` flags
                # landed in setup.py. Later commits (incl. main and the
                # 2025-12 v0.0.2 tag) unconditionally add compute_120 which
                # the pod's CUDA 12.4/12.8 nvcc rejects with
                # `nvcc fatal : Unsupported gpu architecture 'compute_120'`.
                # TORCH_CUDA_ARCH_LIST does NOT override — BSA's setup.py
                # ignores it and hardcodes its own -gencode list.
                # T8 attempts #2 + #3 evidence (2026-07-01, both failed here).
                # If Blackwell targets are needed later, bump the pod base
                # image to CUDA 12.9+ and re-pin BSA to main.
                "pip install "
                '"git+https://github.com/mit-han-lab/Block-Sparse-Attention@3453bbb1" '
                "--no-build-isolation --no-cache-dir\n",
                "pip install "
                '"git+https://github.com/OpenImagingLab/FlashVSR@v1.1" '
                '"imageio[ffmpeg]>=2.34"\n',
                "python -m kinoforge.upscalers.flashvsr._fetch_weights "
                f"--bundle {bundle} --dest /workspace/models/flashvsr "
                f"--include-long-video {long_video}\n",
                "export HF_HUB_OFFLINE=1\n",
            ]
        )
        return RenderedProvision(
            script=script,
            run_cmd=[],
            image="",
            ports=[],
            env_required=["HF_TOKEN"],
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
            raise ValueError("FlashVSREngine requires a compute instance")
        base = self._base_url(instance)

        source_uri = job.source.uri
        if source_uri.startswith("file://") or source_uri.startswith("/"):
            local_path = Path(source_uri.removeprefix("file://"))
            source_uri = self._upload_source(instance, local_path)

        block = cast(
            dict[str, Any],
            cast(dict[str, Any], cfg.get("upscale", {})).get("flashvsr", {}),
        )
        submit_payload = {
            "source_url": source_uri,
            "source_filename": source_uri.rsplit("/", 1)[-1] or "in.mp4",
            "scale": f"{job.scale.value:g}x",
            "engine": "flashvsr",
            "flashvsr": block,
        }
        submit_resp = retry_proxy_call(
            label="flashvsr.submit",
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
                label="flashvsr.status",
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

    def _put_upload(
        self,
        url: str,
        data: IO[bytes],
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        """Single PUT /upload — mirrors SpandrelEngine._put_upload."""
        req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
            url, data=data, method="PUT", headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return cast(dict[str, Any], _json.loads(resp.read().decode("utf-8")))

    def _upload_source(self, instance: Instance, local_path: Path) -> str:
        """Upload ``local_path`` mp4 via PUT /upload; return file:// URL.

        Mirrors SpandrelEngine._upload_source: computes sha256, streams
        the body, verifies the server's reported sha, recovers once
        from a proxy cold-warmup 502.
        """
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": "video/mp4",
            "X-Filename": f"{short}.mp4",
            "Content-Length": str(len(body)),
            "User-Agent": "kinoforge-flashvsr/0.1",
        }

        last_error: HTTPError | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(2):
            with local_path.open("rb") as fobj:
                try:
                    payload = self._put_upload(url, fobj, headers, timeout=600)
                    last_error = None
                    break
                except HTTPError as exc:
                    last_error = exc
                    if exc.code == 502 and attempt == 0:
                        continue
                    raise
        if payload is None:
            raise RuntimeError(
                f"_upload_source loop completed without payload "
                f"(last_error={last_error!r})"
            )

        server_sha = str(payload.get("sha256", ""))
        if server_sha != local_sha:
            raise UploadIntegrityError(
                local_sha256=local_sha,
                server_sha256=server_sha,
                bytes_sent=len(body),
            )
        return f"file://{payload['path']}"

    @staticmethod
    def _base_url(instance: Instance) -> str:
        endpoints = instance.endpoints or {}
        url = endpoints.get(_DEFAULT_SERVER_PORT) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"FlashVSREngine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    # Cloudflare (RunPod's proxy edge) returns 403 to the default Python-urllib
    # User-Agent — a plain kinoforge UA clears the gate. Same fix as
    # SpandrelEngine + DiffusersEngine.
    headers: dict[str, str] = {"User-Agent": "kinoforge-flashvsr/0.1"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
        url,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        body = resp.read()
    return cast(dict[str, Any], _json.loads(body))
