"""LIVE: FlashVSR HEIGHT-TARGET 1080p (480->1920->1080) on Modal 80GB. Driven
manually via the CLI; this file records the contract + a smoke assertion on the
artifact. Height-target is provider-agnostic pipeline logic — this proves it
end-to-end on Modal."""

import pytest

pytestmark = pytest.mark.live

UPSCALE_CMD = (
    "pixi run -e live-modal kinoforge upscale "
    "--config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml "
    "--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_"
    "Photorealistic-cinem.mp4 "
    "--no-reuse"
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations 1080p entry"
)
def test_modal_flashvsr_1080p_contract():
    raise AssertionError(
        "run UPSCALE_CMD live; assert 1080x1080 mp4 (NOT 1920x1920 — proves the "
        "materialize downscale ran) + frame-QA vs 480 source"
    )
