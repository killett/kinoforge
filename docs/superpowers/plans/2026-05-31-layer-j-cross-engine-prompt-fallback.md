# Layer J — Cross-engine prompt fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a shared `resolve_prompt(job)` helper through hosted/diffusers/comfyui (+ retrofit fal) so the orchestrator-supplied `Segment.prompt` reaches every backend's request body.

**Architecture:** New pure module `core/prompt_routing.py` exports one function. Hosted/Diffusers gain a `prompt_body_key` cfg field (default `"prompt"`) and route prompt into `body[key]` at submit. ComfyUI reads `job.spec.prompt_node_ids` (mirroring `asset_node_ids`) and writes the prompt into `node_overrides[node_id]["inputs"]["text"]`. `validate_spec` raises only when routing is configured + no prompt available. Fal's inline fallback is replaced by the helper, behavior preserved.

**Tech Stack:** Python 3.11+, pydantic v2 for config, pytest. No new runtime deps.

**Spec:** `docs/superpowers/specs/2026-05-31-cross-engine-prompt-fallback-design.md`

---

## File map

**Create:**
- `src/kinoforge/core/prompt_routing.py` — `resolve_prompt(job)` helper
- `tests/core/test_prompt_routing.py` — helper unit tests
- `examples/configs/comfyui-img2vid.yaml` — *only if* a prompt_node_ids example is needed and `wan.yaml` is unsuitable (deferred — Task 7 uses wan.yaml comments instead)

**Modify:**
- `src/kinoforge/core/config.py` — add `prompt_body_key` to `HostedEngineConfig` (line 118) and `DiffusersEngineConfig` (line 183)
- `src/kinoforge/engines/hosted/__init__.py` — backend ctor + submit (line 191/244), engine ctor + backend() + validate_spec (line 340/473/539)
- `src/kinoforge/engines/diffusers/__init__.py` — backend ctor + submit (line 136/185), engine ctor + backend() + validate_spec (line 285/358/424)
- `src/kinoforge/engines/comfyui/__init__.py` — backend submit (line 327), engine validate_spec (line 634) — both read `prompt_node_ids` from `job.spec`, no ctor change
- `src/kinoforge/engines/fal/__init__.py` — backend submit retrofit (lines 236–243)
- `tests/engines/test_hosted.py` — 6 new tests (5 routing + 1 E2E)
- `tests/engines/test_diffusers.py` — 6 new tests (5 routing + 1 E2E)
- `tests/engines/test_comfyui.py` — 6 new tests
- `tests/core/test_config.py` — 4 round-trip tests (hosted+diffusers `prompt_body_key` default + null)
- `examples/configs/hosted.yaml` — comment line for `prompt_body_key`
- `examples/configs/diffusers.yaml` — same
- `examples/configs/wan.yaml` — commented `prompt_node_ids` example block
- `README.md` — short paragraph in engines/Hosted section
- `PROGRESS.md` — Phase 20 entry

---

### Task 1: Helper module `resolve_prompt`

**Goal:** Produce the pure `resolve_prompt(job) -> str | None` helper consumed by all four engines.

**Files:**
- Create: `src/kinoforge/core/prompt_routing.py`
- Test:   `tests/core/test_prompt_routing.py`

**Acceptance Criteria:**
- [ ] Returns `spec["prompt"]` when it is a non-empty `str`
- [ ] Returns `segments[0].prompt` when spec lacks `"prompt"` and segment prompt is non-empty `str`
- [ ] Spec-prompt wins precedence over segment-prompt (both present → spec wins)
- [ ] Returns `None` when neither is available
- [ ] Returns `None` when spec-prompt is `""` and no segments
- [ ] Returns `None` when spec-prompt is non-`str` (e.g. `int`)
- [ ] Returns `None` when `job.segments == []`
- [ ] Returns segment-prompt when spec-prompt is `""` (empty does not shadow)
- [ ] No imports outside stdlib + `kinoforge.core.interfaces`
- [ ] `pixi run pre-commit run --files src/kinoforge/core/prompt_routing.py tests/core/test_prompt_routing.py` passes (ruff/format/mypy)

**Verify:** `pixi run pytest tests/core/test_prompt_routing.py -v` → 8 passed

**Steps:**

- [ ] **Step 1: Write failing tests**

Write file `tests/core/test_prompt_routing.py`:

```python
"""Unit tests for the shared prompt-routing helper.

Bug catch: orchestrator places the user prompt on Segment.prompt, never in
job.spec. Backends that build body=dict(job.spec) without consulting segments
silently submit empty-prompt jobs (fal.ai shipped the inline fix in Layer-I
Task 13; this helper hoists that pattern for hosted/diffusers/comfyui).
"""

from __future__ import annotations

from kinoforge.core.interfaces import GenerationJob, Segment
from kinoforge.core.prompt_routing import resolve_prompt


def _job(spec: dict, segments: list[Segment]) -> GenerationJob:  # type: ignore[type-arg]
    return GenerationJob(spec=spec, segments=segments, params={})


def test_resolve_returns_spec_prompt_when_set() -> None:
    """Explicit spec.prompt is the canonical source — return as-is."""
    job = _job({"prompt": "explicit"}, [])
    assert resolve_prompt(job) == "explicit"


def test_resolve_returns_segment_prompt_when_spec_lacks_key() -> None:
    """Orchestrator path: spec carries no prompt; fall back to segments[0]."""
    job = _job({}, [Segment(prompt="from-seg", params={}, assets=[])])
    assert resolve_prompt(job) == "from-seg"


def test_resolve_spec_wins_over_segment() -> None:
    """Bug catch: a permissive over-eager helper would clobber an explicit
    config-supplied prompt with the raw segment text."""
    job = _job(
        {"prompt": "explicit"},
        [Segment(prompt="from-seg", params={}, assets=[])],
    )
    assert resolve_prompt(job) == "explicit"


def test_resolve_returns_none_when_neither_present() -> None:
    """No prompt in spec, no segments — helper signals 'nothing to route'."""
    job = _job({}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_none_when_spec_prompt_empty_and_no_segments() -> None:
    """Empty string does not count as a prompt; with no segments → None."""
    job = _job({"prompt": ""}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_none_when_spec_prompt_is_non_str() -> None:
    """Bug catch: dict.get('prompt') may return any type; helper must guard
    so the caller never receives e.g. ``42`` and writes it into a JSON body."""
    job = _job({"prompt": 42}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_none_when_segments_empty() -> None:
    """Empty segment list must not IndexError — return None cleanly."""
    job = _job({}, [])
    assert resolve_prompt(job) is None


def test_resolve_returns_segment_when_spec_prompt_empty_string() -> None:
    """Empty spec.prompt should NOT shadow a valid segment prompt — the
    orchestrator path treats spec absence and spec=='' identically."""
    job = _job(
        {"prompt": ""},
        [Segment(prompt="from-seg", params={}, assets=[])],
    )
    assert resolve_prompt(job) == "from-seg"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_prompt_routing.py -v`
