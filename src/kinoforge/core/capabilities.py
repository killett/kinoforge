"""Provider capability vocabulary + by-name lookup.

A provider declares what it can ACTUALLY enforce (design doc
docs/superpowers/specs/2026-08-16-provider-capability-declaration-design.md).
Every member below traces to a call site in some provider; nothing here is
aspirational.

This module deliberately imports nothing from kinoforge at module scope so
``core/interfaces.py`` can import it without a cycle. The registry and the
composition root are imported lazily inside :func:`capabilities_for`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kinoforge.core.interfaces import ComputeProvider, FieldSupport

__all__ = [
    "Capability",
    "WorkloadShape",
    "capabilities_for",
    "consumes_for",
    "provider_billed",
    "provider_registered",
]


class Capability(StrEnum):
    """A guardrail a provider can enforce on its own.

    HEARTBEAT_READ       — ``last_heartbeat()`` returns a timestamp sourced
                           from the instance, not the orchestrator clock.
    RUNTIME_PROBE        — ``probe_runtime()`` returns a real RuntimeProbe.
    UTIL_SNAPSHOT        — a util endpoint with a wire path to UtilSnapshot.
    IDLE_AUTOSTOP        — the provider stops the instance after idleness.
    ON_INSTANCE_DEADLINE — something ON the instance kills it at a wall-clock
                           deadline, surviving the controller's death.
    JOB_TIMEOUT          — the provider enforces cfg's per-job timeout.
    PAUSE_BILLING        — ``stop_instance()`` pauses billing without destroying.
    BALANCE_QUERY        — live account balance readable from the provider.
    """

    HEARTBEAT_READ = "HEARTBEAT_READ"
    RUNTIME_PROBE = "RUNTIME_PROBE"
    UTIL_SNAPSHOT = "UTIL_SNAPSHOT"
    IDLE_AUTOSTOP = "IDLE_AUTOSTOP"
    ON_INSTANCE_DEADLINE = "ON_INSTANCE_DEADLINE"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    PAUSE_BILLING = "PAUSE_BILLING"
    BALANCE_QUERY = "BALANCE_QUERY"


class WorkloadShape(StrEnum):
    """Whether the deploy leaves a long-lived process on the instance.

    SERVER — ``spec.launch`` is set; the job never terminates, so
             provider-side idle detection can never fire (verification doc
             F1, confirmed against skypilot 0.12.3.post1).
    BATCH  — ``spec.launch`` is None; the setup steps run and exit, so
             the instance genuinely reaches idle.
    """

    SERVER = "server"
    BATCH = "batch"


_adapters_imported = False


def _ensure_adapters_imported() -> None:
    """Import the composition root once so providers self-register.

    ``kinoforge._adapters`` is the single module that imports all four
    provider packages. Importing it here (lazily, once) keeps a reaper or
    validation context that never touched providers answering the same as
    production instead of silently degrading to "nothing is supported".
    """
    global _adapters_imported
    if _adapters_imported:
        return
    _adapters_imported = True
    import kinoforge._adapters  # noqa: F401,PLC0415 — composition root, lazy by design


def _provider_class(provider_kind: str) -> type[ComputeProvider] | None:
    """Return the registered provider class for ``provider_kind`` or None."""
    from kinoforge.core import registry  # noqa: PLC0415 — avoids an import cycle

    cls = registry.provider_class(provider_kind)
    if cls is None:
        _ensure_adapters_imported()
        cls = registry.provider_class(provider_kind)
    return cls


def capabilities_for(
    provider_kind: str,
    shape: WorkloadShape = WorkloadShape.SERVER,
) -> frozenset[Capability]:
    """Return the capabilities ``provider_kind`` declares at ``shape``.

    Args:
        provider_kind: Registry key, e.g. ``"skypilot"``.
        shape: Workload shape the declaration is evaluated at.

    Returns:
        The declared set, or an empty frozenset for an unknown provider —
        matching the ``name not in frozenset`` semantics of the string
        tables this replaces.
    """
    cls = _provider_class(provider_kind)
    if cls is None:
        return frozenset()
    # getattr default, not a bare cls.capabilities(shape): registry.register_provider
    # accepts whatever it is handed (tests register `object`), so a registered
    # non-ComputeProvider must answer "declares nothing" — the documented
    # contract — rather than AttributeError out of a lookup the reaper makes
    # on every sweep.
    declare = getattr(cls, "capabilities", None)
    if declare is None:
        return frozenset()
    caps: frozenset[Capability] = declare(shape)
    return caps


def consumes_for(provider_kind: str) -> Mapping[str, FieldSupport]:
    """Return the field-support mapping ``provider_kind`` declares.

    The read-side twin of :func:`capabilities_for`, and the ONLY way
    production code should learn which portable fields a provider honours —
    ``tests/providers/test_field_consumption_parity.py`` proves the
    declaration against captured launch payloads, so re-deriving the matrix
    anywhere else would create a second, unproven copy.

    Args:
        provider_kind: Registry key, e.g. ``"skypilot"``.

    Returns:
        The declared mapping, or an empty mapping for an unknown provider or
        one that never declared. Callers that must tell those two apart use
        :func:`provider_registered`, exactly as they do for capabilities.
    """
    cls = _provider_class(provider_kind)
    if cls is None:
        return {}
    # getattr default, not a bare cls.consumes(): register_provider accepts
    # whatever it is handed (tests register bare objects), so a registered
    # non-ComputeProvider must answer "declares nothing" rather than
    # AttributeError out of a validation pass.
    declare = getattr(cls, "consumes", None)
    if declare is None:
        return {}
    declared: Mapping[str, FieldSupport] = declare()
    return declared


def provider_registered(provider_kind: str) -> bool:
    """Return whether ``provider_kind`` names a registered provider at all.

    :func:`capabilities_for` deliberately conflates "unknown provider" with
    "declares nothing" — correct for the boolean support predicates it
    backs. A caller that *refuses* on an empty declaration needs the two
    apart: an unknown name has no declaration to compare against, and
    ``registry.get_provider`` already refuses it at launch with an accurate
    message. Use this to skip rather than to misattribute.

    Args:
        provider_kind: Registry key, e.g. ``"skypilot"``.

    Returns:
        True iff a provider class is registered under ``provider_kind``.
    """
    return _provider_class(provider_kind) is not None


def provider_billed(provider_kind: str) -> bool:
    """Return whether instances of ``provider_kind`` cost money.

    Unknown providers are assumed billed — the conservative direction, since
    the spend-risk validation rows are skipped only when this is False.
    """
    cls = _provider_class(provider_kind)
    if cls is None:
        return True
    # getattr default matches capabilities_for: a registered object that is
    # not a ComputeProvider must fall back to the conservative answer rather
    # than raise AttributeError.
    return bool(getattr(cls, "billed", True))
