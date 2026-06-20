"""Typed exception hierarchy for kinoforge."""


class KinoforgeError(Exception):
    """Base class for all kinoforge errors."""


class ConfigError(KinoforgeError):
    """Configuration is invalid or internally inconsistent."""


class AuthError(KinoforgeError):
    """A credential is missing or rejected by a provider/source."""


class CapacityError(KinoforgeError):
    """No compute offer satisfies the hardware requirements."""


class ProfileNotCached(KinoforgeError):
    """A ModelProfile was requested at plan time but is not in the cache."""


class CapabilityMismatch(KinoforgeError):
    """A live model contradicts its cached profile (verify drift)."""


class ValidationError(KinoforgeError):
    """A GenerationRequest or engine spec failed validation."""


class BudgetExceeded(KinoforgeError):
    """Estimated spend crossed the configured budget ceiling."""


class Cancelled(KinoforgeError):
    """Raised when a CancelToken is set mid-operation.

    Backends honoring cooperative cancellation raise this from their
    submit/result methods so the orchestrator can distinguish an operator
    interrupt from a real failure.
    """


class TeardownError(KinoforgeError):
    """destroy_instance could not confirm termination."""


class TransportError(KinoforgeError):
    """Raised when a HeartbeatEndpoint satisfier's underlying transport fails.

    Examples: RunPod GraphQL non-2xx, RunPod GraphQL ``errors`` response,
    SkyPilot SSH ``Connection refused``, selfterm HTTP timeout. Distinct
    from other KinoforgeError subclasses because callers (HeartbeatLoop
    ._tick_once) treat transport flakes differently from semantic errors
    — they retry on the next tick rather than aborting.
    """


class UnknownAdapter(KinoforgeError):
    """No registered provider/source/engine matches the requested name/scheme."""


class FrameExtractionError(KinoforgeError):
    """Raised when a frame cannot be decoded from an Artifact's video bytes."""


class AssetFetchError(KinoforgeError):
    """Raised when fetching a conditioning asset's bytes fails.

    Wraps unsupported URI scheme, HTTP transport error, missing file,
    and ComfyUI ``/upload/image`` failure into a single typed error.
    """


class LockError(KinoforgeError):
    """Base class for lock-acquisition failures."""


class LockTimeout(LockError):
    """Raised when ``acquire(blocking=True, timeout_s=X)`` elapses without obtaining the lock."""


class ProvisionFailed(KinoforgeError):
    """Pod boot script crashed — provider reported terminal status before ready."""


class ProvisionTimeout(KinoforgeError):
    """Ready check never returned success within ``boot_timeout_s``."""


class GenerationError(KinoforgeError):
    """Raised when a generation backend reports failure of an in-flight job.

    Wraps the backend-supplied error message so the orchestrator can surface
    it to the operator unchanged. Distinguished from ProvisionFailed (which
    is for pod-lifecycle errors before any generation runs) and from
    TimeoutError (which is for polling exhaustion without explicit failure).
    """


class SidecarMismatch(KinoforgeError):
    """cfg.store differs from sidecar on disk.

    Raised by ``cli.sidecar.verify_or_write_sidecar`` when the operator
    runs a cfg-bearing command with a config whose store identity
    differs from the sidecar already recorded in ``state_dir/store.json``.
    """


class SidecarMigrationBlocked(KinoforgeError):
    """First cloud-store command refused while local ledger non-empty.

    Raised by ``cli.sidecar.verify_or_write_sidecar`` when the operator
    runs a cloud-store cfg on a ``state_dir`` whose local ledger still
    has entries — guards against silently orphaning in-flight pods.
    """


class VaultError(KinoforgeError):
    """Base for vault load / validation failures."""


class VaultPathError(VaultError):
    """Vault path missing, unresolvable, or unreadable."""


class VaultUnderRepoError(VaultError):
    """Vault path resolves under the active git repo root."""


