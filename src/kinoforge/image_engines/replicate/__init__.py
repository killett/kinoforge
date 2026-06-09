"""ReplicateImageEngine — Layer-R image-sibling for Replicate predictions API.

Wraps :class:`~kinoforge.engines.replicate.ReplicateBackend`'s submit-poll
lifecycle but conforms to the :class:`~kinoforge.core.interfaces.ImageEngine`
ABC: no segments, single-URL output (flux-schnell / sdxl shape).

Lazy-imports the ``replicate`` SDK inside the inner backend (in turn inside
the engine factory). Self-registers under ``"replicate"`` via the image-
engine registry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

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

_IMAGE_PROBE = ImageProfile(
    name="replicate-image",
    max_resolution=(1024, 1024),
    supported_modes={"t2i"},
)


class _ReplicateImageInnerBackend(RemoteSubmitPollBackend):
    """Inner submit-poll backend for Replicate image predictions.

    Differs from the video backend in two places:
    - ``_extract_output_url`` unwraps the single-image flux-schnell shape
      (``output`` is either a list with one URL or a bare URL string).
    - Asset injection is a no-op — image-from-text only.
    """

    def _submit(self, client: object, job: GenerationJob) -> str:
        """Submit a prediction; reuse the video backend's request shape."""
        model = job.spec["model"]
        prompt = job.segments[0].prompt if job.segments else ""
        input_dict: dict[str, Any] = {
            "prompt": prompt,
            **(job.spec.get("params") or {}),
        }
        try:
            pred = client.predictions.create(  # type: ignore[attr-defined]
                model=model, input=input_dict
            )
        except Exception as exc:  # noqa: BLE001
            raise KinoforgeError(
                f"replicate-image: predictions.create failed: {exc}"
            ) from exc
        return str(pred.id)

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        """Fetch one prediction snapshot via the SDK."""
        try:
            pred = client.predictions.get(job_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise KinoforgeError(
                f"replicate-image: predictions.get failed: {exc}"
            ) from exc
        return {
            "id": pred.id,
            "status": pred.status,
            "output": pred.output,
            "error": pred.error,
        }

    def _is_done(self, status: dict[str, Any]) -> bool:
        """True when ``status.status == 'succeeded'``."""
        return status.get("status") == "succeeded"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        """True when ``status.status == 'failed'``."""
        if status.get("status") == "failed":
            return True, str(status.get("error") or "replicate-image prediction failed")
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return the image URL — single string or list[0]."""
        out = status.get("output")
        if isinstance(out, list):
            return str(out[0]) if out else ""
        return str(out) if out else ""

    def _delete(self, job_id: str) -> None:
        """Scaffold for the ``_delete`` ABC; concrete impl lands in Task 17."""
        raise NotImplementedError(
            "_ReplicateImageInnerBackend._delete is filled in Task 17"
        )

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        """Scaffold for the ABC; concrete impl lands in Task 17."""
        return f"https://replicate.com/predictions/{job_id}"


class ReplicateImageBackend(ImageBackend):
    """Image-shape adapter wrapping the Replicate submit-poll lifecycle."""

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
            client_factory: Zero-arg callable returning a configured SDK client.
            sleep: Injectable sleep between poll iterations.
            max_poll: Maximum poll iterations before TimeoutError.
            poll_interval_s: Seconds between polls.
            probe_profile: ImageProfile returned by capability methods.
        """
        # The inner backend wants a ModelProfile, but the image-side public
        # surface returns an ImageProfile — we keep both, isolated.
        self._probe = probe_profile
        self._inner = _ReplicateImageInnerBackend(
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
        """No endpoint URLs for the SDK-mediated path."""
        return {}


class ReplicateImageEngine(ImageEngine):
    """Hosted ``replicate.com`` image-engine adapter (flux-schnell etc.)."""

    name: str = "replicate"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(self, *, auth: Bearer) -> None:
        """Initialise the engine with an explicit Bearer strategy.

        Args:
            auth: Bearer strategy carrying ``REPLICATE_API_TOKEN``.
        """
        self._auth = auth

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        """Validate credentials; reject any non-None ``instance``."""
        if instance is not None:
            raise KinoforgeError(
                "ReplicateImageEngine.provision: instance must be None"
            )
        if not self._auth.credentials_present():
            raise AuthError("replicate-image: REPLICATE_API_TOKEN not present")

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ReplicateImageBackend:
        """Build the image backend bound to the Bearer credential."""
        del instance, cfg
        kwargs = self._auth.client_kwargs()
        token = kwargs.get("api_key")
        if not token:
            raise AuthError("replicate-image: REPLICATE_API_TOKEN is empty")

        def _factory() -> object:
            import replicate  # lazy

            return replicate.Client(api_token=token)

        return ReplicateImageBackend(client_factory=_factory)

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        """Profiles flow through ImageProfileProvider — not the engine."""
        raise NotImplementedError(
            "ReplicateImageEngine.profile_for is supplied by ImageProfileProvider"
        )

    def validate_spec(self, job: ImageJob) -> None:
        """Require ``spec.model`` and a non-empty prompt."""
        from kinoforge.core.errors import ValidationError

        if not job.spec.get("model"):
            raise ValidationError("replicate-image: spec.model missing")
        if not job.prompt:
            raise ValidationError("replicate-image: prompt is empty")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Replicate image identity is the prediction model slug at ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""


def _default_factory() -> ReplicateImageEngine:
    """Zero-arg engine factory used by the image-engine registry."""
    return ReplicateImageEngine(
        auth=Bearer(
            env_var="REPLICATE_API_TOKEN",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_image_engine("replicate", _default_factory)
