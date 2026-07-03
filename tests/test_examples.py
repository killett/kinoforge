"""Tests for Task 23: example configs, README, and CI workflow.

Verifies that:
- All 4 example configs load without raising.
- Config-only swaps (provider swap, engine swap) both parse.
- README.md exists and contains the 6 required headings.
- .github/workflows/ci.yml is valid YAML and references the 3 OSes + 3 tasks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kinoforge.core.config import load_config

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples" / "configs"
README_PATH = REPO_ROOT / "README.md"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# ---------------------------------------------------------------------------
# AC1 — All 4 example configs load without raising
# ---------------------------------------------------------------------------

EXAMPLE_CONFIGS = [
    "wan.yaml",
    "diffusers.yaml",
    "hosted.yaml",
    "local-fake.yaml",
    "skypilot.yaml",
    "skypilot-gpu.yaml",
    "skypilot-lambda.yaml",
    "cost.yaml",
    "sweeper.yaml",
]


@pytest.mark.parametrize("filename", EXAMPLE_CONFIGS)
def test_example_config_loads(filename: str) -> None:
    """Each example config file must load via load_config without raising."""
    path = EXAMPLES_DIR / filename
    assert path.exists(), f"example config not found: {path}"
    load_config(path)  # must not raise


# ---------------------------------------------------------------------------
# Phase 31 T3 — SkyPilot operator-facing example
# ---------------------------------------------------------------------------


def test_skypilot_example_parses() -> None:
    """examples/configs/skypilot.yaml loads and carries the Phase 31 SkyPilot knobs.

    Bug catch: a future edit drops the compute.provider key or rewrites
    idle_timeout into a non-60 s value, silently breaking the SkyPilot
    autostop=1-minute contract documented in the live-smoke spec §3.3.
    """
    cfg = load_config(Path("examples/configs/skypilot.yaml"))
    assert cfg.compute is not None
    assert cfg.compute.provider == "skypilot"
    # idle_timeout_s == 60 maps to SkyPilot autostop=1 (minute) per spec §3.3.
    lc = cfg.lifecycle()
    assert lc.idle_timeout_s == 60


def test_skypilot_example_uses_pullable_image() -> None:
    """examples/configs/skypilot.yaml must reference an image that exists
    on Docker Hub.

    Stage-E live smoke 2026-06-18 surfaced that the template's original
    placeholder ``skypilot/skypilot:latest`` 404s on ``docker pull``;
    sky reports it as ``Failed to set up SkyPilot runtime on cluster``
    only after the cluster provisions, so the operator pays for an
    unusable VM before the failure shows up. Commit ``05fc93d`` swapped
    the lambda sibling; this lockdown forces the same swap to land on
    the CPU sibling so a follow-up live smoke does not relearn the
    same lesson.

    Bug catch: a future edit reintroduces the ``skypilot/skypilot*``
    placeholder (or any other Docker-Hub-404 string).
    """
    cfg = load_config(Path("examples/configs/skypilot.yaml"))
    assert cfg.compute is not None
    assert cfg.compute.image == "ubuntu:22.04"


def test_skypilot_gpu_example_uses_pullable_image() -> None:
    """examples/configs/skypilot-gpu.yaml must reference an image that
    exists on Docker Hub.

    Same Stage-E lesson as the CPU sibling: the original
    ``skypilot/skypilot-gpu:latest`` placeholder 404s on ``docker pull``.
    Pin to the same nvidia/cuda base the Lambda sibling uses for
    consistency across the GPU templates.

    Bug catch: a future edit reintroduces the ``skypilot/skypilot-gpu``
    placeholder (or any other Docker-Hub-404 string).
    """
    cfg = load_config(Path("examples/configs/skypilot-gpu.yaml"))
    assert cfg.compute is not None
    assert cfg.compute.image == "nvidia/cuda:12.2.0-base-ubuntu22.04"


def test_skypilot_lambda_example_pins_lambda_cloud() -> None:
    """examples/configs/skypilot-lambda.yaml is the Phase 53 Stage C
    operator template for Lambda-only sky launches.

    Bug catches:
      - cloud key missing → sky considers every enabled cloud and Vast.ai
        wins on price (the bug Phase 53 Stage C exists to fix).
      - max_usd_per_hr stays at the pre-Stage-C 1.00 → Lambda A6000
        ($1.09/hr) is filtered out and the YAML fails at provision.
    """
    cfg = load_config(Path("examples/configs/skypilot-lambda.yaml"))
    assert cfg.compute is not None
    assert cfg.compute.provider == "skypilot"
    assert cfg.compute.cloud == ["lambda"]
    # Lambda A6000 = $1.09/hr, A10 = $1.29/hr — bump above 1.00 default.
    assert cfg.compute.requirements.max_usd_per_hr >= 2.00


def test_hosted_yaml_loads_under_new_validators() -> None:
    """examples/configs/hosted.yaml must satisfy Layer I Task 4 validators."""
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/hosted.yaml")
    assert cfg.engine.kind == "hosted"
    assert cfg.engine.hosted is not None
    assert cfg.engine.hosted.endpoint.startswith("https://")
    assert cfg.engine.hosted.api_key_env == "MY_SHIM_KEY"
    assert cfg.engine.hosted.health_url == "https://your-shim.example.com/health"
    assert cfg.engine.hosted.url_path == "video.url"


def test_fal_yaml_loads_under_new_validators() -> None:
    """examples/configs/fal.yaml must satisfy Layer I Task 10 validators."""
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/fal.yaml")
    assert cfg.engine.kind == "fal"
    assert cfg.engine.fal is not None
    assert cfg.engine.fal.endpoint == "fal-ai/wan-t2v"
    assert cfg.engine.fal.queue_base == "https://queue.fal.run"
    assert cfg.engine.fal.api_key_env == "FAL_KEY"
    assert cfg.engine.fal.url_path == "video.url"


def test_luma_ray_example_config_parses() -> None:
    """examples/configs/luma-ray.yaml must satisfy Layer 3 (pivot) validators."""
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/luma-ray.yaml")
    assert cfg.engine.kind == "bedrock_video"
    assert cfg.engine.bedrock_video is not None
    assert cfg.engine.bedrock_video.region_name == "us-west-2"
    assert cfg.engine.bedrock_video.output_s3_uri.startswith("s3://")
    assert cfg.engine.bedrock_video.model_id == "luma.ray-v2:0"


# Layer 4 — comparison batch YAMLs --------------------------------------------


@pytest.mark.parametrize(
    "yaml_path",
    sorted(Path("examples/configs/comparison").glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_comparison_yaml_loads(yaml_path: Path) -> None:
    """Every comparison-batch config parses via Config.model_validate.

    Catches a regression where a typo in engine.kind, missing models
    block, or new validator rejects a previously-shipped config.
    """
    from kinoforge.core.config import load_config

    cfg = load_config(yaml_path)
    assert cfg.engine.kind in {"replicate", "runway"}
    assert isinstance(cfg.spec, dict)
    assert cfg.spec.get("model"), f"{yaml_path.name} missing spec.model"


# ---------------------------------------------------------------------------
# AC2 — Config-only swap tests
# ---------------------------------------------------------------------------

_RUNPOD_YAML = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  lifecycle:
    idle_timeout: 2h
    job_timeout: 30m
    time_buffer: 30m
    max_lifetime: 5h
    budget: 25.0
"""

