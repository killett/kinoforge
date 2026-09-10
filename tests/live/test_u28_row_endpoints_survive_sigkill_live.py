"""LIVE U28: the ephemeral index row carries endpoints when the run is KILLED.

U28's fix (``f1e7f1ef``) upgrades the ``EphemeralIndex`` row from the
orchestrator's ``on_instance_created`` hook, which fires right after
``create_instance`` and before ``engine.provision``. Offline tests pin that
mid-run and on a raised exception — but a raise unwinds Python, and a SIGKILL
does not. Only a real kill proves the row on disk is complete at the instant
the controller stops existing, which is the exact scenario the row was
introduced for.

Mirrors Task 6's A1 cell, which proved U23 the same way: reserve, kill
mid-generation, then read state from processes that never saw the run.

Deliberate deviation, recorded so it is not mistaken for the money leak the
``--no-reuse`` rule exists to prevent: the run is killed, so nothing tears the
pod down. Teardown is therefore MANUAL and verified — ``kinoforge destroy
--id`` off the row itself, which is also criterion 3 below.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

CFG = "examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml"

#: Root-position ``--ephemeral`` (U27 made both positions work; root is the
#: form Task 6 proved live). No ``--no-reuse``: the kill is the point.
GEN_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral generate "
    f"--config {CFG} --mode t2v "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)"'
)

#: Kill the whole process group, not the leader — `pixi run` spawns a child,
#: and killing only the parent leaves the real controller alive and the test
#: measuring nothing.
KILL_CMD = "kill -9 -<pgid>"

#: Both reads come from processes that never saw the run.
INDEX_READ = "EphemeralIndex(store=ctx.store()).rows()  # via a fresh SessionContext"
TEARDOWN_CMD = "pixi run -e live-modal kinoforge destroy --id <row-id>"
VERIFY_CMD = "pixi run -e live-modal kinoforge list"


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS U28 entry")
def test_u28_row_endpoints_survive_a_sigkill_contract() -> None:
    """Contract for the U28 live proof.

    Pass criteria, all read from processes that did not run the generation:

    1. **Before the kill**, while generation is in flight, the index row is
       keyed by the pod's REAL id and its ``endpoints`` is non-empty. Pre-fix
       the row stayed under the reserved name with ``endpoints: {}`` for the
       whole run. Read from a fresh ``SessionContext`` off the same
       ``state_dir``, i.e. what ``kinoforge reap`` would see.
    2. **After SIGKILL of the process group**, that row is unchanged on disk —
       same id, same endpoints. This is the criterion an exception-based test
       cannot reach: a raise unwinds and can still run code, a SIGKILL cannot.
    3. The recorded endpoint is **usable**: ``GET <endpoint>/util`` answers
       from a fresh process, so the reaper's ``note_endpoints`` priming would
       have something real to probe. A row carrying a plausible-looking but
       dead URL would satisfy 1 and 2 and still leave the orphan unreapable,
       which is the failure U28 is about.
    4. ``kinoforge destroy --id <row-id>`` off that row alone destroys the pod
       — the row is a working handle, not just a record.

    Obligations: poll utilisation (``gpuUtilPercent``), never ``est_spend``;
    frame-QA any clip that lands before the kill; and verify teardown from a
    fresh process after everything exits — ``kinoforge list`` both lines, the
    index empty, and no non-stopped kinoforge app in ``modal app list``.
    """
    raise AssertionError(
        "run GEN_CMD live, read the index from a fresh process mid-generation, "
        "SIGKILL the process group, re-read the index, confirm the endpoint "
        "answers /util, then destroy off the row id and verify teardown"
    )
