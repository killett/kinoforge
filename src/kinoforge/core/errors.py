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


# --- LoRA-flexible warm-reuse: swap error hierarchy ------------------------


class LoraSwapError(KinoforgeError):
    """Base for all LoRA-swap failures on a warm pod.

    All subclasses carry the pod_id of the affected pod and a
    ``manual_cleanup_command()`` method returning a copy-paste-able shell
    command the operator can run to recover by hand if needed.
    """

    def __init__(self, *, pod_id: str) -> None:
        """Record the affected pod_id."""
        super().__init__()
        self.pod_id = pod_id

    def manual_cleanup_command(self) -> str:
        """Return the operator-runnable command to destroy the affected pod."""
        return f"kinoforge destroy --id {self.pod_id}"


class LoraSwapDownloadError(LoraSwapError):
    """Download failed BEFORE any eviction. Pod inventory unchanged."""

    def __init__(self, *, pod_id: str, ref: str, underlying: str) -> None:
        """Carry the failed ref + underlying transport cause."""
        super().__init__(pod_id=pod_id)
        self.ref = ref
        self.underlying = underlying

    def __str__(self) -> str:
        """Render with ref + underlying cause + safe-to-retry note."""
        return (
            f"LoRA download failed on pod {self.pod_id}: ref {self.ref} "
            f"({self.underlying}); pod inventory unchanged, retry is safe."
        )


class LoraSwapDegradedPodError(LoraSwapError):
    """Download failed AFTER eviction started.

    Pod left in half-state, marked degraded; matcher will route the next
    retry elsewhere or cold-boot.
    """

    def __init__(
        self,
        *,
        pod_id: str,
        evict_completed: list[str],
        download_failed: str,
        underlying: str,
    ) -> None:
        """Carry what was evicted, what failed to download, and the cause."""
        super().__init__(pod_id=pod_id)
        self.evict_completed = list(evict_completed)
        self.download_failed = download_failed
        self.underlying = underlying

    def __str__(self) -> str:
        """Render the half-state + retry-elsewhere guidance."""
        evicted = ", ".join(self.evict_completed) or "(none)"
        return (
            f"LoRA swap on pod {self.pod_id} failed in the eviction-required "
            f"phase: evicted [{evicted}], failed to download "
            f"{self.download_failed} ({self.underlying}). Pod is now in a "
            f"degraded state and has been marked for reap. Retry your "
            f"generate; the matcher will route elsewhere or cold-boot."
        )


class LoraSwapPodUnreachableError(LoraSwapError):
    """Pod proxy returned past retry budget. Marked degraded."""

    def __init__(self, *, pod_id: str, underlying: str) -> None:
        """Carry the underlying transport-error description."""
        super().__init__(pod_id=pod_id)
        self.underlying = underlying

    def __str__(self) -> str:
        """Render the unreachable-past-retry-budget message."""
        return (
            f"Pod {self.pod_id} unreachable past the proxy-retry budget: "
            f"{self.underlying}. Pod marked degraded."
        )


class LoraSwapVramOomError(LoraSwapError):
    """set_adapters OOM at swap time; rollback to previous adapter set succeeded.

    Pod is healthy at the previous LoRA stack — NOT marked degraded.
    """

    def __init__(self, *, pod_id: str, dropped_refs: list[str]) -> None:
        """Carry the refs that were dropped from the target stack."""
        super().__init__(pod_id=pod_id)
        self.dropped_refs = list(dropped_refs)

    def __str__(self) -> str:
        """Render the rollback-succeeded-pod-healthy message."""
        dropped = ", ".join(self.dropped_refs)
        return (
            f"VRAM OOM during set_adapters on pod {self.pod_id}: target stack "
            f"included {dropped}; pod rolled back to its previous LoRA stack "
            f"and remains healthy. Try a smaller stack or a different pod."
        )