_LOCAL_YAML = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: local
  image: "kinoforge/local:latest"
  lifecycle:
    idle_timeout: 2h
    job_timeout: 30m
    time_buffer: 30m
    max_lifetime: 5h
    budget: 25.0
"""


def test_provider_swap_runpod_to_local() -> None:
    """Configs differing only in compute.provider (runpod vs local) both parse."""
    load_config(_RUNPOD_YAML)
    load_config(_LOCAL_YAML)


_COMFYUI_YAML = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "hf:some-org/some-model:model.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: local
  image: "kinoforge/local:latest"
  lifecycle:
    idle_timeout: 1h
    job_timeout: 30m
    time_buffer: 30m
    max_lifetime: 3h
    budget: 10.0
"""

_DIFFUSERS_YAML = """\
engine:
  kind: diffusers
  precision: fp16
models:
  - ref: "hf:some-org/some-model:model.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: local
  image: "kinoforge/local:latest"
  lifecycle:
    idle_timeout: 1h
    job_timeout: 30m
    time_buffer: 30m
    max_lifetime: 3h
    budget: 10.0
"""


def test_engine_swap_comfyui_to_diffusers() -> None:
    """Configs differing only in engine.kind (comfyui vs diffusers) both parse."""
    load_config(_COMFYUI_YAML)
    load_config(_DIFFUSERS_YAML)