Expected: 8 collected, 8 errored / failed with `ModuleNotFoundError: No module named 'kinoforge.core.prompt_routing'`

- [ ] **Step 3: Implement helper**

Write `src/kinoforge/core/prompt_routing.py`:

```python
"""Cross-engine prompt-routing helper.

The orchestrator places the user's prompt on ``Segment.prompt``, not in
``GenerationJob.spec``. Backends that build their request body from
``job.spec`` alone silently drop the prompt — the same defect FalBackend
patched inline in Layer-I Task 13. This module hoists that pattern into
one pure function shared by every engine.

Pure / no I/O / no state — safe to call from ``submit`` and
``validate_spec`` without side effects.
"""

from __future__ import annotations

from kinoforge.core.interfaces import GenerationJob


def resolve_prompt(job: GenerationJob) -> str | None:
    """Return the prompt to route into the request body, or ``None``.

    Precedence: ``job.spec["prompt"]`` (explicit, config-supplied) wins
    over ``job.segments[0].prompt`` (orchestrator path). Empty strings
    and non-``str`` values do not count.

    Args:
        job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
            prompt to resolve.

    Returns:
        The prompt string, or ``None`` if neither location holds a
        non-empty ``str``.
    """
    spec_prompt = job.spec.get("prompt")
    if isinstance(spec_prompt, str) and spec_prompt:
        return spec_prompt
    if job.segments:
        seg_prompt = getattr(job.segments[0], "prompt", "")
        if isinstance(seg_prompt, str) and seg_prompt:
            return seg_prompt
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/core/test_prompt_routing.py -v`
Expected: 8 passed

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/prompt_routing.py tests/core/test_prompt_routing.py
git add src/kinoforge/core/prompt_routing.py tests/core/test_prompt_routing.py
git commit -m "feat(core): resolve_prompt helper for cross-engine prompt routing (Layer J Task 1)"
```

---

### Task 2: Config — `prompt_body_key` on hosted + diffusers

**Goal:** Add `prompt_body_key: str | None = "prompt"` to `HostedEngineConfig` and `DiffusersEngineConfig`. Round-trip tests prove pydantic round-trips the new field (closes Layer-I cfg-strip defect class proactively).

**Files:**
- Modify: `src/kinoforge/core/config.py` (lines 118–180 for hosted, 183–205 for diffusers)
- Modify: `tests/core/test_config.py`

**Acceptance Criteria:**
- [ ] `HostedEngineConfig(prompt_body_key="prompt")` round-trips via `model_dump()` with the field present
- [ ] `HostedEngineConfig(prompt_body_key=None)` round-trips with `None` preserved
- [ ] Default is `"prompt"` (loading YAML that omits the field yields `"prompt"`)
- [ ] Same three properties on `DiffusersEngineConfig`
- [ ] Loading a YAML with `engine.hosted.prompt_body_key: null` does not raise
- [ ] No ComfyUI cfg change (ComfyUI uses `job.spec.prompt_node_ids` — task 5)
- [ ] `pixi run pre-commit run --files src/kinoforge/core/config.py tests/test_config.py` passes

**Verify:** `pixi run pytest tests/core/test_config.py -v -k prompt_body_key` → 4 new tests passed

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/core/test_config.py`:

```python
def test_hosted_engine_config_prompt_body_key_default() -> None:
    """Bug catch: an absent field must default to "prompt" so existing
    hosted.yaml configs auto-route prompts after Layer J ships."""
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(
        provider="x",
        endpoint="https://x.example/y",
        model="m",
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
        model="m",
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run pytest tests/ -v -k prompt_body_key
```
Expected: 4 tests collected, 4 fail with `ValidationError` or `AttributeError` (field absent from model).

- [ ] **Step 3: Implement config additions**

Edit `src/kinoforge/core/config.py`:

In `HostedEngineConfig` (around line 149, after `asset_paths`):

```python
    asset_paths: dict[str, str] = Field(default_factory=dict)
    prompt_body_key: str | None = "prompt"
```

Update the docstring `Attributes:` block to include:
```
prompt_body_key: Top-level key in the request body where the
    user prompt is written by ``HostedAPIBackend.submit`` when
    the spec does not carry an explicit ``"prompt"``. Defaults
    to ``"prompt"``; set to ``None`` (YAML ``null``) to disable
    routing for endpoints that reject unknown top-level fields.
```

In `DiffusersEngineConfig` (around line 205, after `asset_paths`):

```python
    asset_paths: dict[str, str] = Field(default_factory=dict)
    prompt_body_key: str | None = "prompt"
```

Same docstring addition (s/HostedAPIBackend/DiffusersBackend/).

