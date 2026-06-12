"""Tests for the pydantic config loader."""

import json
import textwrap
from pathlib import Path

import pytest

from kinoforge.core.config import OutputConfig, load_config, parse_duration
from kinoforge.core.errors import ConfigError

MINIMAL_FAKE_ENGINE_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
"""

HOSTED = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: fal
    endpoint: "https://fal.run/x"
    api_key_env: FAL_KEY
lifecycle: {budget: 25.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
spec:
  model: ltx-2
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
    # Layer G: max_in_flight defaults to 1 (sequential behaviour).
    assert lc.max_in_flight == 1


def test_lifecycle_max_in_flight_honoured_from_yaml():
    """YAML max_in_flight value must reach the runtime Lifecycle dataclass.

    Bug this catches: LifecycleConfig declaring the field but lifecycle()
    not propagating it — would silently default to 1 and break Layer G's
    ConcurrentPool fan-out wiring in the orchestrator.
    """
    cfg = load_config(HOSTED.replace("budget: 25.0", "budget: 25.0, max_in_flight: 4"))
    lc = cfg.lifecycle()
    assert lc.max_in_flight == 4


# ---------------------------------------------------------------------------
# Layer U T4 — heartbeat_interval_s
# ---------------------------------------------------------------------------


def test_lifecycle_heartbeat_interval_s_default_is_none():
    """Default is None — feature disabled, backwards-compat for every existing config.

    Bug this catches: a refactor that defaults the field to a positive
    float would silently enable the background HeartbeatLoop for every
    user's deploy_session, adding a thread + lock writes nobody asked for.
    """
    cfg = load_config(HOSTED)
    lc = cfg.lifecycle()
    assert lc.heartbeat_interval_s is None


def test_lifecycle_heartbeat_interval_s_accepts_positive_float():
    """A positive float in YAML round-trips into Lifecycle.heartbeat_interval_s.

    Bug this catches: LifecycleConfig declaring the field but lifecycle()
    not propagating it — would silently default to None and break Layer U's
    deploy_session HeartbeatLoop spawn (T3 gate).
    """
    cfg = load_config(
        HOSTED.replace("budget: 25.0", "budget: 25.0, heartbeat_interval_s: 30")
    )
    lc = cfg.lifecycle()
    assert lc.heartbeat_interval_s == 30.0


def test_lifecycle_heartbeat_interval_s_rejects_negative():
    """Negative heartbeat_interval_s raises at load time.

    Bug this catches: a missing validator would let a negative interval
    reach HeartbeatLoop.__init__, which raises ValueError — but only
    after the orchestrator has already created the instance, leaving the
    pod orphaned. Reject at config-load.
    """
    with pytest.raises(ConfigError, match="heartbeat_interval_s"):
        load_config(
            HOSTED.replace("budget: 25.0", "budget: 25.0, heartbeat_interval_s: -1")
        )


def test_lifecycle_heartbeat_interval_s_rejects_zero():
    """Zero heartbeat_interval_s raises at load time (same rationale as negative)."""
    with pytest.raises(ConfigError, match="heartbeat_interval_s"):
        load_config(
            HOSTED.replace("budget: 25.0", "budget: 25.0, heartbeat_interval_s: 0")
        )


def test_hardware_requirements_defaults_applied():
    cfg = load_config(WAN)
    reqs = cfg.hardware_requirements()
    # Bug this catches: dropping defaults when user only set gpu_preference.
    assert reqs.min_vram_gb == 48
    assert reqs.min_cuda == "12.8"
    assert reqs.max_usd_per_hr == 2.20
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


# ---------------------------------------------------------------------------
# StoreConfig — Phase 13 / Layer C
# ---------------------------------------------------------------------------


def test_default_store_is_local_kind() -> None:
    """When no store block is present, Config.store defaults to kind='local'.

    Bug this catches: default_factory not wired, or default kind != 'local' —
    breaking backwards compat for every pre-Layer-C config file.
    """
    from kinoforge.core.config import load_config

    cfg = load_config(WAN)
    assert cfg.store.kind == "local"
    assert cfg.store.root is None
    assert cfg.store.bucket is None
    assert cfg.store.prefix == ""


def test_s3_kind_requires_bucket() -> None:
    """store.kind='s3' without store.bucket raises pydantic ValidationError.

    Bug this catches: validator silently accepts incomplete config, leading
    to runtime failure deep inside generate() instead of upfront load error.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import StoreConfig

    with pytest.raises(ValidationError, match="bucket"):
        StoreConfig(kind="s3")


