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
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import (
    AuthError,
    ConfigError,
    EphemeralDeleteFailedError,
    EphemeralDeleteHTTPError,
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
    from kinoforge.core.cancel import CancelToken


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

    @abstractmethod
    def _delete(self, job_id: str) -> None:
        """Issue the provider's DELETE for ``job_id``.

        Concrete subclasses send the provider-specific DELETE request and
        raise ``EphemeralDeleteHTTPError`` on a retryable non-2xx (so
        ``_delete_with_retries`` drives the backoff). Engines whose
        provider has no public DELETE endpoint raise
        ``EphemeralDeleteUnsupportedError`` — pre-flight (Task 18)
        refuses those providers under ephemeral so this branch is
        belt-and-suspenders for the runtime path.
        """

    @classmethod
    @abstractmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        """Return the provider's browser-facing cleanup URL for ``job_id``.

        Embedded in ``EphemeralDeleteFailedError.__str__`` so the
        operator can finish a partial scrub by hand. Engines whose
        provider has no public DELETE endpoint return ``""``.
        """

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

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        """Build + submit the request via :meth:`_submit`.

        Honors *cancel_token* with a single cheap ``raise_if_set`` check
        before any provider-side network call — symmetry with
        :meth:`ComfyUIBackend.submit` so an operator who presses Ctrl-C
        between job construction and the first wire call does not pay
        for a wasted submission.

        Args:
            job: The :class:`GenerationJob` to send to the provider.
            cancel_token: Optional :class:`CancelToken`. Checked once
                before ``_submit``; defaults to a never-set sentinel.

        Raises:
            Cancelled: ``cancel_token`` was set when ``submit`` was
                entered.
        """
        from kinoforge.core.cancel import _NULL_TOKEN

        (cancel_token or _NULL_TOKEN).raise_if_set()
        return self._submit(self._client(), job)

    def _delete_with_retries(
        self,
        job_id: str,
        *,
        retries: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Call ``_delete(job_id)`` with 1s/2s/4s exponential backoff.

        Catches ``EphemeralDeleteHTTPError`` on each attempt, sleeps
        ``2 ** attempt`` seconds before the next try, and on exhaustion
        raises ``EphemeralDeleteFailedError`` carrying the manual
        cleanup URL. ``sleep_fn`` is injectable so tests run fast.
        """
        last_error = ""
        for attempt in range(retries):
            try:
                self._delete(job_id)
                return
            except EphemeralDeleteHTTPError as e:
                last_error = str(e)
                if attempt + 1 < retries:
                    sleep_fn(2.0**attempt)
        raise EphemeralDeleteFailedError(
            job_id=job_id,
            provider=type(self).__name__.replace("Backend", "").lower(),
            manual_url=self.manual_cleanup_url(job_id),
            attempts=retries,
            last_error=last_error,
        )

    def result(
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        """Poll until done or failed; return an Artifact on done.

        Honors *cancel_token* both at the top of every iteration (cheap
        ``raise_if_set`` check before any I/O) and across the inter-poll
        wait (``cancel_token.wait`` in place of ``time.sleep`` so a
        sibling thread's ``token.set()`` interrupts the wait promptly).
        This closes the Ctrl-C path for every Bearer-API hosted provider
        in one shot (Replicate / Runway / Luma / Fal all subclass this
        backend).

        Under an active ``EphemeralSession`` with
        ``policy.delete_on_completion=True``, fires
        ``_delete_with_retries(job_id, retries=policy.delete_retries)``
        AFTER the artifact has been built but BEFORE returning. A
        successful delete scrubs the provider-side record so the
        prompt-laden job ID does not survive on the provider's
        dashboard.

        Args:
            job_id: Provider-side job ID returned by :meth:`submit`.
            cancel_token: Optional :class:`CancelToken`. When ``None``,
                the legacy path is preserved verbatim: the inter-poll
                wait calls ``self._sleep`` (which existing engine tests
                inject as ``lambda s: None`` to keep tick latency at
                zero). When supplied, the wait calls ``token.wait`` so
                a mid-wait set() returns promptly.

        Raises:
            Cancelled: ``cancel_token`` was set.
            TimeoutError: ``max_poll`` iterations elapsed without
                completion.
            KinoforgeError: ``_is_failed`` returned ``(True, reason)``.
        """
        from kinoforge.core.cancel import _NULL_TOKEN

        token = cancel_token if cancel_token is not None else _NULL_TOKEN

        # Token-aware wait: when a real token is supplied, use Event.wait
        # so a mid-wait set() returns promptly. When the caller passes no
        # token (existing engine tests inject ``sleep=lambda s: None``
        # to keep ticks instant), fall back to the injected sleep so the
        # legacy timeout / iteration-cap contract is preserved verbatim.
        def _interpoll_wait(seconds: float) -> None:
            if cancel_token is None:
                self._sleep(seconds)
                return
            token.wait(seconds)

        import urllib.parse
        from pathlib import PurePosixPath

        client = self._client()
        for _ in range(self._max_poll):
            token.raise_if_set()
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
                artifact = Artifact(
                    filename=filename,
                    url=url,
                    meta={"job_id": job_id},
                    headers={},
                )
                _session = EphemeralSession.current()
                if _session is not None and _session.policy.delete_on_completion:
                    self._delete_with_retries(
                        job_id, retries=_session.policy.delete_retries
                    )
                return artifact
            _interpoll_wait(self._poll_interval_s)
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

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Remote-submit-poll engines (Replicate, Runway, Luma) read ``spec.model``.

        Subclasses may override if their identity surface diverges.
        """
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""

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