- [ ] **Step 4: Run tests to verify they pass**

```bash
pixi run pytest tests/ -v -k prompt_body_key
```
Expected: 4 passed

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "feat(config): prompt_body_key on HostedEngineConfig + DiffusersEngineConfig (Layer J Task 2)"
```

---

### Task 3: HostedAPIBackend + Engine wire

**Goal:** `HostedAPIBackend.submit` routes the prompt into `body[prompt_body_key]` when set; `HostedAPIEngine.validate_spec` raises if routing is configured but no prompt is available.

**Files:**
- Modify: `src/kinoforge/engines/hosted/__init__.py`
- Modify: `tests/engines/test_hosted.py`

**Acceptance Criteria:**
- [ ] `submit` writes `segments[0].prompt` into `body["prompt"]` when spec lacks it
- [ ] `submit` preserves explicit `spec["prompt"]` (spec wins over segment)
- [ ] `submit` does nothing when `prompt_body_key` is `None`
- [ ] `validate_spec` raises `ValidationError` when `prompt_body_key` is set and no prompt is available
- [ ] `validate_spec` passes when `prompt_body_key` is `None` and no prompt is available (legacy untouched)
- [ ] E2E YAML→engine.backend wire: a YAML with `engine.hosted.prompt_body_key: "input"` produces a backend that routes into `body["input"]`
- [ ] `pixi run pre-commit run --files src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py` passes

**Verify:** `pixi run pytest tests/engines/test_hosted.py -v` → all hosted tests pass (existing + 6 new)

**Steps:**

- [ ] **Step 1: Write failing tests**

Add to `tests/engines/test_hosted.py` (use existing fixtures / `_make_backend` helper pattern):

```python
def test_submit_falls_back_to_segment_prompt() -> None:
    """submit() routes segments[0].prompt into body["prompt"] when spec lacks it.

    Bug catch: without the helper-driven fallback, an orchestrator-built job
    (which carries the user prompt on Segment, not in spec) would POST a body
    with no prompt to the hosted endpoint, which silently 422s or returns an
    empty-prompt render.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIBackend

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = HostedAPIBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        endpoint="https://x.example/inf",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key="prompt",
    )
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="a fox", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["prompt"] == "a fox"


def test_submit_spec_prompt_wins_over_segment_prompt_hosted() -> None:
    """Explicit spec.prompt is preserved — over-eager fallback would clobber
    a config-supplied wrapper prompt with the raw segment text."""
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIBackend

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = HostedAPIBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        endpoint="https://x.example/inf",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key="prompt",
    )
    job = GenerationJob(
        spec={"model": "m", "params": {}, "prompt": "explicit"},
        segments=[Segment(prompt="from-seg", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["prompt"] == "explicit"


def test_submit_skips_routing_when_prompt_body_key_none() -> None:
    """prompt_body_key=None opts out of routing — body must NOT gain a
    "prompt" key from the segment.

    Bug catch: a leaky fallback that inspects segments unconditionally
    would add unwanted fields to a body shape the endpoint does not accept.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIBackend

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = HostedAPIBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        endpoint="https://x.example/inf",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key=None,
    )
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="ignored", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert "prompt" not in posts[0][1]


def test_validate_spec_raises_when_routing_configured_and_no_prompt() -> None:
    """Opt-in validation: prompt_body_key="prompt" with no prompt anywhere
    must raise before the misconfigured POST reaches the network.

    Bug catch: silent fallthrough would let the empty-body defect resurface
    despite the cfg field signalling user intent to route a prompt.
    """
    import pytest
    from kinoforge.core.errors import ValidationError
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIEngine

    engine = HostedAPIEngine()
    # Simulate ``backend()`` having mirrored the cfg routing key.
    engine._prompt_body_key = "prompt"
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="", params={}, assets=[])],
        params={},
    )
    with pytest.raises(ValidationError, match="prompt_body_key is configured"):
        engine.validate_spec(job)


def test_validate_spec_passes_when_routing_disabled_and_no_prompt() -> None:
    """Legacy YAML without prompt_body_key (or prompt_body_key=None) must
    not gain a new failure mode — validate_spec must still pass for jobs
    that drive the prompt entirely via params.prompt.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIEngine

    engine = HostedAPIEngine()
    engine._prompt_body_key = None  # opt-out
    job = GenerationJob(
        spec={"model": "m", "params": {"prompt": "nested"}},
        segments=[Segment(prompt="", params={}, assets=[])],
        params={},
    )
    engine.validate_spec(job)  # must NOT raise


def test_yaml_prompt_body_key_routes_through_engine_backend() -> None:
    """End-to-end: a YAML config with engine.hosted.prompt_body_key="input"
    produces a backend whose submit writes into body["input"].

    Bug catch: this closes the Layer-I cfg-strip defect class (commit
    484e368) for the new field — pydantic must NOT silently drop
    prompt_body_key on the path from YAML → Config → cfg dict →
    engine.backend(cfg) → HostedAPIBackend.
    """
    import yaml as _yaml
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIEngine

    yaml_doc = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: p
    endpoint: "https://x.example/y"
    model: "m"
    api_key_env: "X_KEY"
    prompt_body_key: input
models:
  - {ref: "https://x.example/m.safetensors", kind: base, target: c}
lifecycle:
  budget: 1.0
"""
    cfg = Config.model_validate(_yaml.safe_load(yaml_doc))
    cfg_dict = cfg.model_dump()

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    engine = HostedAPIEngine(http_post=fake_post, http_get=lambda url: {"status": "done"})
    backend = engine.backend(None, cfg_dict)
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="from-seg", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["input"] == "from-seg"
    assert "prompt" not in posts[0][1]  # only the configured key, not the default
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run pytest tests/engines/test_hosted.py -v -k "fall|spec_prompt_wins|skips_routing|routing_configured|routing_disabled|yaml_prompt_body_key"
```
Expected: 6 tests fail with `TypeError: unexpected keyword argument 'prompt_body_key'` or `KeyError: 'prompt'`.

- [ ] **Step 3: Implement HostedAPIBackend changes**

Edit `src/kinoforge/engines/hosted/__init__.py`:

In `HostedAPIBackend.__init__` (after `asset_paths` arg, around line 200):

```python
        asset_paths: dict[str, str] | None = None,
        prompt_body_key: str | None = "prompt",
    ) -> None:
```

Add docstring entry:
```
prompt_body_key: Top-level body key written from
    ``resolve_prompt(job)`` when no explicit ``spec["prompt"]``
    is provided. ``None`` / empty disables routing entirely.
```

Persist field:
```python
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
        self._prompt_body_key: str | None = prompt_body_key
```

In `submit` (line 244), insert at the top of the method body after `body = dict(job.spec)`:

```python
        from kinoforge.core.prompt_routing import resolve_prompt  # local — avoid circular at module load

        body = dict(job.spec)
        if self._prompt_body_key:
            prompt = resolve_prompt(job)
            if prompt is not None:
                body.setdefault(self._prompt_body_key, prompt)
        for role, dot_path in self._asset_paths.items():
            ...  # existing loop unchanged
```

(The local import keeps `core/prompt_routing` free of any engine-package import cycle risk. Top-level import would be acceptable too — `prompt_routing` only imports from `core.interfaces`.)

- [ ] **Step 4: Implement HostedAPIEngine wire**

In `HostedAPIEngine.__init__` (after `self._asset_paths = {}` around line 392), add:

```python
        # Prompt-routing config: top-level body key mirrored from
        # ``cfg["engine"]["hosted"]["prompt_body_key"]`` at backend()
        # time. ``None`` disables routing.
        self._prompt_body_key: str | None = "prompt"
```

In `HostedAPIEngine.backend()` (around line 494, after building `asset_paths`):

```python
        self._asset_paths = asset_paths
        prompt_body_key_raw = hosted_cfg.get("prompt_body_key", "prompt")
        prompt_body_key: str | None = (
            prompt_body_key_raw if isinstance(prompt_body_key_raw, str) and prompt_body_key_raw
            else None
        )
        self._prompt_body_key = prompt_body_key
        return HostedAPIBackend(
            ...
            asset_paths=asset_paths,
            prompt_body_key=prompt_body_key,
        )