def test_gcs_kind_requires_bucket() -> None:
    """store.kind='gcs' without store.bucket raises pydantic ValidationError.

    Bug this catches: validator handles only the s3 case.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import StoreConfig

    with pytest.raises(ValidationError, match="bucket"):
        StoreConfig(kind="gcs")


def test_local_kind_rejects_bucket() -> None:
    """store.kind='local' with store.bucket set raises pydantic ValidationError.

    Bug this catches: validator only guards one direction — users who mistype
    kind='local' but include bucket get a silently misconfigured store.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import StoreConfig

    with pytest.raises(ValidationError, match="local"):
        StoreConfig(kind="local", bucket="should-not-be-here")


def test_prefix_defaults_to_empty_string() -> None:
    """store.prefix defaults to '' when absent (not None, not 'default').

    Bug this catches: prefix typed as Optional with None default, breaking
    string-concat in store._key.
    """
    from kinoforge.core.config import StoreConfig

    cfg = StoreConfig(kind="s3", bucket="b")
    assert cfg.prefix == ""


def test_parses_full_s3_block_from_yaml() -> None:
    """A full store block round-trips through load_config.

    Bug this catches: pydantic discriminator gets stuck on kind='s3' or the
    StoreConfig field isn't merged into Config correctly.
    """
    from kinoforge.core.config import load_config

    cfg_yaml = WAN + (
        "\nstore:\n  kind: s3\n  bucket: my-org-kinoforge\n  prefix: prod/runs\n"
    )
    cfg = load_config(cfg_yaml)
    assert cfg.store.kind == "s3"
    assert cfg.store.bucket == "my-org-kinoforge"
    assert cfg.store.prefix == "prod/runs"


# ---------------------------------------------------------------------------
# Hosted/Diffusers engine config — Layer E url_path + Layer F asset_paths
# round-trip through model_dump (the orchestrator hands cfg.model_dump() to
# engine.backend(); silent drop = broken end-to-end).
# ---------------------------------------------------------------------------


def test_hosted_cfg_asset_paths_round_trips_through_model_dump() -> None:
    """YAML asset_paths under engine.hosted survives model_validate -> model_dump.

    Bug catch: pydantic v2 defaults to extra='ignore', silently dropping any
    YAML field not declared on HostedEngineConfig. The orchestrator calls
    cfg.model_dump() before passing the dict into engine.backend(); a dropped
    asset_paths key means HostedAPIBackend._asset_paths is empty, and every
    image-to-video / asset-driven hosted job breaks end-to-end.
    """
    from kinoforge.core.config import Config

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: fal
    endpoint: "https://fal.run/x"
    api_key_env: FAL_KEY
    asset_paths:
      init_image: input.image_url
