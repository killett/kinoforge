# `kinoforge text` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kinoforge text` — one prompt and zero or more images in, one completion out, on a RunPod pod kinoforge books, boots and tears down — with a `TextEngine` seam, a `transformers` engine, a lean pod-side server, three example configs and two live smokes.

**Architecture:** A `text:` config block coexists with `engine:`/`models:`/`compute:` (the `upscale:` shape). A new `TextEngine` ABC mirrors `UpscalerEngine` (composable provision fragment + pod client); a `TextStage` mirrors `UpscaleStage` and is appended by `orchestrator.generate(skip_clip_stage=True)`. The pod runs a new `servers/text_server.py` (H3 skeleton, `submit_and_poll` status schema, completion inline). Modes `t2t`/`it2t` are derived from `--image`; images against a text-only model are refused pre-spend from the config's declared `capability.supported_modes` and re-checked against the pod's `/health` before any upload.

**Tech Stack:** Python 3.13 (controller, pixi default env — NO torch/transformers), FastAPI + pydantic (server, unit-tested via `TestClient` with an injected fake loader), `transformers>=5.10` + torch 2.8 on the pod (`runpod/pytorch:2.8.0` image), RunPod secure pool.

**Spec:** `docs/superpowers/specs/2026-10-04-text-command-design.md` (read it first; every task cites its sections). Research: `docs/superpowers/research/2026-10-04-open-weight-llm-survey.md`.

## Global Constraints

- **Pod-side modules import NOTHING from `kinoforge.*` outside `kinoforge.engines.diffusers.servers`, and import `torch` / `transformers` / `PIL` only inside functions.** The controller env has none of the three; U66 killed every RunPod pod for three hours with one module-level import a 6348-test green suite could not see. `tests/providers/test_pod_embed_closure.py` enforces the embed set both ways.
- **`/health` → `capabilities` vocabulary is closed.** The text server advertises exactly `["text", "upload"]` when ready; `tests/cli/test_shipped_cfg_want_stages_sweep.py::ADVERTISABLE_STAGES` is hand-transcribed and gains `"text"` in the same commit (Task 4).
- **Status schema is `submit_and_poll`'s** (`{"state": ..., "result": ...}`), never `/generate`'s (`{"status": ..., "filename": ...}`). Spec §6.1.
- **Health gate before upload before submit** in `TextStage.run`. A mode mismatch must leave the upload and complete counters at zero (spec §7.1).
- **stdout of `kinoforge text` is the completion text and nothing else.** Run chatter goes to the logger (stderr).
- **RunPod env-payload ceiling is 101 000 B** (`tests/providers/test_env_payload_ceiling.py`). Every new RunPod diffusers config needs a `_BASELINE_BYTES` row and must measure under the ceiling (Task 8).
- **Launch goldens are regenerated AFTER `pixi run pre-commit run --all-files`**, never before (formatting moves bytes). Review the diff: only the three new golden files may change (Task 8).
- **`git add` new files before trusting a `pre-commit --all-files` run** — it ignores untracked files.
- **Commit RED scaffolds before any live spend; `pixi run preflight` before any live spend; `--no-reuse` on every live run; `kinoforge list` afterwards must print BOTH `No running instances.` and `No instances recorded in ledger.`** (CLAUDE.md).
- Local timezone everywhere (`datetime.now()`); never UTC.
- Every function gets type hints and a Google-style docstring; every test states its behaviour and the concrete bug that would fail it (`test-design` skill).

**User decisions (already made):**
- "Design the pipeline hook now" → then "Both hooks" → then "Yes, two specs": this plan ships the command; the two hook stages are a follow-on spec. This plan only guarantees the seams (port-parameterised client, runtime-phase provision fragment).
- Output: "stdout + artifact" (`.txt` + `.json` sidecar).
- Mismatch handling: "Fast error pre-spend, verified on pod".
- Serving stack: "transformers only"; vLLM deferred.
- Architecture: "upscale-shaped TextStage" (approach 1).
- Smoke models: Qwen3-0.6B (text-only) and SmolVLM-256M-Instruct (vision); quality config Qwen3.8-27B offline-validated only. Spec approved "no changes".

**Refinements to the spec made while planning (not contradictions):** `TextEngine.health` and `TextEngine.upload_image` take `cfg` so the engine can read `text.port` on every call (spec §5.2's port seam needs it; the ABC in §5 omitted the parameter). The server's loader seam is a module attribute `_LOADER` rather than an env-var import path, so `test_pod_embed_closure.py`'s dynamic-import list stays untouched. `_cmd_text` wraps `orchestrator.generate` in the same exit ladder `_cmd_image` uses (a raised `KinoforgeError` is exit 1 with a one-line message, not a traceback).

---

## File structure

| path | responsibility |
|---|---|
| `src/kinoforge/core/interfaces.py` (modify) | `TextJob`, `TextResult`, `TextHealth`, `TextEngine`; `MODE_ROLE_REQUIREMENTS` += `t2t`, `it2t` |
| `src/kinoforge/core/errors.py` (modify) | `TextGenerationFailed` |
| `src/kinoforge/core/registry.py` (modify) | `register_text_engine` / `get_text_engine` / `text_engine_names` |
| `src/kinoforge/core/config.py` (modify) | `TEXT_MODES`, `TextConfig`, `Config.text`, `_validate_text_block`, `capability_key` stages |
| `src/kinoforge/core/text_request.py` (new) | pure helpers: mode derivation, prompt precedence, pre-spend gate, request building, base-ref lookup |
| `src/kinoforge/engines/diffusers/servers/_upload.py` (new) | streaming `PUT /upload` handler (stdlib + fastapi only) |
| `src/kinoforge/engines/diffusers/servers/text_server.py` (new) | the pod server |
| `src/kinoforge/text_engines/__init__.py`, `text_engines/transformers/__init__.py` (new) | `TransformersTextEngine`: client + provision fragment; self-registers |
| `src/kinoforge/_adapters.py` (modify) | one import |
| `src/kinoforge/engines/diffusers/__init__.py` (modify) | third composition block (runtime phase) |
| `src/kinoforge/pipeline/text.py` (new) | `TextStage` |
| `src/kinoforge/core/orchestrator.py` (modify) | append `TextStage`; `artifact_key = "text"` |
| `src/kinoforge/cli/_main.py`, `cli/_commands.py` (modify) | `text` parser, dispatch, interruptible, `_cmd_text` |
| `examples/configs/runpod-diffusers-{qwen3-0_6b-t2t,smolvlm-256m-it2t,qwen3_8-27b-it2t}.yaml`, `examples/configs/prompts/text-smoke-{t2t,it2t}.txt` (new) | configs + verbatim smoke prompts |
| `tests/...` | one test module per task, named below |
| `docs/configuration.md`, `docs/engines.md`, `README.md`, `successful-generations.md`, `PROGRESS.md` (modify) | docs |

Run every command from `/workspace` with `pixi run ...`. The test command for a single file is `pixi run pytest <path> -q`.

---

### Task 1: Interfaces, registry, error type

**Goal:** Define the `TextEngine` seam (`TextJob`, `TextResult`, `TextHealth`, `TextEngine`), the two text modes in `MODE_ROLE_REQUIREMENTS`, the duplicate-rejecting text-engine registry trio, and `TextGenerationFailed`.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (after `class InterpolatorEngine`, ~line 1449+; `MODE_ROLE_REQUIREMENTS` at line 801)
- Modify: `src/kinoforge/core/registry.py` (after `interpolator_names`, ~line 330; import block at line 20)
- Modify: `src/kinoforge/core/errors.py` (append after `UpscaleFailed`, line 569+)
- Test: `tests/core/test_text_interfaces.py`

**Acceptance Criteria:**
- [ ] `register_text_engine` raises `UnknownAdapter` on a duplicate name; `get_text_engine` raises `UnknownAdapter` naming the known engines; `text_engine_names()` is sorted.
- [ ] `required_image_roles("t2t") == []` and `required_image_roles("it2t") == []`; both keys exist in `MODE_ROLE_REQUIREMENTS`.
- [ ] `TextEngine.render_provision` default raises `NotImplementedError`; a concrete subclass with the five abstract methods instantiates.
- [ ] `TextGenerationFailed("j1", "boom")` carries `job_id`, `server_error`, and its message contains both.

**Verify:** `pixi run pytest tests/core/test_text_interfaces.py -q` → all pass; `pixi run mypy src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py src/kinoforge/core/errors.py` → clean.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_text_interfaces.py
"""The TextEngine seam: dataclasses, ABC defaults, registry trio, modes, error."""

from __future__ import annotations

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import TextGenerationFailed, UnknownAdapter
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    TextEngine,
    TextHealth,
    TextJob,
    TextResult,
    required_image_roles,
)


class _MinimalEngine(TextEngine):
    """Smallest concrete TextEngine — exercises the ABC's default methods."""

    name = "minimal"
    requires_compute = False

    def health(self, instance, cfg):  # noqa: ANN001, ANN201
        return TextHealth(ready=True, model="m", supported_modes=frozenset({"t2t"}))

    def upload_image(self, instance, local_path, cfg):  # noqa: ANN001, ANN201
        return "/tmp/x.png"

    def complete(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001, ANN201
        return TextResult(text="t", finish_reason="stop", usage={}, model="m", elapsed_s=0.0)

    def validate_spec(self, job):  # noqa: ANN001, ANN201
        return None

    def model_identity(self, cfg):  # noqa: ANN001, ANN201
        return "m"


@pytest.fixture
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_text_engines", {})


def test_text_modes_require_no_image_roles() -> None:
    """Bug caught: validate_request KeyErrors on an unknown mode, or a mode
    table entry that demands a role no text request carries."""
    assert MODE_ROLE_REQUIREMENTS["t2t"] == {}
    assert MODE_ROLE_REQUIREMENTS["it2t"] == {}
    assert required_image_roles("t2t") == []
    assert required_image_roles("it2t") == []


def test_text_job_defaults_are_text_only() -> None:
    """Bug caught: a mutable default shared across jobs, or images defaulting to None."""
    job = TextJob(prompt="hi")
    assert job.images == ()
    assert job.system is None
    assert job.params == {}
    assert TextJob(prompt="a").params is not TextJob(prompt="b").params


def test_render_provision_default_raises() -> None:
    """Bug caught: a default that returns an empty fragment, hiding a missing override."""
    with pytest.raises(NotImplementedError):
        _MinimalEngine().render_provision({})


@pytest.mark.usefixtures("_clean_registry")
def test_duplicate_text_engine_registration_is_rejected() -> None:
    """Bug caught: overwrite semantics (register_engine's) silently rebinding
    the production text engine on an import-order accident."""
    registry.register_text_engine("minimal", _MinimalEngine)
    with pytest.raises(UnknownAdapter, match="already registered"):
        registry.register_text_engine("minimal", _MinimalEngine)


@pytest.mark.usefixtures("_clean_registry")
def test_unknown_text_engine_names_the_known_ones() -> None:
    """Bug caught: a bare KeyError, or a message that does not say what IS registered."""
    registry.register_text_engine("minimal", _MinimalEngine)
    with pytest.raises(UnknownAdapter, match=r"no text engine registered as 'nope'.*minimal"):
        registry.get_text_engine("nope")
    assert registry.text_engine_names() == ["minimal"]
    assert registry.get_text_engine("minimal") is _MinimalEngine


def test_text_generation_failed_carries_job_and_server_error() -> None:
    """Bug caught: an error whose str() loses the pod's own message."""
    exc = TextGenerationFailed("j1", "CUDA out of memory")
    assert exc.job_id == "j1"
    assert exc.server_error == "CUDA out of memory"
    assert "j1" in str(exc)
    assert "CUDA out of memory" in str(exc)
```

- [ ] **Step 2: Run to confirm failure**

Run: `pixi run pytest tests/core/test_text_interfaces.py -q`
Expected: ImportError on `TextEngine` / `TextGenerationFailed`.

- [ ] **Step 3: Add the interfaces**

In `src/kinoforge/core/interfaces.py`, confirm `from pathlib import Path` is imported at the top (add it if absent — `Instance`/`Artifact` do not use it today). Extend `MODE_ROLE_REQUIREMENTS` (line 801):

```python
MODE_ROLE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "t2v": {},
    # ... existing t2va / i2v / flf2v entries unchanged ...
    # `kinoforge text` (docs/superpowers/specs/2026-10-04-text-command-design.md
    # §2.1). Hugging Face task names in the repo's x2y spelling: t2t is
    # text-generation, it2t is image-text-to-text. Both are EMPTY because the
    # image count is open-ended — roles are image_1 … image_N in flag order and
    # none is *required* by this table; TextStage gates it2t on the pod's
    # /health instead (§4.2).
    "t2t": {},
    "it2t": {},
}
```

Immediately after `class InterpolatorEngine` ends, add:

```python
@dataclass(frozen=True)
class TextJob:
    """One unit of text-generation work — engine-agnostic.

    Attributes:
        prompt: The user turn's text.
        system: Optional system turn; ``None`` sends no system message.
        images: Pod-side paths of already-uploaded images, in flag order.
            Empty for ``t2t``.
        params: Opaque generation parameters passed through to the pod
            (``max_new_tokens``, ``temperature`` …). A ``chat_template_kwargs``
            mapping inside it goes to the chat template, not to ``generate()``.
    """

    prompt: str
    system: str | None = None
    images: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextResult:
    """The pod's answer to one :class:`TextJob`.

    Attributes:
        text: The completion, decoded with special tokens stripped.
        finish_reason: ``"stop"`` or ``"length"`` (hit ``max_new_tokens``).
        usage: ``prompt_tokens`` and ``completion_tokens``.
        model: The checkpoint id the pod loaded.
        elapsed_s: Submit-acknowledged to done, controller clock.
    """

    text: str
    finish_reason: str
    usage: dict[str, int]
    model: str
    elapsed_s: float


@dataclass(frozen=True)
class TextHealth:
    """What the pod's ``/health`` says about the loaded text model.

    Attributes:
        ready: The model is loaded and the worker is running.
        model: The checkpoint id the pod loaded.
        supported_modes: Modes DERIVED on the pod from the model class
            (``{"t2t"}`` or ``{"t2t", "it2t"}``) — the truth the config's
            declaration is checked against.
    """

    ready: bool
    model: str
    supported_modes: frozenset[str]