```

In `HostedAPIEngine.validate_spec` (line 539, append after existing checks before method ends):

```python
        if self._prompt_body_key:
            from kinoforge.core.prompt_routing import resolve_prompt

            if resolve_prompt(job) is None:
                raise ValidationError(
                    "hosted prompt_body_key is configured but no prompt found in "
                    "job.spec or segments[0] — set spec.prompt, set "
                    "segments[0].prompt, or disable routing with "
                    "engine.hosted.prompt_body_key: null"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pixi run pytest tests/engines/test_hosted.py -v
```
Expected: all hosted tests pass (existing count + 6 new).

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py
git add src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py
git commit -m "feat(engines/hosted): route prompt via resolve_prompt helper (Layer J Task 3)"
```

---

### Task 4: DiffusersBackend + Engine wire

**Goal:** Mirror Task 3 for Diffusers.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`
- Modify: `tests/engines/test_diffusers.py`

**Acceptance Criteria:**
- [ ] `submit` writes `segments[0].prompt` into `body["prompt"]` when spec lacks it
- [ ] `submit` preserves explicit `spec["prompt"]`
- [ ] `submit` does nothing when `prompt_body_key` is `None`
- [ ] `validate_spec` raises when routing configured + no prompt
- [ ] `validate_spec` passes when routing disabled + no prompt
- [ ] E2E YAML→engine.backend wire test for `engine.diffusers.prompt_body_key`
- [ ] `pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers.py` passes

**Verify:** `pixi run pytest tests/engines/test_diffusers.py -v` → all diffusers tests pass (existing + 6 new)

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/engines/test_diffusers.py`. The shape mirrors Task 3 but is written out in full here so the engineer reading Task 4 standalone has the complete code:

```python
def test_submit_falls_back_to_segment_prompt_diffusers() -> None:
    """submit() routes segments[0].prompt into body["prompt"] when spec lacks it.

    Bug catch: an orchestrator-built diffusers job (which carries the user
    prompt on Segment, not in spec) would POST a body with no prompt — the
    diffusers server then either 422s on missing-prompt or runs an
    empty-prompt render that wastes GPU time.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.diffusers import DiffusersBackend

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = DiffusersBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key="prompt",
    )
    job = GenerationJob(
        spec={"pipeline": "p", "scheduler": "s"},
        segments=[Segment(prompt="a fox", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["prompt"] == "a fox"


def test_submit_spec_prompt_wins_over_segment_prompt_diffusers() -> None:
    """Explicit spec.prompt is preserved — over-eager fallback would clobber
    a config-supplied wrapper prompt with the raw segment text."""
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.diffusers import DiffusersBackend

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = DiffusersBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key="prompt",
    )
    job = GenerationJob(
        spec={"pipeline": "p", "scheduler": "s", "prompt": "explicit"},
        segments=[Segment(prompt="from-seg", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["prompt"] == "explicit"


def test_submit_skips_routing_when_prompt_body_key_none_diffusers() -> None:
    """prompt_body_key=None opts out — body must NOT gain a "prompt" key
    from the segment, otherwise a strict diffusers server may reject the
    unexpected field."""
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.diffusers import DiffusersBackend

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = DiffusersBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key=None,
    )
    job = GenerationJob(
        spec={"pipeline": "p", "scheduler": "s"},
        segments=[Segment(prompt="ignored", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert "prompt" not in posts[0][1]


def test_validate_spec_raises_when_routing_configured_and_no_prompt_diffusers() -> None:
    """Opt-in validation: prompt_body_key set with no prompt available must
    raise before the misconfigured POST reaches the diffusers server."""
    import pytest
    from kinoforge.core.errors import ValidationError
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.diffusers import DiffusersEngine

    engine = DiffusersEngine()
    engine._prompt_body_key = "prompt"
    job = GenerationJob(
        spec={"pipeline": "p", "scheduler": "s"},
        segments=[Segment(prompt="", params={}, assets=[])],
        params={},
    )
    with pytest.raises(ValidationError, match="prompt_body_key is configured"):
        engine.validate_spec(job)


def test_validate_spec_passes_when_routing_disabled_and_no_prompt_diffusers() -> None:
    """Legacy YAML without prompt_body_key (or =None) keeps existing behavior —
    no new failure mode for jobs that drive the prompt entirely via
    params.prompt nested inside the body."""
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.diffusers import DiffusersEngine

    engine = DiffusersEngine()
    engine._prompt_body_key = None
    job = GenerationJob(
        spec={"pipeline": "p", "scheduler": "s", "params": {"prompt": "nested"}},
        segments=[Segment(prompt="", params={}, assets=[])],
        params={},
    )
    engine.validate_spec(job)  # must NOT raise


def test_yaml_prompt_body_key_routes_through_engine_backend_diffusers() -> None:
    """End-to-end: YAML config with engine.diffusers.prompt_body_key="input"
    produces a backend whose submit writes into body["input"]. Closes the
    Layer-I cfg-strip defect class for the new field."""
    import yaml as _yaml
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.diffusers import DiffusersEngine

    yaml_doc = """
engine:
  kind: diffusers
  precision: fp16
  diffusers:
    base_url: "http://127.0.0.1:8000"
    prompt_body_key: input
models:
  - {ref: "https://x.example/m.safetensors", kind: base, target: c}
lifecycle:
  budget: 1.0
"""
    cfg = Config.model_validate(_yaml.safe_load(yaml_doc))
    cfg_dict = cfg.model_dump()

    posts: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> dict:
        posts.append((url, body))
        return {"job_id": "j1"}

    engine = DiffusersEngine(http_post=fake_post, http_get=lambda url: {"status": "done"})
    backend = engine.backend(None, cfg_dict)
    job = GenerationJob(
        spec={"pipeline": "p", "scheduler": "s"},
        segments=[Segment(prompt="from-seg", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["input"] == "from-seg"
    assert "prompt" not in posts[0][1]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run pytest tests/engines/test_diffusers.py -v -k "fall|spec_prompt_wins|skips_routing|routing_configured|routing_disabled|yaml_prompt_body_key"
```
Expected: 6 fail.

- [ ] **Step 3: Implement DiffusersBackend changes**

Edit `src/kinoforge/engines/diffusers/__init__.py`:

In `DiffusersBackend.__init__` (line 144 area), append kwarg + persist:

```python
        asset_paths: dict[str, str] | None = None,
        prompt_body_key: str | None = "prompt",
    ) -> None:
        ...
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
        self._prompt_body_key: str | None = prompt_body_key
```

Add docstring entry for `prompt_body_key`:
```
prompt_body_key: Top-level body key written from
    ``resolve_prompt(job)`` when no explicit ``spec["prompt"]``
    is provided. ``None`` / empty disables routing entirely.
```

In `submit` (line 185), after `body = dict(job.spec)`, insert the routing block:

```python
        from kinoforge.core.prompt_routing import resolve_prompt

        body = dict(job.spec)
        if self._prompt_body_key:
            prompt = resolve_prompt(job)
            if prompt is not None:
                body.setdefault(self._prompt_body_key, prompt)
        for role, dot_path in self._asset_paths.items():
            ...  # existing loop unchanged
```

- [ ] **Step 4: Implement DiffusersEngine wire**

In `DiffusersEngine.__init__` (around line 326), after `self._asset_paths = {}`, add:

```python
        # Prompt-routing config: top-level body key mirrored from
        # ``cfg["engine"]["diffusers"]["prompt_body_key"]`` at backend()
        # time. ``None`` disables routing.
        self._prompt_body_key: str | None = "prompt"
```

In `DiffusersEngine.backend()` (around line 386, after `self._asset_paths = asset_paths`):

```python
        self._asset_paths = asset_paths
        prompt_body_key_raw = diffusers_cfg.get("prompt_body_key", "prompt")
        prompt_body_key: str | None = (
            prompt_body_key_raw if isinstance(prompt_body_key_raw, str) and prompt_body_key_raw
            else None
        )
        self._prompt_body_key = prompt_body_key
        return DiffusersBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe_profile=self._probe,
            sleep=self._sleep,
            asset_paths=asset_paths,
            prompt_body_key=prompt_body_key,
        )
```

In `DiffusersEngine.validate_spec` (line 424), append after existing checks:

```python
        if self._prompt_body_key:
            from kinoforge.core.prompt_routing import resolve_prompt

            if resolve_prompt(job) is None:
                raise ValidationError(
                    "diffusers prompt_body_key is configured but no prompt found in "
                    "job.spec or segments[0] — set spec.prompt, set "
                    "segments[0].prompt, or disable routing with "
                    "engine.diffusers.prompt_body_key: null"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pixi run pytest tests/engines/test_diffusers.py -v
```
Expected: all pass.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers.py
git add src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers.py
git commit -m "feat(engines/diffusers): route prompt via resolve_prompt helper (Layer J Task 4)"
```

---

### Task 5: ComfyUIBackend + Engine wire (spec-level)

**Goal:** ComfyUI submit reads `job.spec.prompt_node_ids` (mirroring `asset_node_ids`), routes `resolve_prompt(job)` into `node_overrides[node_id]["inputs"]["text"]`. Engine `validate_spec` raises if `prompt_node_ids` configured + no prompt.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py`
- Modify: `tests/engines/test_comfyui.py`

**Acceptance Criteria:**
- [ ] `submit` injects prompt into `node_overrides[node_id]["inputs"]["text"]` for each entry in `spec["prompt_node_ids"]`
- [ ] `submit` does nothing when `spec["prompt_node_ids"]` is absent or empty
- [ ] `submit` does NOT overwrite an explicit `node_overrides[node_id]["inputs"]["text"]` (setdefault semantics)
- [ ] spec-prompt wins over segment-prompt
- [ ] `validate_spec` raises when `spec["prompt_node_ids"]` non-empty + no prompt available
- [ ] `validate_spec` passes when `spec["prompt_node_ids"]` empty/absent + no prompt
- [ ] `pixi run pre-commit run --files src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py` passes

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v` → all comfyui tests pass (existing + 6 new)

**Steps:**

- [ ] **Step 1: Write failing tests**

Append all 6 tests to `tests/engines/test_comfyui.py`. The existing file constructs `ComfyUIBackend` directly (see lines 749–756 for the pattern); follow it.

```python
def test_submit_routes_prompt_into_node_overrides_text() -> None:
    """submit() writes resolve_prompt(job) into node_overrides[node_id].inputs.text
    for each entry in spec['prompt_node_ids'].

    Bug catch: without this routing, an orchestrator-driven ComfyUI run with
    spec.prompt_node_ids={'main': '6'} would POST the baked-in graph text
    unchanged — the user's CLI prompt would never reach the encoder node.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.comfyui import ComfyUIBackend

    posts: list[dict[str, Any]] = []

    def post_spy(u: str, b: dict[str, Any]) -> dict[str, Any]:
        posts.append({"u": u, "b": b})
        return {"prompt_id": "p1"}

    backend = ComfyUIBackend(
        http_post=post_spy,
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=lambda u, **kw: "ignored.png",
        base_url="http://comfy:8188",
        probe=_DEFAULT_PROBE,
    )
    job = GenerationJob(
        spec={
            "graph": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}},
            "node_overrides": {},
            "prompt_node_ids": {"main": "6"},
        },
        segments=[Segment(prompt="a hovering dragon", assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0]["b"]["prompt"]["6"]["inputs"]["text"] == "a hovering dragon"


def test_submit_skips_when_prompt_node_ids_absent() -> None:
    """Legacy spec without spec['prompt_node_ids'] must not have any node
    overrides mutated by the helper.

    Bug catch: an over-eager routing block that inspects segments even
    when prompt_node_ids is absent could silently overwrite a node whose
    ID happens to match a hardcoded default.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.comfyui import ComfyUIBackend

    posts: list[dict[str, Any]] = []

    def post_spy(u: str, b: dict[str, Any]) -> dict[str, Any]:
        posts.append({"u": u, "b": b})
        return {"prompt_id": "p1"}

    backend = ComfyUIBackend(
        http_post=post_spy,
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=lambda u, **kw: "ignored.png",
        base_url="http://comfy:8188",
        probe=_DEFAULT_PROBE,
    )
    job = GenerationJob(
        spec={
            "graph": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "baked"}}},
            "node_overrides": {},
            # NOTE: no prompt_node_ids key
        },
        segments=[Segment(prompt="should-not-route", assets=[])],
        params={},
    )
    backend.submit(job)
    # Baked-in graph text survives — no routing happened.
    assert posts[0]["b"]["prompt"]["6"]["inputs"]["text"] == "baked"