lifecycle: {budget: 5.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
spec:
  model: "vendor/m"
"""
    import yaml

    cfg = Config.model_validate(yaml.safe_load(yaml_text))
    dumped = cfg.model_dump()
    # Bug catch: extra='ignore' drops asset_paths; assert it survives.
    assert dumped["engine"]["hosted"]["asset_paths"] == {
        "init_image": "input.image_url"
    }


def test_hosted_cfg_url_path_round_trips_through_model_dump() -> None:
    """YAML url_path under engine.hosted survives model_validate -> model_dump.

    Bug catch: this is the Layer E equivalent of the asset_paths defect —
    same root cause (undeclared field on HostedEngineConfig + pydantic's
    default extra='ignore'). Without this, HostedAPIBackend.result() can't
    walk the configured dot-path and the artifact URL is always empty.
    """
    from kinoforge.core.config import Config

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: fal
    endpoint: "https://fal.run/x"
    api_key_env: FAL_KEY
    url_path: video.url
lifecycle: {budget: 5.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
spec:
  model: "vendor/m"
"""
    import yaml

    cfg = Config.model_validate(yaml.safe_load(yaml_text))
    dumped = cfg.model_dump()
    assert dumped["engine"]["hosted"]["url_path"] == "video.url"


def test_hosted_cfg_asset_paths_defaults_empty() -> None:
    """HostedEngineConfig.asset_paths defaults to {} when YAML omits it.

    Bug catch: a `None` default would crash engine.backend()'s ``isinstance``
    check (treating None as 'no paths') asymmetrically; an empty dict is the
    contract every hosted backend constructor expects.
    """
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(provider="fal", endpoint="https://e", api_key_env="K")
    assert cfg.asset_paths == {}


def test_hosted_cfg_url_path_defaults_empty() -> None:
    """HostedEngineConfig.url_path defaults to '' when YAML omits it.

    Bug catch: a None default would propagate as the string "None" through
    str(hosted_cfg.get("url_path", "")) in the engine; "" is the canonical
    'walk-no-path' sentinel that result() already checks for.
    """
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(provider="fal", endpoint="https://e", api_key_env="K")
    assert cfg.url_path == ""


def test_diffusers_cfg_class_exists_and_validates_minimal_yaml() -> None:
    """DiffusersEngineConfig accepts the minimal example YAML shape.

    Bug catch: previously no DiffusersEngineConfig existed and the diffusers
    cfg block was an untyped dict on EngineConfig; this asserts the new
    pydantic model accepts a realistic block without raising.
    """
    from kinoforge.core.config import Config

    yaml_text = """
engine:
  kind: diffusers
  precision: fp16
  diffusers:
    pip: ["diffusers==0.30.0"]
    server_cmd: ["python", "-m", "diffusers_server"]
    asset_paths:
      init_image: init_image_url
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
compute:
  provider: runpod
  image: "img:tag"
  lifecycle: {budget: 5.0}
"""
    import yaml

    cfg = Config.model_validate(yaml.safe_load(yaml_text))
    assert cfg.engine.diffusers is not None
    assert cfg.engine.diffusers.pip == ["diffusers==0.30.0"]
    assert cfg.engine.diffusers.server_cmd == ["python", "-m", "diffusers_server"]


def test_diffusers_cfg_asset_paths_round_trips_through_model_dump() -> None:
    """YAML asset_paths under engine.diffusers survives model_validate -> model_dump.

    Bug catch: identical root cause to the hosted defect — without a
    DiffusersEngineConfig model declaring asset_paths, pydantic silently
    strips the key and DiffusersBackend._asset_paths is {} no matter what
    the user wrote in YAML.
    """
    from kinoforge.core.config import Config

    yaml_text = """
engine:
  kind: diffusers
  precision: fp16
  diffusers:
    asset_paths:
      init_image: init_image_url
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
compute:
  provider: runpod
  image: "img:tag"
  lifecycle: {budget: 5.0}
"""
    import yaml

    cfg = Config.model_validate(yaml.safe_load(yaml_text))
    dumped = cfg.model_dump()
    assert dumped["engine"]["diffusers"]["asset_paths"] == {
        "init_image": "init_image_url"
    }


def test_diffusers_cfg_asset_paths_defaults_empty() -> None:
    """DiffusersEngineConfig.asset_paths defaults to {} when YAML omits it.

    Bug catch: a None default would break the dict-comprehension path in
    DiffusersEngine.backend() and produce an AttributeError on .items().
    """
    from kinoforge.core.config import DiffusersEngineConfig

    cfg = DiffusersEngineConfig()
    assert cfg.asset_paths == {}
    assert cfg.pip == []
    assert cfg.server_cmd == []
    assert cfg.base_url == ""


# ---------------------------------------------------------------------------
# Layer I Task 4 — HostedEngineConfig load-time validators
# Move two config errors from runtime to load: empty api_key_env (Bug 7)
# and relative endpoint (Bug 2).
# ---------------------------------------------------------------------------


def test_hosted_validator_rejects_empty_api_key_env() -> None:
    """HostedEngineConfig must reject empty api_key_env at load.

    Bug catch: empty api_key_env propagates to runtime as AuthError("missing ")
    with no context.  Catch it at config load instead.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import HostedEngineConfig

    with pytest.raises(ValidationError) as exc_info:
        HostedEngineConfig(provider="x", endpoint="https://e", api_key_env="")
    assert "api_key_env" in str(exc_info.value)


def test_hosted_validator_rejects_relative_endpoint() -> None:
    """HostedEngineConfig must reject relative endpoint paths at load.

    Bug catch: relative endpoint like '/fal-ai/x' crashes urllib mid-flight
    with ValueError: unknown url type.  Catch it at config load instead.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import HostedEngineConfig

    with pytest.raises(ValidationError) as exc_info:
        HostedEngineConfig(provider="x", endpoint="/relative/path", api_key_env="K")
    assert "endpoint" in str(exc_info.value)


def test_hosted_validator_accepts_well_formed_config() -> None:
    """A correctly-formed HostedEngineConfig constructs without error."""
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(
        provider="x",
        endpoint="https://example.com/api",
        api_key_env="MY_KEY",
        health_url="",
    )
    assert cfg.endpoint == "https://example.com/api"
    assert cfg.api_key_env == "MY_KEY"


def test_fal_engine_config_defaults() -> None:
    """FalEngineConfig fills sensible defaults for queue_base, api_key_env, asset_paths."""
    from kinoforge.core.config import FalEngineConfig

    cfg = FalEngineConfig(endpoint="fal-ai/wan/v2.2/t2v", url_path="video.url")
    assert cfg.queue_base == "https://queue.fal.run"
    assert cfg.api_key_env == "FAL_KEY"
    assert cfg.asset_paths == {}
    assert cfg.health_url == ""


def test_fal_engine_config_rejects_empty_endpoint() -> None:
    """Empty endpoint must be rejected at load (would yield a bogus submit URL)."""
    from pydantic import ValidationError

    from kinoforge.core.config import FalEngineConfig

    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="", url_path="video.url")
    assert "endpoint" in str(exc.value)


