"""ReplicateEngine + ReplicateBackend — hosted Bearer adapter for replicate.com.

Lazy-imports the official ``replicate`` SDK inside method bodies to preserve
the core-import-ban invariant. Self-registers under ``"replicate"``.

Wire-shape note:
    The Replicate Python SDK constructor takes ``api_token`` (not the
    generic ``api_key`` that :class:`Bearer.client_kwargs` returns), so
    the engine re-maps the credential at client-construction time.
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

# Replicate throttles accounts whose rate-limit subsystem reports < $5 credit
# to 6 requests/minute with a burst of 1, regardless of actual billing-UI
# balance (see PROGRESS Phase 43 "Layer 4 carry-forward"). We space submits
# at the documented floor + 2 s margin — empirically, exactly-10 s spacing
# still 429'd with "resets in ~1s" (bucket-refill drift). 12 s clears it.
# Override via ``submit_min_interval_s`` on the engine constructor when the
# throttle clears.
_REPLICATE_SUBMIT_MIN_INTERVAL_S = 12.0

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
    """Submit/poll backend for Replicate predictions API.

    Threads a per-instance submit-rate floor (``submit_min_interval_s``,
    default 10 s) to ride out the Replicate throttle-when-credit-< $5
    behaviour without 429s — see module-level constant for context.
    """

    def __init__(
        self,
        *,
        submit_min_interval_s: float = _REPLICATE_SUBMIT_MIN_INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,
        **kw: Any,  # noqa: ANN401 — forwarded to RemoteSubmitPollBackend
    ) -> None:
        """Initialise the backend with optional submit-rate spacing.

        Args:
            submit_min_interval_s: Minimum wall-clock seconds between
                two consecutive ``_submit`` invocations on this instance.
                Set ``0.0`` to disable (e.g. when Replicate lifts the
                rate-limit on your account).
            monotonic: Injectable clock for tests.
            **kw: Forwarded to :class:`RemoteSubmitPollBackend`.
        """
        super().__init__(**kw)
        self._submit_min_interval_s = float(submit_min_interval_s)
        self._monotonic = monotonic
        self._last_submit_at: float = 0.0

    def _submit(self, client: object, job: GenerationJob) -> str:
        """Submit a prediction; return the SDK-issued prediction id.

        Uses ``model=`` (the human-readable ``owner/name`` slug) rather than
        ``version=`` (a 64-char content hash). When the slug is supplied the
        Replicate SDK resolves the current default version server-side, which
        matches how operators describe models in YAML configs.
        """
        # Throttle floor: wait until at least submit_min_interval_s has
        # elapsed since the previous _submit on this backend instance.
        if self._submit_min_interval_s > 0.0:
            now = self._monotonic()
            elapsed = now - self._last_submit_at
            if self._last_submit_at and elapsed < self._submit_min_interval_s:
                self._sleep(self._submit_min_interval_s - elapsed)
        model = job.spec["model"]
        input_dict: dict[str, Any] = {
            "prompt": resolve_prompt(job) or "",
            # ``job.params`` is the orchestrator's cfg.params; ``job.spec.params``
            # is the inline-spec carry-over for direct backend construction.
            # We merge both so neither path silently drops fields.
            **(job.params or {}),
            **(job.spec.get("params") or {}),
        }
        self._inject_assets(input_dict, job)
        self._last_submit_at = self._monotonic()
        try:
            pred = client.predictions.create(  # type: ignore[attr-defined]
                model=model, input=input_dict
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
