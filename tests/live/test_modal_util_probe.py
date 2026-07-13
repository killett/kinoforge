"""LIVE: Modal util probe returns real GPU% mid-generation (Wan 2.1 1.3B / A10).

Driven manually via the controller; this file records the contract. Marked
`live` so the default suite (`-m 'not live'`) skips it. Mirrors the M5 scaffold.

Runbook:
  1. Start a warm gen (default reuse): pod stays up, ledger carries endpoints.
     pixi run -e live-modal kinoforge generate \
       --config examples/configs/modal-wan-t2v-1_3b.yaml --mode t2v \
       --prompt "$(cat examples/configs/prompts/field-realistic.txt)"
  2. Resolve the instance id from `pixi run kinoforge list`, then poll read_util
     via ModalUtilEndpoint (ledger-resolved) DURING a second gen; assert
     gpu_util_percent > 0 under load, ~0 when idle.
  3. Teardown: kinoforge destroy + verify `kinoforge list` and `modal app list`.
"""

import pytest

pytestmark = pytest.mark.live


@pytest.mark.xfail(reason="live proof driven via controller; see PROGRESS")
def test_modal_util_probe_reports_gpu_load():
    raise AssertionError(
        "run a warm Modal gen; poll ModalUtilEndpoint.read_util(id) mid-gen; "
        "assert gpu_util_percent > 0 under load and ~0 idle; then teardown"
    )
