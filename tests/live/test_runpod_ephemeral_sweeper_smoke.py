"""Live smoke: RunPod sweeper reaps a wedged ephemeral pod (STALL_REAP path).

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5.10

Pre-conditions:
  * ``pixi run preflight`` returns exit 0 (RUNPOD/HF creds present, no
    live pods, clean working tree).
  * Standard test prompt at ``examples/configs/prompts/field-realistic.txt``
    (per project rule for cross-model comparability).

Cost budget: ≤ $0.40 — one ephemeral Wan T2V provision + selfterm-killed
run + sweeper-triggered destroy. The sweeper runs with ``stall_window_s=120``
+ ``interval_s=30`` so STALL_REAP fires ≤ 4 ticks after selfterm dies.

Flow:
  1. Start ``kinoforge sweeper start`` in background with the small
     stall window above.
  2. Provision an ephemeral Wan-T2V pod (``--no-reuse`` so the cold path
     records into the EphemeralIndex on success).
  3. SSH into the pod and ``pkill -9 -f selfterm`` (simulate selfterm
     crash so the pod becomes wedged at 0% GPU).
  4. Wait stall_window_s + ~1 interval; assert sweeper destroyed the
     pod (poll ``kinoforge list`` + the RunPod GraphQL probe).
  5. Assert ``ephemeral-index.json`` no longer carries the pod id.
  6. Sweeper teardown.

This file is RED-scaffolded per project durability rule: the scaffold
is committed BEFORE any live RunPod spend is invoked. Task 10 lifts
the module-level skip and produces evidence.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "live smoke — run manually after `pixi run preflight` exits 0; "
        "budget-controlled spend; remove this skip during Task 10."
    )
)


def test_runpod_sweeper_reaps_wedged_ephemeral_pod() -> None:
    """Placeholder body; Task 10 fills the orchestration.

    Pseudo-code (Task 10 will realise):
        with _background_sweeper(stall_window_s=120, interval_s=30):
            pod_id = _provision_ephemeral_wan_t2v(prompt=_standard_prompt())
            try:
                _kill_selfterm_on(pod_id)
                deadline = time.time() + 240
                while time.time() < deadline:
                    if not _pod_alive(pod_id):
                        break
                    _poll_runtime(pod_id)  # surface util every ~30s
                    time.sleep(15)
                else:
                    pytest.fail(f"sweeper did not destroy {pod_id} within 240s")
                _assert_index_row_gone(pod_id)
            finally:
                if _pod_alive(pod_id):
                    _destroy_pod(pod_id)  # failsafe
    """
    raise AssertionError("Task 10 — flesh out live orchestration + evidence")