def test_fal_engine_config_rejects_empty_url_path() -> None:
    """Empty url_path must be rejected — result() would have nothing to walk."""
    from pydantic import ValidationError

    from kinoforge.core.config import FalEngineConfig

    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="fal-ai/wan", url_path="")
    assert "url_path" in str(exc.value)


def test_fal_engine_config_rejects_relative_queue_base() -> None:
    """queue_base must be an absolute http(s):// URL — relative paths crash urllib."""
    from pydantic import ValidationError

    from kinoforge.core.config import FalEngineConfig

    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="x", url_path="y", queue_base="not-a-url")
    assert "queue_base" in str(exc.value)


def test_fal_engine_config_rejects_empty_api_key_env() -> None:
    """Empty api_key_env propagates as AuthError('missing ') with no context — reject here."""
    from pydantic import ValidationError

    from kinoforge.core.config import FalEngineConfig

    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="x", url_path="y", api_key_env="")
    assert "api_key_env" in str(exc.value)


def test_fal_kind_without_fal_block_raises() -> None:
    """engine.kind == 'fal' but no engine.fal block must fail at load.

    Note: load_config wraps pydantic ValidationError as ConfigError (see
    load_config in src/kinoforge/core/config.py), so this test matches the
    existing pattern of asserting ConfigError rather than ValidationError.
    """
    yaml_text = """
engine:
  kind: fal
  precision: ""
models:
  - ref: "hf:org/m"
    kind: base
    target: checkpoints
lifecycle:
  budget: 1.0
"""
    with pytest.raises(ConfigError, match=r"requires the engine\.fal block"):
        load_config(yaml_text)


def test_fal_kind_rejects_compute_block() -> None:
    """engine.kind == 'fal' must reject a compute block (hosted-like).

    Bug catch: a future refactor that drops the cross-field check in
    Config._validate_cross_fields would let users wire a fal hosted-API
    engine to a compute provider — accidentally provisioning a pod they
    don't need. This test fires when the YAML carries both blocks.
    """
    yaml_text = """
engine:
  kind: fal
  precision: ""
  fal:
    endpoint: "fal-ai/wan"
    url_path: "video.url"
compute:
  provider: runpod
  image: "img:tag"
  lifecycle:
    budget: 1.0
models:
  - ref: "hf:org/m"
    kind: base
    target: checkpoints
lifecycle:
  budget: 1.0
"""
    with pytest.raises(ConfigError, match=r"compute.*fal"):
        load_config(yaml_text)


def test_hosted_engine_config_prompt_body_key_default() -> None:
    """Bug catch: an absent field must default to "prompt" so existing
    hosted.yaml configs auto-route prompts after Layer J ships."""
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(
        provider="x",
        endpoint="https://x.example/y",
        api_key_env="X_KEY",
    )
    assert cfg.prompt_body_key == "prompt"
    dumped = cfg.model_dump()
    assert dumped["prompt_body_key"] == "prompt"


def test_hosted_engine_config_prompt_body_key_null_disables() -> None:
    """Bug catch: pydantic must accept ``None`` (YAML ``null``) so users
    can opt out of routing when their API does not use a top-level
    ``"prompt"`` field — without this, ``cfg.model_dump()`` would emit
    "prompt" and break their hosted endpoint."""
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(
        provider="x",
        endpoint="https://x.example/y",
        api_key_env="X_KEY",
        prompt_body_key=None,
    )
    assert cfg.prompt_body_key is None
    assert cfg.model_dump()["prompt_body_key"] is None


def test_diffusers_engine_config_prompt_body_key_default() -> None:
    """Diffusers default mirrors hosted — orchestrator-driven Diffusers
    runs auto-route the prompt with no YAML change."""
    from kinoforge.core.config import DiffusersEngineConfig

    cfg = DiffusersEngineConfig()
    assert cfg.prompt_body_key == "prompt"
    assert cfg.model_dump()["prompt_body_key"] == "prompt"


def test_diffusers_engine_config_prompt_body_key_null_disables() -> None:
    """Same opt-out for diffusers servers that reject unknown body keys."""
    from kinoforge.core.config import DiffusersEngineConfig

    cfg = DiffusersEngineConfig(prompt_body_key=None)
    assert cfg.prompt_body_key is None
    assert cfg.model_dump()["prompt_body_key"] is None


def test_config_spec_defaults_to_empty_dict() -> None:
    """A YAML without spec: must produce cfg.spec == {} (not None, not missing).

    Bug catch: a typo like `spec: dict | None = None` would let downstream
    `dict(cfg.spec)` raise TypeError on configs that omit the block.
    """
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
"""
    cfg = load_config(yaml_text)
    assert cfg.spec == {}
    assert cfg.params == {}


def test_config_spec_and_params_loaded_from_yaml() -> None:
    """spec: and params: blocks populate cfg.spec and cfg.params verbatim."""
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
params:
  fps: 24
  num_frames: 81
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
"""
    cfg = load_config(yaml_text)
    assert cfg.spec == {"model": "wan-ai/Wan2.2-T2V-A14B"}
    assert cfg.params == {"fps": 24, "num_frames": 81}


