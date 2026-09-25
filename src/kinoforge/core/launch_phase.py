"""The launch-phase wire contract, in a module with no I/O.

Home for the constants and the one predicate that describe the orchestrator's
pre-launch provisional ledger row (compute-seam S5, finding F12). They lived in
``core/lifecycle.py``, which also owns :class:`~kinoforge.core.lifecycle.Ledger`
— and ``tests/test_core_invariant.py::test_core_reaper_module_is_pure`` forbids
``core/reaper.py`` from importing that module at all, precisely so nothing
inside ``classify()`` can reach the ledger.

U64 needed the reaper to honour the phase tag, which made that collision
structural rather than incidental: a wire-format constant is not I/O, and
keeping it in the I/O module forced every pure consumer to choose between
violating the purity invariant and keeping a private copy. Four copies of the
predicate had already accumulated for exactly that reason. This module is the
single owner; ``core/lifecycle.py`` re-exports for its existing importers.

``cli/_reconcile`` keeps its own literals deliberately — it is imported on every
CLI command and these are wire-format values that cannot be renamed without a
migration anyway — and ``tests/core/test_reaper_launching_rows.py`` asserts its
grace window still agrees with the canonical one.
"""

from __future__ import annotations

from typing import Any

#: Tag key + value marking the orchestrator's pre-launch provisional row.
#: Written by ``core.orchestrator._record_provisional_row`` and the ONLY thing
#: that distinguishes a provisional row from a real one when the two share an
#: id — which they do on SkyPilot, where the cluster name is the run id.
LAUNCH_PHASE_TAG = "kf_launch_phase"
LAUNCH_PHASE_LAUNCHING = "launching"

#: How long a provisional row with no matching provider resource is presumed to
#: be a launch still in flight rather than debris (U64).
#:
#: Every consumer that decides whether such a row is actionable must honour the
#: SAME window, or the shorter one deletes rows the longer one still considers
#: in-flight. Deliberately generous — twice the 900 s
#: ``Lifecycle.boot_timeout_s`` default — because the cost of being wrong is
#: asymmetric: too early deletes the only durable handle on a pod that is
#: booting and billing; too late merely prolongs a $0.00 ghost row.
#:
#: NOT a boot timeout, and deliberately not named like one: a caller reading
#: "boot_timeout" off a signature would reasonably pass the 900 s lifecycle
#: value and halve a window whose whole point is generosity.
LAUNCHING_GRACE_S: float = 1800.0


def is_launching(entry: Any) -> bool:  # noqa: ANN401 — ledger-shaped mapping
    """Return whether *entry* is a pre-launch provisional row.

    Compares the tag's VALUE, not its presence: the orchestrator phase-tags
    rows in other states too, and a presence test would exempt them from
    everything this predicate gates.

    Args:
        entry: A ledger entry mapping. A missing or non-dict ``tags`` is not a
            launching row.

    Returns:
        True when ``tags[LAUNCH_PHASE_TAG] == LAUNCH_PHASE_LAUNCHING``.
    """
    tags = entry.get("tags") if hasattr(entry, "get") else None
    if not isinstance(tags, dict):
        return False
    return bool(tags.get(LAUNCH_PHASE_TAG) == LAUNCH_PHASE_LAUNCHING)