def test_submit_spec_prompt_wins_over_segment_comfyui() -> None:
    """When spec carries both 'prompt' (read by resolve_prompt) and a
    segment prompt, the explicit spec.prompt wins and is routed into the
    encoder node — mirrors the precedence rule across all engines."""
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.comfyui import ComfyUIBackend

    posts: list[dict[str, Any]] = []

    def post_spy(u: str, b: dict[str, Any]) -> dict[str, Any]:
        posts.append({"u": u, "b": b})
        return {"prompt_id": "p1"}

    backend = ComfyUIBackend(
        http_post=post_spy,
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=lambda u, **kw: "ignored.png",
        base_url="http://comfy:8188",
        probe=_DEFAULT_PROBE,
    )
    job = GenerationJob(
        spec={
            "graph": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}},
            "node_overrides": {},
            "prompt_node_ids": {"main": "6"},
            "prompt": "explicit-from-spec",
        },
        segments=[Segment(prompt="from-seg", assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0]["b"]["prompt"]["6"]["inputs"]["text"] == "explicit-from-spec"


def test_submit_does_not_overwrite_explicit_node_override_text() -> None:
    """If node_overrides already supplies inputs.text for the configured
    node, the helper must leave it alone (setdefault semantics).

    Bug catch: a naive ``inputs['text'] = prompt`` would clobber a hand-
    crafted negative-prompt encoder or preset wrapper that the workflow
    author had already wired in via node_overrides.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.comfyui import ComfyUIBackend

    posts: list[dict[str, Any]] = []

    def post_spy(u: str, b: dict[str, Any]) -> dict[str, Any]:
        posts.append({"u": u, "b": b})
        return {"prompt_id": "p1"}

    backend = ComfyUIBackend(
        http_post=post_spy,
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=lambda u, **kw: "ignored.png",
        base_url="http://comfy:8188",
        probe=_DEFAULT_PROBE,
    )
    job = GenerationJob(
        spec={
            "graph": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}},
            "node_overrides": {"6": {"inputs": {"text": "preset-from-override"}}},
            "prompt_node_ids": {"main": "6"},
        },
        segments=[Segment(prompt="ignored-by-setdefault", assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0]["b"]["prompt"]["6"]["inputs"]["text"] == "preset-from-override"


def test_validate_spec_raises_when_prompt_node_ids_set_and_no_prompt() -> None:
    """Opt-in validation: spec.prompt_node_ids configured with no prompt
    available anywhere must raise before the misconfigured POST hits the
    ComfyUI server.

    Bug catch: silent fallthrough would resurface the empty-prompt defect
    after the workflow author intentionally declared a prompt sink.
    """
    import pytest
    from kinoforge.core.errors import ValidationError
    from kinoforge.core.interfaces import GenerationJob, Segment

    engine = _make_engine()
    job = GenerationJob(
        spec={
            "graph": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}},
            "node_overrides": {},
            "prompt_node_ids": {"main": "6"},
        },
        segments=[Segment(prompt="", assets=[])],
        params={},
    )
    with pytest.raises(ValidationError, match="prompt_node_ids is configured"):
        engine.validate_spec(job)


def test_validate_spec_passes_when_prompt_node_ids_absent() -> None:
    """Legacy spec without prompt_node_ids must keep passing — workflows
    that bake their prompt into the graph never declared a sink and
    should not gain a new failure mode."""
    from kinoforge.core.interfaces import GenerationJob, Segment

    engine = _make_engine()
    job = GenerationJob(
        spec={
            "graph": {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "baked"}}},
            "node_overrides": {},
        },
        segments=[Segment(prompt="", assets=[])],
        params={},
    )
    engine.validate_spec(job)  # must NOT raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run pytest tests/engines/test_comfyui.py -v -k "routes_prompt|skips_when_prompt_node|spec_prompt_wins_over_segment_comfyui|does_not_overwrite_explicit_node_override|prompt_node_ids_set_and_no_prompt|prompt_node_ids_absent"
```
Expected: 6 fail (KeyError or AssertionError — no routing yet).

- [ ] **Step 3: Implement ComfyUIBackend.submit change**

Edit `src/kinoforge/engines/comfyui/__init__.py`. In `submit` (line 327), after the `asset_node_ids` loop (around line 383) and before the existing deep-merge loop (line 385), insert:

```python
        # Layer J: route the user prompt into the configured text-encoder
        # nodes. Reads ``spec["prompt_node_ids"]`` — mirrors
        # ``asset_node_ids`` — and writes via ``setdefault`` so an explicit
        # ``node_overrides[node_id]["inputs"]["text"]`` from spec wins.
        prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
        if prompt_node_ids:
            from kinoforge.core.prompt_routing import resolve_prompt

            prompt = resolve_prompt(job)
            if prompt is not None:
                for _role, node_id in prompt_node_ids.items():
                    node_patch = overrides.setdefault(str(node_id), {})
                    inputs = node_patch.setdefault("inputs", {})
                    inputs.setdefault("text", prompt)
```

- [ ] **Step 4: Implement ComfyUIEngine.validate_spec change**

In `validate_spec` (line 634), append after the existing asset-role check (after line 669):

```python
        prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
        if prompt_node_ids:
            from kinoforge.core.prompt_routing import resolve_prompt

            if resolve_prompt(job) is None:
                raise ValidationError(
                    "comfyui spec.prompt_node_ids is configured but no "
                    "prompt found in job.spec or segments[0] — set "
                    "spec.prompt, set segments[0].prompt, or clear "
                    "spec.prompt_node_ids"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pixi run pytest tests/engines/test_comfyui.py -v
```
Expected: all pass.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py
git commit -m "feat(engines/comfyui): route prompt via resolve_prompt helper into node_overrides (Layer J Task 5)"
```

---

### Task 6: FalBackend.submit retrofit

**Goal:** Replace the inline 4-line prompt fallback in `FalBackend.submit` with a call to `resolve_prompt`. Behavior identical (spec-precedence preserved). Existing Fal tests pass unchanged.

**Files:**
- Modify: `src/kinoforge/engines/fal/__init__.py` (lines 236–243)

**Acceptance Criteria:**
- [ ] Inline `if "prompt" not in body and job.segments:` block replaced by `resolve_prompt`-driven `setdefault`
- [ ] `tests/engines/test_fal.py::test_submit_falls_back_to_segment_prompt` passes unchanged
- [ ] `tests/engines/test_fal.py::test_submit_spec_prompt_wins_over_segment_prompt` passes unchanged
- [ ] `tests/engines/test_fal.py::test_validate_spec_accepts_prompt_on_segment` passes unchanged
- [ ] `FalEngine.validate_spec` body left as-is (engine-specific stricter contract preserved)
- [ ] `pixi run pre-commit run --files src/kinoforge/engines/fal/__init__.py` passes

**Verify:** `pixi run pytest tests/engines/test_fal.py -v` → all fal tests pass

**Steps:**

- [ ] **Step 1: Confirm baseline green**

```bash
pixi run pytest tests/engines/test_fal.py -v -k "prompt"
```
Expected: 3 passed.

- [ ] **Step 2: Apply retrofit**

Edit `src/kinoforge/engines/fal/__init__.py`. Replace lines 236–243 (the current inline block) with:

```python
        from kinoforge.core.prompt_routing import resolve_prompt

        body = dict(job.spec)
        prompt = resolve_prompt(job)
        if prompt is not None:
            body.setdefault("prompt", prompt)
```

Update the surrounding docstring at line 222–225 ("kinoforge's pipeline places the user prompt on the Segment, not in the engine spec") to reference the helper:

```
        The prompt is sourced via
        :func:`~kinoforge.core.prompt_routing.resolve_prompt` —
        ``job.spec["prompt"]`` wins, otherwise ``job.segments[0].prompt``.
```

- [ ] **Step 3: Run tests to confirm green**

```bash
pixi run pytest tests/engines/test_fal.py -v
```
Expected: all pass with no count change.

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/fal/__init__.py
git add src/kinoforge/engines/fal/__init__.py
git commit -m "refactor(engines/fal): use resolve_prompt helper, drop inline fallback (Layer J Task 6)"
```

---

### Task 7: Examples + README + PROGRESS

**Goal:** Document the new field in example configs and project docs.

**Files:**
- Modify: `examples/configs/hosted.yaml`
- Modify: `examples/configs/diffusers.yaml`
- Modify: `examples/configs/wan.yaml`
- Modify: `README.md`
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] `examples/configs/hosted.yaml` documents `prompt_body_key` (commented showing default)
- [ ] `examples/configs/diffusers.yaml` documents the same
- [ ] `examples/configs/wan.yaml` documents `prompt_node_ids` as a spec-level field (commented example)
- [ ] `README.md` mentions cross-engine prompt routing in the engines section
- [ ] `PROGRESS.md` gets a "Phase 20 — Layer J" section listing tasks 1–7 with the commit SHAs
- [ ] Final full suite: `pixi run pytest -q` passes
- [ ] `pixi run pre-commit run --all-files` passes
- [ ] `tests/test_examples.py` still passes (YAML parse smoke)

**Verify:** `pixi run pytest -q && pixi run pre-commit run --all-files` → green

**Steps:**

- [ ] **Step 1: Edit `examples/configs/hosted.yaml`**

After the `url_path: video.url` line, append:

```yaml
    # Optional: top-level body key where the user prompt is routed when
    # spec.prompt is absent. Default "prompt"; set null for endpoints
    # that reject unknown top-level fields.
    # prompt_body_key: prompt
```

- [ ] **Step 2: Edit `examples/configs/diffusers.yaml`**

After the `engine: ...` block, before `models:`, add a commented `diffusers:` block (if absent) or extend an existing one:

```yaml
engine:
  kind: diffusers
  precision: fp16
  # diffusers:
  #   # Optional: top-level body key for prompt routing (default "prompt").
  #   prompt_body_key: prompt
```

- [ ] **Step 3: Edit `examples/configs/wan.yaml`**

Append a commented spec-level guidance block at the end of the file:

```yaml
# --- Optional: ComfyUI prompt routing (Layer J) ------------------------------
# When the orchestrator drives the CLI prompt, set spec.prompt_node_ids in
# your spec dict to declare which graph node receives the prompt text.
# Currently routed into <node>.inputs.text (suitable for CLIPTextEncode and
# most prompt-encoder nodes); non-standard encoders can override via
# node_overrides directly.
#
# Example (placed in spec, not in cfg):
#   spec:
#     graph: { ... }
#     node_overrides: { ... }
#     prompt_node_ids:
#       main: "6"   # node id of the positive-prompt CLIPTextEncode node
```

- [ ] **Step 4: Edit `README.md`**

Find the "Hosted" or engines section. Add a short paragraph:

```markdown
### Cross-engine prompt routing

The user prompt supplied at the CLI (or via `GenerationRequest.prompt`)
is placed on `Segment.prompt` by the orchestrator. `HostedAPIBackend`,
`DiffusersBackend`, `ComfyUIBackend`, and `FalBackend` all route it
into their request body via `kinoforge.core.prompt_routing.resolve_prompt`.

- Hosted / Diffusers / Fal: top-level `body["prompt"]` (configurable
  on hosted/diffusers via `engine.<name>.prompt_body_key`; set to
  `null` to disable).
- ComfyUI: into `node_overrides[node_id]["inputs"]["text"]` for each
  entry in `spec["prompt_node_ids"]` (declare in spec alongside
  `asset_node_ids`).

An explicit `spec["prompt"]` always wins over the segment-supplied prompt.
```

- [ ] **Step 5: Edit `PROGRESS.md`**

Append a new section under "Post-MVP" mirroring the Phase 19 format:

```markdown
### Phase 20 — Layer J (cross-engine prompt fallback)

- [x] Task 1: `core/prompt_routing.py` + 8 helper tests — commit <SHA>
- [x] Task 2: `prompt_body_key` on hosted + diffusers configs — commit <SHA>
- [x] Task 3: HostedAPIBackend + Engine wire — commit <SHA>
- [x] Task 4: DiffusersBackend + Engine wire — commit <SHA>
- [x] Task 5: ComfyUIBackend + Engine wire (spec-level `prompt_node_ids`) — commit <SHA>
- [x] Task 6: FalBackend retrofit — commit <SHA>
- [x] Task 7: Examples + README + PROGRESS — commit <SHA>

Backfill the `<SHA>` placeholders after each commit lands.
```

Also update the "Single next action" section to point to Layer K or follow-up if known; otherwise note that Layer J is complete and Layer K (base_spec routing from cfg) remains the prerequisite for orchestrator-driven hosted/diffusers/comfyui runs.

- [ ] **Step 6: Run full suite + pre-commit**

```bash
pixi run pytest -q
pixi run pre-commit run --all-files
```
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add examples/configs/hosted.yaml examples/configs/diffusers.yaml examples/configs/wan.yaml README.md PROGRESS.md
git commit -m "docs: Layer J examples + README + PROGRESS Phase 20 entry (Task 7)"
```

Then backfill commit SHAs into PROGRESS.md with a follow-up `docs(progress): backfill Layer J SHAs` commit.

---

## Final gate

After Task 7, before merge:

- [ ] `pixi run pytest -q` — full suite green; new test count ≈ +24 (8 helper + 6 hosted + 6 diffusers + 6 comfyui + 4 config; fal unchanged)
- [ ] `pixi run pre-commit run --all-files` — green
- [ ] `git log --oneline 7be3f73..HEAD` (or the merge-base SHA from `git merge-base main HEAD`) shows 7 atomic commits
- [ ] Two-stage review per Layer pattern (spec compliance → code quality)
- [ ] `--no-ff` merge to `main` referencing this spec + plan
