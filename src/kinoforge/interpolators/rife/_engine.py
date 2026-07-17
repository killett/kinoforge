"""RifeEngine — HTTP-aware InterpolatorEngine impl backed by the RIFE runtime.

Talks to the embedded server's /interpolate + /interpolate/status/{id} + /upload
endpoints. Pod-HTTP client machinery (submit/poll loop, PUT /upload, Cloudflare
UA gate) is shared via :mod:`kinoforge.engines._pod_http`, which wraps
:func:`kinoforge.engines._proxy_retry.retry_proxy_call` for RunPod proxy
startup-window 404/502 tolerance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import InterpolationError
from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    InterpolateJob,
    InterpolateResult,
    InterpolatorEngine,
    RenderedProvision,
)
from kinoforge.engines._pod_http import PodHTTPClientMixin, http_json, submit_and_poll

_USER_AGENT = "kinoforge-rife/0.1"

# Practical-RIFE: the arbitrary-timestep RIFE v4 inference repo. Pinned to the
# latest commit as of 2026-07-05 (verified via the GitHub commits API). RIFE is
# light — no BSA wheel, no diffsynth.
_PRACTICAL_RIFE_COMMIT = "17d8c7a1005b37f4c97bfee04e316aaec7fdc536"

# Where render_provision clones Practical-RIFE; _runtime adds it to sys.path so
# ``from train_log.RIFE_HDv3 import Model`` resolves (script repo, not a pkg).
_RIFE_REPO_DIR = "/workspace/Practical-RIFE"

# RIFE v4 model release bundle (arch .py + flownet.pkl) in the hf:hzwer/RIFE
# repo. The arch code lives here, NOT in the git repo's (empty) train_log/.
_RIFE_MODEL_ZIP = "RIFEv4.26_0921.zip"


class RifeEngine(PodHTTPClientMixin, InterpolatorEngine):
    """RIFE v4 arbitrary-timestep frame interpolator (pod-side)."""

    name = "rife"
    requires_compute = True
    requires_local_weights = True
    capability = InterpCapability.ARBITRARY_TIMESTEP
    _pod_user_agent = _USER_AGENT

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
        """Emit a Practical-RIFE checkout + RIFE weights fetch bootstrap.

        Practical-RIFE is a *script* repo, not a pip package — its
        ``train_log/RIFE_HDv3.py`` (the ``Model`` arch) is only importable with
        the repo root on ``sys.path``. So we ``git clone`` it to a stable path
        (:data:`_RIFE_REPO_DIR`, which :mod:`._runtime` adds to ``sys.path``)
        rather than ``pip install`` it, and stage the weights (``flownet.pkl``)
        into the same ``train_log/`` the arch's ``load_model`` reads from.
        """
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["interpolate"])["rife"])
        weights_ref = str(block["weights_ref"])
        hf_repo = weights_ref.removeprefix("hf:")
        zip_url = f"https://huggingface.co/{hf_repo}/resolve/main/{_RIFE_MODEL_ZIP}"
        script = "".join(
            [
                "set -euo pipefail\n",
                # System ffmpeg — RifeRuntime shells out to `ffprobe` (fps +
                # frame-count probes) and muxes via imageio; the base
                # runpod/pytorch image ships neither on PATH. Missing ffprobe
                # failed the 2026-07-05 interp job server-side.
                "apt-get update -qq && apt-get install -y -qq ffmpeg unzip\n",
                # Clone Practical-RIFE for its model/ package (warplayer, loss —
                # RIFE_HDv3 imports them). torch + torchvision come from the base
                # image (loss.py imports torchvision).
                f"git clone https://github.com/hzwer/Practical-RIFE {_RIFE_REPO_DIR}\n",
                f"cd {_RIFE_REPO_DIR} && git checkout {_PRACTICAL_RIFE_COMMIT}\n",
                'pip install "numpy<2" "opencv-python-headless" "imageio[ffmpeg]"\n',
                # The RIFE v4 arch (RIFE_HDv3.py, IFNet_HDv3.py, refine.py) ships
                # INSIDE the model release zip, NOT the git repo — train_log/ is
                # empty until unzipped. Fetch the bundle + drop its contents into
                # train_log/ so `from train_log.RIFE_HDv3 import Model` (arch) and
                # `load_model(train_log, -1)` (flownet.pkl) both resolve.
                f"mkdir -p {_RIFE_REPO_DIR}/train_log /workspace/models/rife\n",
                f'curl -sL "{zip_url}" -o /tmp/rife_model.zip\n',
                "unzip -oq /tmp/rife_model.zip -d /tmp/rife_model\n",
                "cp -f /tmp/rife_model/*/*.py /tmp/rife_model/*/flownet.pkl "
                f"{_RIFE_REPO_DIR}/train_log/\n",
                f"cp -f {_RIFE_REPO_DIR}/train_log/flownet.pkl /workspace/models/rife/\n",
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
        result, elapsed_s = submit_and_poll(
            label_prefix="rife",
            base_url=base,
            endpoint="/interpolate",
            payload=submit_payload,
            http_json=_http_json,
            make_error=lambda job_id, server_error: InterpolationError(
                job_id=job_id, server_error=server_error
            ),
            cancel_token=cancel_token,
        )
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
            elapsed_s=elapsed_s,
            engine_meta=result.get("engine_meta", {}),
        )


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Pod JSON call with the rife User-Agent.

    Module-level seam — tests monkeypatch this name; keep it stable.
    Delegates to :func:`kinoforge.engines._pod_http.http_json`.
    """
    return http_json(method=method, url=url, payload=payload, user_agent=_USER_AGENT)
