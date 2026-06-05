"""FakeImageEngine: deterministic GPU-free image engine for offline tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
)


@dataclass
class FakeImageBackend(ImageBackend):
    """Deterministic backend: sha256(prompt+spec) → 16-hex submit id; synthetic Artifact on result."""

    profile_to_return: ImageProfile

    def capabilities(self) -> ImageProfile:
        """Return the in-force profile for this backend.

        Returns:
            The configured ``ImageProfile``.
        """
        return self.profile_to_return

    def inspect_capabilities(self) -> ImageProfile:
        """Live-probe the backend to discover capabilities fresh.

        Returns:
            The configured ``ImageProfile``.
        """
        return self.profile_to_return

    def submit(self, job: ImageJob) -> str:
        """Hash (prompt, spec) to a deterministic 16-hex job id.

        Args:
            job: The image job to submit.

        Returns:
            A 16-character hex string derived from the job contents.
        """
        seed = json.dumps(
            [job.prompt, sorted(job.spec.items())],
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def result(self, job_id: str) -> Artifact:
        """Return a synthetic Artifact keyed off ``job_id``.

        Args:
            job_id: The job id returned by ``submit``.

        Returns:
            An ``Artifact`` with a filename derived from ``job_id``.
        """
        return Artifact(
            filename=f"fake-image-{job_id}.png",
            meta={"_kf_job_id": job_id, "_synthetic": True},
        )

    def endpoints(self) -> dict[str, str]:
        """Return the fake endpoint map.

        Returns:
            A dict with a single ``"local"`` endpoint.
        """
        return {"local": "fake://image"}


@dataclass
class FakeImageEngine(ImageEngine):
    """Hosted-style fake image engine; no compute, no weights."""

    name: str = "fake"
    requires_compute: bool = False
    requires_local_weights: bool = False
    profile_to_return: ImageProfile = field(
        default_factory=lambda: ImageProfile(
            name="fake-image",
            max_resolution=(1024, 1024),
            supported_modes={"t2i"},
        )
    )
    required_spec_keys: frozenset[str] = field(
        default_factory=lambda: frozenset({"model"})
    )

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        """No-op provision for hosted engines.

        Args:
            instance: Unused; hosted engine needs no instance.
            cfg: Unused runtime config.
        """
        return  # hosted no-op

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend:
        """Return a ``FakeImageBackend`` configured with the default profile.

        Args:
            instance: Unused.
            cfg: Unused runtime config.

        Returns:
            A ``FakeImageBackend`` instance.
        """
        return FakeImageBackend(profile_to_return=self.profile_to_return)

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        """Return the default ``ImageProfile`` regardless of key.

        Args:
            key: Capability key (ignored for the fake engine).

        Returns:
            The configured ``ImageProfile``.
        """
        return self.profile_to_return

    def validate_spec(self, job: ImageJob) -> None:
        """Raise ``ValidationError`` if required spec keys are absent.

        Args:
            job: The image job whose ``spec`` is validated.

        Raises:
            ValidationError: One or more required keys are missing from ``job.spec``.
        """
        missing = self.required_spec_keys - set(job.spec)
        if missing:
            raise ValidationError(
                f"FakeImageEngine: missing spec keys: {sorted(missing)}"
            )


registry.register_image_engine("fake", lambda: FakeImageEngine())
