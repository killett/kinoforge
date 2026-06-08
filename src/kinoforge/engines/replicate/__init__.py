"""ReplicateEngine + ReplicateBackend — hosted Bearer adapter for replicate.com.

Lazy-imports the official ``replicate`` SDK inside method bodies to preserve
the core-import-ban invariant. Self-registers under ``"replicate"``.

Wire-shape note:
    The Replicate Python SDK constructor takes ``api_token`` (not the
    generic ``api_key`` that :class:`Bearer.client_kwargs` returns), so
    the engine re-maps the credential at client-construction time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core import registry
from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    CredentialProvider,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.prompt_routing import resolve_prompt
from kinoforge.core.remote_backend import (
    RemoteSubmitPollBackend,
    RemoteSubmitPollEngine,
)

_PROBE = ModelProfile(
    name="replicate",
    max_frames=120,
    fps=24,
    supported_modes={"t2v", "i2v", "flf2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


class ReplicateBackend(RemoteSubmitPollBackend):
    """Submit/poll backend for Replicate predictions API."""

    def _submit(self, client: object, job: GenerationJob) -> str:
        """Submit a prediction; return the SDK-issued prediction id."""
        version = job.spec["model"]
        input_dict: dict[str, Any] = {
            "prompt": resolve_prompt(job) or "",
            **(job.spec.get("params") or {}),
        }
        self._inject_assets(input_dict, job)
        try:
            pred = client.predictions.create(  # type: ignore[attr-defined]
                version=version, input=input_dict
            )
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("replicate.predictions.create", exc)
        return str(pred.id)

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        """Fetch a status snapshot for ``job_id`` via the SDK."""
        try:
            pred = client.predictions.get(job_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("replicate.predictions.get", exc)
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
        """True when ``status.status == 'failed'``; reason from ``error``."""
        if status.get("status") == "failed":
            return True, str(status.get("error") or "replicate prediction failed")
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return the output URL; unwraps ``[0]`` if ``output`` is a list."""
        out = status.get("output")
        if isinstance(out, list):
            return str(out[0]) if out else ""
        return str(out) if out else ""

    def _inject_assets(self, input_dict: dict[str, Any], job: GenerationJob) -> None:
        """Map seg-0 conditioning-asset roles onto Replicate input fields.

        ``init_image`` → ``input["image"]``;
        ``start_image`` → ``input["start_image"]``;
        ``end_image`` → ``input["end_image"]``.
        Unknown roles silently skipped — model-specific schemas vary.
        """
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role == "init_image":
                input_dict["image"] = asset.ref.uri
            elif asset.role == "start_image":
                input_dict["start_image"] = asset.ref.uri
            elif asset.role == "end_image":
                input_dict["end_image"] = asset.ref.uri

    def _raise_for_sdk_error(self, op: str, exc: BaseException) -> None:
        """Map a ``replicate.exceptions.ReplicateError`` to AuthError/KinoforgeError."""
        import replicate  # lazy

        if isinstance(exc, replicate.exceptions.ReplicateError):
            status = getattr(exc, "status", None)
            if status in (401, 403):
                raise AuthError(f"replicate auth failed: {exc}") from exc
        raise KinoforgeError(f"replicate: {op} failed: {exc}") from exc


class ReplicateEngine(RemoteSubmitPollEngine):
    """Hosted ``replicate.com`` adapter."""

    name: str = "replicate"

    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider | None
    ) -> Callable[[], object]:
        """Build a zero-arg callable that constructs ``replicate.Client``."""
        kwargs = self._auth.client_kwargs()
        token = kwargs.get("api_key")
        if not token:
            raise AuthError("replicate: REPLICATE_API_TOKEN is empty")

        def _factory() -> object:
            import replicate  # lazy

            return replicate.Client(api_token=token)

        return _factory

    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend:
        """Build a ``ReplicateBackend`` instance bound to ``cfg`` credentials."""
        del instance
        return ReplicateBackend(
            client_factory=self._build_client_factory(cfg, None),
            probe_profile=self._probe,
        )


def _default_factory() -> ReplicateEngine:
    """Zero-arg engine factory used by the registry."""
    return ReplicateEngine(
        auth=Bearer(
            env_var="REPLICATE_API_TOKEN",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_engine("replicate", _default_factory)
