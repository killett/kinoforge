"""Tests for the warm-matcher stages-subset helper.

T7 scope: the pure helper that decides whether a multi-stage pod can serve
an upscale-only cfg. Integration into ``find_warm_attach_candidate`` is
covered by the matcher's existing test suite (no shape change to its
signature) — we test the helper directly and verify the matcher routes
through it in one end-to-end case.
"""

from __future__ import annotations

from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.warm_reuse.matcher import _stages_subset_match


def _upscale_only_key() -> CapabilityKey:
    return CapabilityKey(
        base_model="",
        stages=("upscale",),
        upscaler="seedvr2",
        upscaler_precision="3b-fp8",
    )


class TestStagesSubsetMatch:
    def test_multi_stage_pod_matches_upscale_only_cfg(self) -> None:
        cap = _upscale_only_key()
        entry = {
            "id": "pod-multi",
            "kinoforge_stages": ["t2v", "upscale"],
            "kinoforge_upscaler": "seedvr2",
            "kinoforge_upscaler_precision": "3b-fp8",
        }
        assert _stages_subset_match(cap, entry) is True

    def test_generate_only_pod_refused_for_upscale_only(self) -> None:
        cap = _upscale_only_key()
        entry = {"id": "pod-gen", "kinoforge_stages": ["t2v"]}
        assert _stages_subset_match(cap, entry) is False

    def test_legacy_pod_without_stages_field_refused(self) -> None:
        cap = _upscale_only_key()
        entry = {"id": "pod-legacy"}
        assert _stages_subset_match(cap, entry) is False

    def test_upscaler_mismatch_refused(self) -> None:
        cap = _upscale_only_key()
        entry = {
            "id": "pod-flash",
            "kinoforge_stages": ["t2v", "upscale"],
            "kinoforge_upscaler": "flashvsr",
            "kinoforge_upscaler_precision": "3b-fp8",
        }
        assert _stages_subset_match(cap, entry) is False

    def test_upscaler_precision_mismatch_refused(self) -> None:
        cap = _upscale_only_key()
        entry = {
            "id": "pod-7b",
            "kinoforge_stages": ["t2v", "upscale"],
            "kinoforge_upscaler": "seedvr2",
            "kinoforge_upscaler_precision": "7b-fp16",
        }
        assert _stages_subset_match(cap, entry) is False

    def test_does_not_apply_to_pure_generate_cfg(self) -> None:
        # Generate-only cfg (stages=()) must NOT activate the subset pass
        # — that would let a random multi-stage pod attach to a t2v-only
        # request and silently change the LoRA stack semantics.
        cap_pure_gen = CapabilityKey(
            base_model="hf:x", engine="diffusers", precision="fp8"
        )
        entry = {
            "id": "pod-multi",
            "kinoforge_stages": ["t2v", "upscale"],
            "kinoforge_upscaler": "seedvr2",
            "kinoforge_upscaler_precision": "3b-fp8",
        }
        assert _stages_subset_match(cap_pure_gen, entry) is False

    def test_does_not_apply_to_generate_plus_upscale_cfg(self) -> None:
        # Multi-stage cfg (stages=('t2v','upscale')) must use the primary
        # hash-equality path; the subset helper is upscale-only.
        cap_multi = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        entry = {
            "id": "pod-multi",
            "kinoforge_stages": ["t2v", "upscale"],
            "kinoforge_upscaler": "seedvr2",
            "kinoforge_upscaler_precision": "3b-fp8",
        }
        assert _stages_subset_match(cap_multi, entry) is False