class TextEngine(ABC):
    """A swappable text-generation engine; owns its pod-side server contract.

    Shaped like :class:`UpscalerEngine`: a registry key, a compute flag, a
    composable provision fragment, and the calls a stage makes against a
    booted pod. ``health`` is consulted BEFORE ``upload_image`` and
    ``complete`` so a model that cannot take images refuses before any bytes
    move (design §4.2). Every pod-facing call takes ``cfg`` so the engine can
    read ``text.port`` — the seam a later sidecar launch needs (design §5.2).

    Attributes:
        name: Registry key (e.g. ``"transformers"``).
        requires_compute: True when this engine needs a remote pod.
    """

    name: str
    requires_compute: bool

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit the composable boot fragment. Default raises; pod engines override."""
        del cfg
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    @abstractmethod
    def health(self, instance: Instance | None, cfg: dict[str, object]) -> TextHealth:
        """Read the pod's ``/health`` into a :class:`TextHealth`."""
        ...

    @abstractmethod
    def upload_image(
        self, instance: Instance | None, local_path: Path, cfg: dict[str, object]
    ) -> str:
        """Upload one local PNG/JPEG to the pod; return its pod-side path."""
        ...

    @abstractmethod
    def complete(
        self,
        instance: Instance | None,
        job: TextJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> TextResult:
        """Run one chat completion on the pod and return its result."""
        ...

    @abstractmethod
    def validate_spec(self, job: TextJob) -> None:
        """Raise ``ValidationError`` on a job this engine cannot serve."""
        ...

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Sink-filename slug (e.g. ``"Qwen3-0.6B"``). MUST NOT raise on missing fields."""
        ...

    def attach_get_instance(self, get_instance: Callable[[str], Instance]) -> None:
        """Wire provider lookup; mirrors UpscalerEngine.attach_get_instance."""
        self._get_instance = get_instance  # noqa: SLF001

    def attach_boot_liveness_probe(self, probe: BootLivenessProbe | None) -> None:
        """Store the boot-liveness probe; mirrors UpscalerEngine's setter."""
        self._boot_liveness_probe = probe  # noqa: SLF001
```

- [ ] **Step 4: Add the registry trio**

In `src/kinoforge/core/registry.py`, add `TextEngine` to the `from kinoforge.core.interfaces import (...)` block (line 20), add `_text_engines: dict[str, Callable[[], TextEngine]] = {}` beside the other registries (line ~40), and append after `interpolator_names`:

```python
# ---------------------------------------------------------------------------
# Text engines — `kinoforge text` (design §5.1). Own namespace, like image
# engines: a text-engine name may legitimately collide with a video engine's.
# ---------------------------------------------------------------------------


def register_text_engine(name: str, factory: Callable[[], TextEngine]) -> None:
    """Register a text-engine factory under ``name``.

    Duplicate registration is rejected, as for upscalers: an adapter
    import-order accident must surface loudly rather than silently rebind the
    production engine.

    Args:
        name: Registry key (e.g. ``"transformers"``).
        factory: Zero-arg callable returning a :class:`TextEngine`.

    Raises:
        UnknownAdapter: ``name`` is already registered.
    """
    if name in _text_engines:
        raise UnknownAdapter(f"text engine {name!r} already registered")
    _text_engines[name] = factory


def get_text_engine(name: str) -> Callable[[], TextEngine]:
    """Return the factory registered under ``name``.

    Raises:
        UnknownAdapter: No text engine registered under ``name``.
    """
    try:
        return _text_engines[name]
    except KeyError:
        raise UnknownAdapter(
            f"no text engine registered as {name!r}; known: {sorted(_text_engines)}"
        ) from None


def text_engine_names() -> list[str]:
    """Return the registered text-engine names, sorted."""
    return sorted(_text_engines)
```

- [ ] **Step 5: Add the error**

Append to `src/kinoforge/core/errors.py`:

```python
class TextGenerationFailed(KinoforgeError):
    """The pod's text server reported ``state == "error"`` for a job.

    Attributes:
        job_id: The pod-assigned job id.
        server_error: The server's ``error`` string, verbatim.
    """

    def __init__(self, job_id: str, server_error: str) -> None:
        self.job_id = job_id
        self.server_error = server_error
        super().__init__(f"text job {job_id} failed on the pod: {server_error}")
```

- [ ] **Step 6: Run to confirm green, then lint**

Run: `pixi run pytest tests/core/test_text_interfaces.py -q` → 6 passed.
Run: `pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py src/kinoforge/core/errors.py tests/core/test_text_interfaces.py` → all passed. Also run `pixi run pytest tests/core -q -x` to confirm no existing interface/registry test regressed (the `test_core_invariant` and registry tests are the likely ones).

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py src/kinoforge/core/errors.py tests/core/test_text_interfaces.py
git commit -m "feat(core): TextEngine seam, t2t/it2t modes, text-engine registry"
```

---

### Task 2: `TextConfig`, the text-config validator, and the `text` stage term

**Goal:** `text:` block parses; the §3.3 rules refuse the inert/lying shapes with messages naming the key; `Config.capability_key().stages == ("text",)` so `_cfg_want_stages` wants `text`.

**Files:**
- Modify: `src/kinoforge/core/config.py` (new `TextConfig` after `InterpolateConfig`; `Config.text` after `interpolate` at line 1528; `_validate_cross_fields` after the `KNOWN_ENGINES` check at ~line 1648; `capability_key` after the interpolate block at ~line 1816)
- Test: `tests/core/test_text_config.py`

**Acceptance Criteria:**
- [ ] A valid text config loads with `cfg.text.engine == "transformers"`, `cfg.text.port == 8000`.
- [ ] One refusal per rule (non-diffusers engine; missing/empty `supported_modes`; a non-text mode; top-level `mode:`; each of `upscale`/`interpolate`/`keyframe`/`loras`; zero or two base models), each raising `ConfigError` whose message names the offending key or rule.
- [ ] `cfg.capability_key().stages == ("text",)` and `_cfg_want_stages(cfg) == ("text",)`; a video config's key is unchanged (existing `tests/core/test_capability_key_stages.py` stays green).

**Verify:** `pixi run pytest tests/core/test_text_config.py tests/core/test_capability_key_stages.py tests/core/test_config.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_text_config.py
"""The `text:` block (design §3): shape, the §3.3 refusals, the stage term."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import ConfigError

_BASE: dict[str, Any] = {
    "engine": {
        "kind": "diffusers",
        "precision": "bf16",
        "diffusers": {
            "server_cmd": ["python", "-m", "kinoforge.engines.diffusers.servers.text_server"],
            "capability": {"supported_modes": ["t2t"]},
        },
    },
    "models": [{"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}],
    "text": {"engine": "transformers", "params": {"max_new_tokens": 32}},
    "compute": {"provider": "fake", "image": "fake:latest"},
}


def _cfg(**overrides: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_BASE)
    raw.update(overrides)
    return raw


def test_valid_text_config_loads() -> None:
    """Bug caught: `text:` rejected as an unknown key, or port defaulting wrongly."""
    cfg = Config.model_validate(_cfg())
    assert cfg.text is not None
    assert cfg.text.engine == "transformers"
    assert cfg.text.port == 8000
    assert cfg.text.params == {"max_new_tokens": 32}
    assert cfg.text.system is None


def test_text_block_forbids_unknown_keys() -> None:
    """Bug caught: pydantic's default extra='ignore' dropping a typo'd `prmpt:`."""
    raw = _cfg()
    raw["text"]["prmpt"] = "x"
    with pytest.raises((ConfigError, ValueError), match="prmpt"):
        Config.model_validate(raw)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda r: r["engine"].__setitem__("kind", "comfyui"), "engine.kind == 'diffusers'"),
        (lambda r: r["engine"]["diffusers"].pop("capability"), "capability.supported_modes"),
        (
            lambda r: r["engine"]["diffusers"]["capability"].__setitem__("supported_modes", []),
            "capability.supported_modes",
        ),
        (
            lambda r: r["engine"]["diffusers"]["capability"].__setitem__("supported_modes", ["t2v"]),
            "not text modes",
        ),
        (lambda r: r.__setitem__("mode", "t2t"), "top-level `mode:` must be absent"),
        (
            lambda r: r.__setitem__(
                "upscale",
                {"engine": "spandrel", "scale": "2x", "spandrel": {
                    "model_url": "hf:a/b/c.pth", "arch": "realesrgan", "precision": "fp16",
                    "tile_size": 512, "batch_size": 4}},
            ),
            "`upscale:` is not allowed",
        ),
        (
            lambda r: r.__setitem__("interpolate", {"engine": "rife", "fps": 60}),
            "`interpolate:` is not allowed",
        ),
        (
            lambda r: r.__setitem__(
                "keyframe", {"engine": "fake", "prompt": "p", "spec": {"model": "m"}}
            ),
            "`keyframe:` is not allowed",
        ),
        (
            lambda r: r.__setitem__("loras", [{"ref": "hf:a/b", "strength": 1.0}]),
            "`loras:` is not allowed",
        ),
        (lambda r: r.__setitem__("models", []), "exactly one `kind: base`"),
        (
            lambda r: r["models"].append({"kind": "base", "ref": "hf:x/y", "target": "checkpoints"}),
            "exactly one `kind: base`",
        ),
    ],
    ids=[
        "non-diffusers-engine", "no-capability", "empty-modes", "video-mode",
        "top-level-mode", "upscale", "interpolate", "keyframe", "loras",
        "zero-base", "two-base",
    ],
)
def test_each_text_rule_refuses_and_names_the_key(mutate: Any, needle: str) -> None:
    """One case per §3.3 rule. Bug caught: a validator that refuses everything
    for the wrong reason (the message is asserted, not just the raise)."""
    raw = _cfg()
    mutate(raw)
    with pytest.raises(ConfigError, match=needle):
        Config.model_validate(raw)


def test_text_config_wants_the_text_stage() -> None:
    """Bug caught: stages=() so the warm matcher attaches a text cfg to ANY
    pod with the same base model, or the U14 shape — a stage no pod advertises."""
    from kinoforge.cli._commands import _cfg_want_stages

    cfg = Config.model_validate(_cfg())
    assert cfg.capability_key().stages == ("text",)
    assert _cfg_want_stages(cfg) == ("text",)


def test_video_config_key_is_unchanged_by_the_text_branch() -> None:
    """Bug caught: the new branch appending 'text' when cfg.text is None."""
    raw = _cfg()
    raw.pop("text")
    raw["engine"]["diffusers"].pop("capability")
    cfg = Config.model_validate(raw)
    assert cfg.capability_key().stages == ()


def test_load_config_accepts_the_yaml_shape() -> None:
    """Bug caught: a shape that validates from a dict but not from YAML (e.g. a
    validator reading a raw key the YAML loader renames)."""
    cfg = load_config(
        "engine:\n"
        "  kind: diffusers\n"
        "  precision: bf16\n"
        "  diffusers:\n"
        "    capability:\n"
        "      supported_modes: [t2t, it2t]\n"
        "models:\n"
        "  - kind: base\n"
        "    ref: hf:HuggingFaceTB/SmolVLM-256M-Instruct\n"
        "    target: checkpoints\n"
        "text:\n"
        "  engine: transformers\n"
        "  system: Be terse.\n"
        "compute:\n"
        "  provider: fake\n"
        "  image: fake:latest\n"
    )
    assert cfg.text is not None and cfg.text.system == "Be terse."
```

- [ ] **Step 2: Run to confirm failure**

Run: `pixi run pytest tests/core/test_text_config.py -q`
Expected: the first test fails with a pydantic "extra inputs are not permitted" error on `text`.

- [ ] **Step 3: Add `TEXT_MODES`, `TextConfig`, `Config.text`**

In `src/kinoforge/core/config.py`, near `KNOWN_ENGINES` (line 72):

```python
#: Modes `kinoforge text` can derive (design §2.1): t2t = text-generation,
#: it2t = image-text-to-text. The validator below bounds
#: `capability.supported_modes` on a text config to this set.
TEXT_MODES: frozenset[str] = frozenset({"t2t", "it2t"})
```

After `class InterpolateConfig` (before `class Config`):

```python
class TextConfig(BaseModel):
    """Top-level ``text:`` block; presence routes ``kinoforge text`` to a TextStage.

    Coexists with ``engine:`` / ``models:`` / ``compute:`` the way ``upscale:``
    does — a text config IS a pod config. Design:
    ``docs/superpowers/specs/2026-10-04-text-command-design.md`` §3.

    Attributes:
        engine: Text-engine registry key (``"transformers"``).
        prompt: Optional default prompt. ``--prompt`` overrides it; it
            overrides the top-level ``prompt:``.
        system: Optional system turn sent ahead of the user turn.
        params: Opaque pass-through to the pod's ``generate()`` call; a
            ``chat_template_kwargs`` mapping inside it goes to the chat template.
        port: Pod port the text server listens on. Only the default is
            exercised today; the hooks spec sets it for a sidecar launch.
    """

    engine: str
    prompt: str | None = None
    system: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    port: int = 8000
    model_config = ConfigDict(extra="forbid")
```

In `class Config`, after `interpolate: InterpolateConfig | None = None` (line 1528):

```python
    text: TextConfig | None = None
```

- [ ] **Step 4: Add the validator**

In `_validate_cross_fields`, immediately after the `KNOWN_ENGINES` check (so `self.engine` is non-None), insert:

```python
        if self.text is not None:
            self._validate_text_block()
```

and add the method to `Config` (next to `_validate_cross_fields`):

```python
    def _validate_text_block(self) -> None:
        """Refuse the text-config shapes that would be silently inert or lie.

        Design §3.3. Every message names the offending key or rule, because a
        validator that refuses for the wrong reason is the bug the tests look
        for. Raises :class:`ConfigError` directly (as ``UpscaleConfig`` does),
        so the type survives pydantic unwrapped.

        Raises:
            ConfigError: One of the §3.3 rules is violated.
        """
        assert self.engine is not None  # noqa: S101 — caller checked
        assert self.text is not None  # noqa: S101 — caller checked
        if self.engine.kind != "diffusers":
            raise ConfigError(
                f"text: requires engine.kind == 'diffusers' (got "
                f"{self.engine.kind!r}); only the diffusers engine provisions "
                "an arbitrary server_cmd"
            )
        cap = self.engine.diffusers.capability if self.engine.diffusers else None
        declared = set(cap.supported_modes or []) if cap is not None else set()
        if not declared:
            raise ConfigError(
                "text: requires engine.diffusers.capability.supported_modes to "
                "declare the modes the model serves (['t2t'] or ['t2t', 'it2t']); "
                "without it the shared diffusers probe says ['t2v'] and every "
                "`kinoforge text` run is refused for the wrong reason"
            )
        unknown = sorted(declared - TEXT_MODES)
        if unknown:
            raise ConfigError(
                f"text: capability.supported_modes {unknown} are not text modes; "
                f"allowed: {sorted(TEXT_MODES)}"
            )
        if self.mode is not None:
            raise ConfigError(
                "text: top-level `mode:` must be absent — `kinoforge text` derives "
                "t2t/it2t from --image, so a written mode is documentation that "
                "can lie"
            )
        for key in ("upscale", "interpolate", "keyframe"):
            if getattr(self, key) is not None:
                raise ConfigError(
                    f"text: `{key}:` is not allowed in a text config — it would be "
                    "silently inert on a text pod"
                )
        if self.loras:
            raise ConfigError(
                "text: `loras:` is not allowed in a text config — it would be "
                "silently inert on a text pod"
            )
        base_count = sum(1 for e in self.models if e.kind == "base")
        if base_count != 1:
            raise ConfigError(
                f"text: models must contain exactly one `kind: base` entry "
                f"(found {base_count}); the text server loads one checkpoint"
            )
```

Note: the `models: []` case must reach this method before the existing `base_count == 0` rule raises its generic message — if the parametrised `zero-base` case fails on the generic message, move the `self.text` call above the base-count block (it is already above it when inserted right after the `KNOWN_ENGINES` check).

- [ ] **Step 5: Add the stage term**

In `Config.capability_key`, after the `interpolate` block (line ~1816, before `return CapabilityKey(`):

```python
        if self.text is not None:
            # `kinoforge text` pods advertise "text" in /health capabilities
            # (servers/text_server.py); the warm matcher reads the want-set
            # from here via _cfg_want_stages, so the two must move together.
            stages.append("text")
```

- [ ] **Step 6: Run to confirm green, then the neighbours**

Run: `pixi run pytest tests/core/test_text_config.py -q` → 16 passed.
Run: `pixi run pytest tests/core/test_capability_key_stages.py tests/core/test_config.py tests/core/test_config_validation_integration.py tests/cli/test_shipped_cfg_want_stages_sweep.py -q` → pass (no text config is shipped yet, so the sweep is unaffected).
Run: `pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_text_config.py`.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/config.py tests/core/test_text_config.py
git commit -m "feat(config): text: block, the §3.3 refusals, and the text stage term"
```

---

### Task 3: `core/text_request.py` — pure request helpers

**Goal:** One I/O-free module holding the mode derivation, prompt precedence, the pre-spend gate message, `--image` argument checks, request building, and base-ref/declared-modes lookups over both `Config` and the cfg dict (the engine and stage see dicts).

**Files:**
- Create: `src/kinoforge/core/text_request.py`
- Test: `tests/core/test_text_request.py`

**Acceptance Criteria:**
- [ ] `derive_mode(0) == "t2t"`, `derive_mode(2) == "it2t"`.
- [ ] Prompt precedence: CLI > `text.prompt` > top-level `prompt` > `ValidationError` naming all three sources; whitespace-only counts as absent.
- [ ] `preflight_mode_error(cfg, "it2t")` on a `["t2t"]` config returns a string naming the model ref, the declared modes and `it2t`; returns `None` when declared.
- [ ] `image_arg_error`: empty path, bad suffix, missing file each return a message naming the fault; a real `.png` returns `None`.
- [ ] `build_request` assigns roles `image_1 … image_N` in order, `kind="image"`, and the derived mode.
- [ ] `base_model_ref` / `declared_modes` work on the cfg **dict** (what `_cfg_dict(cfg)` yields).

**Verify:** `pixi run pytest tests/core/test_text_request.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_text_request.py
"""Pure helpers behind `kinoforge text` (design §2.1, §2.2, §4.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact
from kinoforge.core.text_request import (
    base_model_ref,
    build_request,
    declared_modes,
    derive_mode,
    image_arg_error,
    preflight_mode_error,
    resolve_prompt,
)


def _cfg(modes: list[str], *, text_prompt: str | None = None, top_prompt: str | None = None) -> Config:
    raw: dict[str, Any] = {
        "engine": {
            "kind": "diffusers",
            "precision": "bf16",
            "diffusers": {"capability": {"supported_modes": modes}},
        },
        "models": [{"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}],
        "text": {"engine": "transformers"},
        "compute": {"provider": "fake", "image": "fake:latest"},
    }
    if text_prompt is not None:
        raw["text"]["prompt"] = text_prompt
    if top_prompt is not None:
        raw["prompt"] = top_prompt
    return Config.model_validate(raw)


def test_mode_is_derived_from_the_image_count() -> None:
    """Bug caught: a typed --mode leaking back in, or it2t for zero images."""
    assert derive_mode(0) == "t2t"
    assert derive_mode(1) == "it2t"
    assert derive_mode(3) == "it2t"


@pytest.mark.parametrize(
    ("cli", "block", "top", "expected"),
    [
        ("cli", "block", "top", "cli"),
        (None, "block", "top", "block"),
        (None, None, "top", "top"),
        ("   ", None, "top", "top"),
    ],
    ids=["cli-wins", "block-over-top", "top-alone", "whitespace-cli-is-absent"],
)
def test_prompt_precedence(cli: str | None, block: str | None, top: str | None, expected: str) -> None:
    """Bug caught: config winning over the CLI, or '   ' accepted as a prompt."""
    assert resolve_prompt(_cfg(["t2t"], text_prompt=block, top_prompt=top), cli) == expected


def test_all_prompt_sources_absent_is_refused_naming_all_three() -> None:
    """Bug caught: a KeyError or a message that names only one of the fixes."""
    with pytest.raises(ValidationError, match=r"--prompt.*text\.prompt.*prompt:"):
        resolve_prompt(_cfg(["t2t"]), None)


def test_images_against_a_text_only_model_are_refused_pre_spend() -> None:
    """Bug caught: the gate reading the pod instead of the declaration, or a
    message that does not say which model and which modes."""
    err = preflight_mode_error(_cfg(["t2t"]), "it2t")
    assert err is not None
    assert "hf:Qwen/Qwen3-0.6B" in err
    assert "['t2t']" in err
    assert "it2t" in err
    assert preflight_mode_error(_cfg(["t2t", "it2t"]), "it2t") is None
    assert preflight_mode_error(_cfg(["t2t"]), "t2t") is None


def test_image_arg_errors_name_the_fault(tmp_path: Path) -> None:
    """Bug caught: a missing file passing the suffix check and failing on the pod."""
    good = tmp_path / "a.png"
    good.write_bytes(b"\x89PNG")
    assert image_arg_error(str(good)) is None
    assert "empty" in (image_arg_error("") or "")
    assert ".png/.jpg/.jpeg" in (image_arg_error(str(tmp_path / "a.gif")) or "")
    assert "not found" in (image_arg_error(str(tmp_path / "missing.png")) or "")


def test_build_request_numbers_roles_in_flag_order() -> None:
    """Bug caught: a shared role name (duplicate-role refusal downstream) or
    sorted-by-path order."""
    arts = [Artifact(uri="file:///z.png", sha256="z"), Artifact(uri="file:///a.jpg", sha256="a")]
    req = build_request("describe", arts)
    assert req.mode == "it2t"
    assert [a.role for a in req.assets] == ["image_1", "image_2"]
    assert [a.ref.uri for a in req.assets] == ["file:///z.png", "file:///a.jpg"]
    assert {a.kind for a in req.assets} == {"image"}
    assert build_request("hi", []).mode == "t2t"


def test_dict_lookups_match_the_config_lookups() -> None:
    """Bug caught: the engine/stage (which see cfg dicts) disagreeing with the
    CLI (which sees Config) about the model or the declared modes."""
    cfg = _cfg(["t2t", "it2t"])
    raw = cfg.model_dump(mode="json")
    assert base_model_ref(raw) == "hf:Qwen/Qwen3-0.6B"
    assert declared_modes(raw) == frozenset({"t2t", "it2t"})
    assert declared_modes(cfg) == frozenset({"t2t", "it2t"})
    with pytest.raises(ValidationError, match="kind: base"):
        base_model_ref({"models": []})
```

- [ ] **Step 2: Run to confirm failure**

Run: `pixi run pytest tests/core/test_text_request.py -q` → ImportError on `kinoforge.core.text_request`.

- [ ] **Step 3: Write the module**

```python
# src/kinoforge/core/text_request.py
"""Pure helpers behind ``kinoforge text`` — no I/O, no registry, no pod.

Design: ``docs/superpowers/specs/2026-10-04-text-command-design.md`` §2.1 (mode
derivation), §2.2 (prompt precedence), §4.1 (the pre-spend gate). Lives in
``core`` so the CLI handler, the engine and the stage share ONE copy of each
rule; the engine and the stage see the cfg as a dict (``_cfg_dict``), the CLI
sees a :class:`Config`, so the lookups accept both.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kinoforge.core.config import TEXT_MODES, Config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact, ConditioningAsset, GenerationRequest

__all__ = [
    "IMAGE_SUFFIXES",
    "TEXT_MODES",
    "base_model_ref",
    "build_request",
    "declared_modes",
    "derive_mode",
    "image_arg_error",
    "preflight_mode_error",
    "resolve_prompt",
]

#: The suffixes ``PodHTTPClientMixin._upload_source(media="image")`` accepts.
IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})

_VISION_EXAMPLE = "examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml"


def derive_mode(image_count: int) -> str:
    """Return ``"it2t"`` when any image is supplied, else ``"t2t"``.

    Args:
        image_count: Number of ``--image`` flags.

    Returns:
        The request mode (design §2.1: derived, never typed).
    """
    return "it2t" if image_count > 0 else "t2t"


def resolve_prompt(cfg: Config, prompt_override: str | None) -> str:
    """Resolve the prompt: CLI > ``text.prompt`` > top-level ``prompt``.

    Whitespace-only candidates count as absent, matching ``image_run``.

    Args:
        cfg: The loaded config; ``cfg.text`` must be set.
        prompt_override: The ``--prompt`` value, if any.

    Returns:
        The non-empty prompt to submit.

    Raises:
        ValidationError: None of the three sources supplied a prompt.
    """
    block_prompt = cfg.text.prompt if cfg.text is not None else None
    for candidate in (prompt_override, block_prompt, cfg.prompt):
        if candidate and candidate.strip():
            return candidate
    raise ValidationError(
        "no prompt to generate from: pass --prompt, or set `text.prompt` "
        "(or top-level `prompt:`) in the config"
    )


def _as_mapping(cfg: Config | Mapping[str, Any]) -> Mapping[str, Any]:
    return cfg.model_dump(mode="json") if isinstance(cfg, Config) else cfg


def base_model_ref(cfg: Config | Mapping[str, Any]) -> str:
    """Return the ``models[kind: base]`` ref (``hf:...``).

    Raises:
        ValidationError: No base entry (the config validator forbids this for
            text configs, so hitting it means a hand-built dict).
    """
    for entry in _as_mapping(cfg).get("models") or []:
        if entry.get("kind") == "base":
            return str(entry["ref"])
    raise ValidationError("text: models has no `kind: base` entry")


def declared_modes(cfg: Config | Mapping[str, Any]) -> frozenset[str]:
    """Return ``engine.diffusers.capability.supported_modes`` as a set.

    Empty when undeclared — the config validator refuses that for text
    configs, so an empty return here means a hand-built dict.
    """
    engine = _as_mapping(cfg).get("engine") or {}
    diffusers = engine.get("diffusers") or {}
    capability = diffusers.get("capability") or {}
    return frozenset(str(m) for m in capability.get("supported_modes") or [])


def preflight_mode_error(cfg: Config | Mapping[str, Any], mode: str) -> str | None:
    """The pre-spend gate (design §4.1): refuse a mode the config did not declare.

    Args:
        cfg: The loaded config.
        mode: The derived request mode.

    Returns:
        ``None`` when *mode* is declared; else a complete ``error: ...`` line
        naming the model, the declared modes and both fixes.
    """
    declared = declared_modes(cfg)
    if mode in declared:
        return None
    model = base_model_ref(cfg)
    if mode == "it2t":
        return (
            f"error: model {model} declares modes {sorted(declared)}; --image needs "
            f"it2t. Either drop --image, or use a vision-language config such as "
            f"{_VISION_EXAMPLE}"
        )
    return (
        f"error: model {model} declares modes {sorted(declared)}, which does not "
        f"include {mode!r}; declare it under engine.diffusers.capability."
        f"supported_modes, or pass --image for an it2t request"
    )


