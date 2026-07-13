"""LIVE EM2: bare --ephemeral cross-CLI warm-attach on Modal (Wan 2.1 1.3B/A10).
Driven manually via the CLI; this file records the contract."""

import pytest

pytestmark = pytest.mark.live

GEN_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral generate "
    "--config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)"'
)
# Run GEN_CMD twice as SEPARATE processes. Expect run 2 to warm-attach run 1's
# kinoforge-eph-{8hex} app via the ephemeral index (no new deploy).


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS EM2 entry")
def test_modal_ephemeral_em2_contract():
    raise AssertionError(
        "run GEN_CMD twice; assert run2 warm-attaches run1's eph app "
        "(no deploy, faster), then destroy + verify stopped + index row gone"
    )
