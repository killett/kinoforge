"""Behavior: build_instance_spec maps (cfg, rendered, run context) -> InstanceSpec.

Extracted from the deploy_session closure so a spec can be built without a
deploy. These tests pin the field-for-field mapping the closure had; a
careless extraction that drops e.g. the empty-string -> None coercion on
image_build_script would make Modal re-run the heavy installs at container
start (the 2026-07-09 FlashVSR preemption failure).
"""

from __future__ import annotations

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer, RenderedProvision
from kinoforge.core.spec_builder import build_instance_spec

_CFG = "examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml"


def _rendered(**over: object) -> RenderedProvision:
    base: dict[str, object] = {
        "script": "#!/bin/bash\necho hi\nexec python -m server",
        "run_cmd": ["python", "-m", "server"],
        "image": "img:tag",
        "ports": ["8000/http"],
        "env_required": [],
        "build_script": "",
        "runtime_script": "",
    }
    base.update(over)
    return RenderedProvision(**base)  # type: ignore[arg-type]


def _offer() -> Offer:
    return Offer(
        id="NVIDIA A100 80GB PCIe",
        gpu_type="NVIDIA A100 80GB PCIe",
        vram_gb=80,
        cuda="12.4",
        cost_rate_usd_per_hr=1.64,
    )


def _build(**over: object) -> InstanceSpec:
    kwargs: dict[str, object] = {
        "cfg": load_config(_CFG),
        "rendered": _rendered(),
        "offer": _offer(),
        "engine_name": "diffusers",
        "key_hash": "abc123",
        "image": "fallback:img",
        "lifecycle": Lifecycle(),
        "env": {"HF_TOKEN": "kinoforge-prod-deadbeef"},
        "run_id": "run-1",
        "tags": None,
    }
    kwargs.update(over)
    return build_instance_spec(**kwargs)  # type: ignore[arg-type]


def test_rendered_image_wins_over_fallback():
    # Bug caught: spec falls back to the cfg image and boots the wrong
    # container when the engine pinned one (the composed-upscaler images).
    assert _build().image == "img:tag"


def test_empty_rendered_image_falls_back():
    assert _build(rendered=_rendered(image="")).image == "fallback:img"


def test_engine_and_key_tags_are_always_present_and_caller_tags_win():
    # Bug caught: caller tags clobbered by the defaults, or vice versa, which
    # breaks warm-reuse matching (kinoforge_key) and the ephemeral index.
    spec = _build(tags={"kinoforge_key": "override", "extra": "x"})
    assert spec.tags["kinoforge_engine"] == "diffusers"
    assert spec.tags["kinoforge_key"] == "override"
    assert spec.tags["extra"] == "x"


def test_empty_build_script_becomes_none_not_empty_string():
    # Bug caught: "" is falsy but not None; Modal's
    # `spec.runtime_provision_script or spec.provision_script` fallback works
    # either way, but the image-bake branch checks `is not None`.
    spec = _build()
    assert spec.image_build_script is None
    assert spec.runtime_provision_script is None


def test_empty_provision_script_becomes_none_not_empty_string():
    # Bug caught: the bare deploy() call site has no real rendered script and
    # passes RenderedProvision(script="", ...) to stand in for "no script" —
    # RenderedProvision.script is a required `str`, not `str | None`, so ""
    # is the only way to express that. Without this coercion spec
    # .provision_script would be "" rather than None, and RunPodProvider
    # ._encode_provision_script branches on `is not None`: an empty string
    # would wrongly enter the base64/gzip wrap-and-run branch instead of
    # leaving dockerArgs == "" like the pre-extraction closure produced.
    spec = _build(rendered=_rendered(script=""))
    assert spec.provision_script is None


def test_nonempty_provision_script_is_carried_through_unchanged():
    spec = _build(rendered=_rendered(script="#!/bin/bash\nexec real-cmd"))
    assert spec.provision_script == "#!/bin/bash\nexec real-cmd"


def test_split_scripts_are_carried_when_the_engine_emits_them():
    spec = _build(
        rendered=_rendered(build_script="pip install x", runtime_script="exec s")
    )
    assert spec.image_build_script == "pip install x"
    assert spec.runtime_provision_script == "exec s"


def test_ports_come_from_rendered_as_a_tuple():
    assert _build().ports == ("8000/http",)


def test_diagnostic_mode_off_means_no_diagnostic_env_and_restart_always():
    spec = _build()
    assert spec.diagnostic_env == {}
    assert spec.restart_policy == "always"


def test_diagnostic_mode_on_sets_restart_never_and_populates_diagnostic_env():
    # Bug caught: a diagnostic run whose pod RunPod auto-restarts obliterates
    # the snapshot the trap is uploading (C28 A3).
    #
    # Design: the caller computes the diagnostic overlay (_build_diagnostic_env
    # stays in orchestrator.py) and passes it in; build_instance_spec only
    # decides whether to honor it, gated on cfg.diagnostic_mode. So this test
    # must supply the overlay explicitly — a diagnostic_mode=True cfg alone
    # does not conjure one from nothing.
    cfg = load_config(_CFG)
    cfg.diagnostic_mode = True
    spec = _build(cfg=cfg, diagnostic_env={"KINOFORGE_DIAGNOSTIC_RUN_ID": "run-1"})
    assert spec.restart_policy == "never"
    assert spec.diagnostic_env != {}


def test_cloud_type_defaults_to_any_when_cfg_has_no_compute_block():
    cfg = load_config(_CFG)
    cfg.compute = None
    assert _build(cfg=cfg).cloud_type == "any"