def image_arg_error(path: str) -> str | None:
    """Validate one ``--image`` argument without touching a pod.

    Returns:
        ``None`` when *path* names an existing ``.png``/``.jpg``/``.jpeg``
        file; else an ``error: ...`` line naming the fault.
    """
    if not path:
        return "error: --image must name a file (got an empty path)"
    p = Path(path)
    if p.suffix.lower() not in IMAGE_SUFFIXES:
        return f"error: --image {path!r}: only .png/.jpg/.jpeg are accepted"
    if not p.is_file():
        return f"error: --image {path!r}: file not found"
    return None


def build_request(prompt: str, images: list[Artifact]) -> GenerationRequest:
    """Wrap the prompt and image artifacts as a ``GenerationRequest``.

    Roles are ``image_1 … image_N`` in flag order (design §2.1); the mode is
    derived from the count.

    Args:
        prompt: The resolved prompt.
        images: Local-file artifacts from ``_resolve_input_as_artifact``.

    Returns:
        The request ``TextStage`` reads from ``PipelineState``.
    """
    assets = [
        ConditioningAsset(kind="image", role=f"image_{i}", ref=art)
        for i, art in enumerate(images, start=1)
    ]
    return GenerationRequest(prompt=prompt, mode=derive_mode(len(images)), assets=assets)
```

- [ ] **Step 4: Run to confirm green; lint; commit**

Run: `pixi run pytest tests/core/test_text_request.py -q` → 11 passed.
Run: `pixi run pre-commit run --files src/kinoforge/core/text_request.py tests/core/test_text_request.py`.
Also run `pixi run pytest tests/core/test_core_invariant.py -q` (or the test that enforces `core/` imports no adapters — `rg -ln 'core_invariant' tests` to find it) → pass.

```bash
git add src/kinoforge/core/text_request.py tests/core/test_text_request.py
git commit -m "feat(core): text_request helpers — mode derivation, prompt precedence, pre-spend gate"
```

---

### Task 4: Pod-side server — `_upload.py` + `text_server.py`

**Goal:** A lean FastAPI server (H3 skeleton, none of its body) that loads one checkpoint, derives its modes from the processor, serves `/health`, `/util`, `PUT /upload`, `POST /text`, `GET /text/status/{id}`, and is fully unit-tested in the controller env through an injected fake loader. The warm-matcher stage vocabulary gains `"text"`.

**Files:**
- Create: `src/kinoforge/engines/diffusers/servers/_upload.py`
- Create: `src/kinoforge/engines/diffusers/servers/text_server.py`
- Modify: `tests/cli/test_shipped_cfg_want_stages_sweep.py` (line 33, `ADVERTISABLE_STAGES`)
- Test: `tests/engines/diffusers/test_text_server.py`, `tests/engines/diffusers/test_upload_module.py`

**Acceptance Criteria:**
- [ ] `/health` reports `supported_modes == ["t2t"]` for a processor without `image_processor` and `["t2t", "it2t"]` with one; `capabilities == ["text", "upload"]` once ready.
- [ ] `POST /text` with `images` on a `t2t`-only server → 400 and `jobs` stays empty; an image path outside the upload dir → 400.
- [ ] A queued job reaches `state == "done"` via the real worker thread; `result` carries `text`, `finish_reason`, `usage.{prompt_tokens,completion_tokens}`, `model`; `finish_reason == "length"` when the completion fills `max_new_tokens`.
- [ ] Chat assembly: system turn present only when set; for it2t the user content lists every image before the text; `chat_template_kwargs` reach `apply_chat_template` and not `generate()`; for t2t the user content is a plain string.
- [ ] `PUT /upload`: PNG body → 200 with matching sha256 and a path under the upload dir; `video/mp4` → 415; `X-Filename: ../../etc/passwd` → basename `passwd` under the upload dir; oversize → 413 and no `.part` file left.
- [ ] `ADVERTISABLE_STAGES == {"t2v", "upscale", "interpolate", "text"}`.
- [ ] `pixi run python -c "import kinoforge.engines.diffusers.servers.text_server"` succeeds in the default env (no torch).

**Verify:** `pixi run pytest tests/engines/diffusers/test_text_server.py tests/engines/diffusers/test_upload_module.py tests/cli/test_shipped_cfg_want_stages_sweep.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the failing upload-module tests**

```python
# tests/engines/diffusers/test_upload_module.py
"""servers/_upload.py: the PUT /upload contract _upload_source cross-checks."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import _upload

_TYPES = frozenset({"image/png", "image/jpeg"})


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    upload_dir = tmp_path / "uploads"

    @app.put("/upload")
    async def handler(request: Request) -> dict:  # type: ignore[type-arg]
        return await _upload.receive_upload(
            request,
            upload_dir=upload_dir,
            content_types=_TYPES,
            max_bytes=4096,
            fallback_suffix=".png",
        )

    return TestClient(app)


def test_upload_streams_body_and_reports_sha(client: TestClient, tmp_path: Path) -> None:
    """Bug caught: sha computed over the tempfile name or a truncated body —
    _upload_source raises UploadIntegrityError on any mismatch."""
    body = bytes(i % 256 for i in range(2048))
    resp = client.put("/upload", content=body, headers={"Content-Type": "image/png", "X-Filename": "a1b2c3d4.png"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()
    assert payload["size"] == 2048
    written = Path(payload["path"])
    assert written.read_bytes() == body
    assert written.parent == tmp_path / "uploads"
    assert written.name == "a1b2c3d4.png"


def test_wrong_content_type_is_415(client: TestClient) -> None:
    """Bug caught: the text server silently accepting an mp4 it cannot use."""
    resp = client.put("/upload", content=b"xx", headers={"Content-Type": "video/mp4", "X-Filename": "a.mp4"})
    assert resp.status_code == 415


def test_filename_is_reduced_to_a_safe_basename(client: TestClient, tmp_path: Path) -> None:
    """Bug caught: path traversal via X-Filename."""
    resp = client.put("/upload", content=b"xx", headers={"Content-Type": "image/png", "X-Filename": "../../etc/passwd"})
    assert resp.status_code == 200
    written = Path(resp.json()["path"])
    assert written.name == "passwd"
    assert written.parent == tmp_path / "uploads"


def test_missing_filename_gets_a_random_png_name(client: TestClient) -> None:
    """Bug caught: an empty basename → os.replace onto the directory itself."""
    resp = client.put("/upload", content=b"xx", headers={"Content-Type": "image/png"})
    assert resp.status_code == 200
    assert Path(resp.json()["path"]).name.endswith(".png")


def test_oversize_body_is_413_and_leaves_no_partial(client: TestClient, tmp_path: Path) -> None:
    """Bug caught: the .part tempfile surviving a rejected upload and filling the pod disk."""
    resp = client.put("/upload", content=b"x" * 5000, headers={"Content-Type": "image/png", "X-Filename": "big.png"})
    assert resp.status_code == 413
    assert list((tmp_path / "uploads").glob("*.part")) == []
    assert not (tmp_path / "uploads" / "big.png").exists()


def test_sanitize_filename_rules() -> None:
    """Bug caught: a cleaned-to-empty name returned as '' instead of the fallback."""
    assert _upload.sanitize_filename("dir/sub/ok-1.PNG", fallback_suffix=".png") == "ok-1.PNG"
    assert _upload.sanitize_filename("///", fallback_suffix=".png").endswith(".png")
    assert _upload.sanitize_filename(None, fallback_suffix=".jpg").endswith(".jpg")
```

- [ ] **Step 2: Write the failing server tests**

```python
# tests/engines/diffusers/test_text_server.py
"""servers/text_server.py: mode derivation, gates, chat assembly, status schema.

Runs in the controller env (no torch) through the `_LOADER` seam; jobs are
executed by the server's REAL worker thread so the threading the pod uses is
what is under test.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


class _FakeProcessor:
    """Records the chat-template and encode calls; returns 4 prompt tokens."""

    def __init__(self, *, with_images: bool) -> None:
        self.image_processor = object() if with_images else None
        self.calls: list[tuple[str, Any, Any]] = []

    def apply_chat_template(self, messages: Any, *, add_generation_prompt: bool, tokenize: bool, **kw: Any) -> str:
        assert add_generation_prompt is True
        assert tokenize is False
        self.calls.append(("template", messages, kw))
        return "TEMPLATE"

    def __call__(self, *, text: Any, images: Any = None, return_tensors: str) -> dict[str, Any]:
        self.calls.append(("encode", text, images))
        return {"input_ids": np.zeros((1, 4), dtype=np.int64)}

    def decode(self, ids: Any, *, skip_special_tokens: bool) -> str:
        return f"fake completion of {len(ids)} tokens"


class _FakeModel:
    device = None

    def __init__(self) -> None:
        self.generate_kwargs: dict[str, Any] | None = None

    def generate(self, *, input_ids: Any, **kw: Any) -> Any:
        self.generate_kwargs = kw
        n_prompt = input_ids.shape[1]
        n_new = min(3, int(kw.get("max_new_tokens", 3)))
        return np.zeros((1, n_prompt + n_new), dtype=np.int64)


def _make_loader(*, with_images: bool, holder: dict[str, Any]):  # noqa: ANN202
    def _load(model_id: str):  # noqa: ANN202
        srv = holder["srv"]
        proc = _FakeProcessor(with_images=with_images)
        model = _FakeModel()
        holder["proc"], holder["model"] = proc, model
        return srv.LoadedModel(
            model=model, processor=proc, supported_modes=srv.modes_for(proc), model_id=model_id
        )

    return _load


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Reload the server with a fake loader; yield {srv, client, proc, model, upload_dir}."""
    monkeypatch.setenv("KINOFORGE_TEXT_MODEL_ID", "fake/model")
    monkeypatch.setenv("KINOFORGE_UPLOAD_DIR", str(tmp_path / "uploads"))
    from kinoforge.engines.diffusers.servers import text_server as srv

    srv = importlib.reload(srv)
    # The uploaded "PNG" bytes are not a real image; bypass PIL (pod-only) and
    # hand the chat assembly opaque tokens instead.
    monkeypatch.setattr(srv, "_open_images", lambda paths: [f"IMG:{p}" for p in paths])
    holder: dict[str, Any] = {"srv": srv, "upload_dir": tmp_path / "uploads"}
    holder["set_loader"] = lambda with_images: monkeypatch.setattr(
        srv, "_LOADER", _make_loader(with_images=with_images, holder=holder)
    )
    yield holder
    srv.jobs.clear()


def _start(server: dict[str, Any], *, with_images: bool) -> TestClient:
    server["set_loader"](with_images)
    client = TestClient(server["srv"].app)
    client.__enter__()  # runs the startup hook: loader + worker thread + ready
    server["client"] = client
    return client


def _wait_done(client: TestClient, job_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = client.get(f"/text/status/{job_id}").json()
        if payload["state"] in {"done", "error"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("job never finished")


def _upload_png(client: TestClient, name: str = "ab12cd34.png") -> str:
    resp = client.put("/upload", content=b"\x89PNG fake", headers={"Content-Type": "image/png", "X-Filename": name})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["path"])


def test_modes_are_derived_from_the_processor(server: dict[str, Any]) -> None:
    """Bug caught: the server echoing the config's declaration instead of
    deriving — the whole point of the pod-side half of the gate (§4.2)."""
    srv = server["srv"]
    assert srv.modes_for(_FakeProcessor(with_images=False)) == ("t2t",)
    assert srv.modes_for(_FakeProcessor(with_images=True)) == ("t2t", "it2t")


def test_health_reports_modes_and_the_closed_capability_vocabulary(server: dict[str, Any]) -> None:
    """Bug caught: capabilities drifting from the hand-transcribed ADVERTISABLE_STAGES."""
    client = _start(server, with_images=True)
    payload = client.get("/health").json()
    assert payload["ready"] is True
    assert payload["model"] == "fake/model"
    assert payload["supported_modes"] == ["t2t", "it2t"]
    assert payload["capabilities"] == ["text", "upload"]
    assert payload["default_max_new_tokens"] == 256


def test_images_against_a_text_only_model_are_400_before_queueing(server: dict[str, Any]) -> None:
    """Bug caught: a 400 raised from the worker thread, after the queue."""
    client = _start(server, with_images=False)
    path = _upload_png(client)
    resp = client.post("/text", json={"prompt": "describe", "images": [path]})
    assert resp.status_code == 400
    assert "no image path" in resp.json()["detail"]
    assert server["srv"].jobs == {}


def test_image_paths_outside_the_upload_dir_are_400(server: dict[str, Any], tmp_path: Path) -> None:
    """Bug caught: /text reading an arbitrary pod file as an image."""
    client = _start(server, with_images=True)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"x")
    resp = client.post("/text", json={"prompt": "p", "images": [str(outside)]})
    assert resp.status_code == 400


def test_t2t_job_runs_to_done_with_the_submit_and_poll_schema(server: dict[str, Any]) -> None:
    """Bug caught: a `status`/`filename` (generate-style) payload the
    submit_and_poll client cannot read; usage counts wrong."""
    client = _start(server, with_images=False)
    job_id = client.post("/text", json={"prompt": "hello", "params": {"max_new_tokens": 8}}).json()["job_id"]
    payload = _wait_done(client, job_id)
    assert payload["state"] == "done"
    result = payload["result"]
    assert result["text"] == "fake completion of 3 tokens"
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"prompt_tokens": 4, "completion_tokens": 3}
    assert result["model"] == "fake/model"
    assert result["max_new_tokens"] == 8


def test_filling_max_new_tokens_reports_length(server: dict[str, Any]) -> None:
    """Bug caught: finish_reason always 'stop', hiding truncated answers."""
    client = _start(server, with_images=False)
    job_id = client.post("/text", json={"prompt": "hello", "params": {"max_new_tokens": 3}}).json()["job_id"]
    assert _wait_done(client, job_id)["result"]["finish_reason"] == "length"


def test_t2t_chat_is_a_plain_string_user_turn(server: dict[str, Any]) -> None:
    """Bug caught: list-shaped content handed to a text tokenizer's template,
    which renders the Python repr of the list into the prompt."""
    client = _start(server, with_images=False)
    job_id = client.post("/text", json={"prompt": "hello"}).json()["job_id"]
    _wait_done(client, job_id)
    kind, messages, _ = server["proc"].calls[0]
    assert kind == "template"
    assert messages == [{"role": "user", "content": "hello"}]


def test_it2t_chat_interleaves_images_before_text_and_routes_kwargs(server: dict[str, Any]) -> None:
    """Bug caught: chat_template_kwargs leaking into generate() (TypeError on
    the pod), or the system turn appearing when none was given."""
    client = _start(server, with_images=True)
    p1, p2 = _upload_png(client, "11111111.png"), _upload_png(client, "22222222.png")
    body = {
        "prompt": "compare",
        "system": "Be terse.",
        "images": [p1, p2],
        "params": {"max_new_tokens": 5, "temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False}},
    }
    job_id = client.post("/text", json=body).json()["job_id"]
    _wait_done(client, job_id)
    _, messages, template_kwargs = server["proc"].calls[0]
    assert messages[0] == {"role": "system", "content": "Be terse."}
    user = messages[1]["content"]
    assert [c["type"] for c in user] == ["image", "image", "text"]
    assert user[-1]["text"] == "compare"
    assert template_kwargs == {"enable_thinking": False}
    _, text, images = server["proc"].calls[1]
    assert text == ["TEMPLATE"]
    assert len(images) == 2
    assert server["model"].generate_kwargs == {"max_new_tokens": 5, "temperature": 0.2}


def test_unknown_job_is_404(server: dict[str, Any]) -> None:
    client = _start(server, with_images=False)
    assert client.get("/text/status/nope").status_code == 404


def test_module_imports_without_torch() -> None:
    """Bug caught (U66's shape): a module-level torch/transformers/PIL import
    that only a pod can satisfy. The controller env has none of the three, so
    a successful import here IS the check; the sys.modules assertions make it
    explicit that nothing pulled them in as a side effect."""
    import importlib
    import sys

    srv = importlib.import_module("kinoforge.engines.diffusers.servers.text_server")
    assert hasattr(srv, "app")
    for forbidden in ("torch", "transformers", "PIL.Image"):
        assert forbidden not in sys.modules, f"{forbidden} imported at module scope"
```

- [ ] **Step 3: Run to confirm failure**

Run: `pixi run pytest tests/engines/diffusers/test_upload_module.py tests/engines/diffusers/test_text_server.py -q` → ImportError on both modules.

- [ ] **Step 4: Write `_upload.py`**

```python
# src/kinoforge/engines/diffusers/servers/_upload.py
"""Streaming ``PUT /upload`` handler for pod servers.

Extracted by SHAPE from ``wan_t2v_server``'s inline handler (which keeps its
own copy — design §6.4) so a second server can honour the exact contract
``engines/_pod_http.PodHTTPClientMixin._upload_source`` cross-checks:
``Content-Type`` from an allowed set (415 otherwise), ``X-Filename`` sanitised
to a basename in ``[A-Za-z0-9._-]``, body streamed into a tempfile under a size
cap (413 + cleanup), atomic ``os.replace``, and a ``{"path", "size", "sha256"}``
reply the client compares against its own digest.

Imports: stdlib + fastapi only. This module rides onto the pod via
``embed_files``; the pod has no ``kinoforge.core``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import string
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

#: Characters a client-supplied filename may keep.
FILENAME_ALLOWED: frozenset[str] = frozenset(string.ascii_letters + string.digits + "._-")


def sanitize_filename(raw: str | None, *, fallback_suffix: str) -> str:
    """Return a safe basename for *raw*, or a random ``<hex8><fallback_suffix>``.

    Args:
        raw: The client's ``X-Filename`` header; may be absent or dirty.
        fallback_suffix: Suffix for the random fallback name (``".png"``).

    Returns:
        A non-empty basename made only of :data:`FILENAME_ALLOWED` characters.
    """
    if not raw:
        return f"{secrets.token_hex(4)}{fallback_suffix}"
    base = Path(raw).name  # strips every path component
    cleaned = "".join(c for c in base if c in FILENAME_ALLOWED)
    if not cleaned:
        return f"{secrets.token_hex(4)}{fallback_suffix}"
    return cleaned


async def receive_upload(
    request: Request,
    *,
    upload_dir: Path,
    content_types: frozenset[str],
    max_bytes: int,
    fallback_suffix: str,
) -> dict[str, Any]:
    """Stream the body into *upload_dir*; return its path, size and sha256.

    Args:
        request: The incoming PUT.
        upload_dir: Pod-local directory; created ``0o700`` when absent.
        content_types: Accepted ``Content-Type`` values (media type only).
        max_bytes: Reject bodies longer than this with 413.
        fallback_suffix: Suffix for a missing/dirty ``X-Filename``.

    Returns:
        ``{"path": str, "size": int, "sha256": str}``.

    Raises:
        HTTPException: 415 on a disallowed Content-Type; 413 when the body
            exceeds *max_bytes* (the partial tempfile is removed first).
    """
    ct = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ct not in content_types:
        raise HTTPException(
            status_code=415,
            detail=f"Content-Type must be one of {sorted(content_types)}, got {ct!r}",
        )
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_name = sanitize_filename(request.headers.get("x-filename"), fallback_suffix=fallback_suffix)
    fd, tmp_name = tempfile.mkstemp(dir=str(upload_dir), suffix=".part")
    tmp_path = Path(tmp_name)
    hasher = hashlib.sha256()
    written = 0
    try:
        # kinoforge:public-write — pod-local scratch, never the operator's host.
        with os.fdopen(fd, "wb") as fobj:  # kinoforge:public-write
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail=f"upload exceeded {max_bytes} bytes")
                hasher.update(chunk)
                fobj.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    final = upload_dir / safe_name
    os.replace(tmp_path, final)
    return {"path": str(final), "size": written, "sha256": hasher.hexdigest()}
```

- [ ] **Step 5: Write `text_server.py`**