class VaultParseError(VaultError):
    """Vault YAML malformed or pydantic violation."""

    def __init__(self, path: str, original: Exception) -> None:
        """Wrap the underlying parse error with the offending path."""
        super().__init__(f"vault parse failed at {path}: {original}")
        self.path = path
        self.original = original


class VaultEmptyError(VaultError):
    """Neither ``positive_prompt`` nor ``segments`` populated."""


class EphemeralError(KinoforgeError):
    """Base for ephemeral-mode failures."""


class EphemeralDeleteUnsupportedError(EphemeralError):
    """Engine's provider has no public DELETE endpoint.

    Pre-flight (Task 18) refuses ephemeral on such providers — this is
    belt-and-suspenders for the runtime path. Raised by
    ``_delete`` implementations like ``FalBackend._delete``.
    """


class EphemeralDeleteHTTPError(EphemeralError):
    """A single DELETE attempt returned a retryable non-2xx (and not 404).

    Caught by ``_delete_with_retries`` to drive the 1s/2s/4s backoff
    schedule. After ``retries`` attempts elapse, the loop raises
    ``EphemeralDeleteFailedError`` instead.
    """


class EphemeralDeleteFailedError(EphemeralError):
    """``_delete_with_retries`` exhausted its retry budget on a hosted DELETE.

    Carries the provider-specific ``manual_url`` (from
    ``RemoteSubmitPollBackend.manual_cleanup_url(job_id)``) so the
    operator can finish the scrub via ``curl -X DELETE``. ``__str__``
    matches spec §10.5 — provider, job_id, attempt count, last error,
    manual URL, exit-code note. No output-file enumeration (D14).
    """

    def __init__(
        self,
        job_id: str,
        provider: str,
        manual_url: str,
        attempts: int,
        last_error: str,
    ) -> None:
        """Wrap the exhausted-retry failure with the manual cleanup URL."""
        self.job_id = job_id
        self.provider = provider
        self.manual_url = manual_url
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(self._format())

    def _format(self) -> str:
        return (
            "ERROR: --ephemeral could not delete the provider-side record.\n"
            f"  provider: {self.provider}\n"
            f"  job_id:   {self.job_id}\n"
            f"  attempts: {self.attempts}\n"
            f"  last:     {self.last_error}\n"
            "\n"
            "To finish the scrub, run:\n"
            "\n"
            f"  curl -X DELETE {self.manual_url}\n"
            "\n"
            "(kinoforge exited 1 because ephemeral requires a clean scrub.)"
        )


class EphemeralStoreCleanupFailedError(EphemeralError):
    """``EphemeralSession.__exit__`` could not scrub a registered run's bytes.

    Raised after ``store.delete_run(run_id)`` fails inside the session
    exit path under ``policy.delete_on_completion=True``. Exposes the
    store's ``manual_cleanup_command(run_id)`` via ``.cleanup_command``
    so the operator can finish the scrub by hand.
    """

    def __init__(self, store: object, run_id: str, original_error: Exception) -> None:
        """Wrap the underlying scrub failure with the manual cleanup command."""
        self.store = store
        self.run_id = run_id
        self.original_error = original_error
        # ``manual_cleanup_command`` is part of the ArtifactStore ABC since
        # Sub-β Task 6 — no Protocol import needed.
        self.cleanup_command: str = store.manual_cleanup_command(run_id)  # type: ignore[attr-defined]
        super().__init__(self._format())

    def _format(self) -> str:
        return (
            "ERROR: --ephemeral could not delete the run's on-disk artifacts.\n"
            f"  store:    {self.store!r}\n"
            f"  run_id:   {self.run_id}\n"
            f"  error:    {self.original_error}\n"
            "\n"
            "To finish the scrub, run:\n"
            "\n"
            f"  {self.cleanup_command}\n"
            "\n"
            "(kinoforge exited 1 because ephemeral requires a clean scrub.)"
        )
