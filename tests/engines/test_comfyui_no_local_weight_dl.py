"""Regression — :class:`ComfyUIEngine` must default ``requires_local_weights``
to ``False`` so :func:`kinoforge.core.provisioner.provision` does not pre-
download ~20-30 GB of weights through the controller container.

ComfyUI's Layer Q :meth:`render_provision` already emits a pod-side curl
bootstrap that fetches the same weights on the pod itself, so the local
download is pure waste — it doubles wall-clock cost and risks OOM on
lightweight containers (see ``tools/capture_object_info.py:55`` for the
prior incident report).

This test locks the class default so a future revert cannot silently
re-introduce the bug.
"""

from __future__ import annotations

from kinoforge.engines.comfyui import ComfyUIEngine


def test_comfyui_engine_default_skips_local_weight_dl() -> None:
    """Class-attribute default must be ``False``.

    Checks the class attribute directly (not an instance attribute) so a
    future subclass + instance attribute that happens to be ``False``
    cannot pass the test while the class default has regressed back to
    ``True``.
    """
    assert ComfyUIEngine.requires_local_weights is False, (
        "ComfyUIEngine.requires_local_weights defaulted back to True — "
        "every CLI invocation will pull ~24 GB of Wan weights through "
        "the controller before delegating to engine.provision(). See "
        "PROGRESS.md B20 + tools/capture_object_info.py:55 for context."
    )


def test_comfyui_engine_instance_inherits_default() -> None:
    """An instance with no explicit override must also be ``False``.

    Catches the bug where someone leaves the class attribute alone but
    adds an ``__init__`` line that flips the instance attribute back.
    """
    from kinoforge.core.interfaces import ModelProfile

    probe = ModelProfile(
        name="comfyui",
        max_frames=81,
        fps=16,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    engine = ComfyUIEngine(probe_profile=probe)
    assert engine.requires_local_weights is False
