"""Core provisioner: orchestrates shared provisioning steps, delegates to engine.

Workflow (in order):
  1. Resolve each model entry's ref via the source registry.
  2. Merge per-entry ``sha256`` and ``target`` onto each resolved Artifact.
  3. If ``engine.requires_local_weights`` is True, pass the merged artifact list
     to the injected *downloader*.  Otherwise, skip downloads (refs were still
     parsed — useful for identity/CapabilityKey derivation on the hosted path).
  4. Call the optional *post_provision_hook* with the Instance (or None).
  5. Call ``engine.provision(instance, cfg)`` last.

``provisioner.py`` imports ONLY from ``kinoforge.core.*``.  It never imports a
concrete engine, provider, or source adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from kinoforge.core import registry
from kinoforge.core.downloader import download_all
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CredentialProvider,
    GenerationEngine,
    Instance,
)

# ---------------------------------------------------------------------------
# Structural Protocols (private — keep provisioner decoupled from config.py)
# ---------------------------------------------------------------------------


@runtime_checkable
class _ModelEntryLike(Protocol):
    """A single model entry as seen by the provisioner."""

    ref: str
    target: str
    sha256: str | None


@runtime_checkable
class _ProvisionConfig(Protocol):
    """The minimal config surface the provisioner needs."""

    models: list[_ModelEntryLike]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def provision(
    engine: GenerationEngine,
    cfg: _ProvisionConfig,
    instance: Instance | None,
    *,
    creds: CredentialProvider,
    download_dir: Path,
    post_provision_hook: Callable[[Instance | None], None] | None = None,
    downloader: Callable[[list[Artifact], Path], list[Artifact]] = download_all,
) -> None:
    """Orchestrate shared provisioning steps and delegate to *engine*.

    Steps (in order):

    1. For each entry in ``cfg.models``, call
       ``registry.source_for_ref(entry.ref).resolve(entry.ref, creds)`` to obtain
       a list of ``Artifact``s.  Merge ``entry.sha256`` (when set) and
       ``entry.target`` (into ``artifact.meta["target"]``) onto each artifact
       without mutating the original.

    2. If ``engine.requires_local_weights`` is ``True``, pass the flat list of
       all merged artifacts to *downloader* in a single call.  If ``False``,
       skip downloads entirely (refs were still parsed in step 1 for identity).

    3. Call ``post_provision_hook(instance)`` when not ``None``.

    4. Call ``engine.provision(instance, cfg)`` **last**.

    If no registered source handles a ref, ``registry.source_for_ref`` raises
    :class:`~kinoforge.core.errors.UnknownAdapter`; the provisioner does **not**
    catch it.

    Args:
        engine: The generation engine that owns the final provision step.
        cfg: A config-like object exposing ``.models`` (list of model entries).
        instance: The created compute instance, or ``None`` for hosted engines.
        creds: Credential provider forwarded to each source's ``resolve``.
        download_dir: Destination directory for the downloader.
        post_provision_hook: Optional zero-arg-or-instance callable invoked after
            downloads and before ``engine.provision``.  Signature:
            ``(instance: Instance | None) -> None``.  Not called when ``None``.
        downloader: Injectable callable with signature
            ``(artifacts: list[Artifact], dest: Path) -> list[Artifact]``.
            Defaults to :func:`~kinoforge.core.downloader.download_all`.

    Raises:
        UnknownAdapter: Propagated from ``registry.source_for_ref`` when no
            registered source handles a model ref.
    """
    # ------------------------------------------------------------------
    # Step 1 — resolve all entries and merge per-entry fields onto artifacts
    # ------------------------------------------------------------------
    merged: list[Artifact] = []
    for entry in cfg.models:
        source = registry.source_for_ref(entry.ref)
        artifacts = source.resolve(entry.ref, creds)
        # Guard: an entry-level sha256 cannot describe a multi-file resolve.
        # Stamping one hash onto N artifacts would mask N-1 silent integrity
        # failures.  This protects both HF bare-repo refs and CivitAI
        # multi-file model versions from the same operator footgun.
        if len(artifacts) > 1 and entry.sha256 is not None:
            raise ValidationError(
                f"sha256 cannot be set on ref {entry.ref!r} — "
                f"it resolves to {len(artifacts)} artifacts. "
                f"Use a pinned revision (e.g. @<commit-sha>) for "
                f"tree-level integrity, or split the entry into "
                f"per-file refs."
            )
        for art in artifacts:
            merged_art = replace(
                art,
                sha256=entry.sha256 if entry.sha256 is not None else art.sha256,
                meta={**art.meta, "target": entry.target},
            )
            merged.append(merged_art)

    # ------------------------------------------------------------------
    # Step 2 — download only when the engine requires local weights
    # ------------------------------------------------------------------
    if engine.requires_local_weights:
        downloader(merged, download_dir)

    # ------------------------------------------------------------------
    # Step 3 — optional hook
    # ------------------------------------------------------------------
    if post_provision_hook is not None:
        post_provision_hook(instance)

    # ------------------------------------------------------------------
    # Step 4 — delegate to the engine (LAST)
    #
    # engine.provision expects a plain dict (see GenerationEngine ABC).  The
    # provisioner accepts any _ProvisionConfig-shaped struct; convert pydantic
    # models via model_dump(), pass dicts through untouched.
    # ------------------------------------------------------------------
    if hasattr(cfg, "model_dump"):
        cfg_for_engine = cfg.model_dump()
    else:
        cfg_for_engine = cfg
    engine.provision(instance, cfg_for_engine)
