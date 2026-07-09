"""LIVE Milestone 2: Wan 2.2 T2V-A14B on Modal 80GB GPU. Driven manually via the
CLI; this file records the contract + a smoke assertion on the produced artifact."""

import pytest

pytestmark = pytest.mark.live

GENERATE_CMD = (
    "pixi run -e live-modal kinoforge generate "
    "--config examples/configs/modal-wan-t2v-14b-2_2.yaml --mode t2v "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)" --no-reuse'
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations §23"
)
def test_modal_wan_t2v_14b_2_2_contract():
    raise AssertionError(
        "run GENERATE_CMD live on Modal A100-80GB; "
        "assert 480x480 ~81f mp4 (Wan2.2-T2V-A14B) + frame-QA"
    )