```python
# src/kinoforge/engines/diffusers/servers/text_server.py
"""FastAPI text-generation server for ``kinoforge text``.

Runs on the GPU pod. Loads ONE open-weight checkpoint with ``transformers`` and
serves chat completions over the ``submit_and_poll`` contract
(``engines/_pod_http.py``)::

  GET  /health                 -> {"ready", "model", "supported_modes", "capabilities", ...}
  GET  /util                   -> the five UtilSnapshot fields
  PUT  /upload                 -> {"path", "size", "sha256"}   (PNG / JPEG only)
  POST /text                   -> {"job_id"}
  GET  /text/status/{job_id}   -> {"state", "result"?, "error"?}

Built on the ``minimax_h3_server`` skeleton (queue, one worker thread, startup
ordering with ``ready.set()`` LAST) and none of its body. The completion
travels INLINE in ``result`` — no artifact file, no ``/artifacts`` route.

Modes are DERIVED at load, never declared: a checkpoint whose processor carries
an ``image_processor`` loads through ``AutoModelForImageTextToText`` and serves
``t2t`` + ``it2t``; anything else loads through ``AutoModelForCausalLM`` and
serves ``t2t`` only. ``/health`` reports that set so the controller can compare
it with what the config declared (design §4.2).

Import discipline: stdlib, fastapi, pydantic and the two sibling helpers at
module scope; ``torch`` / ``transformers`` / ``PIL`` ONLY inside functions. The
controller env has none of the three, and this module is unit-tested there
through ``_LOADER``. (U66: a module-level pod-only import is invisible to the
controller's suite and kills every pod at boot.)
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from kinoforge.engines.diffusers.servers._upload import receive_upload  # noqa: E402
from kinoforge.engines.diffusers.servers._util_stats import read_gpu_stats  # noqa: E402

_log = logging.getLogger("kinoforge.diffusers.text_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

#: Exported by TransformersTextEngine.render_provision; no default on purpose —
#: a missing export is a boot fault, not a silent fallback to some model.
MODEL_ID: str = os.environ.get("KINOFORGE_TEXT_MODEL_ID", "")
PORT: int = int(os.environ.get("KINOFORGE_TEXT_PORT", "8000"))
#: Reported in /health so a run is reproducible without reading this file.
DEFAULT_MAX_NEW_TOKENS = 256
_UPLOAD_DIR: Path = Path(os.environ.get("KINOFORGE_UPLOAD_DIR", "/tmp/kf-uploads"))  # noqa: S108 — pod-local scratch
_UPLOAD_CONTENT_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg"})
_UPLOAD_MAX_BYTES = int(os.environ.get("KINOFORGE_MAX_UPLOAD_MB", "64")) * 1024 * 1024
TEXT_ONLY_MODES: tuple[str, ...] = ("t2t",)
VISION_MODES: tuple[str, ...] = ("t2t", "it2t")


@dataclass
class LoadedModel:
    """What the loader hands the server.

    Attributes:
        model: The generation model (``generate()`` + ``device``).
        processor: Tokenizer or processor (``apply_chat_template``, ``__call__``, ``decode``).
        supported_modes: Derived from the processor — see :func:`modes_for`.
        model_id: The checkpoint id, echoed in ``/health`` and every result.
    """

    model: Any
    processor: Any
    supported_modes: tuple[str, ...]
    model_id: str


def modes_for(processor: Any) -> tuple[str, ...]:  # noqa: ANN401 — tokenizer or processor
    """Derive the served modes from the processor.

    ``AutoProcessor`` returns a tokenizer for a text-only checkpoint and a
    processor carrying an ``image_processor`` for a vision-language one; that
    attribute IS the derivation.
    """
    accepts_images = getattr(processor, "image_processor", None) is not None
    return VISION_MODES if accepts_images else TEXT_ONLY_MODES


def _load_transformers(model_id: str) -> LoadedModel:
    """Load *model_id* with transformers onto the CUDA device in bf16."""
    import torch
    from transformers import (
        AutoModelForCausalLM,
        AutoModelForImageTextToText,
        AutoProcessor,
    )

    processor = AutoProcessor.from_pretrained(model_id)
    modes = modes_for(processor)
    cls = AutoModelForImageTextToText if "it2t" in modes else AutoModelForCausalLM
    model = cls.from_pretrained(model_id, dtype=torch.bfloat16, device_map="cuda")
    model.eval()
    return LoadedModel(model=model, processor=processor, supported_modes=modes, model_id=model_id)


#: Test seam: the suite replaces this with a loader returning fakes; the pod
#: never touches it. A module attribute rather than an env-var import path so
#: test_pod_embed_closure's dynamic-import list stays untouched.
_LOADER: Callable[[str], LoadedModel] = _load_transformers


@dataclass
class JobState:
    """One queued/running/finished completion."""

    job_id: str
    status: str  # queued | running | done | error
    prompt: str
    system: str | None
    images: list[str]
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class TextRequest(BaseModel):
    """``POST /text`` body. ``extra="forbid"`` so a typo'd knob is a 422, not a no-op."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1)
    system: str | None = None
    images: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


loaded: LoadedModel | None = None
ready = threading.Event()
jobs: dict[str, JobState] = {}
_q: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None
app = FastAPI(title="kinoforge text server")


def _torch_build() -> dict[str, Any]:
    """Torch/CUDA facts for ``/health``; ``{}`` where torch is absent (tests)."""
    try:
        import torch
    except ImportError:
        return {}
    return {"version": torch.__version__, "cuda": torch.version.cuda}


def _build_messages(prompt: str, system: str | None, images: list[Any]) -> list[dict[str, Any]]:
    """Assemble the chat.

    The user content is a plain string for ``t2t`` (text tokenizers' templates
    expect one and would render a list's repr) and an interleaved list —
    every image, then the text — for ``it2t``.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    content: Any
    if images:
        content = [{"type": "image", "image": im} for im in images]
        content.append({"type": "text", "text": prompt})
    else:
        content = prompt
    messages.append({"role": "user", "content": content})
    return messages


def _open_images(paths: list[str]) -> list[Any]:
    """Open uploaded images as RGB PIL images (PIL imported here, pod-only)."""
    from PIL import Image

    return [Image.open(p).convert("RGB") for p in paths]


def _validate_image_paths(paths: list[str]) -> None:
    """400 unless every path is an existing file under the upload dir."""
    root = _UPLOAD_DIR.resolve()
    for p in paths:
        target = Path(p).resolve()
        try:
            target.relative_to(root)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"image path {p!r} is outside the upload dir") from e
        if not target.is_file():
            raise HTTPException(status_code=400, detail=f"image path {p!r} does not exist on the pod")


def _to_device(inputs: Any, device: Any) -> Any:  # noqa: ANN401 — BatchEncoding or a test dict
    return inputs.to(device) if device is not None and hasattr(inputs, "to") else inputs


def _generate(model: Any, inputs: Any, gen_kwargs: dict[str, Any]) -> Any:  # noqa: ANN401
    """``model.generate`` under ``torch.inference_mode`` when torch is present."""
    try:
        import torch
    except ImportError:  # the controller test env; fakes need no grad guard
        return model.generate(**inputs, **gen_kwargs)
    with torch.inference_mode():
        return model.generate(**inputs, **gen_kwargs)


def _run_job(state: JobState) -> None:
    """Run one completion; mutate *state* with the outcome. Never raises."""
    assert loaded is not None  # noqa: S101 — /text refuses before ready
    state.status = "running"
    state.started_at = time.time()
    try:
        params = dict(state.params)
        chat_kwargs = dict(params.pop("chat_template_kwargs", None) or {})
        gen_kwargs: dict[str, Any] = {"max_new_tokens": DEFAULT_MAX_NEW_TOKENS, **params}
        images = _open_images(state.images) if state.images else []
        messages = _build_messages(state.prompt, state.system, images)
        proc = loaded.processor
        text = proc.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, **chat_kwargs)
        inputs = (
            proc(text=[text], images=images, return_tensors="pt")
            if images
            else proc(text=[text], return_tensors="pt")
        )
        inputs = _to_device(inputs, getattr(loaded.model, "device", None))
        prompt_tokens = int(inputs["input_ids"].shape[1])
        out = _generate(loaded.model, inputs, gen_kwargs)
        new_ids = out[0][prompt_tokens:]
        completion_tokens = int(len(new_ids))
        max_new = int(gen_kwargs["max_new_tokens"])
        state.result = {
            "text": proc.decode(new_ids, skip_special_tokens=True),
            "finish_reason": "length" if completion_tokens >= max_new else "stop",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "model": loaded.model_id,
            "max_new_tokens": max_new,
        }
        state.status = "done"
        _log.info("job %s done: %d prompt + %d new tokens", state.job_id, prompt_tokens, completion_tokens)
    except Exception as e:  # noqa: BLE001 — one bad job must not kill the worker
        _log.exception("job %s failed", state.job_id)
        state.error = f"{type(e).__name__}: {e}"
        state.status = "error"
    finally:
        state.finished_at = time.time()


def _worker_loop() -> None:
    """Drain the job queue forever, one job at a time."""
    while True:
        job_id = _q.get()
        state = jobs.get(job_id)
        if state is None:
            _log.warning("worker: job %s vanished from registry", job_id)
            continue
        _run_job(state)


@app.on_event("startup")
def _startup() -> None:
    """Load the model, spawn the worker, flip ``ready`` LAST."""
    global loaded, _worker_thread
    if not MODEL_ID:
        raise RuntimeError(
            "KINOFORGE_TEXT_MODEL_ID is not set; TransformersTextEngine.render_provision exports it"
        )
    _log.info("startup: loading %s", MODEL_ID)
    t0 = time.monotonic()
    loaded = _LOADER(MODEL_ID)
    _log.info("startup: loaded in %.1f s; modes=%s", time.monotonic() - t0, list(loaded.supported_modes))
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True)
    _worker_thread.start()
    ready.set()


@app.get("/health")
def health() -> dict[str, Any]:
    """Readiness, model identity, DERIVED modes, and the closed capability vocabulary."""
    is_ready = ready.is_set() and loaded is not None
    return {
        "ready": is_ready,
        "model": loaded.model_id if loaded is not None else MODEL_ID,
        "supported_modes": list(loaded.supported_modes) if loaded is not None else [],
        "capabilities": ["text", "upload"] if is_ready else [],
        "default_max_new_tokens": DEFAULT_MAX_NEW_TOKENS,
        "torch": _torch_build(),
    }


@app.get("/util")
def util() -> dict[str, Any]:
    """Per-tick GPU/CPU/mem stats; sync def so the NVML read runs in the threadpool."""
    return read_gpu_stats()


@app.put("/upload")
async def upload_handler(request: Request) -> dict[str, Any]:
    """PNG/JPEG upload with the shared contract; see ``_upload.receive_upload``."""
    return await receive_upload(
        request,
        upload_dir=_UPLOAD_DIR,
        content_types=_UPLOAD_CONTENT_TYPES,
        max_bytes=_UPLOAD_MAX_BYTES,
        fallback_suffix=".png",
    )


@app.post("/text")
def submit_text(req: TextRequest) -> dict[str, str]:
    """Enqueue one completion; 503 while loading, 400 on images a text-only model cannot take."""
    if not ready.is_set() or loaded is None:
        raise HTTPException(status_code=503, detail="model loading")
    if req.images and "it2t" not in loaded.supported_modes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"model {loaded.model_id} serves {list(loaded.supported_modes)}; it has "
                "no image path, so a request carrying images is refused"
            ),
        )
    _validate_image_paths(req.images)
    job_id = uuid.uuid4().hex
    jobs[job_id] = JobState(
        job_id=job_id, status="queued", prompt=req.prompt, system=req.system,
        images=list(req.images), params=dict(req.params),
    )
    _q.put(job_id)
    return {"job_id": job_id}


@app.get("/text/status/{job_id}")
def text_status(job_id: str) -> dict[str, Any]:
    """``submit_and_poll`` schema: ``state`` plus ``result`` when done or ``error`` on failure."""
    state = jobs.get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown job_id")
    out: dict[str, Any] = {"state": state.status}
    if state.status == "done" and state.result is not None:
        out["result"] = state.result
    elif state.status == "error" and state.error is not None:
        out["error"] = state.error
    return out


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)  # noqa: S104 — pod-internal bind
```

- [ ] **Step 6: Update the stage vocabulary**

In `tests/cli/test_shipped_cfg_want_stages_sweep.py` line 33, change to:

```python
# ``"text"`` is advertised by servers/text_server.py (`kinoforge text`);
# transcribed by hand like the other three, for the same reason.
ADVERTISABLE_STAGES = frozenset({"t2v", "upscale", "interpolate", "text"})
```

- [ ] **Step 7: Run to confirm green; the embed/import guards; lint; commit**

Run: `pixi run pytest tests/engines/diffusers/test_upload_module.py tests/engines/diffusers/test_text_server.py tests/cli/test_shipped_cfg_want_stages_sweep.py -q` → pass.
Run: `pixi run python -c "import kinoforge.engines.diffusers.servers.text_server"` → no output, exit 0.
Run: `pixi run pytest tests/providers/test_pod_embed_closure.py tests/test_pod_path_audit.py -q` → pass (no config names the server yet; the servers-package `__init__` docstring-only test must still hold).
Run: `pixi run pre-commit run --files src/kinoforge/engines/diffusers/servers/_upload.py src/kinoforge/engines/diffusers/servers/text_server.py tests/engines/diffusers/test_text_server.py tests/engines/diffusers/test_upload_module.py tests/cli/test_shipped_cfg_want_stages_sweep.py`.

```bash
git add src/kinoforge/engines/diffusers/servers/_upload.py src/kinoforge/engines/diffusers/servers/text_server.py tests/engines/diffusers/test_text_server.py tests/engines/diffusers/test_upload_module.py tests/cli/test_shipped_cfg_want_stages_sweep.py
git commit -m "feat(server): text_server — transformers chat completions over the submit_and_poll contract"
```

---

### Task 5: `TransformersTextEngine` — client, provision fragment, composition

**Goal:** The controller-side `TextEngine` for the pod server: a port-aware `PodHTTPClientMixin` client (`health`, `upload_image`, `complete`), a runtime-phase provision fragment composed into the diffusers engine's `render_provision`, self-registered under `transformers`.

**Files:**
- Create: `src/kinoforge/text_engines/__init__.py` (docstring only), `src/kinoforge/text_engines/transformers/__init__.py`
- Modify: `src/kinoforge/_adapters.py` (new "Text engines" import block, after the "Interpolators" block)
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (third composition block after the interpolator one, ~line 1393, BEFORE the `WAN_MODEL_ID` export)
- Test: `tests/text_engines/__init__.py` (empty), `tests/text_engines/test_transformers_engine.py`; add three tests to `tests/engines/diffusers/test_render_provision_composition.py`

**Acceptance Criteria:**
- [ ] `render_provision` emits `export KINOFORGE_TEXT_MODEL_ID=Qwen/Qwen3-0.6B` (the `hf:` prefix stripped, shell-quoted) and `export KINOFORGE_TEXT_PORT=8000`, `ports == ["8000"]`; `text.port: 8002` changes both.
- [ ] `DiffusersEngine.render_provision` on a cfg with a `text:` block contains the export BEFORE the `server_cmd` line, in a `SetupStep` with `runtime=True, bakeable=False`; without the block it contains neither.
- [ ] `complete` POSTs `{"prompt","system","images","params"}` to `/text`, polls `/text/status/{id}`, returns a `TextResult` from `result`; `state == "error"` raises `TextGenerationFailed` carrying the server's message.
- [ ] `health` parses `supported_modes` into a frozenset; `upload_image` returns the pod path without `file://`; `model_identity("hf:Qwen/Qwen3-0.6B") == "Qwen3-0.6B"` and `""` with no base entry.
- [ ] `_base_url` picks the endpoint for `text.port`.
- [ ] `registry.get_text_engine("transformers")` works after `import kinoforge._adapters`.

**Verify:** `pixi run pytest tests/text_engines tests/engines/diffusers/test_render_provision_composition.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the failing engine tests**

```python
# tests/text_engines/test_transformers_engine.py
"""TransformersTextEngine: fragment, port-aware client, result/error mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401 — self-register
from kinoforge.core import registry
from kinoforge.core.errors import TextGenerationFailed
from kinoforge.core.interfaces import Instance, TextJob
from kinoforge.text_engines import transformers as te


def _cfg(port: int | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "engine": {"kind": "diffusers", "precision": "bf16", "diffusers": {"capability": {"supported_modes": ["t2t"]}}},
        "models": [{"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}],
        "text": {"engine": "transformers", "params": {}},
    }
    if port is not None:
        cfg["text"]["port"] = port
    return cfg


def _instance(**endpoints: str) -> Instance:
    return Instance(id="pod1", provider="runpod", status="ready", created_at=0.0, endpoints=endpoints)


def test_registered_under_transformers() -> None:
    """Bug caught: the _adapters import missing, so `text.engine: transformers` is UnknownAdapter."""
    assert registry.get_text_engine("transformers") is te.TransformersTextEngine


def test_fragment_exports_model_and_port() -> None:
    """Bug caught: exporting the raw `hf:` ref (from_pretrained cannot load it),
    or a fixed port that defeats the sidecar seam."""
    rp = te.TransformersTextEngine().render_provision(_cfg())
    assert "export KINOFORGE_TEXT_MODEL_ID=Qwen/Qwen3-0.6B" in rp.script
    assert "export KINOFORGE_TEXT_PORT=8000" in rp.script
    assert rp.ports == ["8000"]
    assert rp.env_required == ["HF_TOKEN"]
    rp2 = te.TransformersTextEngine().render_provision(_cfg(port=8002))
    assert "export KINOFORGE_TEXT_PORT=8002" in rp2.script
    assert rp2.ports == ["8002"]


def test_base_url_follows_text_port() -> None:
    """Bug caught: the mixin's hard-coded 8000 lookup ignoring text.port."""
    eng = te.TransformersTextEngine()
    inst = _instance(**{"8000": "https://a-8000.proxy", "8002": "https://a-8002.proxy"})
    eng._bind(_cfg(port=8002))  # noqa: SLF001
    assert eng._base_url(inst) == "https://a-8002.proxy"  # noqa: SLF001
    eng._bind(_cfg())  # noqa: SLF001
    assert eng._base_url(inst) == "https://a-8000.proxy"  # noqa: SLF001


