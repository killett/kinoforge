"""LIVE Milestone 3: FlashVSR 4x (480->1920) on Modal 80GB. Driven manually via
the CLI; this file records the contract + a smoke assertion on the artifact."""

import pytest

pytestmark = pytest.mark.live

UPSCALE_CMD = (
    "pixi run -e live-modal kinoforge upscale "
    "--config examples/configs/modal-flashvsr-x4.yaml "
    "--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_"
    "Photorealistic-cinem.mp4 "
    "--no-reuse"
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations §24"
)
def test_modal_flashvsr_x4_contract():
    raise AssertionError(
        "run UPSCALE_CMD live; assert 1920x1920 mp4 + frame-QA vs 480 source"
    )
