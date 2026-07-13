"""LIVE Milestone 5: Modal warm-reuse + HF Volume cache on Wan 2.1 1.3B / A10.

Driven manually via the CLI; this file records the 3-run contract. Mirrors the
M4 RIFE live scaffold (tests/live/test_modal_rife_60fps.py). Marked `live` so
the default suite (`-m 'not live'`) skips it.

Sequence (all separate CLI invocations; default warm-reuse, NO --no-reuse until
teardown):
  RUN_A  cold boot, deploy, fetch 1.3B weights -> Volume, write index row.
  RUN_B  within the idle window -> warm-attach to RUN_A's live container
         (NO new image build, NO new deploy URL, wall-clock << RUN_A).
  DESTROY the app (named Volume survives), then:
  RUN_C  fresh deploy that SKIPS the weight download (weights already on
         /cache/hf); fresh boot present, download absent, boot < RUN_A.
Teardown: destroy + verify `kinoforge list` and `modal app list` clean.

Prompts are passed inline via `--prompt` (the real generate flag; there is no
`--prompt-file`). Text is taken verbatim from the §20 4-prompt matrix files
examples/configs/prompts/{field-realistic,field-dreamlike,forest}.txt so the
distinct outputs stay frame-QA-comparable across runs.
"""

import pytest

pytestmark = pytest.mark.live

_CFG = "examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml"

# Prompt text verbatim from examples/configs/prompts/field-realistic.txt
_PROMPT_A = (
    "Photorealistic, cinematic 5-second shot on anamorphic lenses with shallow "
    "depth of field and subtle lens flare. A slow push-in toward a young woman "
    "in a sweeping alpine meadow of wildflowers; behind her, a tall waterfall "
    "tumbles down moss-covered cliffs into a misting pool. Warm golden-hour "
    "light rakes across the field, backlighting her glowing silhouette."
)
# Prompt text verbatim from examples/configs/prompts/field-dreamlike.txt
_PROMPT_B = (
    "Photorealistic yet dreamlike cinematic 5-second shot, anamorphic lenses, "
    "soft diffusion blooming highlights into halos. Slow, weightless push-in "
    "toward a young woman in an enchanted, sun-drenched meadow where wildflowers "
    "glow violet, rose, and gold. Behind her, a waterfall spills down "
    "moss-draped cliffs, its mist scattering into a prismatic rainbow."
)
# Prompt text verbatim from examples/configs/prompts/forest.txt
_PROMPT_C = (
    "A dense old-growth forest at first light. Mist coils between the trunks, "
    "backlit by a low golden sun. Camera drifts slowly forward through the "
    "underbrush; ferns brush the lens; a single shaft of light pierces the "
    "canopy."
)

RUN_A_COLD = (
    "pixi run -e live-modal kinoforge generate "
    f"--config {_CFG} --mode t2v "
    f'--prompt "{_PROMPT_A}"'
)
RUN_B_WARM = (
    "pixi run -e live-modal kinoforge generate "
    f"--config {_CFG} --mode t2v "
    f'--prompt "{_PROMPT_B}"'
)
DESTROY = "pixi run kinoforge destroy --id <run-a-app-id>"
RUN_C_COLD_CACHE = (
    "pixi run -e live-modal kinoforge generate "
    f"--config {_CFG} --mode t2v "
    f'--prompt "{_PROMPT_C}" --no-reuse'
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations §26"
)
def test_modal_warm_reuse_hf_cache_contract():
    raise AssertionError(
        "run RUN_A_COLD -> RUN_B_WARM (assert warm-attach, no redeploy, "
        "faster) -> DESTROY -> RUN_C_COLD_CACHE (assert fresh boot skips the "
        "weight download); frame-QA the three distinct prompts"
    )