# ---------------------------------------------------------------------------
# AC3 — README headings
# ---------------------------------------------------------------------------

REQUIRED_HEADINGS = [
    "# kinoforge",
    "## Quickstart",
    "## Configuration at a glance",
    "## Contributing / extending",
]


def test_readme_exists() -> None:
    """README.md must exist at the repo root."""
    assert README_PATH.exists(), f"README.md not found at {README_PATH}"


@pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
def test_readme_contains_heading(heading: str) -> None:
    """README.md must contain each required heading (case-sensitive)."""
    content = README_PATH.read_text(encoding="utf-8")
    assert heading in content, f"README.md missing heading: {heading!r}"


# ---------------------------------------------------------------------------
# AC4 — CI workflow is valid YAML and references 3 OSes + 3 pixi tasks
# ---------------------------------------------------------------------------

CI_REQUIRED_OSES = ["ubuntu-latest", "macos-latest", "windows-latest"]
CI_REQUIRED_TASKS = ["pixi run lint", "pixi run typecheck", "pixi run test"]


def test_ci_workflow_exists() -> None:
    """CI workflow file must exist."""
    assert CI_PATH.exists(), f"CI workflow not found at {CI_PATH}"


def test_ci_workflow_is_valid_yaml() -> None:
    """CI workflow must be parseable as YAML."""
    content = CI_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, dict), "CI workflow must be a YAML mapping"


@pytest.mark.parametrize("os_name", CI_REQUIRED_OSES)
def test_ci_references_os(os_name: str) -> None:
    """CI workflow must reference each of the three OS targets."""
    content = CI_PATH.read_text(encoding="utf-8")
    assert os_name in content, f"CI workflow missing OS: {os_name!r}"


@pytest.mark.parametrize("task", CI_REQUIRED_TASKS)
def test_ci_references_task(task: str) -> None:
    """CI workflow must invoke each pixi task."""
    content = CI_PATH.read_text(encoding="utf-8")
    assert task in content, f"CI workflow missing task: {task!r}"


# ---------------------------------------------------------------------------
# Layer K — spec: shape tests
# ---------------------------------------------------------------------------


def test_hosted_yaml_has_non_empty_spec() -> None:
    """examples/configs/hosted.yaml ships a spec: block with required keys.

    Layer M: the lock-equality assertion against engine.hosted.model is
    deleted (the field no longer exists).  Replaced with a positive
    assertion on the single remaining source of truth.
    """
    cfg = load_config(EXAMPLES_DIR / "hosted.yaml")
    assert "model" in cfg.spec
    assert "params" in cfg.spec
    assert isinstance(cfg.spec["model"], str) and cfg.spec["model"]


def test_layer_m_hosted_yaml_no_engine_hosted_model() -> None:
    """examples/configs/hosted.yaml carries spec.model only — never engine.hosted.model.

    Bug catch: a future edit re-introduces the duplicated field and the
    documented migration silently regresses.
    """
    cfg = load_config(EXAMPLES_DIR / "hosted.yaml")
    hosted_dump = cfg.engine.hosted.model_dump() if cfg.engine.hosted else {}
    assert "model" not in hosted_dump


def test_diffusers_yaml_has_non_empty_spec() -> None:
    """examples/configs/diffusers.yaml ships pipeline+scheduler in spec:."""
    cfg = load_config(EXAMPLES_DIR / "diffusers.yaml")
    assert "pipeline" in cfg.spec
    assert "scheduler" in cfg.spec


def test_wan_yaml_has_non_empty_spec() -> None:
    """examples/configs/wan.yaml ships graph+node_overrides in spec:."""
    cfg = load_config(EXAMPLES_DIR / "wan.yaml")
    assert "graph" in cfg.spec
    assert "node_overrides" in cfg.spec


def test_fal_and_local_fake_yaml_have_empty_spec() -> None:
    """fal.yaml + local-fake.yaml keep cfg.spec = {} (no required spec keys)."""
    fal_cfg = load_config(EXAMPLES_DIR / "fal.yaml")
    fake_cfg = load_config(EXAMPLES_DIR / "local-fake.yaml")
    assert fal_cfg.spec == {}
    assert fake_cfg.spec == {}


# ---------------------------------------------------------------------------
# Layer L — batch manifest example
# ---------------------------------------------------------------------------

from kinoforge.core.batch import load_manifest  # noqa: E402


