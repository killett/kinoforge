"""LumaEngine + LumaBackend — hosted Bearer adapter for Luma Dream Machine.

Lazy-imports the official ``lumaai`` SDK inside method bodies to preserve
the core-import-ban invariant. Self-registers under ``"luma"``.

Wire-shape note:
    The SDK constructor takes ``auth_token`` (not the generic ``api_key``
    that :class:`Bearer.client_kwargs` returns), so the engine re-maps the
    credential at client-construction time. Luma's status field is
    ``state`` (not ``status``) and the output URL nests under
    ``assets.video``.
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
    name="luma",
    max_frames=216,
    fps=24,
    supported_modes={"t2v", "i2v", "flf2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


class LumaBackend(RemoteSubmitPollBackend):
    """Submit/poll backend for the Luma Dream Machine generations API."""

    def _submit(self, client: object, job: GenerationJob) -> str:
        """Submit a generation; return the SDK-issued id."""
        model = job.spec["model"]
        kw: dict[str, Any] = {
            "prompt": resolve_prompt(job) or "",
            "model": model,
            **(job.params or {}),
            **(job.spec.get("params") or {}),
        }
        self._inject_assets(kw, job)
        try:
            gen = client.generations.create(**kw)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("luma.generations.create", exc)
        return str(gen.id)

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        """Fetch one generation snapshot."""
        try:
            gen = client.generations.get(job_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("luma.generations.get", exc)
        return {
            "id": gen.id,
            "state": gen.state,
            "assets": dict(gen.assets) if getattr(gen, "assets", None) else {},
            "failure_reason": getattr(gen, "failure_reason", None),
        }

    def _is_done(self, status: dict[str, Any]) -> bool:
        """True when ``status.state == 'completed'``."""
        return status.get("state") == "completed"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        """True when ``status.state == 'failed'``."""
        if status.get("state") == "failed":
            return True, str(status.get("failure_reason") or "luma generation failed")
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return ``assets.video`` — the nested video URL."""
        assets = status.get("assets") or {}
        return str(assets.get("video", "") or "")

    def _inject_assets(self, kw: dict[str, Any], job: GenerationJob) -> None:
        """Map seg-0 conditioning-asset roles onto Luma keyframes shape.

        ``init_image`` / ``start_image`` → ``keyframes.frame0``;
        ``end_image`` → ``keyframes.frame1``.
        Each keyframe is a ``{"type": "image", "url": "..."}`` record.
        """
        if not job.segments:
            return
        keyframes: dict[str, Any] = {}
        for asset in job.segments[0].assets:
            if asset.role in ("init_image", "start_image"):
                keyframes["frame0"] = {"type": "image", "url": asset.ref.uri}
            elif asset.role == "end_image":
                keyframes["frame1"] = {"type": "image", "url": asset.ref.uri}
        if keyframes:
            kw["keyframes"] = keyframes

    def _raise_for_sdk_error(self, op: str, exc: BaseException) -> None:
        """Map ``lumaai.APIError`` 401/403 to AuthError; otherwise KinoforgeError."""
        import lumaai  # lazy

        if isinstance(exc, lumaai.APIError):
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise AuthError(f"luma auth failed: {exc}") from exc
        raise KinoforgeError(f"luma: {op} failed: {exc}") from exc


class LumaEngine(RemoteSubmitPollEngine):
    """Hosted Luma Dream Machine adapter."""

    name: str = "luma"

    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider | None
    ) -> Callable[[], object]:
        """Build a zero-arg callable that constructs ``lumaai.LumaAI``."""
        kwargs = self._auth.client_kwargs()
        token = kwargs.get("api_key")
        if not token:
            raise AuthError("luma: LUMAAI_API_KEY is empty")

        def _factory() -> object:
            import lumaai  # lazy

            return lumaai.LumaAI(auth_token=token)

        return _factory

    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend:
        """Build a ``LumaBackend`` instance bound to ``cfg`` credentials."""
        del instance
        return LumaBackend(
            client_factory=self._build_client_factory(cfg, None),
            probe_profile=self._probe,
        )


def _default_factory() -> LumaEngine:
    """Zero-arg engine factory used by the registry."""
    return LumaEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_engine("luma", _default_factory)