class LoraSwapDiskFullError(LoraSwapError):
    """Mid-download disk full. Marked degraded."""

    def __init__(
        self,
        *,
        pod_id: str,
        evict_completed: list[str],
        download_failed: str,
    ) -> None:
        """Carry evicted refs + the ref whose download triggered ENOSPC."""
        super().__init__(pod_id=pod_id)
        self.evict_completed = list(evict_completed)
        self.download_failed = download_failed

    def __str__(self) -> str:
        """Render evicted + failed-download list."""
        evicted = ", ".join(self.evict_completed) or "(none)"
        return (
            f"Pod {self.pod_id} disk full mid-download: evicted [{evicted}], "
            f"failed to download {self.download_failed}. Pod marked degraded."
        )


class LoraStackConflict(KinoforgeError):
    """``cfg.loras`` and ``vault.loras`` both populated with diverging refs.

    Resolution: remove ``cfg.loras`` and use ``vault.loras`` as sole
    source per ephemeral spec D2's "vault is the canonical confidential
    source" rule.
    """


class SetStackRequestRejected(KinoforgeError):
    """Pod's ``/lora/set_stack`` endpoint returned 4xx — usually request shape.

    Defense-in-depth: client validation should have caught the same
    Pydantic bounds, so this firing indicates a contract drift between
    client and server schemas.
    """


# --- Video upscaling -------------------------------------------------------


class NotYetImplementedError(KinoforgeError):
    """Raised when a code path is intentionally deferred to a future session.

    Distinct from stdlib NotImplementedError (ABC abstract method) — this is
    explicit "we chose to parse-then-raise instead of refuse-at-parse-time"
    semantics. See ScaleTarget(kind="height").
    """


class UnsupportedScaleError(KinoforgeError):
    """Raised when an UpscalerEngine refuses a ScaleTarget its model can't serve.

    Carries enough context for post-mortem without session memory.
    """

    def __init__(self, scale: object, engine_name: str) -> None:
        """Record the scale and engine_name for post-mortem rendering."""
        super().__init__(
            f"engine {engine_name!r} does not support scale {scale!r}; "
            f"declared supported_scales gates this refusal"
        )
        self.scale = scale
        self.engine_name = engine_name


class ScaleUnsatisfiableError(KinoforgeError):
    """No supported upscale factor can reach the requested height target.

    Raised by :func:`kinoforge.core.scale_resolver.resolve_height_target` when
    even the largest declared factor leaves the output below the requested
    vertical resolution. Carries full context for post-mortem without session
    memory.
    """

    def __init__(
        self, source_h: int, largest_factor: float, reached_h: int, requested_h: int
    ) -> None:
        """Record source height, largest factor, reached height, and target."""
        super().__init__(
            f"no supported factor reaches {requested_h}p: source {source_h}p x "
            f"largest factor {largest_factor:g} = {reached_h}p (< {requested_h}p); "
            f"use a larger-factor engine"
        )
        self.source_h = source_h
        self.largest_factor = largest_factor
        self.reached_h = reached_h
        self.requested_h = requested_h


class UpscaleFailed(KinoforgeError):
    """Server-side upscale job entered an error state."""

    def __init__(self, job_id: str, server_error: str) -> None:
        """Record the failed job_id and server-supplied error description."""
        super().__init__(f"upscale job {job_id} failed on server: {server_error}")
        self.job_id = job_id
        self.server_error = server_error


class InterpolationError(KinoforgeError):
    """Server-side frame-interpolation job entered an error state."""

    def __init__(self, job_id: str, server_error: str) -> None:
        """Record the failed job_id and server-supplied error description."""
        super().__init__(f"interpolate job {job_id} failed on server: {server_error}")
        self.job_id = job_id
        self.server_error = server_error


class VRAMEvictionFailed(KinoforgeError):
    """Eviction policy exhausted all targets and the requested model still doesn't fit."""

    def __init__(self, model: str, reason: str) -> None:
        """Record the model that wouldn't fit and the reason eviction failed."""
        super().__init__(f"VRAM eviction failed for {model}: {reason}")
        self.model = model
        self.reason = reason


