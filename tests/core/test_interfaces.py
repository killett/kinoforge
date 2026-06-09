import math

import pytest

from kinoforge.core import errors
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    CapabilityKey,
    ModelProfile,
)


def test_capability_key_is_order_sensitive_over_loras():
    a = CapabilityKey(
        base_model="wan2.2", loras=("svi", "detail"), engine="comfyui", precision="fp16"
    )
    b = CapabilityKey(
        base_model="wan2.2", loras=("detail", "svi"), engine="comfyui", precision="fp16"
    )
    # Bug this catches: derive() that sorts/sets loras would collapse stack order.
    assert a.derive() != b.derive()


def test_capability_key_is_stable_across_instances():
    a = CapabilityKey(
        base_model="wan2.2", loras=("svi",), engine="comfyui", precision="fp16"
    )
    b = CapabilityKey(
        base_model="wan2.2", loras=("svi",), engine="comfyui", precision="fp16"
    )
    # Bug this catches: derive() keyed on id()/object identity instead of field values.
    assert a.derive() == b.derive()


def test_capability_key_distinguishes_engine_and_precision():
    assert (
        CapabilityKey(
            base_model="wan2.2", loras=("svi",), engine="comfyui", precision="fp16"
        ).derive()
        != CapabilityKey(
            base_model="wan2.2", loras=("svi",), engine="diffusers", precision="fp16"
        ).derive()
    )
    assert (
        CapabilityKey(
            base_model="wan2.2", loras=("svi",), engine="comfyui", precision="fp16"
        ).derive()
        != CapabilityKey(
            base_model="wan2.2", loras=("svi",), engine="comfyui", precision="gguf-q8"
        ).derive()
    )


def test_max_segment_seconds_is_frames_over_fps():
    p = ModelProfile(
        name="wan",
        max_frames=81,
        fps=16,
        supported_modes={"t2v"},
        max_resolution=(1280, 720),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    assert math.isclose(p.max_segment_seconds, 81 / 16)


def test_mode_role_requirements_shape() -> None:
    # Bug this catches: per-model role logic creeping in instead of one shared table.
    # Shape: dict[mode, dict[role, kind]] since Layer R.
    assert MODE_ROLE_REQUIREMENTS == {
        "t2v": {},
        "i2v": {"init_image": "image"},
        "flf2v": {"first_frame": "image", "last_frame": "image"},
    }


def test_errors_share_common_base():
    assert issubclass(errors.ProfileNotCached, errors.KinoforgeError)
    assert issubclass(errors.ConfigError, errors.KinoforgeError)


def test_capability_key_no_collision_on_control_char_in_field():
    # Bug this catches: derive() using a control char (e.g. \x1f) as separator
    # collides when a field legitimately contains that char.
    a = CapabilityKey(base_model="a\x1fb", loras=(), engine="e", precision="p")
    b = CapabilityKey(base_model="a", loras=("b",), engine="e", precision="p")
    assert a.derive() != b.derive()

    c = CapabilityKey(base_model="x", loras=("y\x1ez",), engine="e", precision="p")
    d = CapabilityKey(base_model="x", loras=("y", "z"), engine="e", precision="p")
    assert c.derive() != d.derive()


# ---------------------------------------------------------------------------
# GenerationEngine.extract_last_frame default behaviour
# ---------------------------------------------------------------------------


def test_extract_last_frame_default_raises_with_engine_name() -> None:
    """A GenerationEngine subclass that doesn't override extract_last_frame
    must raise NotImplementedError with the class name in the message.

    Bug this catches: default body forgets to include the engine class name,
    making runtime errors uninformative when a multi-segment run hits an
    engine that didn't opt in to continuity.
    """
    from kinoforge.core.interfaces import (
        Artifact,
        GenerationEngine,
    )

    class _NonOverriding(GenerationEngine):
        name: str = "non-overriding"
        requires_compute: bool = False
        requires_local_weights: bool = False

        def provision(self, instance, cfg):  # noqa: ANN001
            pass

        def backend(self, instance, cfg):  # noqa: ANN001
            raise NotImplementedError

        def profile_for(self, key):  # noqa: ANN001
            raise NotImplementedError

        def declared_flags(self, key):  # noqa: ANN001
            return {}

        def validate_spec(self, job):  # noqa: ANN001
            pass

        def model_identity(self, cfg):  # noqa: ANN001
            return ""

    eng = _NonOverriding()
    with pytest.raises(NotImplementedError, match="_NonOverriding"):
        eng.extract_last_frame(Artifact(filename="x.mp4"))


# ---------------------------------------------------------------------------
# MODE_ROLE_REQUIREMENTS drift guards
# ---------------------------------------------------------------------------

VALID_KINDS = {"image", "audio", "video"}


def test_mode_role_requirements_kinds_are_valid() -> None:
    """Drift guard: every kind in MODE_ROLE_REQUIREMENTS must be a known kind.
    Bug guard: catches typos (e.g. "imag") and accidental drift on additions."""
    for mode, roles in MODE_ROLE_REQUIREMENTS.items():
        for role, kind in roles.items():
            assert kind in VALID_KINDS, (
                f"role {role!r} in mode {mode!r} has unknown kind {kind!r}; "
                f"valid kinds: {sorted(VALID_KINDS)}"
            )
