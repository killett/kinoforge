"""CapabilityKey + WarmAttachKey stages/upscaler factors with backward-compat hash."""

from __future__ import annotations

from kinoforge.core.interfaces import CapabilityKey, WarmAttachKey

# Frozen outputs from the pre-stages-factor implementation. If these values
# change, EVERY warm-pod ledger entry written before the change becomes
# unmatchable. These tests are the mandatory pre-merge gate.
_LEGACY_GOLDEN_HASH = "8d260dfd5158c43961b13a077f7668e218f2c61c249ae6a512e45d72f0b8241b"
_LEGACY_GOLDEN_HASH_X = (
    "03884c43e63c3b956c11bce6b415ecfc2be860e7dad23ed776d7d29b091f0eeb"
)
_WAK_LEGACY_GOLDEN_HASH = (
    "14843fd0e7ad7c6b204be54d6f8aad0bb2ae961a3d313b481c5b4cde16a4d16f"
)


class TestBackwardCompatHash:
    def test_legacy_shape_matches_golden(self) -> None:
        k = CapabilityKey(
            base_model="hf:org/m",
            loras=("hf:org/lora1",),
            engine="diffusers",
            precision="fp8",
        )
        assert k.derive() == _LEGACY_GOLDEN_HASH

    def test_default_stages_factor_does_not_change_hash(self) -> None:
        legacy = CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8")
        explicit_empty = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=(),
            upscaler="",
            upscaler_precision="",
        )
        assert legacy.derive() == explicit_empty.derive()
        assert legacy.derive() == _LEGACY_GOLDEN_HASH_X


class TestNewFactorsChangeHash:
    def test_non_default_stages_differs(self) -> None:
        legacy = CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8")
        with_stages = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        assert legacy.derive() != with_stages.derive()

    def test_stage_order_matters(self) -> None:
        a = CapabilityKey(
            base_model="hf:x",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        b = CapabilityKey(
            base_model="hf:x",
            stages=("upscale", "t2v"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        assert a.derive() != b.derive()


class TestDeterminism:
    def test_repeated_derive_is_stable(self) -> None:
        k = CapabilityKey(
            base_model="hf:x",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        outputs = {k.derive() for _ in range(5)}
        assert len(outputs) == 1


class TestWarmAttachKeyShape:
    def test_warm_attach_key_unchanged_signature(self) -> None:
        k = CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8")
        wak = k.warm_attach_key()
        assert wak.base_model == "hf:x"
        assert wak.engine == "diffusers"
        assert wak.precision == "fp8"


class TestWarmAttachKeyBackwardCompat:
    def test_legacy_shape_matches_golden(self) -> None:
        k = WarmAttachKey(base_model="hf:x", engine="diffusers", precision="fp8")
        assert k.derive() == _WAK_LEGACY_GOLDEN_HASH

    def test_default_factors_match_legacy(self) -> None:
        legacy = WarmAttachKey(base_model="hf:x", engine="diffusers", precision="fp8")
        explicit_empty = WarmAttachKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=(),
            upscaler="",
            upscaler_precision="",
        )
        assert legacy.derive() == explicit_empty.derive()

    def test_non_default_differs(self) -> None:
        legacy = WarmAttachKey(base_model="hf:x", engine="diffusers", precision="fp8")
        extended = WarmAttachKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("upscale",),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        assert legacy.derive() != extended.derive()


class TestCapabilityKeyToWarmAttachKey:
    def test_propagates_new_factors(self) -> None:
        ck = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        wak = ck.warm_attach_key()
        assert wak.stages == ("t2v", "upscale")
        assert wak.upscaler == "seedvr2"
        assert wak.upscaler_precision == "3b-fp8"
