"""LIVE EM3: sweeper reaps an idle bare---ephemeral Modal app. Driven manually
via the CLI; this file records the contract."""

import pytest

pytestmark = pytest.mark.live

GEN_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral generate "
    "--config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml "
    "--mode t2v "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)"'
)
# After GEN_CMD exits (app idle), run the sweeper with stall-tight thresholds
# (mirror tests/live/test_runpod_ephemeral_sweeper_smoke.py mechanics) and
# assert the eph app is reaped.


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS EM3 entry")
def test_modal_ephemeral_em3_contract():
    raise AssertionError(
        "run GEN_CMD, leave idle, run sweeper; assert STALL/IDLE reap of the "
        "eph app: destroy fired, modal app list -> stopped, index row gone"
    )
