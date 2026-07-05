"""Tests for finalize_upscaled_bytes."""

from kinoforge.pipeline.materialize import finalize_upscaled_bytes


def test_downscales_when_target_set() -> None:
    # Behaviour: a downscale_to triggers the downscale seam with those args.
    seen: dict[str, object] = {}

    def fake_downscale(body: bytes, target_h: int) -> bytes:
        seen["args"] = (body, target_h)
        return b"SMALL"

    out = finalize_upscaled_bytes(b"BIG", 1080, downscale=fake_downscale)
    assert out == b"SMALL"
    assert seen["args"] == (b"BIG", 1080)


def test_passthrough_when_no_target() -> None:
    # Behaviour: no downscale_to -> bytes returned untouched, seam not called.
    called = False

    def fake_downscale(body: bytes, target_h: int) -> bytes:
        nonlocal called
        called = True
        return b"X"

    assert finalize_upscaled_bytes(b"BIG", None, downscale=fake_downscale) == b"BIG"
    assert called is False
