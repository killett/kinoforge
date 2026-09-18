"""Behavior: a diffusers config declares its own capability profile.

``DiffusersEngine._DEFAULT_PROBE`` is a module constant shared by every diffusers
config, declaring ``supported_modes={"t2v"}``. MiniMax-H3 needs ``t2va``.

Widening the constant would change what EVERY diffusers config reports, and
``JsonProfileCache.verify`` compares ``supported_modes`` against the live probe and
raises ``CapabilityMismatch`` — which the orchestrator handles by destroying the
instance. This workspace's ``.kinoforge/_profiles/`` already holds 10 cached
``diffusers`` profiles saying ``["t2v"]``, so the global widening is a teardown on
the next warm Wan run, after the boot has been paid for. Hence: per-config
declaration, with the second test below as the no-regression guard.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import _DEFAULT_PROBE, DiffusersEngine

_H3_CAPABILITY = {
    "supported_modes": ["t2va"],
    "max_frames": 360,
    "fps": 24,
    "max_resolution": [1344, 768],
    "supports_joint_audio": True,
}


def _cfg(capability: dict | None) -> dict:  # type: ignore[type-arg]
    """Return a minimal cfg dict with an optional capability block.

    Args:
        capability: The ``engine.diffusers.capability`` block, or ``None`` to omit
            it entirely.

    Returns:
        A cfg dict shaped like ``Config.model_dump()`` output.
    """
    diffusers_block: dict = {"base_url": "http://localhost:8000"}  # type: ignore[type-arg]
    if capability is not None:
        diffusers_block["capability"] = capability
    return {"engine": {"kind": "diffusers", "diffusers": diffusers_block}}


def test_declared_capability_reaches_the_backend_profile() -> None:
    """The cfg block, not the module constant, is what the backend reports.

    Bug caught: the capability block is declared in the config model and threaded
    no further, so ``validate_request`` rejects ``mode='t2va'`` with "not in
    supported_modes" — and it does so AFTER the pod is booked, because the profile
    is probed from the live backend.
    """
    backend = DiffusersEngine().backend(None, _cfg(_H3_CAPABILITY))
    profile = backend.inspect_capabilities()
    assert profile.supported_modes == {"t2va"}
    assert profile.supports_joint_audio is True
    assert profile.fps == 24
    assert profile.max_frames == 360
    assert profile.max_resolution == (1344, 768)
    # capabilities() and inspect_capabilities() must agree — the cache reads one
    # and the mode gate reads the other.
    assert backend.capabilities() == profile


def test_a_cfg_without_a_capability_block_reports_the_default_probe() -> None:
    """Wan configs are untouched — the no-regression guard.

    Bug caught: t2va is delivered by widening ``_DEFAULT_PROBE.supported_modes`` to
    ``{"t2v", "t2va"}``. Every cached Wan profile then mismatches the live probe on
    the next warm run, ``JsonProfileCache.verify`` raises ``CapabilityMismatch``,
    and the orchestrator destroys a pod it just paid ~25 minutes of boot for.
    """
    profile = DiffusersEngine().backend(None, _cfg(None)).inspect_capabilities()
    assert profile.supported_modes == {"t2v"}
    assert profile.supports_joint_audio is False
    assert profile == _DEFAULT_PROBE


def test_present_but_empty_capability_block_reports_the_default_probe() -> None:
    """A cfg that omits the block dumps it as present-and-None (U33).

    Bug caught: the merge reads ``diffusers_cfg.get("capability", {})``, which
    returns ``None`` rather than the default for a key pydantic emitted as
    ``None`` — and ``None`` then explodes or, worse, is treated as a declaration
    that zeroes every field.
    """
    empty: dict | None  # type: ignore[type-arg]
    for empty in (None, {}):
        cfg = {"engine": {"kind": "diffusers", "diffusers": {"capability": empty}}}
        profile = DiffusersEngine().backend(None, cfg).inspect_capabilities()
        assert profile == _DEFAULT_PROBE, f"capability={empty!r} changed the profile"


def test_partial_capability_block_keeps_the_default_for_absent_fields() -> None:
    """Declaring only the modes leaves the rest of the probe alone.

    Bug caught: the merge is written as a wholesale replacement, so a config
    declaring ``supported_modes`` alone silently zeroes ``fps`` / ``max_frames``
    and ``ModelProfile.max_segment_seconds`` divides by zero.
    """
    profile = (
        DiffusersEngine()
        .backend(None, _cfg({"supported_modes": ["t2va"]}))
        .inspect_capabilities()
    )
    assert profile.supported_modes == {"t2va"}
    assert profile.fps == _DEFAULT_PROBE.fps
    assert profile.max_frames == _DEFAULT_PROBE.max_frames
    assert profile.max_resolution == _DEFAULT_PROBE.max_resolution
    assert profile.supports_joint_audio is _DEFAULT_PROBE.supports_joint_audio


def test_declaring_a_capability_does_not_mutate_the_shared_default() -> None:
    """The override returns a new profile instead of editing the constant.

    Bug caught: ``_DEFAULT_PROBE.supported_modes.add("t2va")`` (or any in-place
    edit) makes the H3 config leak into every diffusers config in the same
    process — batch runs and the golden harness both build several backends in one
    interpreter.
    """
    DiffusersEngine().backend(None, _cfg(_H3_CAPABILITY))
    assert _DEFAULT_PROBE.supported_modes == {"t2v"}
    assert _DEFAULT_PROBE.supports_joint_audio is False
    later = DiffusersEngine().backend(None, _cfg(None)).inspect_capabilities()
    assert later.supported_modes == {"t2v"}


def test_capability_block_survives_load_config() -> None:
    """The YAML key is not silently dropped by pydantic.

    Bug caught: ``DiffusersEngineConfig`` carries no ``model_config``, so
    pydantic's default ``extra="ignore"`` drops any key the model does not
    declare. A capability block added to YAML but not to the model vanishes
    between ``load_config`` and the engine, and the whole feature is a no-op no
    offline test would notice.
    """
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/modal-diffusers-minimax-h3-t2va.yaml")
    assert cfg.engine.diffusers is not None
    capability = cfg.engine.diffusers.capability
    assert capability is not None
    assert capability.supported_modes == ["t2va"]
    assert capability.supports_joint_audio is True
    assert capability.fps == 24


def test_unknown_capability_key_is_refused() -> None:
    """A typo in the capability block fails loudly at load.

    Bug caught: ``suported_modes`` (one 'p') is ignored, the profile keeps
    ``{"t2v"}``, and the failure surfaces as "mode 't2va' not in supported_modes"
    on a booked pod rather than as a config error at $0.
    """
    from kinoforge.core.config import DiffusersCapabilityConfig

    with pytest.raises(PydanticValidationError):
        DiffusersCapabilityConfig(suported_modes=["t2va"])  # type: ignore[call-arg]


def test_remote_instance_does_not_lose_the_declared_capability() -> None:
    """The remote branch of ``backend()`` keeps the profile override.

    Bug caught: the override is applied on the local branch only, so it works in
    every unit test and is absent on the one path that runs live.
    """
    instance = Instance(
        id="pod-1",
        provider="modal",
        status="running",
        created_at=0.0,
        endpoints={"8000": "https://example.modal.run"},
    )
    profile = (
        DiffusersEngine().backend(instance, _cfg(_H3_CAPABILITY)).inspect_capabilities()
    )
    assert profile.supported_modes == {"t2va"}