def test_config_spec_preserves_nested_types() -> None:
    """Nested dicts/lists/floats/ints survive without string coercion.

    Bug catch: a `dict[str, str]` annotation would silently stringify
    guidance_scale=5.0 -> "5.0" and break hosted's wire request body.
    """
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
spec:
  params:
    guidance_scale: 5.0
    steps: 30
  graph:
    nodes: [1, 2, 3]
"""
    cfg = load_config(yaml_text)
    assert cfg.spec["params"]["guidance_scale"] == 5.0
    assert isinstance(cfg.spec["params"]["guidance_scale"], float)
    assert cfg.spec["params"]["steps"] == 30
    assert isinstance(cfg.spec["params"]["steps"], int)
    assert cfg.spec["graph"]["nodes"] == [1, 2, 3]


def test_config_spec_and_params_round_trip_via_model_dump() -> None:
    """cfg.model_dump() returns the same spec/params it loaded."""
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
spec:
  pipeline: "DiffusionPipeline"
  scheduler: "DDIMScheduler"
params:
  seed: 42
"""
    cfg = load_config(yaml_text)
    dumped = cfg.model_dump()
    assert dumped["spec"] == {
        "pipeline": "DiffusionPipeline",
        "scheduler": "DDIMScheduler",
    }
    assert dumped["params"] == {"seed": 42}


# ---------------------------------------------------------------------------
# Layer M Task 1 — hosted-YAML collapse: spec.model is the single source
# ---------------------------------------------------------------------------