class StageMismatch(KinoforgeError):
    """Pod /health capabilities disagree with cfg's stages requirement."""

    def __init__(self, want: tuple[str, ...], have: tuple[str, ...]) -> None:
        """Record requested + actual stage tuples."""
        super().__init__(f"pod missing stages: want={want!r}, have={have!r}")
        self.want = want
        self.have = have


class UploadIntegrityError(KinoforgeError):
    """sha256 of bytes received by the server did not match what the client sent.

    Raised by ``SpandrelEngine._upload_source`` when the ``/upload`` response
    sha256 disagrees with the locally computed digest. Either side of the
    upload pipe corrupted bytes (network, kernel buffer, dirty filename
    sanitization).
    """

    def __init__(self, local_sha256: str, server_sha256: str, bytes_sent: int) -> None:
        """Record both hashes and the byte count for post-mortem rendering."""
        self.local_sha256 = local_sha256
        self.server_sha256 = server_sha256
        self.bytes_sent = bytes_sent
        super().__init__(
            f"upload sha256 mismatch: client={local_sha256} server={server_sha256} "
            f"bytes_sent={bytes_sent}"
        )


class ExtrasNotInstalled(KinoforgeError):
    """Raised when a kinoforge component requires a pip extras group that is not installed.

    Args:
        extras_name: The extras-group key (e.g. ``"seedvr"`` for
            ``kinoforge[seedvr]``).
        install_hint: Operator-facing remediation text (concrete command,
            workstream reference, or "use ``cfg.upscale.engine = 'spandrel'``
            instead" pointer).
    """

    def __init__(self, extras_name: str, install_hint: str) -> None:
        """Format the standard extras-not-installed message and stash both fields."""
        super().__init__(
            f"kinoforge[{extras_name}] extras not installed — {install_hint}"
        )
        self.extras_name = extras_name
        self.install_hint = install_hint


class BSACompileFailed(KinoforgeError):
    """Block-Sparse-Attention nvcc compile failed on pod.

    Raised inside the server when ``import block_sparse_attention`` fails at
    first ``/upscale`` after cold boot — the compile happened at provision
    time but produced no importable module.
    """

    def __init__(self, pod_id: str, stderr_tail: str) -> None:
        """Record the pod that failed to compile + tail of the compiler stderr."""
        super().__init__(
            f"Block-Sparse-Attention compile failed on pod {pod_id}: "
            f"{stderr_tail[-500:]}"
        )
        self.pod_id = pod_id
        self.stderr_tail = stderr_tail


class FlashVSRWeightsIncomplete(KinoforgeError):
    """FlashVSR weights bundle failed SHA256 verification against manifest.

    Distinguishes CDN corruption / repo tampering from a plain
    download-timeout (which would surface as TransportError).
    """

    def __init__(self, filename: str, got_sha256: str, want_sha256: str) -> None:
        """Record the file, observed sha, and expected sha for post-mortem."""
        super().__init__(
            f"FlashVSR weights integrity failure on {filename}: "
            f"got sha256={got_sha256[:8]}..., want={want_sha256[:8]}..."
        )
        self.filename = filename
        self.got_sha256 = got_sha256
        self.want_sha256 = want_sha256


class UnsupportedGpuArch(KinoforgeError):
    """Pod GPU compute capability is below FlashVSR's SM80 requirement.

    Raised via provision-script exit code 87. Surfaces up the orchestrator
    as a hard-fail before any ``/upscale`` work is attempted.
    """

    def __init__(self, got: tuple[int, int], required_major: int) -> None:
        """Record the observed SM (major, minor) and the minimum required major."""
        super().__init__(
            f"GPU compute capability sm_{got[0]}{got[1]} below required "
            f"sm_{required_major}0+"
        )
        self.got = got
        self.required_major = required_major
