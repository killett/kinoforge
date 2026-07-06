"""RifeEngine — HTTP-aware InterpolatorEngine impl backed by the RIFE runtime.

Talks to the embedded server's /interpolate + /interpolate/status/{id} + /upload
endpoints. Reuses :func:`kinoforge.engines._proxy_retry.retry_proxy_call` for
RunPod proxy startup-window 404/502 tolerance. Mirrors
:mod:`kinoforge.upscalers.flashvsr._engine`.
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
from kinoforge.core.errors import InterpolationError, UploadIntegrityError
from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    InterpolateJob,
    InterpolateResult,
    InterpolatorEngine,
    RenderedProvision,
)
from kinoforge.engines._proxy_retry import retry_proxy_call

_DEFAULT_SERVER_PORT = "8000"

# Practical-RIFE: the arbitrary-timestep RIFE v4 inference repo. Pinned to the
# latest commit as of 2026-07-05 (verified via the GitHub commits API). RIFE is
# light — no BSA wheel, no diffsynth.
_PRACTICAL_RIFE_COMMIT = "17d8c7a1005b37f4c97bfee04e316aaec7fdc536"


class RifeEngine(InterpolatorEngine):
    """RIFE v4 arbitrary-timestep frame interpolator (pod-side)."""

    name = "rife"
    requires_compute = True
    requires_local_weights = True
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def validate_spec(self, job: InterpolateJob) -> None:
        """Refuse a non-positive target frame rate."""
        if job.target_fps <= 0:
            raise ValueError(f"rife: target_fps must be > 0, got {job.target_fps}")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return ``rife-<model>`` slug for the server LRU. MUST NOT raise."""
        try:
            block = cast(
                dict[str, Any], cast(dict[str, Any], cfg["interpolate"])["rife"]
            )
            return f"rife-{block['model']}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit a Practical-RIFE install + RIFE weights fetch bootstrap.

        Weights are fetched inline via ``huggingface-cli download`` from the
        ``hf:`` ref rather than a bespoke ``_fetch_weights`` module — RIFE ships
        a single small ``train_log/`` folder (flownet.pkl + arch .py). The
        exact per-model file layout is confirmed against the live repo in the
        Task 11 example cfg + smoke.
        """
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["interpolate"])["rife"])
        weights_ref = str(block["weights_ref"])
        hf_repo = weights_ref.removeprefix("hf:")
        script = "".join(
            [
                "set -euo pipefail\n",
                "pip install --no-deps "
                f'"git+https://github.com/hzwer/Practical-RIFE'
                f'@{_PRACTICAL_RIFE_COMMIT}"\n',
                'pip install "torch" "numpy" "opencv-python-headless" '
                '"imageio[ffmpeg]" "huggingface_hub"\n',
                # Fetch RIFE weights (train_log/*.pkl + arch) to a stable path.
                "mkdir -p /workspace/models/rife\n",
                f'huggingface-cli download "{hf_repo}" '
                "--local-dir /workspace/models/rife\n",
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

    def interpolate(
        self,
        instance: Instance | None,
        job: InterpolateJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> InterpolateResult:
        """POST /interpolate, poll /interpolate/status/{id}, return the result."""
        self.validate_spec(job)
        if instance is None:
            raise ValueError("RifeEngine requires a compute instance")
        base = self._base_url(instance)

        source_uri = job.source.uri
        if source_uri.startswith("file://") or source_uri.startswith("/"):
            local_path = Path(source_uri.removeprefix("file://"))
            source_uri = self._upload_source(instance, local_path)

        block = cast(
            dict[str, Any],
            cast(dict[str, Any], cfg.get("interpolate", {})).get("rife", {}),
        )
        submit_payload = {
            "source_url": source_uri,
            "source_filename": source_uri.rsplit("/", 1)[-1] or "in.mp4",
            "target_fps": job.target_fps,
            "engine": "rife",
            "rife": block,
        }
        submit_resp = retry_proxy_call(
            label="rife.submit",
            url=f"{base}/interpolate",
            fn=lambda: _http_json(
                method="POST", url=f"{base}/interpolate", payload=submit_payload
            ),
            sleep=time.sleep,
        )
        job_id: str = submit_resp["job_id"]

        t0 = time.monotonic()
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            status = retry_proxy_call(
                label="rife.status",
                url=f"{base}/interpolate/status/{job_id}",
                fn=lambda: _http_json(
                    method="GET", url=f"{base}/interpolate/status/{job_id}"
                ),
                sleep=time.sleep,
            )
            state = status["state"]
            if state == "done":
                result = status["result"]
                return InterpolateResult(
                    artifact=Artifact(
                        uri=f"{base}/artifacts/{result['filename']}",
                        sha256=result["sha256"],
                        size=result["size"],
                    ),
                    input_fps=result["input_fps"],
                    output_fps=result["output_fps"],
                    input_frame_count=result["input_frame_count"],
                    output_frame_count=result["output_frame_count"],
                    elapsed_s=time.monotonic() - t0,
                    engine_meta=result.get("engine_meta", {}),
                )
            if state == "error":
                raise InterpolationError(
                    job_id=job_id, server_error=status.get("error", "")
                )
            time.sleep(2.0)

    def _put_upload(
        self,
        url: str,
        data: IO[bytes],
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        """Single PUT /upload — mirrors FlashVSREngine._put_upload."""
        req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
            url, data=data, method="PUT", headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return cast(dict[str, Any], _json.loads(resp.read().decode("utf-8")))

    def _upload_source(self, instance: Instance, local_path: Path) -> str:
        """Upload ``local_path`` mp4 via PUT /upload; return file:// URL.

        Mirrors FlashVSREngine._upload_source: computes sha256, streams the
        body, verifies the server's reported sha, recovers once from a proxy
        cold-warmup 502.
        """
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": "video/mp4",
            "X-Filename": f"{short}.mp4",
            "Content-Length": str(len(body)),
            "User-Agent": "kinoforge-rife/0.1",
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
                f"RifeEngine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    # Cloudflare (RunPod's proxy edge) 403s the default Python-urllib UA — a
    # plain kinoforge UA clears the gate (same fix as FlashVSREngine).
    headers: dict[str, str] = {"User-Agent": "kinoforge-rife/0.1"}
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