def test_health_parses_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: modes left as a list (membership tests still work) but ready
    coerced wrongly, or the URL missing /health."""
    seen: list[str] = []

    def fake_http(*, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        seen.append(f"{method} {url}")
        return {"ready": True, "model": "Qwen/Qwen3-0.6B", "supported_modes": ["t2t"], "capabilities": ["text", "upload"]}

    monkeypatch.setattr(te, "_http_json", fake_http)
    h = te.TransformersTextEngine().health(_instance(**{"8000": "https://p"}), _cfg())
    assert seen == ["GET https://p/health"]
    assert h.ready is True and h.model == "Qwen/Qwen3-0.6B"
    assert h.supported_modes == frozenset({"t2t"})


def test_complete_posts_the_job_and_maps_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: polling /status/{id} (the generate contract) instead of
    /text/status/{id}; usage values left as strings."""
    calls: list[tuple[str, str, Any]] = []

    def fake_http(*, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        calls.append((method, url, payload))
        if method == "POST":
            return {"job_id": "j1"}
        return {"state": "done", "result": {"text": "hi", "finish_reason": "stop", "usage": {"prompt_tokens": "4", "completion_tokens": 3}, "model": "m"}}

    monkeypatch.setattr(te, "_http_json", fake_http)
    job = TextJob(prompt="p", system="s", images=("/tmp/kf-uploads/a.png",), params={"max_new_tokens": 8})
    res = te.TransformersTextEngine().complete(_instance(**{"8000": "https://p"}), job, _cfg())
    assert calls[0] == ("POST", "https://p/text", {"prompt": "p", "system": "s", "images": ["/tmp/kf-uploads/a.png"], "params": {"max_new_tokens": 8}})
    assert calls[1][:2] == ("GET", "https://p/text/status/j1")
    assert res.text == "hi" and res.finish_reason == "stop" and res.model == "m"
    assert res.usage == {"prompt_tokens": 4, "completion_tokens": 3}
    assert res.elapsed_s >= 0.0


def test_server_error_raises_text_generation_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: a generic KeyError on a missing `result` when state is error."""
    def fake_http(*, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        return {"job_id": "j9"} if method == "POST" else {"state": "error", "error": "RuntimeError: OOM"}

    monkeypatch.setattr(te, "_http_json", fake_http)
    with pytest.raises(TextGenerationFailed, match="j9.*OOM"):
        te.TransformersTextEngine().complete(_instance(**{"8000": "https://p"}), TextJob(prompt="p"), _cfg())


def test_upload_image_returns_a_pod_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bug caught: handing the server a file:// URL it cannot open."""
    eng = te.TransformersTextEngine()
    monkeypatch.setattr(eng, "_upload_source", lambda inst, p, *, media: f"file:///tmp/kf-uploads/{p.name}")
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG")
    assert eng.upload_image(_instance(**{"8000": "https://p"}), png, _cfg()) == "/tmp/kf-uploads/x.png"


def test_model_identity_is_the_repo_tail_and_never_raises() -> None:
    """Bug caught (§17's `_unknown_` class): an identity that raises or returns the full ref."""
    eng = te.TransformersTextEngine()
    assert eng.model_identity(_cfg()) == "Qwen3-0.6B"
    assert eng.model_identity({"models": []}) == ""
```

- [ ] **Step 2: Add the composition tests**

Append to `tests/engines/diffusers/test_render_provision_composition.py`:

```python
def _with_text(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["engine"] = dict(cfg["engine"])
    cfg["engine"]["diffusers"] = {
        **cfg["engine"]["diffusers"],
        "server_cmd": ["python", "-m", "kinoforge.engines.diffusers.servers.text_server"],
        "capability": {"supported_modes": ["t2t"]},
    }
    cfg["text"] = {"engine": "transformers", "params": {}}
    return cfg


def test_compose_text_fragment_before_server_exec() -> None:
    # Bug caught: the text fragment missing (KINOFORGE_TEXT_MODEL_ID unset →
    # the server's startup RuntimeError on a booked card) or appended AFTER
    # the server line, where it never runs.
    rp = DiffusersEngine().render_provision(_with_text(_wan_only_cfg()))
    export_idx = rp.script.find("export KINOFORGE_TEXT_MODEL_ID=Wan-AI/Wan2.2-T2V")
    server_idx = rp.script.find("kinoforge.engines.diffusers.servers.text_server")
    assert export_idx >= 0
    assert server_idx >= 0
    assert export_idx < server_idx


def test_text_fragment_is_a_runtime_step_not_bakeable() -> None:
    # Bug caught: composing the exports in the "build" phase like the weight
    # fetches — Modal bakes build steps into the image, where an `export` is
    # gone by the time the container runs (design §5.3).
    rp = DiffusersEngine().render_provision(_with_text(_wan_only_cfg()))
    steps = [s for s in rp.setup_steps if "KINOFORGE_TEXT_MODEL_ID" in s.script]
    assert steps, "no setup step carries the text export"
    assert all(s.runtime and not s.bakeable for s in steps)


def test_no_text_block_means_no_text_composition() -> None:
    # Bug caught: composition firing unconditionally.
    rp = DiffusersEngine().render_provision(_wan_only_cfg())
    assert "KINOFORGE_TEXT_MODEL_ID" not in rp.script
```

- [ ] **Step 3: Run to confirm failure**

Run: `pixi run pytest tests/text_engines tests/engines/diffusers/test_render_provision_composition.py -q` → ImportError / assertion failures on the three new composition tests.

- [ ] **Step 4: Write the engine**

`src/kinoforge/text_engines/__init__.py`:

```python
"""Text engines — controller-side clients + provision fragments for `kinoforge text`.

Each sub-package self-registers via ``registry.register_text_engine`` and is
imported exactly once from ``kinoforge._adapters``. Design:
``docs/superpowers/specs/2026-10-04-text-command-design.md`` §5.
"""
```

`src/kinoforge/text_engines/transformers/__init__.py`:

```python
"""``TextEngine`` for the transformers-backed pod server (design §5.2).

Client + provision fragment only; the model runs in
``engines/diffusers/servers/text_server.py``. Every pod-facing call binds the
port from ``cfg["text"]["port"]`` first, so one engine object can talk to a
server on any port — the seam the hooks spec's sidecar launch needs.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import TextGenerationFailed, ValidationError
from kinoforge.core.interfaces import (
    Instance,
    RenderedProvision,
    TextEngine,
    TextHealth,
    TextJob,
    TextResult,
)
from kinoforge.core.text_request import base_model_ref
from kinoforge.engines._pod_http import PodHTTPClientMixin, http_json, submit_and_poll

_USER_AGENT = "kinoforge-text-transformers/0.1"
_DEFAULT_PORT = 8000


def _port(cfg: dict[str, object]) -> int:
    block = cfg.get("text") or {}
    return int(block.get("port", _DEFAULT_PORT)) if isinstance(block, dict) else _DEFAULT_PORT


class TransformersTextEngine(PodHTTPClientMixin, TextEngine):
    """Pod client for ``text_server.py`` plus its boot fragment."""

    name = "transformers"
    requires_compute = True
    _pod_user_agent = _USER_AGENT

    def __init__(self) -> None:
        self._port_str = str(_DEFAULT_PORT)

    # -- port seam -----------------------------------------------------------

    def _bind(self, cfg: dict[str, object]) -> None:
        """Remember ``text.port`` for the next ``_base_url`` call."""
        self._port_str = str(_port(cfg))

    def _base_url(self, instance: Instance) -> str:
        """Port-aware override of the mixin's default-port lookup."""
        endpoints = instance.endpoints or {}
        url = endpoints.get(self._port_str) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"{type(self).__name__}: instance {instance.id} has no endpoint for "
                f"port {self._port_str}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")

    # -- TextEngine ----------------------------------------------------------

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Export the model id and port for ``text_server.py``.

        No pip, no embeds: those are explicit in ``engine.diffusers.pip`` /
        ``embed_files`` like every other config, and the embed-closure test
        enforces the set. The diffusers engine composes these lines in the
        RUNTIME phase (design §5.3).
        """
        model_id = base_model_ref(cfg).removeprefix("hf:")
        port = _port(cfg)
        lines = [
            f"export KINOFORGE_TEXT_MODEL_ID={shlex.quote(model_id)}",
            f"export KINOFORGE_TEXT_PORT={port}",
        ]
        return RenderedProvision(
            script="\n".join(lines) + "\n",
            image="",
            ports=[str(port)],
            env_required=["HF_TOKEN"],
        )

    def health(self, instance: Instance | None, cfg: dict[str, object]) -> TextHealth:
        """GET ``/health`` → :class:`TextHealth`."""
        if instance is None:
            raise ValueError("TransformersTextEngine requires a compute instance")
        self._bind(cfg)
        payload = _http_json(method="GET", url=f"{self._base_url(instance)}/health")
        return TextHealth(
            ready=bool(payload.get("ready")),
            model=str(payload.get("model", "")),
            supported_modes=frozenset(str(m) for m in payload.get("supported_modes", [])),
        )

    def upload_image(
        self, instance: Instance | None, local_path: Path, cfg: dict[str, object]
    ) -> str:
        """PUT ``/upload`` (sha256 cross-checked by the mixin); return the pod path."""
        if instance is None:
            raise ValueError("TransformersTextEngine requires a compute instance")
        self._bind(cfg)
        return self._upload_source(instance, Path(local_path), media="image").removeprefix("file://")

    def complete(
        self,
        instance: Instance | None,
        job: TextJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> TextResult:
        """POST ``/text``, poll ``/text/status/{id}``, map ``result``."""
        self.validate_spec(job)
        if instance is None:
            raise ValueError("TransformersTextEngine requires a compute instance")
        self._bind(cfg)
        payload: dict[str, Any] = {
            "prompt": job.prompt,
            "system": job.system,
            "images": list(job.images),
            "params": dict(job.params),
        }
        result, elapsed_s = submit_and_poll(
            label_prefix="text",
            base_url=self._base_url(instance),
            endpoint="/text",
            payload=payload,
            http_json=_http_json,
            make_error=lambda job_id, server_error: TextGenerationFailed(job_id, str(server_error)),
            cancel_token=cancel_token,
        )
        usage_raw = result.get("usage") or {}
        return TextResult(
            text=str(result["text"]),
            finish_reason=str(result.get("finish_reason", "")),
            usage={str(k): int(v) for k, v in dict(usage_raw).items()},
            model=str(result.get("model", "")),
            elapsed_s=elapsed_s,
        )

    def validate_spec(self, job: TextJob) -> None:
        """Refuse an empty prompt or non-mapping params before any HTTP."""
        if not job.prompt.strip():
            raise ValidationError("text: prompt is empty")
        if not isinstance(job.params, dict):
            raise ValidationError("text: params must be a mapping")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """The repo tail of the base ref (``Qwen3-0.6B``); ``""`` when absent."""
        try:
            ref = base_model_ref(cfg)
        except ValidationError:
            return ""
        return ref.removeprefix("hf:").rstrip("/").rsplit("/", 1)[-1]


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Module-level seam (tests patch it) delegating to the shared helper."""
    return http_json(method=method, url=url, payload=payload, user_agent=_USER_AGENT)


registry.register_text_engine("transformers", TransformersTextEngine)
```

- [ ] **Step 5: Wire `_adapters.py` and the composition block**

In `src/kinoforge/_adapters.py`, after the `# Interpolators` import:

```python
# Text engines
import kinoforge.text_engines.transformers  # noqa: F401  # self-registers under "transformers"
```

In `src/kinoforge/engines/diffusers/__init__.py`, directly after the interpolator composition block (before `if wan_model_id and server_cmd:`):

```python
        # Compose the text engine's provision fragment when cfg.text is set.
        # Mirrors the two blocks above with ONE difference: phase "runtime",
        # not "build". Those fragments install weights, which Modal bakes into
        # the image; this one is `export` lines, and an export baked into an
        # image layer is gone by the time the container runs. RunPod
        # concatenates both phases, so this is invisible there and correct on
        # Modal (design §5.3).
        text_block_raw = cfg.get("text") if isinstance(cfg, dict) else None
        if isinstance(text_block_raw, dict):
            text_engine_name = text_block_raw.get("engine")
            if text_engine_name:
                from kinoforge.core import registry as _registry

                text_engine = _registry.get_text_engine(str(text_engine_name))()
                text_rp = text_engine.render_provision(cfg)
                _add(
                    "runtime",
                    "# ---- text engine provision (composed) ----",
                    *(line for line in text_rp.script.split("\n") if line),
                )
```

- [ ] **Step 6: Run to confirm green; the invariants; lint; commit**

Run: `pixi run pytest tests/text_engines tests/engines/diffusers/test_render_provision_composition.py tests/engines/diffusers/test_render_provision_split.py -q` → pass.
Run: `pixi run pytest tests/core/test_core_invariant.py tests/providers/test_launch_payload_goldens.py -q` (find the invariant test with `rg -ln 'core_invariant|_adapters' tests/core | head`) → pass: no shipped config has a `text:` block yet, so no golden moves.
Run: `pixi run pre-commit run --files src/kinoforge/text_engines/__init__.py src/kinoforge/text_engines/transformers/__init__.py src/kinoforge/_adapters.py src/kinoforge/engines/diffusers/__init__.py tests/text_engines/__init__.py tests/text_engines/test_transformers_engine.py tests/engines/diffusers/test_render_provision_composition.py`.

```bash
git add src/kinoforge/text_engines src/kinoforge/_adapters.py src/kinoforge/engines/diffusers/__init__.py tests/text_engines tests/engines/diffusers/test_render_provision_composition.py
git commit -m "feat(text-engines): transformers TextEngine — port-aware client + runtime-phase fragment"
```

---

### Task 6: `TextStage` and the two orchestrator branches

**Goal:** `pipeline/text.py::TextStage` runs health → upload → complete → store → publish, and `orchestrator.generate(skip_clip_stage=True)` appends it for a `text:` config and returns `state.artifacts["text"]`.

**Files:**
- Create: `src/kinoforge/pipeline/text.py`
- Modify: `src/kinoforge/core/orchestrator.py` (stage assembly after the `UpscaleStage` block, ~line 2935; `artifact_key` chain at ~line 3014)
- Test: `tests/pipeline/test_text_stage.py`, `tests/core/test_orchestrator_text.py`

**Acceptance Criteria:**
- [ ] Mode mismatch against `/health` raises `ValidationError` naming the pod's modes, the request mode and the declared modes, with `upload_image` and `complete` never called.
- [ ] Happy path: `upload_image` called once per asset in role order; `complete` receives `TextJob(prompt, system=cfg.text.system, images=(pod paths…), params=cfg.text.params)`; `response.txt` and `response.json` stored under `run_id`; the sink receives exactly two `publish` calls with `kind="text"`, extensions `.txt` then `.json`, `provider == cfg.text.engine`, `model == engine.model_identity(cfg)`; the returned artifact's `meta["text"]` equals the completion; `meta["json_uri"]` names the stored sidecar.
- [ ] `sink=None` stores and publishes nothing further; `meta["published"] is None`.
- [ ] A `--image` artifact whose uri is `http(s)://` is refused with `ValidationError` before `health` (URLs are not supported inputs).
- [ ] `orchestrator.generate(cfg_with_text, request=…, skip_clip_stage=True)` constructs no `GenerateClipStage`, appends one `TextStage`, and returns the `"text"` artifact.

**Verify:** `pixi run pytest tests/pipeline/test_text_stage.py tests/core/test_orchestrator_text.py tests/core/test_orchestrator_skip_clip_stage.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the failing stage tests**

```python
# tests/pipeline/test_text_stage.py
"""TextStage (design §7.1): health BEFORE upload BEFORE submit; store + publish."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationRequest,
    PipelineState,
    TextEngine,
    TextHealth,
    TextJob,
    TextResult,
)
from kinoforge.pipeline.text import TextStage
from kinoforge.stores.local import LocalArtifactStore


class _CountingEngine(TextEngine):
    """Records every call; the health modes are configurable per test."""

    name = "transformers"
    requires_compute = True

    def __init__(self, modes: frozenset[str]) -> None:
        self.modes = modes
        self.health_calls = 0
        self.uploads: list[Path] = []
        self.jobs: list[TextJob] = []

    def health(self, instance, cfg):  # noqa: ANN001, ANN201
        self.health_calls += 1
        return TextHealth(ready=True, model="pod-model", supported_modes=self.modes)

    def upload_image(self, instance, local_path, cfg):  # noqa: ANN001, ANN201
        self.uploads.append(Path(local_path))
        return f"/tmp/kf-uploads/{Path(local_path).name}"

    def complete(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001, ANN201
        self.jobs.append(job)
        return TextResult(text="the answer", finish_reason="stop", usage={"prompt_tokens": 4, "completion_tokens": 2}, model="pod-model", elapsed_s=1.5)

    def validate_spec(self, job):  # noqa: ANN001, ANN201
        return None

    def model_identity(self, cfg):  # noqa: ANN001, ANN201
        return "Qwen3-0.6B"


class _RecordingSink:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []

    def publish(self, data: bytes, *, prompt: str, extension: str, namespace: str | None = None, provider: str | None = None, model: str | None = None, kind: str | None = None) -> str:
        self.calls.append({"data": data, "prompt": prompt, "extension": extension, "provider": provider, "model": model, "kind": kind})
        path = self.root / f"out{extension}"
        path.write_bytes(data)
        return str(path)


def _cfg(system: str | None = "Be terse.") -> dict[str, Any]:
    return {
        "engine": {"kind": "diffusers", "precision": "bf16", "diffusers": {"capability": {"supported_modes": ["t2t", "it2t"]}}},
        "models": [{"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}],
        "text": {"engine": "transformers", "system": system, "params": {"max_new_tokens": 8}, "port": 8000},
    }


def _image_asset(tmp_path: Path, name: str, role: str) -> ConditioningAsset:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG")
    return ConditioningAsset(kind="image", role=role, ref=Artifact(uri=f"file://{p}", sha256="s" * 64, size=4))


def _stage(tmp_path: Path, engine: TextEngine, sink: _RecordingSink | None) -> TextStage:
    return TextStage(engine=engine, instance=None, cfg=_cfg(), store=LocalArtifactStore(tmp_path / "store"), sink=sink, run_id="text-test", cancel_token=None)


def test_mode_mismatch_stops_before_any_upload(tmp_path: Path) -> None:
    """Bug caught: a stage that uploads first and gates after — the §4.2 half
    of the gate would then cost the upload AND leak bytes to a pod that cannot use them."""
    eng = _CountingEngine(frozenset({"t2t"}))
    stage = _stage(tmp_path, eng, None)
    req = GenerationRequest(prompt="describe", mode="it2t", assets=[_image_asset(tmp_path, "a.png", "image_1")])
    with pytest.raises(ValidationError, match=r"\['t2t'\].*'it2t'.*\['it2t', 't2t'\]"):
        stage.run(PipelineState(request=req, artifacts={}))
    assert eng.health_calls == 1
    assert eng.uploads == []
    assert eng.jobs == []


def test_happy_path_uploads_in_role_order_and_publishes_two_files(tmp_path: Path) -> None:
    """Bug caught: images uploaded out of order (the prompt refers to 'the
    first image'), params/system dropped from the job, or a single publish."""
    eng = _CountingEngine(frozenset({"t2t", "it2t"}))
    sink = _RecordingSink(tmp_path / "out")
    (tmp_path / "out").mkdir()
    stage = _stage(tmp_path, eng, sink)
    assets = [_image_asset(tmp_path, "z.png", "image_1"), _image_asset(tmp_path, "a.jpg", "image_2")]
    state = stage.run(PipelineState(request=GenerationRequest(prompt="compare", mode="it2t", assets=assets), artifacts={}))

    assert [p.name for p in eng.uploads] == ["z.png", "a.jpg"]
    job = eng.jobs[0]
    assert job == TextJob(prompt="compare", system="Be terse.", images=("/tmp/kf-uploads/z.png", "/tmp/kf-uploads/a.jpg"), params={"max_new_tokens": 8})

    art = state.artifacts["text"]
    assert art.meta["text"] == "the answer"
    assert Path(art.uri.removeprefix("file://")).read_text() == "the answer"
    sidecar = json.loads(Path(art.meta["json_uri"].removeprefix("file://")).read_text())
    assert sidecar["mode"] == "it2t" and sidecar["text"] == "the answer"
    assert sidecar["model"] == "hf:Qwen/Qwen3-0.6B" and sidecar["run_id"] == "text-test"
    assert [i["pod_path"] for i in sidecar["images"]] == ["/tmp/kf-uploads/z.png", "/tmp/kf-uploads/a.jpg"]
    assert sidecar["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}

    assert [c["extension"] for c in sink.calls] == [".txt", ".json"]
    assert {c["kind"] for c in sink.calls} == {"text"}
    assert {c["provider"] for c in sink.calls} == {"transformers"}
    assert {c["model"] for c in sink.calls} == {"Qwen3-0.6B"}
    assert sink.calls[0]["data"] == b"the answer"
    assert art.meta["published"] == str(tmp_path / "out" / "out.txt")


def test_no_sink_is_store_only(tmp_path: Path) -> None:
    """Bug caught: a publish attempted on None (--no-output-dir path)."""
    eng = _CountingEngine(frozenset({"t2t"}))
    state = _stage(tmp_path, eng, None).run(PipelineState(request=GenerationRequest(prompt="hi", mode="t2t"), artifacts={}))
    assert state.artifacts["text"].meta["published"] is None
    assert state.artifacts["text"].meta["text"] == "the answer"
    assert eng.jobs[0].images == ()


def test_url_images_are_refused_before_health(tmp_path: Path) -> None:
    """Bug caught: a URL handed to upload_image, which read_bytes()es a path."""
    eng = _CountingEngine(frozenset({"t2t", "it2t"}))
    asset = ConditioningAsset(kind="image", role="image_1", ref=Artifact(uri="https://x/y.png", sha256="", size=0))
    with pytest.raises(ValidationError, match="local file"):
        _stage(tmp_path, eng, None).run(PipelineState(request=GenerationRequest(prompt="p", mode="it2t", assets=[asset]), artifacts={}))
    assert eng.health_calls == 0
```

- [ ] **Step 2: Write the failing orchestrator test**

```python
# tests/core/test_orchestrator_text.py
"""generate(skip_clip_stage=True) with a text: cfg appends TextStage and returns 'text'."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge._adapters  # noqa: F401
from kinoforge.core import orchestrator, registry
from kinoforge.core.config import Config
from kinoforge.core.interfaces import Artifact, GenerationRequest, PipelineState, TextEngine, TextHealth, TextResult
from kinoforge.core.orchestrator import DeploySession, generate
from kinoforge.stores.local import LocalArtifactStore


class _FakeTextEngine(TextEngine):
    name = "fake-text"
    requires_compute = True

    def health(self, instance, cfg):  # noqa: ANN001, ANN201
        return TextHealth(ready=True, model="m", supported_modes=frozenset({"t2t"}))

    def upload_image(self, instance, local_path, cfg):  # noqa: ANN001, ANN201
        raise AssertionError("no images in this test")

    def complete(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001, ANN201
        return TextResult(text="pong", finish_reason="stop", usage={}, model="m", elapsed_s=0.1)

    def validate_spec(self, job):  # noqa: ANN001, ANN201
        return None

    def model_identity(self, cfg):  # noqa: ANN001, ANN201
        return "m"


def _text_cfg() -> Config:
    return Config.model_validate({
        "engine": {"kind": "diffusers", "precision": "bf16", "diffusers": {"capability": {"supported_modes": ["t2t"]}}},
        "models": [{"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}],
        "text": {"engine": "fake-text"},
        "compute": {"provider": "fake", "image": "fake:latest"},
    })


@pytest.fixture
def _fake_session(monkeypatch: pytest.MonkeyPatch) -> DeploySession:
    fake_engine = MagicMock(name="GenerationEngine")
    fake_engine.name = "diffusers"
    fake_engine.accepted_kinds = {"image"}
    session = DeploySession(backend=MagicMock(), profile=MagicMock(), pool=MagicMock(), instance=None, engine=fake_engine, provider=None)

    @contextmanager
    def fake_deploy(*args: Any, **kwargs: Any) -> Any:
        yield session

    monkeypatch.setattr(orchestrator, "deploy_session", fake_deploy)
    return session


@pytest.fixture
def _fake_text_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_text_engines", {**registry._text_engines, "fake-text": _FakeTextEngine})  # noqa: SLF001


@pytest.mark.usefixtures("_fake_session", "_fake_text_engine")
def test_text_cfg_returns_the_text_artifact_without_a_clip_stage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """Bug caught: artifact_key falling through to "clip" (KeyError on the
    skip path), or GenerateClipStage constructed for a text run."""
    constructed: list[Any] = []

    class _SpyClip:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        def run(self, state: PipelineState) -> PipelineState:
            raise AssertionError("clip stage must not run")

    monkeypatch.setattr(orchestrator, "GenerateClipStage", _SpyClip)
    artifact, instance = generate(
        _text_cfg(),
        request=GenerationRequest(prompt="ping", mode="t2t"),
        store=LocalArtifactStore(tmp_path / "store"),
        run_id="text-orch",
        state_dir=tmp_path / "state",
        sink=None,
        skip_clip_stage=True,
    )
    assert constructed == []
    assert isinstance(artifact, Artifact)
    assert artifact.meta["text"] == "pong"
    assert instance is None
```

If `orchestrator` does not expose `GenerateClipStage` as a module attribute, use the same patch target `tests/core/test_orchestrator_skip_clip_stage.py::_spy_clip_stage` uses (read that fixture, lines 60-90, and copy its `monkeypatch.setattr` target verbatim).

- [ ] **Step 3: Run to confirm failure**

Run: `pixi run pytest tests/pipeline/test_text_stage.py tests/core/test_orchestrator_text.py -q` → ImportError on `kinoforge.pipeline.text`; the orchestrator test then fails with `KeyError: 'clip'`.

- [ ] **Step 4: Write the stage**

```python
# src/kinoforge/pipeline/text.py
"""TextStage — the terminal stage behind ``kinoforge text`` (design §7.1).

Order is the contract: **health, then upload, then submit.** The pod's
``/health`` is the only place the model's TRUE modes are known (the diffusers
probe never reads the pod — design §4.3), so the stage checks it before a
single image byte moves. Stores ``response.txt`` + ``response.json`` under the
run id, publishes both to the sink, and returns the state with
``artifacts["text"]`` set; the orchestrator returns that artifact without
re-publishing it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge import __version__
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact, Instance, PipelineState, TextEngine, TextJob
from kinoforge.core.text_request import base_model_ref, declared_modes
from kinoforge.outputs.base import OutputSink
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)

# kinoforge:public-name — fixed identifiers, not prompt-derived (image_run precedent).
_STORE_TEXT = "response.txt"  # kinoforge:public-name
_STORE_JSON = "response.json"  # kinoforge:public-name


def _local_path(artifact: Artifact) -> Path:
    """Resolve a ``--image`` artifact to a local path; URLs are not inputs here."""
    uri = artifact.uri
    if uri.startswith(("http://", "https://")):
        raise ValidationError(f"text: --image must be a local file, got URL {uri!r}")
    return Path(uri.removeprefix("file://"))


@dataclass
class TextStage:
    """A Stage that runs one chat completion on the session's pod.

    Attributes:
        engine: Configured ``TextEngine`` (registry-resolved).
        instance: The booked pod; ``None`` only for in-process fakes.
        cfg: Runtime config dict (``_cfg_dict(cfg)``); ``cfg["text"]`` is read.
        store: Where ``response.txt`` / ``response.json`` land under ``run_id``.
        sink: User-facing publish seam, or ``None`` for ``--no-output-dir``.
        run_id: The run id (also written into the sidecar).
        cancel_token: Threaded into ``engine.complete``.
    """

    engine: TextEngine
    instance: Instance | None
    cfg: dict[str, Any]
    store: ArtifactStore
    sink: OutputSink | None
    run_id: str
    cancel_token: CancelToken | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Health-gate, upload, complete, store, publish; return the new state."""
        request = state.request
        block: dict[str, Any] = dict(self.cfg.get("text") or {})
        local_paths = [_local_path(asset.ref) for asset in request.assets]  # refuses URLs first

        # 1. Health gate BEFORE any bytes move (design §4.2).
        health = self.engine.health(self.instance, self.cfg)
        if request.mode not in health.supported_modes:
            raise ValidationError(
                f"text: the pod's model {health.model!r} serves modes "
                f"{sorted(health.supported_modes)} but the request needs "
                f"{request.mode!r}; the config declared "
                f"{sorted(declared_modes(self.cfg))}. Fix "
                "capability.supported_modes to match the checkpoint, or drop --image."
            )

        # 2. Upload each image in role order; keep the pairing for the sidecar.
        uploaded: list[dict[str, Any]] = []
        for asset, local in zip(request.assets, local_paths, strict=True):
            pod_path = self.engine.upload_image(self.instance, local, self.cfg)
            uploaded.append(
                {"role": asset.role, "local": str(local), "sha256": asset.ref.sha256, "pod_path": pod_path}
            )

        # 3. Complete.
        job = TextJob(
            prompt=request.prompt,
            system=block.get("system"),
            images=tuple(u["pod_path"] for u in uploaded),
            params=dict(block.get("params") or {}),
        )
        self.engine.validate_spec(job)
        result = self.engine.complete(self.instance, job, self.cfg, cancel_token=self.cancel_token)

        # 4. Store + publish.
        provider = str(block.get("engine") or self.engine.name)
        sidecar = {
            "prompt": request.prompt,
            "system": job.system,
            "mode": request.mode,
            "images": uploaded,
            "model": base_model_ref(self.cfg),
            "engine": provider,
            "params": job.params,
            "text": result.text,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "elapsed_s": result.elapsed_s,
            "run_id": self.run_id,
            "kinoforge_version": __version__,
            "instance_id": self.instance.id if self.instance is not None else None,
        }
        text_bytes = result.text.encode("utf-8")
        json_bytes = json.dumps(sidecar, indent=2, sort_keys=True).encode("utf-8")
        stored_txt = self.store.put_bytes(self.run_id, _STORE_TEXT, text_bytes)
        stored_json = self.store.put_bytes(self.run_id, _STORE_JSON, json_bytes)
        published: str | None = None
        if self.sink is not None:
            model = self.engine.model_identity(self.cfg)
            published = self.sink.publish(
                text_bytes, prompt=request.prompt, extension=".txt", provider=provider, model=model, kind="text"
            )
            self.sink.publish(
                json_bytes, prompt=request.prompt, extension=".json", provider=provider, model=model, kind="text"
            )
            _log.info("text published: %s", published)
        artifact = replace(
            stored_txt,
            meta={**stored_txt.meta, "text": result.text, "json_uri": stored_json.uri, "published": published},
        )
        return replace(state, artifacts={**state.artifacts, "text": artifact})
```

Note: `LocalArtifactStore.put_bytes` returns an `Artifact` whose `uri` is the absolute path string (no `file://`); `removeprefix("file://")` in the tests is a no-op there and keeps them valid for a store that does prefix. If `OutputSink` is not importable from `kinoforge.outputs.base` under that name, use the name `format_filename`'s module exports (`rg -n '^class OutputSink' src/kinoforge/outputs`).

- [ ] **Step 5: Wire the orchestrator**

In `src/kinoforge/core/orchestrator.py`, directly after the `if cfg.upscale is not None:` block that appends `UpscaleStage` (~line 2935), add:

```python
        # `kinoforge text` — terminal text stage (design §7.2). Exclusive with
        # upscale/interpolate by the config validator, so ordering is moot.
        if cfg.text is not None:
            from kinoforge.core import registry as _registry
            from kinoforge.pipeline.text import TextStage

            text_engine = _registry.get_text_engine(cfg.text.engine)()
            stages.append(
                TextStage(
                    engine=text_engine,
                    instance=session.instance,
                    cfg=cfg_dict,
                    store=store,
                    sink=sink,
                    run_id=run_id,
                    cancel_token=cancel_token,
                )
            )
```

And in the `artifact_key` chain (~line 3014):

```python
        if skip_clip_stage and cfg.interpolate is not None:
            artifact_key = "interpolated"
        elif skip_clip_stage and cfg.upscale is not None:
            artifact_key = "upscaled"
        elif skip_clip_stage and cfg.text is not None:
            artifact_key = "text"
        else:
            artifact_key = "clip"
```

- [ ] **Step 6: Run to confirm green; neighbours; lint; commit**

Run: `pixi run pytest tests/pipeline/test_text_stage.py tests/core/test_orchestrator_text.py tests/core/test_orchestrator_skip_clip_stage.py tests/core/test_orchestrator_interpolate.py -q` → pass.
Run: `pixi run pytest tests/core -q -x` (the orchestrator has many invariants; the AST guard in `tests/core/test_image_run.py` must stay green — `pipeline/text.py` is not `image_run`).
Run: `pixi run pre-commit run --files src/kinoforge/pipeline/text.py src/kinoforge/core/orchestrator.py tests/pipeline/test_text_stage.py tests/core/test_orchestrator_text.py`.

```bash
git add src/kinoforge/pipeline/text.py src/kinoforge/core/orchestrator.py tests/pipeline/test_text_stage.py tests/core/test_orchestrator_text.py
git commit -m "feat(pipeline): TextStage — health, upload, complete, store, publish; orchestrator text branch"
```

---

### Task 7: The `text` subcommand

**Goal:** `kinoforge text -c CFG [--prompt] [--image …] [--no-reuse|--attach-pod] [--output-dir|--no-output-dir] [--run-id] [--dry-run]` with every config-fact refusal before pod work and before `--dry-run`, the upscale-shaped warm-scan/attach/launch-row wiring, and stdout carrying the completion only.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (parser block after `p_image`, ~line 822; `_INTERRUPTIBLE_CMDS` line 97; `_DISPATCH` line 154)
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_text` after `_cmd_image`, ~line 1620)
- Test: `tests/cli/test_cmd_text.py`

**Acceptance Criteria:**
- [ ] `--image` against a `["t2t"]` config exits 2 with the model and `it2t` named, and `_scan_warm_candidates` / `orchestrator.generate` are never called (no `--dry-run` on that run).
- [ ] Missing file / bad suffix / no prompt / no `text:` block / `--no-reuse` + `--attach-pod` each exit 2 with a message naming the fault.
- [ ] `--dry-run` prints `mode: t2t` (or `it2t` with an image), the model, the declared modes, and makes no pod call; exit 0.
- [ ] Happy path (generate patched): `request.mode == "it2t"`, roles `image_1`, `skip_clip_stage is True`, `single == no_reuse`; stdout is exactly the completion plus a newline.
- [ ] A `KinoforgeError` from `generate` → exit 1 with a one-line `error:` on stderr and `_settle_unused_launch_row` called; `Cancelled` → exit 1.
- [ ] `"text"` in `_DISPATCH` and in `_INTERRUPTIBLE_CMDS`; `--prompt` optional at the parser; `tests/cli/test_session_global_flag_positions.py` stays green (it introspects required args, so the new leaf is covered automatically).

**Verify:** `pixi run pytest tests/cli/test_cmd_text.py tests/cli/test_session_global_flag_positions.py tests/cli/test_sigint_handler.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_cmd_text.py
"""kinoforge text: refusal ordering, dry run, request shape, stdout contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401
from kinoforge.cli._main import main
from kinoforge.core.errors import Cancelled, TextGenerationFailed
from kinoforge.core.interfaces import Artifact

_CFG = """\
engine:
  kind: diffusers
  precision: bf16
  diffusers:
    server_cmd: [python, -m, kinoforge.engines.diffusers.servers.text_server]
    capability:
      supported_modes: [{modes}]
models:
  - kind: base
    ref: hf:Qwen/Qwen3-0.6B
    target: checkpoints
text:
  engine: transformers
{prompt_line}compute:
  provider: fake
  image: fake:latest
"""


def _cfg(tmp_path: Path, modes: str = "t2t", prompt: str | None = None) -> Path:
    line = f"  prompt: {prompt!r}\n" if prompt else ""
    p = tmp_path / "text.yaml"
    p.write_text(_CFG.format(modes=modes, prompt_line=line))
    return p


def _png(tmp_path: Path, name: str = "in.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


@pytest.fixture
def no_pod_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any pod-adjacent call fails the test — proves refusals fire first."""
    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("pod work must not start")

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", _boom)
    monkeypatch.setattr("kinoforge.cli._commands._resolve_attach_pod", _boom)
    monkeypatch.setattr("kinoforge.core.orchestrator.generate", _boom)


def _run(tmp_path: Path, *argv: str) -> int:
    return main(["--state-dir", str(tmp_path / "state"), "text", *argv])


@pytest.mark.usefixtures("no_pod_work")
def test_images_to_a_text_only_model_exit_2_before_pod_work(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Design §4.1. Bug caught: booking the pod and refusing after (the weak
    assert-raises version passes against that); a message naming neither model nor mode."""
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path, "t2t")), "--prompt", "describe", "--image", str(_png(tmp_path)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "hf:Qwen/Qwen3-0.6B" in err and "it2t" in err and "['t2t']" in err


@pytest.mark.usefixtures("no_pod_work")
@pytest.mark.parametrize(
    ("argv_extra", "needle"),
    [
        (["--image", "/nonexistent/x.png"], "not found"),
        (["--image", "__GIF__"], ".png/.jpg/.jpeg"),
        (["--no-reuse", "--attach-pod", "p1"], "mutually exclusive"),
    ],
    ids=["missing-image", "bad-suffix", "reuse-vs-attach"],
)
def test_precondition_faults_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str], argv_extra: list[str], needle: str) -> None:
    if "__GIF__" in argv_extra:
        gif = tmp_path / "x.gif"
        gif.write_bytes(b"GIF89a")
        argv_extra = [a if a != "__GIF__" else str(gif) for a in argv_extra]
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path, "t2t, it2t")), "--prompt", "p", *argv_extra)
    assert rc == 2
    assert needle in capsys.readouterr().err