def test_layer_m_stale_engine_hosted_model_raises_with_guidance(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """YAML with stale ``engine.hosted.model`` raises a guiding migration error.

    Bug catch: silently dropping the stale field would leave the user with a
    cache-identity hash derived from ``""`` (empty ``spec.model``), poisoning
    the ModelProfile cache across hosted configs that share an env.

    Note: load_config wraps pydantic ValidationError in ConfigError, so we
    catch ConfigError; the str() of the exception preserves the full pydantic
    message including our migration guidance.
    """
    from kinoforge.core.config import load_config
    from kinoforge.core.errors import ConfigError

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: my-shim
    endpoint: "https://shim.example.com/inference"
    model: "wan-ai/Wan2.2-T2V-A14B"
    api_key_env: "MY_SHIM_KEY"
    health_url: "https://shim.example.com/health"
models:
  - {ref: "hf:org/m:weights.safetensors", kind: base, target: checkpoints}
lifecycle:
  budget: 5.0
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
"""
    cfg_path = tmp_path / "stale.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path)
    msg = str(exc_info.value)
    assert "engine.hosted.model" in msg
    assert "spec.model" in msg


def test_layer_m_clean_config_with_only_spec_model_loads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """YAML with only top-level ``spec.model`` (no ``engine.hosted.model``) loads.

    Bug catch: deleting the pydantic field accidentally requires the value
    elsewhere on the model (e.g. a stray ``Required`` annotation).
    """
    from kinoforge.core.config import load_config

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: my-shim
    endpoint: "https://shim.example.com/inference"
    api_key_env: "MY_SHIM_KEY"
    health_url: "https://shim.example.com/health"
models:
  - {ref: "hf:org/m:weights.safetensors", kind: base, target: checkpoints}
lifecycle:
  budget: 5.0
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
"""
    cfg_path = tmp_path / "clean.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.engine.kind == "hosted"
    assert cfg.spec["model"] == "wan-ai/Wan2.2-T2V-A14B"


# ---------------------------------------------------------------------------
# Layer O Task 3 — OutputConfig pydantic block + Config.output field
# ---------------------------------------------------------------------------


class TestOutputConfig:
    """Round-trip tests for the OutputConfig pydantic block and Config.output field."""

    def test_absent_output_block_defaults(self) -> None:
        """Config with no output: block defaults to kind='local', dir=Path('output'), enabled=True.

        Bug this catches: default_factory not wired on Config.output, or
        OutputConfig defaults wrong — any missing-output-block YAML would
        AttributeError or produce None instead of a usable OutputConfig.
        """
        cfg = load_config(MINIMAL_FAKE_ENGINE_YAML)
        assert isinstance(cfg.output, OutputConfig)
        assert cfg.output.kind == "local"
        assert cfg.output.dir == Path("output")
        assert cfg.output.enabled is True

    def test_explicit_output_block_round_trips(self) -> None:
        """An explicit output: block with kind, dir, and enabled round-trips through load_config.

        Bug this catches: OutputConfig field missing from Config, or pydantic
        drops unknown keys at the top level — the explicit block would silently
        vanish and the CLI would fall back to the default dir instead of /tmp/foo.
        """
        yaml_text = MINIMAL_FAKE_ENGINE_YAML + (
            "\noutput:\n  kind: local\n  dir: /tmp/foo\n  enabled: true\n"
        )
        cfg = load_config(yaml_text)
        assert cfg.output.kind == "local"
        assert cfg.output.dir == Path("/tmp/foo")
        assert cfg.output.enabled is True

    def test_output_enabled_false_round_trips(self) -> None:
        """output: {enabled: false} round-trips and cfg.output.enabled is False.

        Bug this catches: bool coercion or missing field causes enabled to stay
        True even when the user writes enabled: false — the CLI would then always
        publish even when the user intended to disable output.
        """
        yaml_text = MINIMAL_FAKE_ENGINE_YAML + "\noutput:\n  enabled: false\n"
        cfg = load_config(yaml_text)
        assert cfg.output.enabled is False


def test_layer_m_model_dump_roundtrips_spec_model(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``cfg.model_dump()["spec"]["model"]`` equals the value written in YAML.

    Bug catch: a future pydantic field rename or default elision silently
    drops the value during ``model_dump()`` (which is what the orchestrator
    feeds to ``engine.key_base()``).
    """
    from kinoforge.core.config import load_config

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: my-shim
    endpoint: "https://shim.example.com/inference"
    api_key_env: "MY_SHIM_KEY"
    health_url: "https://shim.example.com/health"
models:
  - {ref: "hf:org/m:weights.safetensors", kind: base, target: checkpoints}
lifecycle:
  budget: 5.0
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
"""
    cfg_path = tmp_path / "roundtrip.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    dumped = cfg.model_dump()
    assert dumped["spec"]["model"] == "wan-ai/Wan2.2-T2V-A14B"


# ---- Layer P: spec.graph_file loader convention ---------------------------


def test_spec_graph_file_relative_resolves_against_yaml_parent_dir(
    tmp_path: Path,
) -> None:
    """spec.graph_file with a relative path resolves against the YAML's parent dir."""
    graph_payload = {"nodes": {"1": {"class_type": "LoadImage"}}}
    (tmp_path / "graph.json").write_text(json.dumps(graph_payload))

    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine:
              kind: fake
              precision: fp16
            models:
              - ref: hf:org/repo:weights.safetensors
                kind: base
                target: checkpoints
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m, budget: 1.0}
            spec:
              graph_file: graph.json
            """
        ).strip()
    )

    cfg = load_config(yaml_path)

    assert cfg.spec["graph"] == graph_payload
    assert "graph_file" not in cfg.spec


def test_spec_graph_file_both_set_raises(tmp_path: Path) -> None:
    """Both spec.graph_file and spec.graph set → ConfigError naming both keys."""
    (tmp_path / "graph.json").write_text("{}")
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine:
              kind: fake
              precision: fp16
            models:
              - {ref: hf:o/r:w, kind: base, target: checkpoints}
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m, budget: 1.0}
            spec:
              graph_file: graph.json
              graph: {nodes: {}}
            """
        ).strip()
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(yaml_path)
    msg = str(excinfo.value)
    assert "'graph_file'" in msg
    assert "'graph'" in msg


def test_spec_graph_file_not_found_raises_with_path(tmp_path: Path) -> None:
    """Missing graph_file → ConfigError mentioning the resolved file path."""
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine:
              kind: fake
              precision: fp16
            models:
              - {ref: hf:o/r:w, kind: base, target: checkpoints}
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m, budget: 1.0}
            spec:
              graph_file: nope.json
            """
        ).strip()
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(yaml_path)
    msg = str(excinfo.value)
    assert "nope.json" in msg


def test_spec_graph_file_absolute_path_used_verbatim(tmp_path: Path) -> None:
    """Absolute graph_file path used verbatim (not joined against YAML parent)."""
    graph_payload = {"nodes": {"42": {"class_type": "WanSampler"}}}
    abs_graph = tmp_path / "abs-graph.json"
    abs_graph.write_text(json.dumps(graph_payload))

    # YAML in a DIFFERENT directory; absolute path must still resolve.
    yaml_dir = tmp_path / "elsewhere"
    yaml_dir.mkdir()
    yaml_path = yaml_dir / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            f"""
            engine: {{kind: fake, precision: fp16}}
            models:
              - {{ref: hf:o/r:w, kind: base, target: checkpoints}}
            compute:
              provider: local
              image: scratch
              requirements: {{min_vram_gb: 0}}
              lifecycle: {{idle_timeout: 10m, budget: 1.0}}
            spec:
              graph_file: {abs_graph}
            """
        ).strip()
    )

    cfg = load_config(yaml_path)

    assert cfg.spec["graph"] == graph_payload
    assert "graph_file" not in cfg.spec


def test_spec_graph_file_invalid_json_raises_with_path_and_parse_error(
    tmp_path: Path,
) -> None:
    """Malformed JSON in graph_file → error with file path + JSON parse error."""
    bad_graph = tmp_path / "bad.json"
    bad_graph.write_text("{this is not valid json")

    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine: {kind: fake}
            models:
              - {ref: hf:o/r:w, kind: base, target: checkpoints}
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m}
            spec:
              graph_file: bad.json
            """
        ).strip()
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(yaml_path)
    msg = str(excinfo.value)
    assert "bad.json" in msg
    # JSON parse errors typically mention "Expecting" or "delimiter" or "char";
    # the exact wording depends on stdlib json — assert SOMETHING from the
    # underlying JSONDecodeError is surfaced, not just our wrapper text.
    assert any(token in msg.lower() for token in ("expecting", "char", "delimiter")), (
        f"expected stdlib JSONDecodeError tokens in message, got: {msg!r}"
    )


