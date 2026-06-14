"""FalImageEngine: live-fire image engine wrapping fal.ai queue API.

Reuses ``kinoforge.engines.fal.wire`` helpers (FalStatus, interpret_status,
build_status_url, build_response_url) — pure functions, no HTTP. HTTP I/O
lives in :class:`FalImageBackend` via injected ``http_post`` / ``http_get``
seams (mirror of FalBackend pattern; same User-Agent override for edge-proxy
compatibility).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
)
from kinoforge.engines.fal import wire

_DEFAULT_USER_AGENT = "kinoforge/0.1"
_QUEUE_BASE = "https://queue.fal.run"


def _default_post(url: str, body: dict, headers: dict) -> dict:  # type: ignore[type-arg]
    """POST ``url`` with JSON ``body`` and return parsed JSON response.

    Injects ``User-Agent: kinoforge/0.1`` when absent so edge proxies on fal
    do not reject the stdlib default ``Python-urllib/<ver>`` with HTTP 403.
    """
    merged = dict(headers)
    if not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = _DEFAULT_USER_AGENT
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]


def _default_get(url: str, headers: dict) -> dict:  # type: ignore[type-arg]
    """GET ``url`` and return parsed JSON response."""
    merged = dict(headers)
    if not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = _DEFAULT_USER_AGENT
    req = urllib.request.Request(url, headers=merged)  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]


@dataclass
class FalImageBackend(ImageBackend):
    """Live-fire fal queue backend for image endpoints (e.g. fal-ai/flux-schnell).

    HTTP I/O via injected seams. submit POSTs to queue.fal.run/<endpoint>;
    result polls status_url then fetches response_url.
    """

    cfg: dict  # type: ignore[type-arg]
    creds: CredentialProvider
    profile_to_return: ImageProfile
    http_post: Callable[[str, dict, dict], dict] = field(  # type: ignore[type-arg]
        default=_default_post
    )
    http_get: Callable[[str, dict], dict] = field(  # type: ignore[type-arg]
        default=_default_get
    )
    sleep: Callable[[float], None] = field(default=time.sleep)
    poll_interval_s: float = 1.0
    max_polls: int = 600
    _jobs: dict[str, dict] = field(  # type: ignore[type-arg]
        default_factory=dict, init=False, repr=False
    )

    def capabilities(self) -> ImageProfile:
        """Return the static profile for this backend."""
        return self.profile_to_return

    def inspect_capabilities(self) -> ImageProfile:
        """Return the static profile (no live inference needed)."""
        return self.profile_to_return

    def submit(self, job: ImageJob) -> str:
        """POST job to fal queue; return the request_id string.

        Args:
            job: The image job to submit.

        Returns:
            The fal request_id string for use with :meth:`result`.

        Raises:
            AuthError: ``FAL_KEY`` credential is absent.
            ValidationError: No endpoint derivable from ``job.spec`` or cfg.
        """
        endpoint = job.spec.get("model") or self.cfg.get("model")
        if not endpoint:
            raise ValidationError(
                "FalImageBackend.submit: no endpoint in spec.model / cfg.model"
            )
        api_key = self.creds.get("FAL_KEY")
        if not api_key:
            raise AuthError("FAL_KEY required for FalImageBackend")
        body: dict = {"prompt": job.prompt}  # type: ignore[type-arg]
        body.update(job.spec.get("input", {}))
        body.update(job.params)
        resp = self.http_post(
            f"{_QUEUE_BASE}/{endpoint}",
            body,
            {
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json",
            },
        )
        request_id = str(resp["request_id"])
        # Persist the submit response so result() can use the canonical
        # status_url + response_url instead of reconstructing them from the
        # endpoint name (fal's request paths use the family root, not the
        # leaf endpoint, so reconstruction would 404 on families like
        # fal-ai/flux/schnell → requests live under fal-ai/flux/).
        self._jobs[request_id] = resp
        return request_id

    def result(self, job_id: str) -> Artifact:
        """Poll fal status URL then fetch response URL; return image Artifact.

        Args:
            job_id: The fal request_id returned by :meth:`submit`.

        Returns:
            Artifact with ``url`` pointing at the first image, ``filename``
            derived from the URL path, and empty ``headers`` (fal signed URLs
            need no auth for the subsequent fetch).

        Raises:
            KinoforgeError: Job failed, timed out, or returned no images.
        """
        endpoint = self.cfg.get("model", "")
        api_key = self.creds.get("FAL_KEY") or ""
        headers = {"Authorization": f"Key {api_key}"}

        submit_resp = self._jobs.get(job_id, {})
        status_url = wire.build_status_url(
            submit_response=submit_resp,
            queue_base=_QUEUE_BASE,
            endpoint=endpoint,
            request_id=job_id,
        )
        response_url = wire.build_response_url(
            submit_response=submit_resp,
            queue_base=_QUEUE_BASE,
            endpoint=endpoint,
            request_id=job_id,
        )

        for _ in range(self.max_polls):
            status_data = self.http_get(status_url, headers)
            s = wire.interpret_status(str(status_data.get("status", "")))
            if s == wire.FalStatus.COMPLETED:
                break
            if s in (wire.FalStatus.FAILED, wire.FalStatus.UNKNOWN):
                raise KinoforgeError(f"fal image job {job_id} failed: {status_data}")
            self.sleep(self.poll_interval_s)
        else:
            raise KinoforgeError(
                f"fal image job {job_id} timed out after {self.max_polls} polls"
            )

        data = self.http_get(response_url, headers)
        images = data.get("images") or []
        if not images:
            raise KinoforgeError(
                f"fal image job {job_id}: no images in response: {data}"
            )
        url = str(images[0]["url"])
        return Artifact(
            url=url,
            filename=Path(urlparse(url).path).name or f"fal-image-{job_id[:8]}.png",
            headers={},  # fal signed URLs need no auth for fetch
        )

    def endpoints(self) -> dict[str, str]:
        """Return the static queue base URL."""
        return {"queue": _QUEUE_BASE}


@dataclass
class FalImageEngine(ImageEngine):
    """Hosted image engine wrapping fal.ai queue API; no compute, no local weights."""

    name: str = "fal"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: object | None = None,
    ) -> None:
        """Check FAL_KEY credential is present.

        Args:
            instance: Ignored — fal is a hosted API.
            cfg: Ignored at provision time.
            cancel_token: Ignored (Protocol parity for C29 boot-phase reap).

        Raises:
            AuthError: ``FAL_KEY`` env var is absent.
        """
        del cancel_token
        creds = EnvCredentialProvider()
        if not creds.get("FAL_KEY"):
            raise AuthError("FAL_KEY required for FalImageEngine")

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend:
        """Construct a :class:`FalImageBackend` for the given cfg.

        Args:
            instance: Ignored — fal is hosted.
            cfg: Engine config dict; ``cfg["spec"]`` (if a dict) is forwarded
                to the backend as its spec context.

        Returns:
            A live :class:`FalImageBackend`.
        """
        raw_spec = cfg.get("spec")
        spec: dict[str, object] = dict(raw_spec) if isinstance(raw_spec, dict) else {}
        return FalImageBackend(
            cfg=spec,
            creds=EnvCredentialProvider(),
            profile_to_return=self.profile_for(
                CapabilityKey(
                    base_model=str(spec.get("model", "")),
                    engine="fal",
                )
            ),
        )

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        """Return a static profile for the given capability key.

        Args:
            key: The capability key identifying the model.

        Returns:
            Static :class:`ImageProfile` — fal hosted endpoints have fixed
            max resolution and support only text-to-image at this time.
        """
        return ImageProfile(
            name=key.base_model or "fal-image",
            max_resolution=(1024, 1024),
            supported_modes={"t2i"},
        )

    def validate_spec(self, job: ImageJob) -> None:
        """Validate that the job has a non-empty prompt and a spec.model.

        Args:
            job: The image job to validate.

        Raises:
            ValidationError: Prompt is empty or ``spec.model``/``spec.endpoint``
                is absent.
        """
        if not job.prompt or not job.prompt.strip():
            raise ValidationError("FalImageEngine: prompt required")
        if not job.spec.get("model") and not job.spec.get("endpoint"):
            raise ValidationError("FalImageEngine: spec.model (fal endpoint) required")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Fal image identity is the queue endpoint."""
        engine_block = cfg.get("engine", {})
        if not isinstance(engine_block, dict):
            return ""
        fal_block = engine_block.get("fal", {})
        if not isinstance(fal_block, dict):
            return ""
        return str(fal_block.get("endpoint", "") or "")


registry.register_image_engine("fal", lambda: FalImageEngine())