@pytest.mark.usefixtures("no_pod_work")
def test_no_prompt_anywhere_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path)))
    assert rc == 2
    assert "--prompt" in capsys.readouterr().err


@pytest.mark.usefixtures("no_pod_work")
def test_config_without_text_block_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "video.yaml"
    cfg.write_text(
        "engine:\n  kind: diffusers\n  precision: fp8\nmodels:\n  - kind: base\n    ref: hf:Wan-AI/Wan2.2-T2V\n"
        "    target: diffusion_models\ncompute:\n  provider: fake\n  image: fake:latest\n"
    )
    rc = _run(tmp_path, "-c", str(cfg), "--prompt", "p")
    assert rc == 2
    assert "`text:` block" in capsys.readouterr().err


@pytest.mark.usefixtures("no_pod_work")
def test_dry_run_reports_the_derived_mode_and_makes_no_call(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Bug caught: a dry run that scans warm pods, or reports t2t with an image."""
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path, "t2t, it2t", prompt="from config")), "--image", str(_png(tmp_path)), "--dry-run")
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode: it2t" in out and "images: 1" in out
    assert "hf:Qwen/Qwen3-0.6B" in out and "['it2t', 't2t']" in out
    assert "'from config'" in out


def test_happy_path_request_shape_and_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Bug caught: stdout polluted with a `text: uri=...` line (breaks piping);
    request=None (the upscale placeholder) so the stage has no prompt."""
    captured: dict[str, Any] = {}

    def fake_generate(cfg: Any, request: Any, **kw: Any) -> Any:
        captured["request"] = request
        captured.update(kw)
        return (Artifact(uri="/store/text-x/response.txt", meta={"text": "hello world", "published": None}), None)

    monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path, "t2t, it2t")), "--prompt", "describe", "--image", str(_png(tmp_path)), "--no-reuse")
    assert rc == 0
    assert capsys.readouterr().out == "hello world\n"
    req = captured["request"]
    assert req.prompt == "describe" and req.mode == "it2t"
    assert [a.role for a in req.assets] == ["image_1"]
    assert req.assets[0].ref.uri.startswith("file://")
    assert captured["skip_clip_stage"] is True
    assert captured["single"] is True
    assert "initial_clip" not in captured or captured["initial_clip"] is None


@pytest.mark.parametrize(("exc", "needle"), [(TextGenerationFailed("j1", "OOM"), "OOM"), (Cancelled(), "cancelled")])
def test_pod_side_failures_exit_1_with_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: Exception, needle: str) -> None:
    """Bug caught: a traceback on a pod-side error (upscale's current shape),
    or exit 2 for an operational failure."""
    def fake_generate(*a: Any, **k: Any) -> Any:
        raise exc

    settled: list[Any] = []
    monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
    monkeypatch.setattr("kinoforge.cli._commands._settle_unused_launch_row", lambda *a, **k: settled.append(a))
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path)), "--prompt", "p", "--no-reuse")
    assert rc == 1
    assert needle in capsys.readouterr().err.lower()
    assert settled, "the launch row must be settled on failure"


def test_text_is_dispatched_and_interruptible() -> None:
    """Bug caught: a handler nobody can reach, or a 2-minute boot that ignores Ctrl-C."""
    from kinoforge.cli._main import _DISPATCH, _INTERRUPTIBLE_CMDS

    assert "text" in _DISPATCH
    assert "text" in _INTERRUPTIBLE_CMDS


def test_prompt_is_optional_and_images_repeat_at_the_parser() -> None:
    from kinoforge.cli._main import _build_parser

    args = _build_parser().parse_args(["text", "-c", "cfg.yaml", "--image", "a.png", "--image", "b.jpg"])
    assert args.prompt is None
    assert args.images == ["a.png", "b.jpg"]
    assert _build_parser().parse_args(["text", "-c", "cfg.yaml"]).images == []
```

`Cancelled()`'s constructor: check `src/kinoforge/core/errors.py:80` for required args and build it accordingly in the parametrize list.

- [ ] **Step 2: Run to confirm failure**

Run: `pixi run pytest tests/cli/test_cmd_text.py -q` → argparse "invalid choice: 'text'" (SystemExit 2) on every test.

- [ ] **Step 3: Add the parser, dispatch and interruptible entries**

In `src/kinoforge/cli/_main.py`, after the `p_image` block (before `# list`):

```python
    # text — `kinoforge text`: one chat completion on a reserved pod
    # (docs/superpowers/specs/2026-10-04-text-command-design.md §2).
    p_text = sub.add_parser(
        "text", help="generate text with an open-weight LLM on a reserved pod"
    )
    p_text.add_argument("-c", "--config", required=True, metavar="PATH")
    p_text.add_argument(
        "--prompt",
        default=None,
        metavar="TEXT",
        help=(
            "prompt text; overrides cfg.text.prompt, which overrides the "
            "top-level cfg.prompt. Optional — a config carrying a prompt runs "
            "without it."
        ),
    )
    p_text.add_argument(
        "--image",
        action="append",
        default=[],
        dest="images",
        metavar="PATH",
        help=(
            "input image (.png/.jpg/.jpeg, local file); repeatable. Any --image "
            "selects mode it2t, which engine.diffusers.capability.supported_modes "
            "must declare — refused before any pod work otherwise."
        ),
    )
    p_text.add_argument(
        "--no-reuse",
        action="store_true",
        dest="no_reuse",
        help="force cold create + destroy on completion. Mutex with --attach-pod.",
    )
    p_text.add_argument(
        "--attach-pod",
        type=str,
        default=None,
        metavar="POD_ID",
        help="attach to an existing pod; skip provision. Mutex with --no-reuse.",
    )
    p_text_output = p_text.add_mutually_exclusive_group()
    p_text_output.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="user-facing output directory (overrides cfg.output.dir)",
    )
    p_text_output.add_argument(
        "--no-output-dir",
        action="store_true",
        help="disable user-facing publish; the text remains only in the store",
    )
    p_text.add_argument("--run-id", default=None, metavar="ID")
    p_text.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="emit the resolved plan to stdout and exit 0; no pod work",
    )
```

Line 97: `_INTERRUPTIBLE_CMDS = frozenset({"generate", "batch", "upscale", "interpolate", "image", "text"})`.
Line 154 `_DISPATCH`: add `"text": _cmd_text,` after `"image": _cmd_image,` and add `_cmd_text` to the `from kinoforge.cli._commands import (...)` block at the top of `_main.py`.

- [ ] **Step 4: Write the handler**

Append to `src/kinoforge/cli/_commands.py` after `_cmd_image`:

```python
def _cmd_text(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``text`` — one chat completion on a reserved pod.

    Design ``docs/superpowers/specs/2026-10-04-text-command-design.md`` §2 and
    §7.3. Ordering mirrors ``_cmd_upscale``: flag conflicts (no config needed),
    config presence, then EVERY config-fact refusal — image args, the pre-spend
    mode gate, the prompt — BEFORE the ``--dry-run`` block, so a dry run
    surfaces them too and nothing here can cost a pod boot. After that the
    warm-scan / attach / launch-row wiring is ``_cmd_upscale``'s, and the
    orchestrator runs ``TextStage`` on the skip-clip path.

    stdout carries the completion text and NOTHING else, so the command pipes;
    the run id, store uri and published path go to the logger.

    Args:
        args: Parsed CLI arguments for the ``text`` subcommand.
        ctx: Per-invocation session context.

    Returns:
        2 for a config/precondition fault, 1 for a pod-side or operational
        failure (including a pod-reported mode mismatch and cancellation), 0 on
        success or ``--dry-run``.
    """
    from kinoforge.core.errors import Cancelled, KinoforgeError, ValidationError
    from kinoforge.core.text_request import (
        base_model_ref,
        build_request,
        declared_modes,
        derive_mode,
        image_arg_error,
        preflight_mode_error,
        resolve_prompt,
    )

    # Mutual exclusion FIRST — does not require cfg load.
    if getattr(args, "no_reuse", False) and getattr(args, "attach_pod", None):
        print(
            "error: --no-reuse and --attach-pod are mutually exclusive "
            "(--no-reuse forces cold create + destroy; --attach-pod "
            "implies pod survival)",
            file=sys.stderr,
        )
        return 2
    if ctx.cfg is None:
        print("error: --config required for text", file=sys.stderr)
        return 2
    cfg = ctx.cfg
    if cfg.text is None:
        print(
            "error: --config must contain a `text:` block; "
            "see examples/configs/runpod-diffusers-qwen3-0_6b-t2t.yaml",
            file=sys.stderr,
        )
        return 2

    # Config-fact refusals — all BEFORE --dry-run and long before any pod work.
    image_paths: list[str] = list(getattr(args, "images", None) or [])
    for path in image_paths:
        if (img_err := image_arg_error(path)) is not None:
            print(img_err, file=sys.stderr)
            return 2
    mode = derive_mode(len(image_paths))
    if (mode_err := preflight_mode_error(cfg, mode)) is not None:
        print(mode_err, file=sys.stderr)
        return 2
    try:
        prompt = resolve_prompt(cfg, getattr(args, "prompt", None))
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sink = _build_sink(cfg, args)
    run_id = _resolve_run_id(args, "text")
    if getattr(args, "dry_run", False):
        print("text plan:")
        print(f"  mode: {mode}")
        print(f"  images: {len(image_paths)}")
        print(f"  engine: {cfg.text.engine}")
        print(f"  model: {base_model_ref(cfg)}")
        print(f"  declared_modes: {sorted(declared_modes(cfg))}")
        print(f"  prompt: {prompt[:80]!r}")
        print(f"  run_id: {run_id}")
        print(f"  no_reuse: {bool(getattr(args, 'no_reuse', False))}")
        print(f"  attach_pod: {getattr(args, 'attach_pod', None)}")
        print(f"  sink: {'disabled' if sink is None else 'enabled'}")
        return 0

    from kinoforge.core import orchestrator as _orchestrator

    request = build_request(prompt, [_resolve_input_as_artifact(p, "image") for p in image_paths])
    store = ctx.store()

    instance: Instance | None = None
    attach_pod_id = getattr(args, "attach_pod", None)
    if attach_pod_id:
        instance, rc = _resolve_attach_pod(ctx, cfg, attach_pod_id)
        if rc is not None:
            return rc
    elif not args.no_reuse:
        instance, report = _scan_warm_candidates(ctx, cfg)
        logger.info(report.summarize())

    # Spec A2 — same pre-create reservation as _cmd_generate / _cmd_upscale.
    launch = (
        _ephemeral_launch_row_reserve(ctx, cfg, run_id) if instance is None else None
    )
    try:
        artifact, returned_instance = _orchestrator.generate(
            cfg,
            request=request,
            store=store,
            sink=sink,
            run_id=run_id,
            state_dir=ctx.state_dir,
            cancel_token=ctx.cancel_token,
            instance=instance,
            single=bool(args.no_reuse),
            skip_clip_stage=True,
            on_instance_created=_ephemeral_row_upgrade_hook(ctx, cfg, launch),
        )
    except Cancelled:
        _settle_unused_launch_row(ctx, cfg, launch, None)
        print("text: cancelled", file=sys.stderr)
        return 1
    except KinoforgeError as exc:
        # A pod-reported mode mismatch (TextStage's ValidationError) lands here
        # too: by then a boot has been paid for, so it is operational (exit 1),
        # not a precondition fault — those all returned 2 above.
        _settle_unused_launch_row(ctx, cfg, launch, None)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if returned_instance is not None and instance is None and not args.no_reuse:
        _stamp_cold_created_instance(
            ctx,
            cfg,
            returned_instance,
            created_at_local=launch.created_at_local if launch else None,
            supersedes=launch.id if launch else None,
        )
    else:
        _settle_unused_launch_row(ctx, cfg, launch, returned_instance)

    text = str(artifact.meta.get("text", ""))
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    logger.info(
        "text: run_id=%s store=%s published=%s",
        run_id,
        artifact.uri,
        artifact.meta.get("published"),
    )
    return 0
```

- [ ] **Step 5: Run to confirm green; the CLI matrix; lint; commit**