def test_spec_graph_file_relative_path_with_raw_string_yaml_raises(
    tmp_path: Path,
) -> None:
    """Raw-string YAML + relative graph_file path → ConfigError (cwd-resolve is footgun)."""
    raw_yaml = textwrap.dedent(
        """
        engine: {kind: fake, precision: fp16}
        models:
          - {ref: hf:o/r:w, kind: base, target: checkpoints}
        compute:
          provider: local
          image: scratch
          requirements: {min_vram_gb: 0}
          lifecycle: {idle_timeout: 10m, budget: 1.0}
        spec:
          graph_file: nope.json
        """
    ).strip()

    with pytest.raises(ConfigError) as excinfo:
        load_config(raw_yaml)
    assert "graph_file" in str(excinfo.value)
    assert (
        "absolute" in str(excinfo.value).lower()
        or "file-based" in str(excinfo.value).lower()
    )


def test_config_keyframe_absent_defaults_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Layer R: Config without keyframe block has cfg.keyframe is None.
    Bug guard: regression that makes keyframe required would break every existing config."""
    import yaml

    from kinoforge.core.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "engine": {"kind": "fake", "precision": "fp16"},
                "models": [
                    {"kind": "base", "ref": "fake://m", "target": "checkpoints"}
                ],
                "compute": None,
            }
        )
    )
    cfg = load_config(p)
    assert cfg.keyframe is None


# ---------------------------------------------------------------------------
# Layer V — grace_after_session_s on LifecycleConfig
# ---------------------------------------------------------------------------


def test_lifecycle_config_grace_after_session_s_default_is_300() -> None:
    """Default surfaces through pydantic load too."""
    from kinoforge.core.config import LifecycleConfig

    assert LifecycleConfig(budget=1.0).grace_after_session_s == 300.0


def test_lifecycle_config_grace_after_session_s_round_trips() -> None:
    """YAML-style round-trip via model_dump_json / model_validate_json."""
    from kinoforge.core.config import LifecycleConfig

    raw = LifecycleConfig(budget=1.0, grace_after_session_s=42.0).model_dump_json()
    parsed = LifecycleConfig.model_validate_json(raw)
    assert parsed.grace_after_session_s == 42.0


def test_lifecycle_config_grace_after_session_s_rejects_negative() -> None:
    """Validator rejects negative values at load time."""
    import pytest
    from pydantic import ValidationError

    from kinoforge.core.config import LifecycleConfig

    with pytest.raises(ValidationError):
        LifecycleConfig(budget=1.0, grace_after_session_s=-1.0)


def test_lifecycle_config_grace_after_session_s_accepts_zero() -> None:
    """Zero is allowed (boundary); only negatives are rejected.

    Regression guard against a future tightening of the validator
    from `v < 0` to `v <= 0`.
    """
    from kinoforge.core.config import LifecycleConfig

    assert (
        LifecycleConfig(budget=1.0, grace_after_session_s=0.0).grace_after_session_s
        == 0.0
    )


def test_config_lifecycle_wires_grace_after_session_s() -> None:
    """Top-level Config.lifecycle() populates the field on the interface dataclass."""
    from kinoforge.core.config import load_config

    cfg = load_config(
        WAN.replace(
            "lifecycle: {idle_timeout: 2h, job_timeout: 30m, max_lifetime: 5h, budget: 25.0}",
            "lifecycle: {idle_timeout: 2h, job_timeout: 30m, max_lifetime: 5h, budget: 25.0, grace_after_session_s: 999.0}",
        )
    )
    assert cfg.lifecycle().grace_after_session_s == 999.0


# ---------------------------------------------------------------------------
# Layer 3 — BedrockVideoEngineConfig (pivot from Nova Reel to generic engine)
# ---------------------------------------------------------------------------

_LUMA_RAY_TEMPLATE = {
    "prompt": "${PROMPT}",
    "duration": 5,
    "aspect_ratio": "16:9",
    "loop": False,
    "resolution": "720p",
}


def test_bedrock_video_engine_config_loads_required_fields() -> None:
    from kinoforge.core.config import BedrockVideoEngineConfig

    cfg = BedrockVideoEngineConfig(
        region_name="us-west-2",
        model_id="luma.ray-v2:0",
        output_s3_uri="s3://bedrock-video-generation-us-west-2-nw51wr/kinoforge-output/",
        model_input_template=_LUMA_RAY_TEMPLATE,
    )
    assert cfg.region_name == "us-west-2"
    assert cfg.model_id == "luma.ray-v2:0"
    assert cfg.output_s3_uri == (
        "s3://bedrock-video-generation-us-west-2-nw51wr/kinoforge-output/"
    )
    assert cfg.model_input_template == _LUMA_RAY_TEMPLATE
    assert cfg.declared_flags_map == {}
    assert cfg.output_kms_key_id is None


def test_bedrock_video_engine_config_rejects_non_s3_output_uri() -> None:
    import pydantic

    from kinoforge.core.config import BedrockVideoEngineConfig

    with pytest.raises(pydantic.ValidationError, match="s3://"):
        BedrockVideoEngineConfig(
            region_name="us-west-2",
            model_id="luma.ray-v2:0",
            output_s3_uri="https://wrong.example.com/",
            model_input_template=_LUMA_RAY_TEMPLATE,
        )


def test_bedrock_video_engine_config_forbids_unknown_keys() -> None:
    import pydantic

    from kinoforge.core.config import BedrockVideoEngineConfig

    with pytest.raises(pydantic.ValidationError, match="extra"):
        BedrockVideoEngineConfig.model_validate(
            {
                "region_name": "us-west-2",
                "model_id": "luma.ray-v2:0",
                "output_s3_uri": "s3://bedrock-video-generation-us-west-2-nw51wr/",
                "model_input_template": _LUMA_RAY_TEMPLATE,
                "unknown_field": "oops",
            }
        )


def test_engine_config_bedrock_video_optional() -> None:
    from kinoforge.core.config import BedrockVideoEngineConfig, EngineConfig

    cfg = EngineConfig(
        kind="bedrock_video",
        precision="fp16",
        bedrock_video=BedrockVideoEngineConfig(
            region_name="us-west-2",
            model_id="luma.ray-v2:0",
            output_s3_uri="s3://bedrock-video-generation-us-west-2-nw51wr/kinoforge-output/",
            model_input_template=_LUMA_RAY_TEMPLATE,
        ),
    )
    assert cfg.kind == "bedrock_video"
    assert cfg.bedrock_video is not None
    assert cfg.bedrock_video.region_name == "us-west-2"
    # Sibling engines still default to None
    assert cfg.hosted is None
    assert cfg.fal is None


# ---------------------------------------------------------------------------
# B5a: ComputeConfig.heartbeat_mode validator tests (Task c)
# ---------------------------------------------------------------------------


def test_compute_config_heartbeat_mode_default_is_none() -> None:
    """Backward compat: existing YAMLs without compute.heartbeat_mode
    must load unchanged with mode='none' (no-op heartbeat path)."""
    from kinoforge.core.config import ComputeConfig

    cfg = ComputeConfig(
        provider="runpod",
        image="runpod/base:latest",
    )
    assert cfg.heartbeat_mode == "none"


@pytest.mark.parametrize("mode", ["none", "graphql-tag", "selfterm-http", "ssh-touch"])
def test_compute_config_heartbeat_mode_accepts_valid_literals(mode: str) -> None:
    """All four literals in the union of supported modes load.

    Provider-mode compatibility (e.g. RunPod doesn't accept 'ssh-touch')
    is enforced at adapter dispatch time, not config load — config can't
    know which provider satisfies which mode without violating
    core-import-ban.
    """
    from kinoforge.core.config import ComputeConfig

    cfg = ComputeConfig(
        provider="runpod",
        image="runpod/base:latest",
        heartbeat_mode=mode,
    )
    assert cfg.heartbeat_mode == mode


def test_compute_config_heartbeat_mode_rejects_unknown() -> None:
    """Typo-class bugs ('graphqltag', 'graphql_tag', 'none ') fail loud
    at config-load, not at runtime when the orchestrator dispatches."""
    from pydantic import ValidationError as PydanticValidationError

    from kinoforge.core.config import ComputeConfig

    with pytest.raises(PydanticValidationError, match="heartbeat_mode"):
        ComputeConfig(
            provider="runpod",
            image="runpod/base:latest",
            heartbeat_mode="graphql_tag",  # underscore not dash — common typo
        )
