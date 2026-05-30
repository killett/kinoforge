"""Tests for the pydantic config loader."""

import pytest

from kinoforge.core.config import load_config, parse_duration
from kinoforge.core.errors import ConfigError

HOSTED = """
engine:
  kind: hosted
  precision: ""
  hosted: {provider: fal, endpoint: "x", model: ltx-2}
lifecycle: {budget: 25.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
"""

WAN = """
engine:
  kind: comfyui
  precision: fp16
  comfyui: {version: v0.3.40}
models:
  - {ref: "hf:Wan-AI/Wan2.2-T2V-A14B", kind: base, target: diffusion_models}
  - {ref: "civitai:1234@5678", kind: lora, target: loras}
  - {ref: "https://e/x.vae", kind: vae, target: vae, sha256: abc}
compute:
  provider: runpod
  image: "img:tag"
  mode: pod
  requirements: {gpu_preference: ["RTX 4090"]}
  lifecycle: {idle_timeout: 2h, job_timeout: 30m, max_lifetime: 5h, budget: 25.0}
"""


def test_parse_duration_units():
    assert parse_duration("2h") == 2 * 3600
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("90s") == 90


def test_bare_int_duration_rejected():
    # Bug this catches: silently treating "120" as 120 seconds — easy to mis-author.
    with pytest.raises(ConfigError):
        parse_duration("120")


def test_idle_ge_lifetime_rejected():
    bad = WAN.replace("idle_timeout: 2h", "idle_timeout: 6h")
    with pytest.raises(ConfigError, match="idle_timeout"):
        load_config(bad)


def test_job_gt_lifetime_rejected():
    bad = WAN.replace("job_timeout: 30m", "job_timeout: 6h")
    with pytest.raises(ConfigError, match="job_timeout"):
        load_config(bad)


def test_compute_present_for_hosted_rejected():
    bad = HOSTED + "compute: {provider: runpod, image: x, lifecycle: {budget: 1.0}}\n"
    with pytest.raises(ConfigError, match="hosted"):
        load_config(bad)


def test_inconsistent_kind_target_rejected():
    # Bug this catches: routing a base-model file into the loras dir would break the engine.
    bad = WAN.replace(
        '{ref: "hf:Wan-AI/Wan2.2-T2V-A14B", kind: base, target: diffusion_models}',
        '{ref: "hf:Wan-AI/Wan2.2-T2V-A14B", kind: base, target: loras}',
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_unknown_engine_kind_rejected():
    with pytest.raises(ConfigError, match="engine"):
        load_config(WAN.replace("kind: comfyui", "kind: bogus"))


def test_budget_required():
    with pytest.raises(ConfigError, match="budget"):
        load_config(WAN.replace(", budget: 25.0", ""))


def test_capability_key_derivation_orders_loras_and_excludes_vae():
    cfg = load_config(WAN)
    key = cfg.capability_key()
    # Bug this catches: including VAE in the key (VAE doesn't change generation capability).
    assert key.base_model == "hf:Wan-AI/Wan2.2-T2V-A14B"
    assert key.loras == ("civitai:1234@5678",)
    assert key.engine == "comfyui"
    assert key.precision == "fp16"


def test_lifecycle_defaults_applied():
    cfg = load_config(HOSTED)
    lc = cfg.lifecycle()
    assert lc.idle_timeout_s == 2 * 3600
    assert lc.job_timeout_s == 30 * 60
    assert lc.time_buffer_s == 30 * 60
    assert lc.max_lifetime_s == 5 * 3600


def test_hardware_requirements_defaults_applied():
    cfg = load_config(WAN)
    reqs = cfg.hardware_requirements()
    # Bug this catches: dropping defaults when user only set gpu_preference.
    assert reqs.min_vram_gb == 48
    assert reqs.min_cuda == "12.8"
    assert reqs.max_cost_rate_usd_per_hr == 2.20
    assert reqs.disk_gb == 100
    assert reqs.gpu_preference == ("RTX 4090",)


def test_zero_base_models_rejected():
    # Bug this catches: capability_key() raising late instead of load_config rejecting
    # at parse time; user wouldn't see the problem until they tried to derive a key.
    bad = WAN.replace(
        '  - {ref: "hf:Wan-AI/Wan2.2-T2V-A14B", kind: base, target: diffusion_models}\n',
        "",
    )
    with pytest.raises(ConfigError, match="base"):
        load_config(bad)


def test_multiple_base_models_rejected():
    # Bug this catches: silently using the LAST base entry — the CapabilityKey is
    # then a function of declaration order in a way the user can't see.
    bad = WAN.replace(
        '  - {ref: "civitai:1234@5678", kind: lora, target: loras}',
        '  - {ref: "hf:other/base", kind: base, target: diffusion_models}',
    )
    with pytest.raises(ConfigError, match="base"):
        load_config(bad)


def test_splitter_defaults_to_heuristic_when_block_absent():
    # Bug: pydantic default missing or wrong key; every config that omits
    # the optional splitter: block would blow up at generate() with an
    # AttributeError instead of resolving the heuristic default.
    cfg = load_config(WAN)
    assert cfg.splitter.kind == "heuristic"


def test_splitter_explicit_heuristic_kind_parses():
    # Bug: schema rejects the explicit-default form, forcing users to omit
    # the block to avoid validation errors.
    yaml_with_block = WAN + "\nsplitter:\n  kind: heuristic\n"
    cfg = load_config(yaml_with_block)
    assert cfg.splitter.kind == "heuristic"


def test_splitter_unknown_kind_parses_at_load_time():
    # Bug: Config validation couples the schema to global registry state,
    # so import order or test isolation flakes the loader. The unknown-kind
    # error must surface at generate() time via registry.get_splitter, not
    # at config load — matches today's engine/provider behaviour.
    yaml_with_block = WAN + "\nsplitter:\n  kind: bespoke_xyz\n"
    cfg = load_config(yaml_with_block)
    assert cfg.splitter.kind == "bespoke_xyz"
