"""Luma image-keyframe live smoke — first `(luma, t2i)` capability tuple.

Env-gated on KINOFORGE_LIVE_SPEND (same contract as the flashvsr live
module) AND marked `live` so plain `pixi run test` deselects it. Spend:
one image generation (~cents) from the $20 Luma platform credit.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_LIVE_SPEND_ENV = "KINOFORGE_LIVE_SPEND"

_STANDARD_PROMPT_PATH = Path("/workspace/examples/configs/prompts/field-realistic.txt")

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG"


def _require_live_spend_env() -> None:
    if os.environ.get(_LIVE_SPEND_ENV) != "1":
        pytest.skip(f"live-spend gate: set {_LIVE_SPEND_ENV}=1 to spend Luma credit")


def test_luma_keyframe_generation(tmp_path: Path) -> None:
    """Generate one image; assert real image bytes; delete the record.

    Bug caught: request-shape drift against the live API (the offline
    suite can only pin OUR side of the wire), and the uni-1.1 model-id
    question the docs left open (design doc §2 — probe uni-1.1, fall
    back to photon-1; whichever passes gets pinned in the example cfg).
    """
    _require_live_spend_env()
    import kinoforge._adapters  # noqa: F401 — registry side-effects
    from kinoforge.core import registry
    from kinoforge.core.errors import KinoforgeError
    from kinoforge.core.interfaces import ImageJob
    from kinoforge.image_engines.luma import LumaImageBackend

    engine = registry.get_image_engine("luma")()
    engine.provision(None, {})
    backend = engine.backend(None, {})
    assert isinstance(backend, LumaImageBackend)

    prompt = _STANDARD_PROMPT_PATH.read_text().strip()
    art = None
    model_used = None
    job_id = ""
    for model in ("uni-1.1", "photon-1"):
        job = ImageJob(
            spec={"model": model, "params": {"aspect_ratio": "16:9"}},
            prompt=prompt,
        )
        engine.validate_spec(job)
        try:
            job_id = backend.submit(job)
            art = backend.result(job_id)
            model_used = model
            break
        except KinoforgeError as exc:
            # Model-id rejection (400 naming the model field) -> try next.
            if "model" in str(exc).lower():
                continue
            raise
    assert art is not None, "both model ids rejected — API surface changed"

    out = tmp_path / "keyframe.img"
    with urllib.request.urlopen(art.url, timeout=120) as resp:  # noqa: S310
        out.write_bytes(resp.read())
    data = out.read_bytes()
    assert len(data) > 10_000, f"suspiciously small image ({len(data)} B)"
    assert data[:4] == _PNG_MAGIC or data[:3] == _JPEG_MAGIC, (
        f"not PNG/JPEG magic: {data[:8]!r}"
    )
    # Record which model id the live API accepted — close-out pins it in cfg.
    print(f"MODEL_USED={model_used} BYTES={len(data)} URL={art.url}")

    backend._inner._delete(job_id)
