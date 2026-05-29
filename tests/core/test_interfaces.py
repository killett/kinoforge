import math

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


def test_mode_role_requirements_table_is_authoritative():
    # Bug this catches: per-model role logic creeping in instead of one shared table.
    assert MODE_ROLE_REQUIREMENTS == {
        "t2v": set(),
        "i2v": {"init_image"},
        "flf2v": {"first_frame", "last_frame"},
    }


def test_errors_share_common_base():
    assert issubclass(errors.ProfileNotCached, errors.KinoforgeError)
    assert issubclass(errors.ConfigError, errors.KinoforgeError)