def test_batch_prompts_example_loads() -> None:
    """examples/configs/manifests/batch-prompts.yaml must parse cleanly.

    Bug catch: an example that rots silently (missing prompt file,
    pydantic schema drift) breaks the documented quickstart.
    """
    path = EXAMPLES_DIR / "manifests" / "batch-prompts.yaml"
    m = load_manifest(path)
    assert len(m.entries) == 3
    for entry in m.entries:
        assert entry.prompt is not None and len(entry.prompt) > 0
        assert entry.run_id is not None
        assert entry.prompt_file is None  # collapsed to inline


def test_batch_prompts_example_uses_valid_modes() -> None:
    """Every example entry must declare a supported mode.

    Bug catch: a typo'd mode (e.g. "t2vv") would silently fail at
    request validation, not at load time.
    """
    path = EXAMPLES_DIR / "manifests" / "batch-prompts.yaml"
    m = load_manifest(path)
    for entry in m.entries:
        assert entry.mode in {"t2v", "i2v", "flf2v"}, (
            f"unexpected mode in example: {entry.mode!r} (run_id={entry.run_id})"
        )


# ---------------------------------------------------------------------------
# Layer N — RunPod live-smoke config
# ---------------------------------------------------------------------------


def test_runpod_comfyui_wan_yaml_loads() -> None:
    """examples/configs/runpod-comfyui-wan.yaml loads and reports Layer N cost caps."""
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/runpod-comfyui-wan.yaml"))
    assert cfg.engine.kind == "comfyui"
    assert cfg.compute is not None, (
        "runpod-comfyui-wan.yaml must populate the compute block; "
        "got None which means the YAML schema dropped it silently"
    )
    assert cfg.compute.provider == "runpod"
    assert cfg.compute.mode == "pod"
    assert cfg.compute.requirements.min_vram_gb == 24
    assert cfg.compute.requirements.max_usd_per_hr == 0.50
    assert cfg.compute.lifecycle is not None
    assert cfg.compute.lifecycle.budget == 2.0
    assert cfg.compute.lifecycle.idle_timeout == 1500.0  # 25m parsed via parse_duration


def test_runpod_comfyui_wan_manifest_yaml_loads() -> None:
    """examples/configs/manifests/runpod-comfyui-wan-manifest.yaml loads via load_manifest.

    Verifies the single i2v entry with an assets block is schema-valid and
    that load_manifest collapses run_id correctly.
    """
    path = EXAMPLES_DIR / "manifests" / "runpod-comfyui-wan-manifest.yaml"
    assert path.exists(), f"manifest not found: {path}"
    m = load_manifest(path)
    assert len(m.entries) == 1
    entry = m.entries[0]
    assert entry.mode == "i2v"
    assert entry.run_id == "layer-n-smoke"
    assert entry.prompt is not None and len(entry.prompt) > 0
    assert entry.prompt_file is None  # collapsed at load time
    assert entry.assets is not None and len(entry.assets) == 1
    asset = entry.assets[0]
    assert asset["role"] == "init_image"
    assert asset["kind"] == "image"
    assert asset["ref"].startswith("file://")


# ---------------------------------------------------------------------------
# Layer P Task 6 — runpod-comfyui-wan YAML scaffold with graph_file resolution
# ---------------------------------------------------------------------------


def test_runpod_comfyui_wan_yaml_loads_with_graph_file_resolution() -> None:
    """runpod-comfyui-wan.yaml loads cleanly; Task 1 graph_file resolver inlines JSON."""
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/runpod-comfyui-wan.yaml"))

    assert cfg.engine.kind == "comfyui"
    assert cfg.compute is not None
    assert cfg.compute.provider == "runpod"
    # Task 1 graph_file -> graph resolution
    assert isinstance(cfg.spec.get("graph"), dict)
    assert "graph_file" not in cfg.spec
    # graph dict matches the companion JSON file (proves Task 1 end-to-end).
    # The on-disk JSON carries a top-level _meta provenance header (item #3 T1)
    # for AC12's SHA cross-reference test; _resolve_spec_graph_file strips it
    # at load time, so cfg.spec["graph"] is _meta-free. Compare against the
    # post-strip expected dict.
    graph_path = Path("examples/configs/runpod-comfyui-wan.graph.json")
    import json

    expected_graph = json.loads(graph_path.read_text(encoding="utf-8"))
    expected_graph.pop("_meta", None)
    assert cfg.spec["graph"] == expected_graph
    # Models: UNet (base), VAE, text encoder, clip_vision
    assert len(cfg.models) == 4
    kinds = [m.kind for m in cfg.models]
    assert kinds.count("base") == 1
    assert kinds.count("vae") == 1
    assert kinds.count("text_encoder") == 1
    assert kinds.count("clip_vision") == 1
    # custom_nodes SHA-pinned to real commits (resolved offline pre-live-run)
    comfyui_block = cfg.engine.comfyui
    if comfyui_block is not None:
        nodes = comfyui_block.custom_nodes
        assert len(nodes) >= 2
        for node in nodes:
            assert "git" in node
            ref = node.get("ref")
            assert isinstance(ref, str) and len(ref) == 40, (
                f"expected 40-char git SHA, got {ref!r}"
            )


