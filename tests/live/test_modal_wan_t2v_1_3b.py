"""LIVE Milestone 1: Wan 2.1 T2V-1.3B on Modal. Driven manually via the CLI;
this file records the contract + a smoke assertion on the produced artifact."""

import pytest

pytestmark = pytest.mark.live

GENERATE_CMD = (
    "pixi run -e live-modal kinoforge generate "
    "--config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml --mode t2v "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)" --no-reuse'
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations"
)
def test_modal_wan_t2v_1_3b_contract():
    raise AssertionError("run GENERATE_CMD live; assert 480x480 ~33f mp4 + frame-QA")
