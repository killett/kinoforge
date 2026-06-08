"""RemoteSubmitPollBackend + RemoteSubmitPollEngine — foundation ABCs.

The submit-poll-fetch lifecycle every hosted video API follows. Subclasses
implement 5 wire-shape-specific hooks; the base class owns the poll loop,
AuthStrategy wiring, error mapping, and the public GenerationBackend +
GenerationEngine surfaces. Cross-cutting features (rate limiting, spend
tracking, retry policy, webhook callbacks, telemetry) bolt onto this single
foundation in future layers.

Stable contract — the public method set of both ABCs is locked by
``tests.test_core_invariant.test_remote_submit_poll_backend_abc_stable_surface``
against a checked-in baseline.
"""

from __future__ import annotations

import time
import urllib.request
from abc import abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kinoforge.core import frames
from kinoforge.core.errors import (
    AuthError,
    ConfigError,
    FrameExtractionError,
    KinoforgeError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
    RenderedProvision,
)

if TYPE_CHECKING:
    from kinoforge.core.auth import AuthStrategy


def _urllib_get_bytes(url: str) -> bytes:
    """Default HTTP GET returning raw bytes."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())


class RemoteSubmitPollBackend(GenerationBackend):
    """Submit-poll-fetch lifecycle backend for hosted video APIs.

    Concrete subclasses implement five abstract hooks; the base class
    owns the poll loop, AuthStrategy wiring, error mapping, and the
    public GenerationBackend surface (``submit`` / ``result`` /
    ``capabilities`` / ``inspect_capabilities`` / ``endpoints``).
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        sleep: Callable[[float], None] = time.sleep,
        max_poll: int = 120,
        poll_interval_s: float = 2.0,
        probe_profile: ModelProfile,
    ) -> None:
        """Initialise the backend with injected lifecycle seams.

        Args:
            client_factory: Zero-arg callable returning a configured
                SDK client. Called lazily on first ``submit`` /
                ``result`` invocation so credential resolution can run
                at construction time without forcing SDK import.
            sleep: Injectable sleep between poll iterations.
            max_poll: Maximum poll iterations before TimeoutError.
            poll_interval_s: Seconds between poll iterations.
            probe_profile: ModelProfile returned by capability methods.
        """
        self._client_factory = client_factory
        self._sleep = sleep
        self._max_poll = max_poll
        self._poll_interval_s = poll_interval_s
        self._probe = probe_profile
        self._client_cached: Any = None

    # ------------------------------------------------------------------
    # Subclass hooks (abstract)
    # ------------------------------------------------------------------

    @abstractmethod
    def _submit(self, client: object, job: GenerationJob) -> str:
        """Submit a job; return the provider's job id string."""

    @abstractmethod
    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        """Fetch one status snapshot for ``job_id``; return a dict."""

    @abstractmethod
    def _is_done(self, status: dict[str, Any]) -> bool:
        """True when ``status`` indicates the job completed successfully."""

    @abstractmethod
    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(failed, reason)``; ``reason`` may be empty."""

    @abstractmethod
    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return the output URL from a done ``status``."""

    # ------------------------------------------------------------------
    # Subclass hooks (default impls)
    # ------------------------------------------------------------------

    def _extract_filename(self, status: dict[str, Any]) -> str:
        """Return the provider's filename suggestion; default empty."""
        return ""

    def _endpoints_map(self) -> dict[str, str]:
        """Return a dict for :meth:`endpoints`; default empty."""
        return {}

    # ------------------------------------------------------------------
    # GenerationBackend interface (final — do not override)
    # ------------------------------------------------------------------

    def _client(self) -> object:
        """Return the cached SDK client; build on first call."""
        if self._client_cached is None:
            self._client_cached = self._client_factory()
        return self._client_cached

    def submit(self, job: GenerationJob) -> str:
        """Build + submit the request via :meth:`_submit`."""
        return self._submit(self._client(), job)

    def result(self, job_id: str) -> Artifact:
        """Poll until done or failed; return an Artifact on done."""
        import urllib.parse
        from pathlib import PurePosixPath

        client = self._client()
        for _ in range(self._max_poll):
            status = self._poll_one(client, job_id)
            failed, reason = self._is_failed(status)
            if failed:
                raise KinoforgeError(f"{type(self).__name__}: {reason or 'job failed'}")
            if self._is_done(status):
                url = self._extract_output_url(status)
                # Fallback: derive filename from URL basename so downstream
                # stages get a real extension (.mp4 / .png) instead of the
                # ".bin" default LocalOutputSink uses when the engine
                # returns an empty filename.
                filename = self._extract_filename(status)
                if not filename and url:
                    path = urllib.parse.urlparse(url).path
                    filename = PurePosixPath(path).name
                return Artifact(
                    filename=filename,
                    url=url,
                    meta={"job_id": job_id},
                    headers={},
                )
            self._sleep(self._poll_interval_s)
        raise TimeoutError(
            f"{type(self).__name__}: job {job_id!r} not done after "
            f"{self._max_poll} polls"
        )

    def capabilities(self) -> ModelProfile:
        """Return the probe profile (no extra discovery)."""
        return self._probe

    def inspect_capabilities(self) -> ModelProfile:
        """Return the probe profile (no extra discovery)."""
        return self._probe

    def endpoints(self) -> dict[str, str]:
        """Return provider-specific endpoint URLs (default empty)."""
        return self._endpoints_map()


