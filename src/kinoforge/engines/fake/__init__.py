"""FakeEngine / FakeBackend — a deterministic, GPU-free test substrate.

Importing this module registers the ``"fake"`` engine factory in the global
registry so that ``registry.get_engine("fake")()`` returns a ready
``FakeEngine`` without any weight downloads or compute provisioning.

Design notes
------------
* ``FakeBackend`` stores submitted jobs in ``dict[str, GenerationJob]``.
* ``Artifact.filename`` is derived deterministically:
  ``sha256("|".join(s.prompt for s in job.segments))[:12]`` → ``clip-<hex12>.mp4``.
* ``FakeBackend.inspect_capabilities()`` returns the probe profile unchanged
  (flags stay False; strategy flags are layered in by ``discover``, not here).
* ``profile_for`` is deferred until Task 12 wires ``ModelProfileProvider``.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ConditioningAsset,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
)


class FakeBackend(GenerationBackend):
    """In-process, GPU-free backend for tests and dry-runs.

    Submitted jobs are held in memory; results are deterministic and
    require no real compute or model weights.

    Attributes:
        _probe: The injected ``ModelProfile`` returned by capability queries.
        _jobs: Map of job-id → ``GenerationJob`` for submitted jobs.
    """

    def __init__(self, probe: ModelProfile) -> None:
        """Initialise with an injected probe profile.

        Args:
            probe: The ``ModelProfile`` returned unchanged by
                ``inspect_capabilities`` and ``capabilities``.
        """
        self._probe = probe
        self._jobs: dict[str, GenerationJob] = {}

    def capabilities(self) -> ModelProfile:
        """Return the probe profile.

        Returns:
            The ``ModelProfile`` supplied at construction time.
        """
        return self._probe

    def inspect_capabilities(self) -> ModelProfile:
        """Return the probe profile unchanged.

        Probe surfaces probeable fields only; strategy flags (which come from
        ``declared_flags``) must remain False on the probe object.

        Returns:
            The ``ModelProfile`` supplied at construction time, unmodified.
        """
        return self._probe

    def submit(self, job: GenerationJob) -> str:
        """Store ``job`` and return a unique job identifier.

        Args:
            job: The ``GenerationJob`` to queue.

        Returns:
            A non-empty UUID-4 string that identifies this submission.
        """
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = job
        return job_id

    def result(self, job_id: str) -> Artifact:
        """Return a deterministic ``Artifact`` for the previously submitted job.

        The filename is derived from the segment prompts so that two
        ``submit→result`` round-trips on equivalent jobs produce the same
        ``Artifact.filename``.

        Args:
            job_id: The identifier returned by a prior ``submit`` call.

        Returns:
            An ``Artifact`` whose ``filename`` is
            ``clip-<sha256[:12] of joined prompts>.mp4``.

        Raises:
            KeyError: ``job_id`` was never submitted to this backend.
        """
        job = self._jobs[job_id]
        combined = "|".join(s.prompt for s in job.segments)
        hex12 = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]
        return Artifact(filename=f"clip-{hex12}.mp4")

    def endpoints(self) -> dict[str, str]:
        """Return the fake endpoint map.

        Returns:
            A dict with a single ``"generate"`` key pointing at a fake URL.
        """
        return {"generate": "fake://local"}


class FakeEngine(GenerationEngine):
    """Deterministic, GPU-free generation engine for tests and dry-runs.

    This engine is a SHIPPED adapter (not test-only) under ``engines/fake/``.
    It proves the no-weights / no-compute path and is reused by later tasks
    (Profiles, Strategy, Orchestrator).

    Class attributes:
        name: Always ``"fake"`` — the registry key.
        requires_compute: ``True`` (declared; the fake never actually uses it).
        requires_local_weights: ``False`` (no weights needed).

    Args:
        probe_profile: The ``ModelProfile`` injected into every ``FakeBackend``
            that this engine creates.
        declared_flags_map: Maps ``CapabilityKey.derive()`` hex strings to a
            ``dict[str, bool]`` of strategy flags (e.g.
            ``{"supports_native_extension": True}``).
        required_spec_keys: Keys that must be present in ``job.spec``; their
            absence causes ``validate_spec`` to raise ``ValidationError``.
    """

    name: str = "fake"
    requires_compute: bool = True
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        probe_profile: ModelProfile,
        declared_flags_map: dict[str, dict[str, Any]],
        required_spec_keys: set[str],
    ) -> None:
        """Initialise the fake engine with injected strategy and probe data.

        Args:
            probe_profile: Passed unchanged to every ``FakeBackend`` instance.
            declared_flags_map: Keyed by ``CapabilityKey.derive()``; returned
                verbatim by ``declared_flags``.
            required_spec_keys: ``validate_spec`` raises ``ValidationError``
                when any key in this set is absent from ``job.spec``.
        """
        self._probe = probe_profile
        self._declared_flags_map = declared_flags_map
        self._required_spec_keys = required_spec_keys

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        """No-op — the fake engine requires no downloads or setup.

        Args:
            instance: Ignored.
            cfg: Ignored.
        """

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> FakeBackend:
        """Create and return a ``FakeBackend`` wired to the injected probe.

        Args:
            instance: Unused; present to satisfy ``GenerationEngine`` contract.
            cfg: Unused; present to satisfy ``GenerationEngine`` contract.

        Returns:
            A fresh ``FakeBackend`` backed by the engine's probe profile.
        """
        del instance, cfg
        return FakeBackend(probe=self._probe)

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise ``NotImplementedError`` — deferred to Task 12.

        # DEFERRED: profile_for is supplied by ModelProfileProvider.resolve in Task 12.

        Args:
            key: The ``CapabilityKey`` to resolve (unused here).

        Raises:
            NotImplementedError: Always. Task 12 wires the real cache.
        """
        raise NotImplementedError(
            "FakeEngine.profile_for is supplied by ModelProfileProvider in Task 12"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        """Return the strategy flags declared for ``key``, or ``{}`` if unknown.

        Args:
            key: The ``CapabilityKey`` whose derive() is looked up in the map.

        Returns:
            A ``dict[str, bool]`` of strategy flags, e.g.
            ``{"supports_native_extension": True}``, or an empty dict when
            ``key`` was not registered.
        """
        return dict(self._declared_flags_map.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Raise ``ValidationError`` when required spec keys are missing.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is checked.

        Raises:
            ValidationError: One or more keys from ``required_spec_keys`` are
                absent from ``job.spec``.
        """
        missing = self._required_spec_keys - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"job.spec is missing required keys: {sorted(missing)}"
            )

    def extract_last_frame(self, artifact: Artifact) -> ConditioningAsset:
        """Deterministic tail-frame asset for tests.

        Returns a ConditioningAsset whose ref carries a synthetic filename
        derived from the input artifact's filename, so tests can assert on
        a predictable shape without real image data.

        Args:
            artifact: A clip Artifact from a prior render.

        Returns:
            ConditioningAsset(kind="image", role="init_image", ref=Artifact(
                filename=f"{artifact.filename}.tail.png",
                meta={"derived_from": artifact.filename},
            ))
        """
        return ConditioningAsset(
            kind="image",
            role="init_image",
            ref=Artifact(
                filename=f"{artifact.filename}.tail.png",
                meta={"derived_from": artifact.filename},
            ),
        )


# ---------------------------------------------------------------------------
# Module-level default probe and self-registration
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="fake",
    max_frames=16,
    fps=8,
    supported_modes={"t2v"},
    max_resolution=(512, 512),
    supports_native_extension=False,
    supports_joint_audio=False,
)

registry.register_engine(
    "fake",
    lambda: FakeEngine(
        probe_profile=_DEFAULT_PROBE,
        declared_flags_map={},
        required_spec_keys=set(),
    ),
)