# ---------------------------------------------------------------------------
# Layer O Task 8 — commented output: block round-trip tests
# ---------------------------------------------------------------------------


class TestOutputBlockExamples:
    """Each example YAML still round-trips with the new commented output: block (Layer O)."""

    @pytest.mark.parametrize(
        "filename",
        [
            "wan.yaml",
            "diffusers.yaml",
            "fal.yaml",
            "hosted.yaml",
            "local-fake.yaml",
            "runpod-comfyui-wan.yaml",
        ],
    )
    def test_example_loads_with_default_output_block(self, filename: str) -> None:
        """The commented output block must not break YAML parsing; the
        loaded Config should have output at its defaults.  Catches a
        regression where someone uncomments only one line of the block
        and breaks the indentation invariant.
        """
        path = Path("examples/configs") / filename
        cfg = load_config(path)
        assert cfg.output.kind == "local"
        assert cfg.output.dir == Path("output")
        assert cfg.output.enabled is True


# ---------------------------------------------------------------------------
# Layer R Task 12 — keyframe example configs load-lockdown tests
# ---------------------------------------------------------------------------


def test_keyframe_fal_i2v_example_loads() -> None:
    """Bug guard: example YAML must round-trip through load_config + reflect cfg.keyframe.

    A broken example silently misleads operators into a config shape that doesn't work.
    """
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/keyframe-fal-i2v.yaml")
    assert cfg.mode == "i2v"
    assert cfg.keyframe is not None
    assert cfg.keyframe.engine == "fal"
    assert cfg.keyframe.prompt
    assert cfg.keyframe.spec["model"] == "fal-ai/flux/schnell"


def test_keyframe_fal_flf2v_example_loads() -> None:
    """Bug guard: flf2v with per-role overrides must round-trip and preserve distinct prompts."""
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/keyframe-fal-flf2v.yaml")
    assert cfg.mode == "flf2v"
    assert cfg.keyframe is not None
    assert "first_frame" in cfg.keyframe.roles
    assert "last_frame" in cfg.keyframe.roles
    assert (
        cfg.keyframe.roles["first_frame"].prompt
        != cfg.keyframe.roles["last_frame"].prompt
    )


def test_keyframe_examples_in_master_loader() -> None:
    """Bug guard: existing 'every example loads' iteration in this file MUST cover the 2 new files."""
    from pathlib import Path

    yamls = sorted(Path("examples/configs").glob("*.yaml"))
    names = {p.name for p in yamls}
    assert "keyframe-fal-i2v.yaml" in names
    assert "keyframe-fal-flf2v.yaml" in names


def test_keyframe_examples_have_no_compute() -> None:
    """Bug guard: keyframe examples ship as hosted/queue path; compute: null must hold.

    Accidental compute requirement would force users to provision a GPU pod.
    """
    from kinoforge.core.config import load_config

    for name in ("keyframe-fal-i2v.yaml", "keyframe-fal-flf2v.yaml"):
        cfg = load_config(f"examples/configs/{name}")
        assert cfg.compute is None, f"{name}: expected compute=null"


