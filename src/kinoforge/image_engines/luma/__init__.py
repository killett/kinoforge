"""LumaImageEngine — Layer-R image engine for Luma's dream-machine image API.

Raw-REST (urllib) — no ``lumaai`` SDK dependency. Video surface retired
by the provider (Phase 44 deleted the old ``LumaEngine``); this module is
image/keyframe-only. API contract verified 2026-07-03 against
``docs.lumalabs.ai`` — see
``docs/superpowers/specs/2026-07-03-luma-image-keyframes-design.md``.

Self-registers under ``"luma"`` via the image-engine registry.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError

from kinoforge.core import registry
from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationJob,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
    ModelProfile,
    Segment,
)
from kinoforge.core.remote_backend import RemoteSubmitPollBackend

_BASE_URL = "https://api.lumalabs.ai"
_IMAGE_PROBE = ImageProfile(
    name="luma-image",
    max_resolution=(1920, 1080),
    supported_modes={"t2i"},
)


class _LumaHttp:
    """Minimal Bearer-authenticated JSON client for the Luma REST API."""

    def __init__(self, *, token: str, base_url: str = _BASE_URL) -> None:
        """Bind the Bearer token + base URL.

        Args:
            token: LUMAAI_API_KEY value.
            base_url: API origin; overridable for tests.
        """
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue one JSON request; raise KinoforgeError with body tail on 4xx/5xx."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 — https base, fixed host
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
        except HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            raise KinoforgeError(
                f"luma-image: {method} {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        if not raw:
            return {}
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` as JSON; return the parsed JSON response."""
        return self._request("POST", path, body)

    def get_json(self, path: str) -> dict[str, Any]:
        """GET; return the parsed JSON response."""
        return self._request("GET", path)

    def delete(self, path: str) -> None:
        """DELETE; response body (if any) ignored."""
        self._request("DELETE", path)


class _LumaImageInnerBackend(RemoteSubmitPollBackend):
    """Submit-poll backend for ``/dream-machine/v1/generations/image``."""

    def _submit(self, client: object, job: GenerationJob) -> str:
        """POST the generation; return the provider id."""
        http: _LumaHttp = client  # type: ignore[assignment]
        prompt = job.segments[0].prompt if job.segments else ""
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": job.spec["model"],
            **(job.spec.get("params") or {}),
        }
        resp = http.post_json("/dream-machine/v1/generations/image", body)
        gen_id = str(resp.get("id", ""))
        if not gen_id:
            raise KinoforgeError(f"luma-image: submit returned no id: {resp!r}")
        return gen_id

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        """Fetch one generation snapshot."""
        http: _LumaHttp = client  # type: ignore[assignment]
        return http.get_json(f"/dream-machine/v1/generations/{job_id}")

    def _is_done(self, status: dict[str, Any]) -> bool:
        """True when ``state == 'completed'``."""
        return status.get("state") == "completed"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        """True + reason when ``state == 'failed'``."""
        if status.get("state") == "failed":
            return True, str(
                status.get("failure_reason") or "luma-image generation failed"
            )
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return ``assets.image`` — the video slot is null for images."""
        assets = status.get("assets") or {}
        return str(assets.get("image") or "")

    def _delete(self, job_id: str) -> None:
        """DELETE the generation record (documented Luma endpoint)."""
        http: _LumaHttp = self._client()  # type: ignore[assignment]
        http.delete(f"/dream-machine/v1/generations/{job_id}")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        """Dashboard URL an operator can visit to purge the record by hand."""
        return "https://lumalabs.ai/dream-machine/creations"


class LumaImageBackend(ImageBackend):
    """Image-shape adapter around the Luma submit-poll lifecycle."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], object],
        sleep: Callable[[float], None] = time.sleep,
        max_poll: int = 60,
        poll_interval_s: float = 2.0,
        probe_profile: ImageProfile = _IMAGE_PROBE,
    ) -> None:
        """Initialise the backend with injectable lifecycle seams.

        Args:
            client_factory: Zero-arg callable returning a ``_LumaHttp``
                (or a fake with the same three methods in tests).
            sleep: Injectable sleep between poll iterations.
            max_poll: Maximum poll iterations before TimeoutError.
            poll_interval_s: Seconds between polls (~31 s generations →
                the 60 × 2 s default is ample headroom).
            probe_profile: ImageProfile returned by capability methods.
        """
        self._probe = probe_profile
        self._inner = _LumaImageInnerBackend(
            client_factory=client_factory,
            sleep=sleep,
            max_poll=max_poll,
            poll_interval_s=poll_interval_s,
            probe_profile=ModelProfile(
                name=probe_profile.name,
                max_frames=1,
                fps=24,
                supported_modes={"t2i"},
                max_resolution=probe_profile.max_resolution,
                supports_native_extension=False,
                supports_joint_audio=False,
            ),
        )

    def capabilities(self) -> ImageProfile:
        """Return the configured ImageProfile."""
        return self._probe

    def inspect_capabilities(self) -> ImageProfile:
        """Return the configured ImageProfile (no live probe)."""
        return self._probe

    def submit(self, job: ImageJob) -> str:
        """Adapt the ImageJob to a single-segment GenerationJob and submit."""
        adapted = GenerationJob(
            segments=[Segment(prompt=job.prompt, params={}, assets=[])],
            spec=job.spec,
            params=job.params,
        )
        return self._inner.submit(adapted)

    def result(self, job_id: str) -> Artifact:
        """Poll until the image is ready."""
        return self._inner.result(job_id)

    def endpoints(self) -> dict[str, str]:
        """No endpoint URLs for the hosted path."""
        return {}


class LumaImageEngine(ImageEngine):
    """Hosted Luma dream-machine image-engine adapter (photon / UNI-1)."""

    name: str = "luma"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(self, *, auth: Bearer) -> None:
        """Initialise the engine with an explicit Bearer strategy.

        Args:
            auth: Bearer strategy carrying ``LUMAAI_API_KEY``.
        """
        self._auth = auth

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: object | None = None,
    ) -> None:
        """Validate credentials; reject any non-None ``instance``."""
        if instance is not None:
            raise KinoforgeError("LumaImageEngine.provision: instance must be None")
        if not self._auth.credentials_present():
            raise AuthError("luma-image: LUMAAI_API_KEY not present")

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> LumaImageBackend:
        """Build the image backend bound to the Bearer credential."""
        del instance, cfg
        kwargs = self._auth.client_kwargs()
        token = kwargs.get("api_key")
        if not token:
            raise AuthError("luma-image: LUMAAI_API_KEY is empty")
        return LumaImageBackend(client_factory=lambda: _LumaHttp(token=token))

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        """Profiles flow through ImageProfileProvider — not the engine."""
        raise NotImplementedError(
            "LumaImageEngine.profile_for is supplied by ImageProfileProvider"
        )

    def validate_spec(self, job: ImageJob) -> None:
        """Require ``spec.model`` and a non-empty prompt."""
        from kinoforge.core.errors import ValidationError

        if not job.spec.get("model"):
            raise ValidationError("luma-image: spec.model missing")
        if not job.prompt:
            raise ValidationError("luma-image: prompt is empty")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Luma image identity is the model slug at ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""


def _default_factory() -> LumaImageEngine:
    """Zero-arg engine factory used by the image-engine registry."""
    return LumaImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_image_engine("luma", _default_factory)