Run: `pixi run pytest tests/cli/test_cmd_text.py -q` → pass.
Run: `pixi run pytest tests/cli -q -x` → pass (`test_session_global_flag_positions.py` introspects the new leaf's required `-c`; `test_sigint_handler.py` reads `_INTERRUPTIBLE_CMDS`).
Run: `pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_cmd_text.py`.

```bash
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_cmd_text.py
git commit -m "feat(cli): kinoforge text — derived mode, pre-spend gate, stdout-only completion"
```

---

### Task 8: Example configs, smoke prompts, and the config sweeps

**Goal:** Three shipped RunPod configs (two smoke, one quality) plus two verbatim smoke prompts, registered in every sweep that enumerates shipped configs: `EXAMPLE_CONFIGS`, `_BASELINE_BYTES`, the launch goldens, the embed-closure, long-boot, want-stages and capabilities sweeps.

**Files:**
- Create: `examples/configs/runpod-diffusers-qwen3-0_6b-t2t.yaml`, `examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml`, `examples/configs/runpod-diffusers-qwen3_8-27b-it2t.yaml`
- Create: `examples/configs/prompts/text-smoke-t2t.txt`, `examples/configs/prompts/text-smoke-it2t.txt`
- Modify: `tests/test_examples.py` (`EXAMPLE_CONFIGS`, line 33), `tests/providers/test_env_payload_ceiling.py` (`_BASELINE_BYTES`, line ~155)
- Create (generated): `tests/providers/golden/launch_payloads/<three new goldens>`

**Acceptance Criteria:**
- [ ] All three configs load; `cfg.text.engine == "transformers"`; the quality config declares `["t2t", "it2t"]`, the Qwen3-0.6B config `["t2t"]`, the SmolVLM config `["t2t", "it2t"]`.
- [ ] `_BASELINE_BYTES` has a row per new config and each measures under 101 000 B; `test_baseline_covers_every_shipped_runpod_diffusers_config` passes.
- [ ] Launch goldens exist for all three; regenerating changes ONLY those three files.
- [ ] `tests/test_examples.py`, `tests/providers/test_pod_embed_closure.py`, `tests/test_runpod_long_boot_needs_secure_pool.py`, `tests/cli/test_shipped_cfg_want_stages_sweep.py`, `tests/validation/test_shipped_configs_capabilities.py`, `tests/test_layer_r_backcompat.py`, `tests/engines/test_bakeable_steps_set_u_safe.py`, `tests/engines/test_diffusers_optional_cfg_defaults.py` all pass.
- [ ] Guard-the-guard: a new test asserts the config discovery under `examples/configs` finds exactly three configs with a `text:` block.

**Verify:** `pixi run pytest tests/test_examples.py tests/providers tests/test_runpod_long_boot_needs_secure_pool.py tests/cli/test_shipped_cfg_want_stages_sweep.py tests/validation tests/test_layer_r_backcompat.py tests/engines/test_bakeable_steps_set_u_safe.py tests/engines/test_diffusers_optional_cfg_defaults.py -q` → pass.

**Steps:**

- [ ] **Step 1: Write the guard-the-guard test (fails: zero text configs)**

Append to `tests/core/test_text_config.py`:

```python
def test_exactly_three_text_configs_ship() -> None:
    """Guard the guard: a discovery that finds nothing passes every per-config
    sweep. Bug caught: a config renamed or moved out of the sweep's reach."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "examples" / "configs"
    found = sorted(
        p.name
        for p in root.rglob("*.yaml")
        if not p.name.endswith(".grid.yaml")
        and "manifests" not in p.parts
        and "text:" in p.read_text()
        and load_config(str(p)).text is not None
    )
    assert found == [
        "runpod-diffusers-qwen3-0_6b-t2t.yaml",
        "runpod-diffusers-qwen3_8-27b-it2t.yaml",
        "runpod-diffusers-smolvlm-256m-it2t.yaml",
    ]
```

Run: `pixi run pytest tests/core/test_text_config.py::test_exactly_three_text_configs_ship -q` → FAIL (`[] != [...]`).

- [ ] **Step 2: Write the smoke prompts (verbatim inputs; never paraphrased in a run)**

```bash
cat > examples/configs/prompts/text-smoke-it2t.txt <<'TXT'
Describe this image in two sentences. Name the dominant colours.
TXT
{ printf '%s\n\n' 'Summarise the following shot description in two sentences, naming the subject, the setting and the light.'; cat examples/configs/prompts/field-realistic.txt; } > examples/configs/prompts/text-smoke-t2t.txt
```

- [ ] **Step 3: Write the Qwen3-0.6B smoke config**

```yaml
# examples/configs/runpod-diffusers-qwen3-0_6b-t2t.yaml
# `kinoforge text` smoke config — TEXT-ONLY model (modes: t2t).
#
# Design: docs/superpowers/specs/2026-10-04-text-command-design.md
# Plan:   docs/superpowers/plans/2026-10-04-text-command.md
#
# Qwen3-0.6B (0.75 B params, 1.5 GB bf16, Apache 2.0) has NO image processor,
# so `--image` against this config is refused at the CLI before any pod work —
# the §4 negative path is real, not mocked. The checkpoint is the smallest
# credible instruction-tuned LLM in the 2026-10-04 survey
# (docs/superpowers/research/2026-10-04-open-weight-llm-survey.md).
#
# Boot is pip (transformers + accelerate, no torch pull — the 2.8 image ships
# torch 2.8) plus a 1.5 GB download: ~3-5 min on the secure pool.
#
# Usage (one-shot; --no-reuse so the pod auto-destroys, then ALWAYS verify with
# a fresh `pixi run kinoforge list` — a mid-run "No running instances" line is
# not proof):
#   pixi run kinoforge text \
#     --config examples/configs/runpod-diffusers-qwen3-0_6b-t2t.yaml \
#     --prompt "$(cat examples/configs/prompts/text-smoke-t2t.txt)" \
#     --no-reuse

engine:
  kind: diffusers
  precision: bf16
  diffusers:
    image: "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.text_server"
    pip:
      - "transformers>=5.10"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "pillow>=10"
      - "psutil>=5.9"
      - "nvidia-ml-py>=12"
    embed_files:
      # Needs-only embed (U53): the server and exactly the two sibling helpers
      # it imports. tests/providers/test_pod_embed_closure.py asserts both
      # directions.
      - "kinoforge.engines.diffusers.servers.text_server"
      - "kinoforge.engines.diffusers.servers._util_stats"
      - "kinoforge.engines.diffusers.servers._upload"
    capability:
      # The pre-spend half of the multimodal gate reads THIS (design §4.1); the
      # pod derives the truth from the checkpoint and /health re-checks it.
      supported_modes: ["t2t"]

models:
  - ref: "hf:Qwen/Qwen3-0.6B"
    kind: base
    target: checkpoints  # informational; transformers manages the HF cache

text:
  engine: transformers
  params:
    max_new_tokens: 256
    do_sample: true
    temperature: 0.7
    top_p: 0.9
    # Qwen3's chat template defaults to a <think> block; the smoke wants a terse
    # answer. Routed to apply_chat_template, never to generate().
    chat_template_kwargs:
      enable_thinking: false

compute:
  provider: runpod
  image: "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
  mode: pod
  # Secure pool: the community pool reclaims pods minutes after create (U65 —
  # seventeen long-boot configs were being killed mid-fetch). Guarded by
  # tests/test_runpod_long_boot_needs_secure_pool.py.
  backend_options:
    runpod:
      cloud_type: secure
  placement:
    min_vram_gb: 16
    disk_gb: 40
    # Pin NVIDIA — without an allowlist the offer chooser can pick an AMD
    # Instinct that RunPod's create-pod mutation rejects with HTTP 500.
    accelerators:
      - "NVIDIA RTX A4000"
      - "NVIDIA RTX A5000"
      - "NVIDIA L4"
      - "NVIDIA GeForce RTX 4090"
      - "NVIDIA GeForce RTX 3090"
  lifecycle:
    boot_timeout: 20m
    idle_timeout: 10m
    job_timeout: 10m
    time_buffer: 3m
    max_lifetime: 45m
    budget: 1.0
    heartbeat_interval_s: 30

# spec.model is informational for text cfgs (TextStage never POSTs /generate),
# but test_no_unknown_slug_for_example_configs asserts DiffusersEngine.
# model_identity is non-empty for every shipped config.
spec:
  model: "qwen3-0.6b-bf16"
```

- [ ] **Step 4: Write the SmolVLM smoke config**

Same file with these differences (copy the Qwen3 file, then edit):

```yaml
# examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml
# `kinoforge text` smoke config — VISION-LANGUAGE model (modes: t2t, it2t).
#
# SmolVLM-256M-Instruct (0.26 B params, 0.5 GB, Apache 2.0, Idefics3) is the
# smallest credible VLM in the 2026-10-04 survey and its architecture has been
# stable in transformers since 4.46 — a smoke should fail on kinoforge, not on
# a library version. Qwen3.5-0.8B is the stronger tiny VLM (also Apache 2.0,
# 1.75 GB) and the documented alternative: swap the ref and nothing else.
#
# Usage:
#   pixi run kinoforge text \
#     --config examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml \
#     --prompt "$(cat examples/configs/prompts/text-smoke-it2t.txt)" \
#     --image output/<some>.png \
#     --no-reuse
```

- `pip` gains `- "num2words>=0.5"` with the comment `# Idefics3's processor imports num2words for its image-token phrasing.`
- `capability.supported_modes: ["t2t", "it2t"]`
- `models[0].ref: "hf:HuggingFaceTB/SmolVLM-256M-Instruct"`
- `text.params`: `max_new_tokens: 128`, `do_sample: false` (deterministic captions), NO `chat_template_kwargs` (the Idefics3 template takes none).
- `spec.model: "smolvlm-256m-bf16"`; `boot_timeout: 20m` (the long-boot guard requires the secure pin, already present).

- [ ] **Step 5: Write the quality config (offline-validated only — the header says so)**

```yaml
# examples/configs/runpod-diffusers-qwen3_8-27b-it2t.yaml
# `kinoforge text` QUALITY config — Qwen3.8-27B bf16 on an 80 GB card
# (modes: t2t, it2t).
#
# *** OFFLINE-VALIDATED ONLY. This config has NOT been live-fired. ***
# It loads, renders a provision payload under the RunPod ceiling, and passes
# every config sweep; the missing proof is one real run — a bf16 27 B load on
# an A100-80GB (~56 GB download, ~$1.50-2.50 for the boot + one completion).
# Record that run in successful-generations.md before trusting this file.
# (Precedent: the Replicate image engine shipped the same way, stated plainly.)
#
# Why this model: first in its class on the Artificial Analysis Intelligence
# Index (v4.3.2 = 34), GPQA Diamond 89.2, LiveCodeBench v6 90.3, SWE-bench Pro
# 61.7; dense 27.8 B incl. a vision encoder, 262K native context, Apache 2.0.
# Nothing stronger fits one card — everything above Gemma-4-31B on LMArena is
# a 295 B+ MoE. See docs/superpowers/research/2026-10-04-open-weight-llm-survey.md.
#
# Usage:
#   pixi run kinoforge text \
#     --config examples/configs/runpod-diffusers-qwen3_8-27b-it2t.yaml \
#     --prompt "..." [--image a.png ...] \
#     --no-reuse
```

- `capability.supported_modes: ["t2t", "it2t"]`
- `models[0].ref: "hf:Qwen/Qwen3.8-27B"`
- `text.params`: `max_new_tokens: 1024`, `do_sample: true`, `temperature: 0.7`, `top_p: 0.9`, `chat_template_kwargs: {enable_thinking: false}`
- `placement`: `min_vram_gb: 80`, `disk_gb: 150`, accelerators `"NVIDIA A100 80GB PCIe"`, `"NVIDIA A100-SXM4-80GB"`, `"NVIDIA H100 80GB HBM3"`, `"NVIDIA H100 PCIe"`
- `lifecycle`: `boot_timeout: 45m`, `idle_timeout: 15m`, `job_timeout: 20m`, `time_buffer: 5m`, `max_lifetime: 90m`, `budget: 5.0`, `heartbeat_interval_s: 30`
- `spec.model: "qwen3.8-27b-bf16"`

- [ ] **Step 6: Register in `EXAMPLE_CONFIGS` and measure the payloads**

In `tests/test_examples.py` line 33 add the three filenames to `EXAMPLE_CONFIGS` (and update the `# AC1 — All 9 example configs` comment to 12).

Measure each config's rendered env size:

```bash
pixi run python -c "
from pathlib import Path
from tests.providers.test_env_payload_ceiling import _rendered_env_bytes
for n in ['runpod-diffusers-qwen3-0_6b-t2t','runpod-diffusers-smolvlm-256m-it2t','runpod-diffusers-qwen3_8-27b-it2t']:
    print(n, _rendered_env_bytes(Path('examples/configs')/f'{n}.yaml'))
"
```

Add a `_BASELINE_BYTES` row per config with the printed value, and a dated `#:` note above the dict in the file's existing style:

```python
#: 2026-10-XX (kinoforge text, Task 8): three new rows. The text server embeds
#: `text_server.py` + `_util_stats.py` + `_upload.py` only (needs-only, U53), so
#: these sit far below the Wan-server configs — expect ~25-35 KB each.
```

Replace `XX` with the actual day. If any value exceeds 101 000 B, STOP: shrink `text_server.py` docstrings before touching anything else, and report the number.

- [ ] **Step 7: Regenerate the launch goldens — AFTER formatting**

```bash
git add examples/configs tests/test_examples.py tests/providers/test_env_payload_ceiling.py tests/core/test_text_config.py
pixi run pre-commit run --all-files
pixi run python tools/snapshot_launch_payloads.py
git status --short tests/providers/golden/
```

Expected: exactly three `??` (untracked) files under `tests/providers/golden/launch_payloads/` named after the new configs and NO `M` lines. If any existing golden moved, do not commit — find out why (`git diff tests/providers/golden/`) and report.

- [ ] **Step 8: Run every sweep; commit**

Run: `pixi run pytest tests/core/test_text_config.py tests/test_examples.py tests/providers tests/test_runpod_long_boot_needs_secure_pool.py tests/cli/test_shipped_cfg_want_stages_sweep.py tests/validation tests/test_layer_r_backcompat.py tests/engines/test_bakeable_steps_set_u_safe.py tests/engines/test_diffusers_optional_cfg_defaults.py -q` → pass. If `tests/validation/test_shipped_configs_capabilities.py` reports a capability-gap ERROR for a text config, read the error — it names a provider capability the config's lifecycle demands — and adjust the lifecycle block (not the test).
Run: `pixi run kinoforge text -c examples/configs/runpod-diffusers-qwen3-0_6b-t2t.yaml --prompt "$(cat examples/configs/prompts/text-smoke-t2t.txt)" --dry-run` → exit 0, `mode: t2t`. Then the same with `--image examples/configs/prompts/../../../output/*.png`-style real PNG → exit 2 naming `it2t`. Then `kinoforge doctor -c <each config>` → no ERROR rows (WARN about absent live creds is fine).

```bash
git add examples/configs tests/test_examples.py tests/providers tests/core/test_text_config.py
git commit -m "feat(configs): three kinoforge text configs, smoke prompts, payload baselines + goldens"
```

---

### Task 9: Documentation

**Goal:** `docs/configuration.md` documents the `text:` block and the §3.3 rules; `docs/engines.md` documents text engines as a fourth engine family and the `text` stage term; `README.md` shows the quick usage; the `successful-generations.md` preamble covers non-video generations; `PROGRESS.md` records plan path and status.

**Files:**
- Modify: `docs/configuration.md` (new `## text:` section after `## image:`, line 210+), `docs/engines.md` (new `## Text engines` section after `## Interpolators`), `README.md` (quick usage), `successful-generations.md` (lines 3-7), `PROGRESS.md` (Pointers entry written during design: flip "IN DESIGN" to "IN FLIGHT", add the plan path and the checklist).

**Acceptance Criteria:**
- [ ] `docs/configuration.md` has a `## text:` section with the full YAML shape, the mode-derivation rule, the prompt precedence, the `port` seam note, and one line per §3.3 refusal.
- [ ] `docs/engines.md` has a `## Text engines` section: the `TextEngine` surface, the `text_server.py` routes table, the `submit_and_poll` schema choice, the `/health` `text` capability, and the `_upload.py` duplicate note (§6.4).
- [ ] `README.md` quick usage includes a `kinoforge text` example with `--image`.
- [ ] `successful-generations.md` line 3 reads "every qualifying successful kinoforge generation (video, image or text)" and the axis list includes `t2t, it2t`.
- [ ] `PROGRESS.md` Pointers entry names the plan and `.tasks.json`, lists Tasks 1-10 with status, and the next action.
- [ ] `pixi run pre-commit run --all-files` green.

**Verify:** `rg -n '^## text:' docs/configuration.md && rg -n '^## Text engines' docs/engines.md && rg -n 'kinoforge text' README.md && rg -n 't2t, it2t' successful-generations.md` → four hits; `pixi run pytest tests/test_source_audit.py -q` → pass.

**Steps:**

- [ ] **Step 1: `docs/configuration.md` — add after the `## image:` section**

```markdown
## `text:` (optional, text generation on a reserved pod)

`kinoforge text` runs one chat completion against an open-weight LLM on a pod
kinoforge books and tears down. A text config is a **pod config** — it keeps
`engine:`, `models:` and `compute:` — plus this block
(design: `docs/superpowers/specs/2026-10-04-text-command-design.md`):

```yaml
engine:
  kind: diffusers                          # the only engine with an arbitrary server_cmd
  precision: bf16
  diffusers:
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.text_server"]
    pip: ["transformers>=5.10", "accelerate>=1.0", "fastapi>=0.115", "uvicorn>=0.30",
          "pillow>=10", "psutil>=5.9", "nvidia-ml-py>=12"]
    embed_files: ["kinoforge.engines.diffusers.servers.text_server",
                  "kinoforge.engines.diffusers.servers._util_stats",
                  "kinoforge.engines.diffusers.servers._upload"]
    capability:
      supported_modes: ["t2t"]             # ["t2t", "it2t"] for a vision-language model
models:
  - {ref: "hf:Qwen/Qwen3-0.6B", kind: base, target: checkpoints}
text:
  engine: transformers                     # text-engine registry key
  prompt: "..."                            # optional default
  system: "..."                            # optional system turn
  params:                                  # opaque pass-through to generate()
    max_new_tokens: 256
    temperature: 0.7
    chat_template_kwargs: {enable_thinking: false}   # goes to the chat template instead
  port: 8000                               # pod port; only the default is used today
compute: { ... }
```

**The mode is derived, never typed.** No `--image` → `t2t` (text-generation);
one or more `--image PATH` (PNG/JPEG) → `it2t` (image-text-to-text). Images go
to the pod over `PUT /upload` with a sha256 cross-check.

**Prompt precedence:** `--prompt` > `text.prompt` > top-level `prompt:`; all
absent is refused.

**The multimodal gate runs twice.** Before any pod work the CLI refuses a mode
the config did not declare in `capability.supported_modes` (exit 2). On the pod
the server DERIVES its modes from the checkpoint (a processor with an image
processor → `it2t`) and reports them in `/health`; `TextStage` refuses before
any upload if the declaration lied.

**Refused at load** (each message names the key): `engine.kind` other than
`diffusers`; a missing or empty `capability.supported_modes`, or one naming a
non-text mode; a top-level `mode:`; `upscale:`, `interpolate:`, `keyframe:` or
`loras:` (silently inert on a text pod); `models` with anything but exactly one
`kind: base` entry.

**Output.** stdout carries the completion and nothing else. Two files land in
`output.dir`: `{ts}_text_{engine}_{model}_{slug}.txt` (the text) and the
`.json` sibling (prompt, system, mode, images with sha256 and pod path, model,
params, text, usage, finish reason, elapsed seconds, run id, kinoforge version).

Shipped configs: `runpod-diffusers-qwen3-0_6b-t2t.yaml` (text-only smoke),
`runpod-diffusers-smolvlm-256m-it2t.yaml` (vision smoke),
`runpod-diffusers-qwen3_8-27b-it2t.yaml` (quality; offline-validated only).
```

- [ ] **Step 2: `docs/engines.md` — add after `## Interpolators`**

```markdown
## Text engines

The fourth engine family (after video, image, upscale/interpolate). A
`TextEngine` (`core/interfaces.py`) is shaped like `UpscalerEngine`: a registry
key (`registry.register_text_engine`, duplicate-rejecting), a compute flag, a
composable `render_provision` fragment, and the calls `TextStage` makes against
a booted pod — `health`, `upload_image`, `complete`, `validate_spec`,
`model_identity`. Every pod-facing call takes `cfg` so the engine reads
`text.port`; that, and the fragment being composed in the RUNTIME phase, are
the two seams a sidecar launch beside a video server would use.

### `transformers` (`text_engines/transformers/`)

Controller-side client for `engines/diffusers/servers/text_server.py`:

| route | contract |
|---|---|
| `GET /health` | `ready`, `model`, `supported_modes` (DERIVED on the pod), `capabilities: ["text", "upload"]`, `default_max_new_tokens`, torch facts |
| `GET /util` | the five `UtilSnapshot` fields |
| `PUT /upload` | PNG/JPEG only; `X-Filename` sanitised; `{"path", "size", "sha256"}` |
| `POST /text` | `{prompt, system?, images: [pod paths], params}` → `{job_id}`; 503 while loading; 400 on images against a text-only model |
| `GET /text/status/{id}` | `{"state": queued|running|done|error, "result"?, "error"?}` |

The status schema is `submit_and_poll`'s (`state`/`result`), not `/generate`'s
(`status`/`filename`): the completion travels inline, so there is no artifact
file and no `/artifacts` route. `/health` advertises `text`, the stage term
`Config.capability_key()` emits for a text config, so warm-attach matches text
pods to text configs with the same base model.

`servers/_upload.py` holds the upload handler. `wan_t2v_server.py` keeps its
own inline copy on purpose: migrating it moves sixteen configs' embed sets,
goldens and payload baselines and needs a Wan-pod witness
(design §6.4 / §13.5).
```

- [ ] **Step 3: README quick usage**

Under the existing quick-usage examples add:

```markdown
Text generation on a reserved pod (open-weight LLM; `--image` selects the
vision mode, refused before any spend when the model is text-only):

```bash
pixi run kinoforge text \
  -c examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml \
  --prompt "Describe this image in two sentences." \
  --image output/some-frame.png \
  --no-reuse
```
```

- [ ] **Step 4: `successful-generations.md` preamble (lines 3-5)**

Change "every qualifying successful kinoforge video generation" to "every qualifying successful kinoforge generation (video, image or text)" and the axis list to `(t2v, i2v, flf2v, keyframe, t2i, t2t, it2t, ...)`.

- [ ] **Step 5: `PROGRESS.md`**

In the Pointers entry added during design: change `**IN DESIGN —` to `**IN FLIGHT —`, add `plan docs/superpowers/plans/2026-10-04-text-command.md (+ .tasks.json)`, and a checklist `Tasks 1-9 done; Task 10 (live smokes) next`. Keep the RESUME SNAPSHOT untouched until Task 10 ships.

- [ ] **Step 6: Lint and commit**

```bash
git add docs/configuration.md docs/engines.md README.md successful-generations.md PROGRESS.md
pixi run pre-commit run --all-files
git commit -m "docs: kinoforge text — configuration, engines, README, generations-log preamble"
```

---

### Task 10: Live smokes — RED scaffold, then two real runs, then the log

**Goal:** Prove the command end to end on real hardware: a `t2t` run on Qwen3-0.6B and an `it2t` run on SmolVLM-256M, each through the real CLI with `--no-reuse`, evidence captured before assertions, the util probe polled during the run, teardown verified, outputs READ and judged, and two new `successful-generations.md` sections written.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation ("run your testing on an ultrasmall model"). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_text_command_smoke.py` (RED scaffold committed BEFORE any spend)
- Create: `tests/live/evidence/<YYYY-MM-DD>-text-command/` (stdout/stderr/.txt/.json per run)
- Modify: `successful-generations.md` (two new sections + TOC lines), `PROGRESS.md` (RESUME SNAPSHOT + Pointers)

**Acceptance Criteria:**
- [ ] The scaffold is committed and `git status` is clean BEFORE `pixi run preflight` runs; `pixi run preflight` exits 0.
- [ ] `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_text_command_smoke.py -v` → both tests PASS; each writes `stdout.txt`, `stderr.txt`, the `.txt` and `.json` into the evidence dir BEFORE asserting.
- [ ] t2t run: exit 0; stdout equals the `.txt` content plus a newline; `.json` has `mode == "t2t"`, `usage.completion_tokens > 0`, `finish_reason in {"stop", "length"}`.
- [ ] it2t run: exit 0; `.json` has `mode == "it2t"`, `images[0].sha256` equal to the local PNG's sha256, `usage.completion_tokens > 0`.
- [ ] During each run the util probe (CLAUDE.md "Live smoke monitoring") was polled every 60-90 s and the readings recorded in the evidence dir as `util.log`; no run sat at 0 % GPU for three consecutive probes without being destroyed.
- [ ] After each run `pixi run kinoforge list` prints BOTH `No running instances.` and `No instances recorded in ledger.` (a `⚠ launching — pod not confirmed` row is an accepted terminal state per CLAUDE.md; wait it out, do not `destroy`).
- [ ] Each completion was READ and judged for coherence and prompt adherence; the verdict (PASS or ⚠️ with reasons) is recorded in the `successful-generations.md` entry.
- [ ] `successful-generations.md` gains §37 (`kinoforge text` / t2t / Qwen3-0.6B) and §38 (it2t / SmolVLM-256M) following the §36 schema (Stack triple, Mode, kinoforge version, First-success SHA, Date local TZ, Layer/phase, capability axis, exact command, cfg, input, verdict), plus TOC lines.
- [ ] `PROGRESS.md` RESUME SNAPSHOT updated: plan complete, spend total, next action.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_text_command_smoke.py -v` → 2 passed; then `pixi run kinoforge list` → both teardown lines.

**Steps:**

- [ ] **Step 1: Write the RED scaffold and COMMIT it**

```python
# tests/live/test_text_command_smoke.py
"""Live smokes — `kinoforge text` on an ultrasmall text-only model and an
ultrasmall vision-language model (design §11.2).

RED scaffold committed BEFORE the live spend per CLAUDE.md. Both runs go through
the real CLI as subprocesses with `--no-reuse` and `--output-dir tmp_path`, so
the repo output/ guard stays clean. Evidence lands under
``tests/live/evidence/<local date>-text-command/`` BEFORE any assertion.
Prompts are read VERBATIM from examples/configs/prompts/text-smoke-*.txt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_ROOT = Path(__file__).parent.parent.parent
_CFG_T2T = _ROOT / "examples" / "configs" / "runpod-diffusers-qwen3-0_6b-t2t.yaml"
_CFG_IT2T = _ROOT / "examples" / "configs" / "runpod-diffusers-smolvlm-256m-it2t.yaml"
_PROMPT_T2T = _ROOT / "examples" / "configs" / "prompts" / "text-smoke-t2t.txt"
_PROMPT_IT2T = _ROOT / "examples" / "configs" / "prompts" / "text-smoke-it2t.txt"
_INPUT_PNG = _ROOT / "output" / "20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png"
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / f"{datetime.now():%Y-%m-%d}-text-command"


def _run_text(cfg: Path, prompt_file: Path, out_dir: Path, tag: str, *extra: str) -> tuple[subprocess.CompletedProcess[str], Path, dict]:  # type: ignore[type-arg]
    """Run the CLI, write evidence FIRST, return (proc, txt_path, sidecar)."""
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603,S607
        [
            "pixi", "run", "kinoforge", "text",
            "--config", str(cfg),
            "--prompt", prompt_file.read_text(),
            "--output-dir", str(out_dir),
            "--no-reuse",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=2400,
        check=False,
    )
    (_EVIDENCE_DIR / f"{tag}-stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / f"{tag}-stderr.txt").write_text(proc.stderr)
    txts = sorted(out_dir.glob("*_text_*.txt"))
    jsons = sorted(out_dir.glob("*_text_*.json"))
    for p in [*txts, *jsons]:
        shutil.copy2(p, _EVIDENCE_DIR / f"{tag}-{p.name}")
    assert proc.returncode == 0, proc.stderr
    assert len(txts) == 1 and len(jsons) == 1, (txts, jsons)
    return proc, txts[0], json.loads(jsons[0].read_text())


def _assert_torn_down() -> None:
    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"], capture_output=True, text=True, timeout=60, check=False
    )
    assert "No running instances." in ledger.stdout, ledger.stdout
    assert "No instances recorded in ledger." in ledger.stdout, ledger.stdout


@pytest.mark.live
def test_t2t_on_qwen3_0_6b(tmp_path: Path) -> None:
    assert _CFG_T2T.exists() and _PROMPT_T2T.exists()
    proc, txt, sidecar = _run_text(_CFG_T2T, _PROMPT_T2T, tmp_path, "t2t")
    text = txt.read_text()
    assert text.strip(), "empty completion"
    assert proc.stdout == (text if text.endswith("\n") else text + "\n")
    assert sidecar["mode"] == "t2t"
    assert sidecar["images"] == []
    assert sidecar["usage"]["completion_tokens"] > 0
    assert sidecar["finish_reason"] in {"stop", "length"}
    assert sidecar["model"] == "hf:Qwen/Qwen3-0.6B"
    _assert_torn_down()


@pytest.mark.live
def test_it2t_on_smolvlm_256m(tmp_path: Path) -> None:
    if not _INPUT_PNG.exists():
        pytest.skip(
            f"input PNG missing ({_INPUT_PNG.name}); regenerate with "
            "`kinoforge image -c examples/configs/luma-uni1-t2i.yaml`"
        )
    assert _CFG_IT2T.exists() and _PROMPT_IT2T.exists()
    _, txt, sidecar = _run_text(_CFG_IT2T, _PROMPT_IT2T, tmp_path, "it2t", "--image", str(_INPUT_PNG))
    assert txt.read_text().strip(), "empty completion"
    assert sidecar["mode"] == "it2t"
    assert len(sidecar["images"]) == 1
    assert sidecar["images"][0]["sha256"] == hashlib.sha256(_INPUT_PNG.read_bytes()).hexdigest()
    assert sidecar["usage"]["completion_tokens"] > 0
    _assert_torn_down()
```

```bash
git add tests/live/test_text_command_smoke.py
pixi run pre-commit run --files tests/live/test_text_command_smoke.py
git commit -m "test(live): RED scaffold for the kinoforge text t2t + it2t smokes"
git status --short   # MUST be empty before spend
```

- [ ] **Step 2: Preflight**

Run: `pixi run preflight` → exit 0 (creds present, zero active pods, clean tree). If it fails, fix the reported condition; never proceed on a non-zero exit.

- [ ] **Step 3: Fire the t2t smoke, polling util in parallel**

In one shell (background, controller side):

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_text_command_smoke.py::test_t2t_on_qwen3_0_6b -v -s
```

Every 60-90 s while it runs, find the pod id with `pixi run kinoforge list` and probe:

```bash
pixi run python -c "
from kinoforge.core.dotenv_loader import load_env_file; load_env_file()
import os
from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint
print(RunPodGraphQLUtilEndpoint(api_key=os.environ['RUNPOD_API_KEY']).probe('<pod-id>'))
" | tee -a tests/live/evidence/$(date +%F)-text-command/util.log
```

Expect CPU/memory rising during pip + download (first 2-4 min), then a short GPU blip at load, then the completion (seconds). GPU 0 % for three consecutive probes AFTER `/health` went ready → pull `curl -s https://<pod-id>-8001.proxy.runpod.net/bootstrap.log | tail -40`, `pixi run kinoforge destroy --id <pod-id>`, and debug before retrying. A RunPod create that returns a raw HTTP 500 is almost never an outage — re-measure the payload size first (CLAUDE.md "Known infra gotchas").

- [ ] **Step 4: Verify teardown, then fire the it2t smoke the same way**

`pixi run kinoforge list` → both lines. Then:

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_text_command_smoke.py::test_it2t_on_smolvlm_256m -v -s
```

Same polling, same teardown check. If the it2t run fails inside the server with a template or processor error (the one risk the offline fakes cannot see — see `text_server._run_job`), read `<tag>-stderr.txt` and the pod's `bootstrap.log`, fix the server, re-run the offline server tests, commit, and re-fire. Do not paper over it with a model swap unless the error is model-specific; if it is, Qwen3.5-0.8B is the documented alternative (one ref change in the config + a baseline re-measure).

- [ ] **Step 5: READ the outputs and judge them**

Open `tests/live/evidence/<date>-text-command/t2t-*.txt` and `it2t-*.txt`. For t2t: is it two sentences, does it name the subject, setting and light from `field-realistic.txt`, is it coherent? For it2t: does it describe the §35 image (the Luma UNI-1 still) and name plausible dominant colours? Anything not clearly right gets an explicit ⚠️ with the reason. Token counts cannot see nonsense; this step can.

- [ ] **Step 6: Write §37 and §38 in `successful-generations.md`**

Follow §36's table exactly (`Stack triple`, `Mode`, `kinoforge version` from `pixi run kinoforge --version`, `First-success SHA` = the scaffold commit, `Date (local TZ)` from the `.json`'s run id / `datetime.now()`, `Layer / phase` naming the design and plan paths), then "The new capability axis" (new command, new engine family, new modes), "Exact command", "Cfg", "Input" (prompt file; the PNG's sha256 for §38), "Output" (the completion verbatim, usage, elapsed), "Verdict" from Step 5, "Spend" (pod id, card, minutes, dollars from `kinoforge cost` or the RunPod dashboard), and the evidence dir. Add both to the TOC.

- [ ] **Step 7: Update `PROGRESS.md` and commit everything**

Add a dated RESUME SNAPSHOT section at the top of the snapshot: plan complete (10/10), live-proven with pod ids and total spend, the single next action ("the hooks spec — prompt enhancement + frame QA — is the follow-on; the quality config remains offline-validated"). Flip the Pointers entry to **SHIPPED**.

```bash
git add tests/live/evidence successful-generations.md PROGRESS.md
pixi run pre-commit run --all-files
git commit -m "test(live): kinoforge text live-proven — t2t on Qwen3-0.6B, it2t on SmolVLM-256M (§37, §38)"
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "requiresUserSpecification": false, "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_text_command_smoke.py -v && pixi run kinoforge list", "acceptanceCriteria": ["scaffold committed before spend; preflight exit 0", "both live tests PASS with evidence files written before assertions", "util probe polled every 60-90 s and logged", "kinoforge list shows both teardown lines after each run", "outputs read and judged; verdict recorded", "successful-generations.md §37 + §38 written", "PROGRESS.md snapshot updated"], "modelTier": "frontier"}
```

---

## Plan self-review (done at authoring time)

- **Spec coverage.** §2 surface → Task 7. §3 config/validator/capability key → Task 2. §4 gate (both halves) → Tasks 3 (pre-spend), 4 (server derivation + 400), 6 (stage health gate). §5 engine seam, registry, fragment, composition → Tasks 1, 5. §6 server + `_upload.py` + status schema → Task 4. §7 stage + orchestrator → Task 6. §8 output (stdout, `.txt`, `.json`) → Tasks 6, 7. §9 stage vocabulary → Tasks 2, 4. §10 configs + prompts + sweeps → Task 8. §11 tests → every task; live → Task 10. §12 docs → Task 9. §13 non-scope → not planned, by design.
- **Type consistency.** `TextEngine.health(instance, cfg)`, `upload_image(instance, local_path, cfg)`, `complete(instance, job, cfg, *, cancel_token)` are used with those exact arities in Tasks 1, 5, 6 and the fakes in 6's tests. `TextResult(text, finish_reason, usage, model, elapsed_s)` matches Tasks 4 (server result keys), 5 (mapping) and 6. `base_model_ref` / `declared_modes` accept `Config | Mapping` and are called with a dict in Tasks 5, 6 and with `Config` in Task 7. `modes_for` is the server function Task 4's tests call. The server's `/text/status` returns `state`/`result`, which `submit_and_poll` reads in Task 5.
- **Placeholders.** None intended; the only `XX` is the dated baseline note in Task 8 Step 6, which the engineer fills with the real day.
