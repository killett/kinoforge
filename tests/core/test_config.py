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
    model: "vendor/m"
    asset_paths:
      init_image: input.image_url
lifecycle: {budget: 5.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
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
    model: "vendor/m"
    url_path: video.url
lifecycle: {budget: 5.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
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

    cfg = HostedEngineConfig(provider="fal", endpoint="x", model="m")
    assert cfg.asset_paths == {}


def test_hosted_cfg_url_path_defaults_empty() -> None:
    """HostedEngineConfig.url_path defaults to '' when YAML omits it.

    Bug catch: a None default would propagate as the string "None" through
    str(hosted_cfg.get("url_path", "")) in the engine; "" is the canonical
    'walk-no-path' sentinel that result() already checks for.
    """
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(provider="fal", endpoint="x", model="m")
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
