"""LIVE EM1: --ephemeral --no-reuse FlashVSR 1080p upscale on Modal. Driven
manually via the CLI; this file records the contract."""

import pytest

pytestmark = pytest.mark.live

UPSCALE_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral upscale "
    "--config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml "
    "--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_"
    "Photorealistic-cinem.mp4 "
    "--no-reuse"
)


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS EM1 entry")
def test_modal_ephemeral_em1_contract():
    raise AssertionError(
        "run UPSCALE_CMD live; assert: opaque kinoforge-eph-{8hex} app (stopped), "
        "1080x1080 artifact in output/, empty ledger, store run dir scrubbed"
    )
