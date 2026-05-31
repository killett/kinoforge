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
]


@pytest.mark.parametrize("filename", EXAMPLE_CONFIGS)
def test_example_config_loads(filename: str) -> None:
    """Each example config file must load via load_config without raising."""
    path = EXAMPLES_DIR / filename
    assert path.exists(), f"example config not found: {path}"
    load_config(path)  # must not raise


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
    "## Configuration",
    "## Extending: add a provider/source/engine",
    "## Roadmap (deferred layers and their seams)",
    "## Design references",
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
    """examples/configs/hosted.yaml ships a spec: block with required keys."""
    cfg = load_config(EXAMPLES_DIR / "hosted.yaml")
    assert "model" in cfg.spec
    assert "params" in cfg.spec
    # Sanity: documented duplication holds in the shipped example.
    assert cfg.engine.hosted is not None
    assert cfg.spec["model"] == cfg.engine.hosted.model


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
    """examples/configs/batch-prompts.yaml must parse cleanly.

    Bug catch: an example that rots silently (missing prompt file,
    pydantic schema drift) breaks the documented quickstart.
    """
    path = EXAMPLES_DIR / "batch-prompts.yaml"
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
    path = EXAMPLES_DIR / "batch-prompts.yaml"
    m = load_manifest(path)
    for entry in m.entries:
        assert entry.mode in {"t2v", "i2v", "flf2v"}, (
            f"unexpected mode in example: {entry.mode!r} (run_id={entry.run_id})"
        )
