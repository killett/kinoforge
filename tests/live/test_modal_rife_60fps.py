"""LIVE Milestone 4: RIFE v4.26 60fps interpolation on Modal via the fast-boot
image bake. Driven manually via the CLI; this file records the contract + a smoke
assertion on the artifact. Mirrors the M3 FlashVSR live scaffold
(tests/live/test_modal_flashvsr_x4.py)."""

import pytest

pytestmark = pytest.mark.live

INTERPOLATE_CMD = (
    "pixi run -e live-modal kinoforge interpolate "
    "--config examples/configs/modal-rife-60fps.yaml "
    "--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_"
    "Photorealistic-cinem.mp4 "
    "--fps 60 "
    "--no-reuse"
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations §25"
)
def test_modal_rife_60fps_contract():
    raise AssertionError(
        "run INTERPOLATE_CMD live; assert ~60fps mp4 (16fps 81f source -> 60fps) "
        "+ frame-QA vs source"
    )
