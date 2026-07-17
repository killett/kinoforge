"""SpandrelEngine — HTTP-aware UpscalerEngine impl backed by the spandrel runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} endpoints (T9).
Pod-HTTP client machinery (submit/poll loop, PUT /upload, Cloudflare UA gate)
is shared via :mod:`kinoforge.engines._pod_http`, which wraps
:func:`kinoforge.engines._proxy_retry.retry_proxy_call` to absorb
RunPod proxy startup-window 404/502s.

Split out of ``__init__.py`` so the on-pod embed (which only embeds
``kinoforge.core.errors`` + ``.scale_target``) can fail the engine import
without poisoning the package — only ``_runtime`` is needed on the pod.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    NotYetImplementedError,
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
from kinoforge.engines._pod_http import PodHTTPClientMixin, http_json, submit_and_poll

_USER_AGENT = "kinoforge-spandrel/0.1"


class SpandrelEngine(PodHTTPClientMixin, UpscalerEngine):
    """spandrel-based image super-resolution per-frame video upscaler."""

    name = "spandrel"
    requires_compute = True
    requires_local_weights = True
    _pod_user_agent = _USER_AGENT
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
        result, elapsed_s = submit_and_poll(
            label_prefix="spandrel",
            base_url=base,
            endpoint="/upscale",
            payload=submit_payload,
            http_json=_http_json,
            make_error=lambda job_id, server_error: UpscaleFailed(
                job_id=job_id, server_error=server_error
            ),
            cancel_token=cancel_token,
        )
        return UpscaleResult(
            artifact=Artifact(
                uri=f"{base}/artifacts/{result['filename']}",
                sha256=result["sha256"],
                size=result["size"],
            ),
            input_resolution=tuple(result["input_resolution"]),
            output_resolution=tuple(result["output_resolution"]),
            elapsed_s=elapsed_s,
            engine_meta=result.get("engine_meta", {}),
        )


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Pod JSON call with the spandrel User-Agent.

    Module-level seam — tests monkeypatch this name; keep it stable.
    Delegates to :func:`kinoforge.engines._pod_http.http_json`.
    """
    return http_json(method=method, url=url, payload=payload, user_agent=_USER_AGENT)
