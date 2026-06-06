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


class TeardownError(KinoforgeError):
    """destroy_instance could not confirm termination."""


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