class RemoteSubmitPollEngine(GenerationEngine):
    """Companion ABC: hosted-no-compute engine wrapping the submit-poll backend.

    Subclasses implement two methods:

    - :meth:`_build_client_factory` — returns a zero-arg callable that
      constructs the provider's SDK client using ``Bearer.client_kwargs()``
      (or equivalent) from the AuthStrategy stashed at construction.
    - :meth:`_build_backend` — returns a configured
      :class:`RemoteSubmitPollBackend` subclass instance.
    """

    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        auth: AuthStrategy,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        probe_profile: ModelProfile | None = None,
        declared_flags_map: dict[str, dict[str, bool]] | None = None,
    ) -> None:
        """Initialise the engine.

        Args:
            auth: AuthStrategy used by :meth:`provision` to verify
                credentials and by :meth:`_build_client_factory` to
                build SDK kwargs.
            http_get_bytes: Injectable bytes-fetch seam for
                :meth:`extract_last_frame`.
            ffmpeg_run: Injectable subprocess seam for ffmpeg.
            probe_profile: ModelProfile (subclass may override).
            declared_flags_map: CapabilityKey-keyed flag map.
        """
        self._auth = auth
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
        self._probe = probe_profile or ModelProfile(
            name=type(self).__name__,
            max_frames=81,
            fps=24,
            supported_modes={"t2v"},
            max_resolution=(1280, 720),
            supports_native_extension=False,
            supports_joint_audio=False,
        )
        self._declared_flags_map: dict[str, dict[str, bool]] = dict(
            declared_flags_map or {}
        )

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider | None
    ) -> Callable[[], Any]:
        """Return a zero-arg callable that builds the provider's SDK client."""

    @abstractmethod
    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend:
        """Return a configured subclass backend instance."""

    # ------------------------------------------------------------------
    # GenerationEngine interface
    # ------------------------------------------------------------------

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        """Verify creds; reject any non-None ``instance`` (no compute)."""
        if instance is not None:
            raise KinoforgeError(
                f"{type(self).__name__}.provision: instance must be None "
                "(hosted engine has no compute to configure)"
            )
        if not self._auth.credentials_present():
            raise AuthError(
                f"{type(self).__name__}: credentials not present "
                f"(strategy={type(self._auth).__name__})"
            )

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> RemoteSubmitPollBackend:
        """Build a configured backend via :meth:`_build_backend`."""
        return self._build_backend(dict(cfg), instance)

    def key_base(self, cfg: dict[str, object]) -> str:
        """Return ``cfg['spec']['model']``; raise ``ConfigError`` if missing."""
        spec = cfg.get("spec", {})
        model = str(spec.get("model", "")) if isinstance(spec, dict) else ""
        if not model:
            raise ConfigError(
                f"{type(self).__name__} requires spec.model at the top level "
                "of the YAML config"
            )
        return model

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Profiles are supplied by ``ModelProfileProvider`` — not by engine."""
        raise NotImplementedError(
            f"{type(self).__name__}.profile_for is supplied by ModelProfileProvider"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        """Return the declared-flag map for ``key`` (copy; empty default)."""
        return dict(self._declared_flags_map.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Default: require spec.model present. Subclasses extend."""
        if not job.spec.get("model"):
            from kinoforge.core.errors import ValidationError

            raise ValidationError(f"{type(self).__name__}: job.spec is missing 'model'")

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Hosted engines have no remote-provision payload."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the artifact bytes and pipe through ffmpeg."""
        if not artifact.url:
            raise FrameExtractionError(f"{type(self).__name__}: artifact.url is empty")
        try:
            video_bytes = self._http_get_bytes(artifact.url)
        except Exception as exc:
            raise FrameExtractionError(
                f"{type(self).__name__}: fetch from {artifact.url!r} failed: {exc}"
            ) from exc
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)