def test_diffusers_wan_t2v_14b_cfg_pins_server_module() -> None:
    """Pin the diffusers Wan 2.2 14B cfg's load-bearing fields.

    The cfg's server_cmd must point at our maintained server module
    so doctor / live smoke / warm-reuse all hit the right HTTP shape.
    The base ref must be the Wan-AI native repo (not Kijai's I2V pair,
    which has a 36-channel patch_embedding incompatible with T2V).
    """
    from pathlib import Path

    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml"))
    assert cfg.engine.kind == "diffusers"
    assert cfg.engine.precision == "bf16"
    assert cfg.engine.diffusers is not None
    assert cfg.engine.diffusers.server_cmd[-1] == (
        "kinoforge.engines.diffusers.servers.wan_t2v_server"
    )
    # embed_modules MUST list the server package so the bootstrap can
    # ship the source to the pod; the stock pytorch image has no
    # kinoforge install.
    assert cfg.engine.diffusers.embed_modules == ["kinoforge.engines.diffusers.servers"]
    base_refs = [m.ref for m in cfg.models if m.kind == "base"]
    # The diffusers cfg must point at the `-Diffusers` variant — the
    # bare Wan-AI/Wan2.2-T2V-A14B repo is native checkpoint layout and
    # diffusers `from_pretrained` 404s against it (no model_index.json
    # at root). See plan amendment 2026-06-19, Task 8 attempt #7.
    assert base_refs == ["hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers"]


def test_diffusers_wan_t2v_14b_cap_key_differs_from_kijai_5b() -> None:
    """Cross-engine + cross-model capability_key isolation invariant.

    The new diffusers Wan 2.2 14B cfg MUST derive a different
    capability_key from the Kijai 5B cfg so a kinoforge generate
    invocation never accidentally warm-reuses across them.
    """
    from pathlib import Path

    from kinoforge.core.config import load_config

    cfg_14b = load_config(
        Path("examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml")
    )
    cfg_5b = load_config(Path("examples/configs/runpod-comfyui-wan-t2v-5b.yaml"))
    assert cfg_14b.capability_key().derive() != cfg_5b.capability_key().derive()


def test_wan_with_upscale_flashvsr_pins_engine_and_gpu_allowlist() -> None:
    """FlashVSR multi-stage lockdown: engine, GPU allowlist, long-video default.

    Bug caught: a future edit swaps engine to spandrel silently, or drops
    the SM80+ GPU tier from the allowlist so RunPod picks a T4 that fails
    BSA compile at cold boot.
    """
    with (EXAMPLES_DIR / "wan-with-upscale-flashvsr.yaml").open() as f:
        raw = yaml.safe_load(f)
    assert raw["upscale"]["engine"] == "flashvsr"
    assert raw["upscale"]["scale"] == "2x"
    assert raw["upscale"]["flashvsr"]["long_video_mode"] is False
    assert raw["upscale"]["flashvsr"]["precision"] == "fp16"
    # Set-equality — order is separately tested elsewhere; the invariant
    # here is that the exact 4-GPU allowlist stays intact. NVIDIA-prefixed
    # names are non-negotiable: plain tokens like "A100 80GB" fall through
    # RunPod's fuzzy matcher to a no-GPU offer (T8 attempt #1 evidence).
    assert set(raw["compute"]["requirements"]["gpu_preference"]) == {
        "NVIDIA A100 80GB PCIe",
        "NVIDIA A100-SXM4-80GB",
        "NVIDIA H100 80GB HBM3",
        "NVIDIA H100 PCIe",
    }


@pytest.mark.xfail(
    reason="T7.6.4 renames this cfg to upscale-flashvsr-x4.yaml with scale=4x",
    strict=True,
)
def test_upscale_flashvsr_x2_marks_upscale_only_and_a6000_first() -> None:
    """FlashVSR upscale-only lockdown: upscale_only=true + A6000 first.

    Bug caught: a future edit re-enables eager WanPipeline load in the
    upscale-only cfg (upscale_only: false) — cold boot balloons from 5min
    to 30min and blows the boot_timeout.
    """
    with (EXAMPLES_DIR / "upscale-flashvsr-x2.yaml").open() as f:
        raw = yaml.safe_load(f)
    assert raw["engine"]["diffusers"]["upscale_only"] is True
    assert raw["upscale"]["engine"] == "flashvsr"
    # A6000 pinned first — cheapest SM80+ pod on RunPod fits FlashVSR's
    # ~8 GB peak with generous headroom. NVIDIA-prefixed name required
    # (see F-single T8 attempt-#1 evidence: bare "A6000" → no-GPU pod).
    assert raw["compute"]["requirements"]["gpu_preference"][0] == "NVIDIA RTX A6000"
    # Load through full validator to catch schema regressions.
    load_config(EXAMPLES_DIR / "upscale-flashvsr-x2.yaml")
