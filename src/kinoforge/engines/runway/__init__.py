"""RunwayEngine + RunwayBackend — hosted Bearer adapter for runwayml.com.

Lazy-imports the official ``runwayml`` SDK inside method bodies to preserve
the core-import-ban invariant. Self-registers under ``"runway"``.

Wire-shape note:
    The SDK constructor takes ``api_key`` and reads ``RUNWAYML_API_SECRET``
    by default; we explicitly pass the value so kinoforge's
    ``EnvCredentialProvider`` stays the sole credential source.
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
    name="runway",
    max_frames=120,
    fps=24,
    supported_modes={"t2v", "i2v", "flf2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


class RunwayBackend(RemoteSubmitPollBackend):
    """Submit/poll backend for Runway tasks API."""

    def _submit(self, client: object, job: GenerationJob) -> str:
        """Submit a task; dispatches on mode → text_to_video / image_to_video."""
        model = job.spec["model"]
        mode = str(job.spec.get("mode") or "t2v").lower()
        prompt = resolve_prompt(job) or ""
        base_kw: dict[str, Any] = {
            "model": model,
            **(job.params or {}),
            **(job.spec.get("params") or {}),
        }
        self._inject_assets(base_kw, job)
        try:
            if mode == "t2v":
                task = client.text_to_video.create(  # type: ignore[attr-defined]
                    prompt_text=prompt, **base_kw
                )
            else:  # i2v / flf2v
                task = client.image_to_video.create(  # type: ignore[attr-defined]
                    prompt_text=prompt, **base_kw
                )
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("runway.create", exc)
        return str(task.id)

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        """Fetch one task snapshot."""
        try:
            task = client.tasks.retrieve(job_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("runway.tasks.retrieve", exc)
        return {
            "id": task.id,
            "status": task.status,
            "output": getattr(task, "output", None),
            "failure": getattr(task, "failure", None)
            or getattr(task, "failure_reason", None),
        }

    def _is_done(self, status: dict[str, Any]) -> bool:
        """True when ``status.status == 'SUCCEEDED'``."""
        return status.get("status") == "SUCCEEDED"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        """True when ``status.status == 'FAILED'``; reason from ``failure``."""
        if status.get("status") == "FAILED":
            return True, str(status.get("failure") or "runway task failed")
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return ``output[0]`` — Runway always returns a list."""
        out = status.get("output") or []
        if isinstance(out, list):
            return str(out[0]) if out else ""
        return str(out)

    def _inject_assets(self, kw: dict[str, Any], job: GenerationJob) -> None:
        """Map seg-0 conditioning-asset roles onto Runway create kwargs.

        ``init_image`` → ``prompt_image`` (i2v);
        ``start_image`` → ``first_image`` (flf2v);
        ``end_image`` → ``last_image`` (flf2v).
        """
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role == "init_image":
                kw["prompt_image"] = asset.ref.uri
            elif asset.role == "start_image":
                kw["first_image"] = asset.ref.uri
            elif asset.role == "end_image":
                kw["last_image"] = asset.ref.uri

    def _raise_for_sdk_error(self, op: str, exc: BaseException) -> None:
        """Map ``runwayml.AuthenticationError`` to AuthError; otherwise KinoforgeError.

        Runway returns 403 for both auth failures and model-access failures —
        we narrow on the SDK-specific :class:`runwayml.AuthenticationError`
        rather than the raw HTTP status to avoid misclassifying "model not
        available" as an auth problem.
        """
        import runwayml  # lazy

        if isinstance(exc, runwayml.AuthenticationError):
            raise AuthError(f"runway auth failed: {exc}") from exc
        raise KinoforgeError(f"runway: {op} failed: {exc}") from exc


class RunwayEngine(RemoteSubmitPollEngine):
    """Hosted ``runwayml.com`` adapter."""

    name: str = "runway"

    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider | None
    ) -> Callable[[], object]:
        """Build a zero-arg callable that constructs ``runwayml.RunwayML``."""
        kwargs = self._auth.client_kwargs()
        token = kwargs.get("api_key")
        if not token:
            raise AuthError("runway: RUNWAYML_API_SECRET is empty")

        def _factory() -> object:
            import runwayml  # lazy

            return runwayml.RunwayML(api_key=token)

        return _factory

    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend:
        """Build a ``RunwayBackend`` instance bound to ``cfg`` credentials."""
        del instance
        return RunwayBackend(
            client_factory=self._build_client_factory(cfg, None),
            probe_profile=self._probe,
        )


def _default_factory() -> RunwayEngine:
    """Zero-arg engine factory used by the registry."""
    return RunwayEngine(
        auth=Bearer(
            env_var="RUNWAYML_API_SECRET",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_engine("runway", _default_factory)
