"""SpandrelEngine — HTTP-aware UpscalerEngine impl backed by the spandrel runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} endpoints (T9).
Reuses :func:`kinoforge.engines._proxy_retry.retry_proxy_call` to absorb
RunPod proxy startup-window 404/502s.

Split out of ``__init__.py`` so the on-pod embed (which only embeds
``kinoforge.core.errors`` + ``.scale_target``) can fail the engine import
without poisoning the package — only ``_runtime`` is needed on the pod.
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
from kinoforge.engines._proxy_retry import interpoll_wait, retry_proxy_call

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
        """Emit the spandrel-only pip-install + weights-fetch fragment.

        Weights fetch is inlined as a ``curl`` invocation rather than a
        ``python -m kinoforge.upscalers.spandrel._fetch_weights`` call so
        the on-pod bootstrap doesn't need the kinoforge package
        importable (which would require embedding the full
        kinoforge.core dependency tree, busting the bootstrap script
        past RunPod's 64KB env-var ceiling). Supports the same two
        ref shapes the live SR-weights cfgs use: ``hf:<org>/<repo>/<path>``
        (with ``HF_TOKEN`` Authorization) and plain ``http(s)://``.
        """
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["spandrel"])
        model_url = str(block["model_url"])
        dest_dir = "/workspace/models/spandrel"
        script_lines = [
            'pip install "spandrel>=0.4.2" "imageio[ffmpeg]>=2.34"',
            f"mkdir -p {dest_dir}",
        ]
        if model_url.startswith("hf:"):
            ref = model_url.removeprefix("hf:")
            parts = ref.split("/", 2)
            if len(parts) < 3:
                raise ValueError(
                    f"spandrel hf ref {model_url!r} must be "
                    "hf:<org>/<repo>/<file-or-path>"
                )
            org, repo, sub_path = parts
            file_name = sub_path.rsplit("/", 1)[-1]
            url = f"https://huggingface.co/{org}/{repo}/resolve/main/{sub_path}"
            script_lines.append(
                "curl -L --fail-with-body "
                '-H "Authorization: Bearer ${HF_TOKEN}" '
                f"-o {dest_dir}/{file_name} {url}"
            )
        elif model_url.startswith(("http://", "https://")):
            file_name = model_url.rsplit("/", 1)[-1] or "weights.bin"
            script_lines.append(
                f"curl -L --fail-with-body -o {dest_dir}/{file_name} {model_url}"
            )
        else:
            raise ValueError(
                f"spandrel model_url {model_url!r}: unsupported scheme "
                "(supported: hf:, http(s)://)"
            )
        return RenderedProvision(
            script="\n".join(script_lines) + "\n",
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
            raise ValueError("SpandrelEngine requires a compute instance")
        base = self._base_url(instance)

        source_uri = job.source.uri
        # Accept ``file://`` schemes AND bare absolute paths — the latter is
        # what LocalStore / LocalOutputSink produce for a stage-1 clip when
        # chained into stage-2 upscale (multi-stage warm-reuse). Both branches
        # upload the local mp4 to the pod's ``PUT /upload`` before submitting
        # the ``/upscale`` job.
        if source_uri.startswith("file://") or source_uri.startswith("/"):
            local_path = Path(source_uri.removeprefix("file://"))
            source_uri = self._upload_source(instance, local_path)

        block = cast(
            dict[str, Any],
            cast(dict[str, Any], cfg.get("upscale", {})).get("spandrel", {}),
        )
        submit_payload = {
            "source_url": source_uri,
            "source_filename": source_uri.rsplit("/", 1)[-1] or "in.mp4",
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
            cancel_token=cancel_token,
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
                cancel_token=cancel_token,
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
            interpoll_wait(2.0, cancel_token, time.sleep)

    def _put_upload(
        self,
        url: str,
        data: IO[bytes],
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        """Single PUT /upload request — streams ``data`` body, parses JSON response.

        Split out so tests can patch HTTP without monkeypatching urllib globally,
        and so the retry loop in ``_upload_source`` can swap a fresh file handle
        on each attempt.
        """
        req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
            url, data=data, method="PUT", headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return cast(dict[str, Any], _json.loads(resp.read().decode("utf-8")))

    def _upload_source(self, instance: Instance, local_path: Path) -> str:
        """Upload ``local_path`` mp4 to the pod via PUT /upload; return file:// URL.

        Computes sha256 locally, streams the file body as the PUT payload, and
        cross-checks the server's reported sha256 before returning. Recovers
        once from a proxy cold-warmup 502; subsequent failures bubble.
        """
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": "video/mp4",
            "X-Filename": f"{short}.mp4",
            "Content-Length": str(len(body)),
            "User-Agent": "kinoforge-spandrel/0.1",
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
                f"_upload_source loop completed without payload (last_error={last_error!r})"
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
                f"SpandrelEngine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    # Cloudflare (RunPod's proxy edge) returns HTTP 403 to the default
    # Python-urllib User-Agent — sending a plain kinoforge UA clears
    # the gate. Same fix already in DiffusersEngine; mirror it here so
    # the spandrel HTTP path doesn't 403 against live pods.
    headers: dict[str, str] = {"User-Agent": "kinoforge-spandrel/0.1"}
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
