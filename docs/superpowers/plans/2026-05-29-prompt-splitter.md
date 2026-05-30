# Prompt Splitter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fifth swappable axis to kinoforge — a registry-mediated `Splitter` plugin with an in-core `HeuristicSplitter` default — so `orchestrator.generate()` can convert long-form prompts into ordered `list[Segment]` for downstream packaging.

**Architecture:** Pure-function ABC mirrors existing axes (provider / source / engine / store). `HeuristicSplitter` lives in `core/splitter.py`, splits on blank-line markers (`\n\n`), self-registers at import. Future LLM/scene-detect splitters plug in as adapters via `_adapters.py`. Orchestrator owns asset attachment so the splitter contract stays prompt-only.

**Tech Stack:** Python 3.12+, pydantic v2, pytest, mypy strict, ruff, pixi. No new runtime dependencies. Spec: `docs/superpowers/specs/2026-05-29-prompt-splitter-design.md`.

---

### Task 1: Add Splitter ABC + registry helpers

**Goal:** Introduce the `Splitter` ABC and `register_splitter`/`get_splitter` registry helpers so plugins have a contract to implement and the orchestrator has a lookup path.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (insert `Splitter` ABC after `ModelProfileProvider` around line 260)
- Modify: `src/kinoforge/core/registry.py` (add `_splitters` dict + `register_splitter` + `get_splitter` after the store helpers around line 146)
- Create: `tests/core/test_splitter.py` (tests T1, T9–T11 from spec §8)

**Acceptance Criteria:**
- [ ] `from kinoforge.core.interfaces import Splitter` succeeds.
- [ ] `Splitter` is an `ABC` with an abstract `split(prompt: str, profile: ModelProfile, params: dict) -> list[Segment]` method.
- [ ] Instantiating an abstract `Splitter` subclass without overriding `split` raises `TypeError` (Python's default ABC behaviour — proves the abstract decorator landed).
- [ ] `register_splitter(name, factory)` + `get_splitter(name)` exist with the same shape as `register_engine`/`get_engine`.
- [ ] `get_splitter("nope")` raises `UnknownAdapter` with a message naming the missing key.
- [ ] Re-registration under the same name overwrites the prior factory.

**Verify:** `pixi run test -- tests/core/test_splitter.py -q` reports green; `pixi run typecheck` clean.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_splitter.py`:

```python
"""Tests for the Splitter ABC + registry plumbing (Task 1)."""

from __future__ import annotations

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import (
    ModelProfile,
    Segment,
    Splitter,  # The ABC under test.
)


def _profile() -> ModelProfile:
    """Build a minimal ModelProfile fixture for splitter calls."""
    return ModelProfile(
        name="fake",
        max_frames=24,
        fps=12,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


class _IncompleteSplitter(Splitter):
    """Subclass that does not implement split — should not be instantiable."""

    name = "incomplete"


def test_splitter_is_abstract_method_required():
    # Bug: someone removes @abstractmethod and downstream plugins
    # silently skip the contract.
    with pytest.raises(TypeError):
        _IncompleteSplitter()  # type: ignore[abstract]


class _ConcreteSplitter(Splitter):
    """Minimal concrete subclass used to prove the contract is implementable."""

    name = "concrete"

    def split(
        self, prompt: str, profile: ModelProfile, params: dict
    ) -> list[Segment]:
        return [Segment(prompt=prompt)]


def test_concrete_splitter_returns_segments():
    # Bug: ABC signature drifts (e.g., wrong arg order) and subclasses break.
    s = _ConcreteSplitter()
    out = s.split("hello", _profile(), {})
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0].prompt == "hello"


def test_register_and_get_splitter_round_trip():
    # Bug: registry stores instances instead of factories, or loses entries
    # under the wrong key.
    registry.register_splitter("rt_test", lambda: _ConcreteSplitter())
    factory = registry.get_splitter("rt_test")
    instance = factory()
    assert isinstance(instance, _ConcreteSplitter)


def test_get_splitter_unknown_raises_unknown_adapter():
    # Bug: silent fallthrough on bad config produces opaque runtime error;
    # caller should see a clear "not registered" message.
    with pytest.raises(UnknownAdapter) as exc_info:
        registry.get_splitter("definitely_not_registered_xyz")
    assert "definitely_not_registered_xyz" in str(exc_info.value)


def test_register_splitter_reregistration_overwrites():
    # Bug: duplicate registrations stack instead of replace, leaking memory
    # and creating subtle behaviour differences between fresh and re-imported
    # modules.
    registry.register_splitter("dup", lambda: _ConcreteSplitter())

    class _Other(_ConcreteSplitter):
        name = "other"

    registry.register_splitter("dup", lambda: _Other())
    instance = registry.get_splitter("dup")()
    assert isinstance(instance, _Other)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test -- tests/core/test_splitter.py -q`
Expected: 5 errors, all at collection time, complaining `Splitter` is not importable from `kinoforge.core.interfaces` and `register_splitter` is not on `kinoforge.core.registry`.

- [ ] **Step 3: Add the Splitter ABC**

In `src/kinoforge/core/interfaces.py`, insert the new ABC immediately after the `ModelProfileProvider` class (after the existing `def verify(...)` line — somewhere near line 261):

```python
class Splitter(ABC):
    """Convert a long-form prompt into ordered ``Segment`` objects.

    A splitter is a pure function: deterministic, side-effect-free, no I/O.
    The output list must contain at least one ``Segment``; each segment carries
    only ``prompt``.  ``assets`` and ``params`` default to empty — asset
    attachment is performed by the orchestrator and per-segment param merging
    by ``strategy.decide``.

    Attributes:
        name: The registry key under which the splitter is registered.
    """

    name: str

    @abstractmethod
    def split(  # noqa: D102
        self, prompt: str, profile: ModelProfile, params: dict
    ) -> list[Segment]:
        ...
```

- [ ] **Step 4: Add registry helpers**

In `src/kinoforge/core/registry.py`:

1. Update the imports near line 19 to include `Splitter`:

```python
from kinoforge.core.interfaces import (
    ComputeProvider,
    GenerationEngine,
    ModelSource,
    Splitter,
)
```

2. Add a new module-level dict near line 25 alongside the existing ones:

```python
_splitters: dict[str, Callable[[], Splitter]] = {}
```

3. Append the new helpers at the end of the file:

```python
def register_splitter(name: str, factory: Callable[[], Splitter]) -> None:
    """Register a splitter factory under ``name`` (overwrites).

    Args:
        name: The registry key for this splitter.
        factory: Zero-arg callable that returns a ``Splitter`` instance.
    """
    _splitters[name] = factory


def get_splitter(name: str) -> Callable[[], Splitter]:
    """Return the splitter factory for ``name`` or raise ``UnknownAdapter``.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory registered under ``name``.

    Raises:
        UnknownAdapter: No splitter is registered under ``name``.
    """
    try:
        return _splitters[name]
    except KeyError:
        raise UnknownAdapter(f"no splitter registered: {name!r}") from None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run test -- tests/core/test_splitter.py -q`
Expected: 5 passed.

- [ ] **Step 6: Run mypy + ruff**

Run: `pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py tests/core/test_splitter.py`
Expected: ruff + ruff-format + mypy all Passed.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py tests/core/test_splitter.py
git commit -m "feat(splitter): add Splitter ABC + register/get registry helpers

Introduces the fifth swappable axis. Pure-function contract with
self-registration pattern matching engine/provider/store. No default
implementation yet — Task 2 ships HeuristicSplitter."
```

---

### Task 2: Implement HeuristicSplitter + self-registration trigger

**Goal:** Ship the in-core default splitter (`\n\n` markers, paragraph-per-segment) and guarantee its self-registration fires whenever core is imported.

**Files:**
- Create: `src/kinoforge/core/splitter.py` (`HeuristicSplitter` + module-footer registration)
- Modify: `src/kinoforge/core/__init__.py` (add the import that triggers registration)
- Extend: `tests/core/test_splitter.py` (tests T2–T8, T12 from spec §8)

**Acceptance Criteria:**
- [ ] `HeuristicSplitter("one paragraph")` returns 1 Segment with prompt `"one paragraph"` (single-paragraph passthrough — guarantees existing single-segment behaviour is preserved).
- [ ] `HeuristicSplitter("a\n\nb")` returns 2 Segments with prompts `["a", "b"]`.
- [ ] `HeuristicSplitter("a\n\n\n\nb")` collapses to 2 Segments (no empty middle segment).
- [ ] `HeuristicSplitter("  a  \n\n  b  ")` strips whitespace per segment → `["a", "b"]`.
- [ ] `HeuristicSplitter("a\nb\n\nc")` preserves the single newline inside paragraph one → `["a\nb", "c"]`.
- [ ] `HeuristicSplitter("   \n\n   ")` raises `ValueError`.
- [ ] Every returned Segment has `assets == []` and `params == {}`; the caller's `params` dict is not mutated.
- [ ] `registry.get_splitter("heuristic")()` returns a `HeuristicSplitter` instance after a fresh import of `kinoforge.core`.

**Verify:** `pixi run test -- tests/core/test_splitter.py -q` reports all tests green; `pixi run pre-commit run --files src/kinoforge/core/splitter.py src/kinoforge/core/__init__.py tests/core/test_splitter.py` clean.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_splitter.py` (after the tests from Task 1):

```python
from kinoforge.core.splitter import HeuristicSplitter


def test_heuristic_single_paragraph_passthrough():
    # Bug: regex over-eagerly splits on single newlines or whitespace,
    # breaking every existing single-segment test in the suite.
    out = HeuristicSplitter().split("one paragraph", _profile(), {})
    assert len(out) == 1
    assert out[0].prompt == "one paragraph"


def test_heuristic_double_newline_splits():
    out = HeuristicSplitter().split("a\n\nb", _profile(), {})
    assert [s.prompt for s in out] == ["a", "b"]


def test_heuristic_collapses_runs_of_newlines():
    # Bug: `re.split(r"\n\n")` against `"a\n\n\n\nb"` yields ["a", "", "b"];
    # we must collapse so the middle empty segment never reaches downstream.
    out = HeuristicSplitter().split("a\n\n\n\nb", _profile(), {})
    assert [s.prompt for s in out] == ["a", "b"]


def test_heuristic_strips_whitespace_per_segment():
    # Bug: leading/trailing whitespace silently inflates the prompt sent
    # to backends; many engines treat "  cat" and "cat" as different prompts
    # (different tokenisation), so leaks cause spurious cache misses.
    out = HeuristicSplitter().split("  a  \n\n  b  ", _profile(), {})
    assert [s.prompt for s in out] == ["a", "b"]


def test_heuristic_preserves_inparagraph_single_newline():
    # Bug: someone over-aggressively normalises whitespace and destroys
    # intentional line breaks inside a paragraph (e.g. "a\nb" for line layout).
    out = HeuristicSplitter().split("a\nb\n\nc", _profile(), {})
    assert [s.prompt for s in out] == ["a\nb", "c"]


def test_heuristic_all_whitespace_raises_value_error():
    # Bug: all-whitespace prompt silently produces zero segments and the
    # downstream pool gets an empty job list — NPE or worse.
    with pytest.raises(ValueError):
        HeuristicSplitter().split("   \n\n   ", _profile(), {})


def test_heuristic_segments_have_empty_assets_and_params():
    # Bug: defaults accidentally share state across calls or splitter
    # writes back into Segment defaults via mutation.
    out = HeuristicSplitter().split("a\n\nb", _profile(), {})
    for seg in out:
        assert seg.assets == []
        assert seg.params == {}


def test_heuristic_does_not_mutate_caller_params():
    # Bug: splitter writes into the caller's params dict and downstream
    # callers see unexpected keys appear.
    caller_params = {"seed": 42, "steps": 30}
    snapshot = dict(caller_params)
    HeuristicSplitter().split("a\n\nb", _profile(), caller_params)
    assert caller_params == snapshot


def test_heuristic_self_registers_under_heuristic():
    # Bug: someone forgets the registry.register_splitter line at module
    # footer and the orchestrator default lookup fails at runtime.
    instance = registry.get_splitter("heuristic")()
    assert isinstance(instance, HeuristicSplitter)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test -- tests/core/test_splitter.py -q`
Expected: import errors — `kinoforge.core.splitter` doesn't exist.

- [ ] **Step 3: Create `src/kinoforge/core/splitter.py`**

```python
"""HeuristicSplitter: in-core default that splits prompts on blank-line markers.

Pure function. No I/O. Self-registers under the name ``"heuristic"`` at import
time. Future LLM-semantic or scene-detect splitters plug in as adapters under
``src/kinoforge/splitters/<name>/`` and register via ``_adapters.py``.
"""

from __future__ import annotations

import re

from kinoforge.core import registry
from kinoforge.core.interfaces import ModelProfile, Segment, Splitter

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")


class HeuristicSplitter(Splitter):
    """Split a prompt on blank-line boundaries.

    Each non-empty paragraph (after stripping whitespace) becomes one
    :class:`~kinoforge.core.interfaces.Segment`. Runs of newlines collapse
    rather than yielding empty middle segments. The ``profile`` and ``params``
    arguments are accepted for ABC compliance and reserved for future
    duration-aware strategies; the heuristic itself does not consult them.

    Single-paragraph prompts pass through as a 1-element list so existing
    single-segment callers see no behavioural change.
    """

    name = "heuristic"

    def split(
        self, prompt: str, profile: ModelProfile, params: dict
    ) -> list[Segment]:
        """Return ordered ``Segment``s carrying paragraph-sized prompt chunks.

        Args:
            prompt: The user-supplied prompt; paragraphs separated by blank lines.
            profile: The model's capability profile (unused by the heuristic).
            params: Engine-neutral params (unused by the heuristic; not mutated).

        Returns:
            An ordered list of ``Segment`` objects, length >= 1.

        Raises:
            ValueError: ``prompt`` yields zero non-empty segments after stripping.
        """
        chunks = [c.strip() for c in _PARAGRAPH_BREAK.split(prompt)]
        chunks = [c for c in chunks if c]
        if not chunks:
            raise ValueError("prompt yielded zero non-empty segments")
        return [Segment(prompt=c) for c in chunks]


registry.register_splitter("heuristic", lambda: HeuristicSplitter())
```

- [ ] **Step 4: Wire the import into `core/__init__.py`**

Open `src/kinoforge/core/__init__.py`. If the file is empty (or contains only a docstring), append:

```python
# Trigger in-core self-registrations (HeuristicSplitter).
from kinoforge.core import splitter  # noqa: F401,E402
```

If the file already has content, add the same import at the bottom. The `noqa: F401` suppresses the unused-import warning — the import is for its registration side-effect.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run test -- tests/core/test_splitter.py -q`
Expected: all 13 tests passed (5 from Task 1 + 8 new).

- [ ] **Step 6: Run the full suite**

Run: `pixi run test -q`
Expected: 357 prior tests + 13 new = 370 passed. (Adjust the count if Task 1 added additional tests.)

- [ ] **Step 7: Run pre-commit**

Run: `pixi run pre-commit run --files src/kinoforge/core/splitter.py src/kinoforge/core/__init__.py tests/core/test_splitter.py`
Expected: ruff + ruff-format + mypy all Passed.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/splitter.py src/kinoforge/core/__init__.py tests/core/test_splitter.py
git commit -m "feat(splitter): add HeuristicSplitter default + core self-registration

Paragraph-per-segment via blank-line marker (\\n\\s*\\n+). Pure function;
profile/params accepted but unused (reserved for future strategies).
core/__init__.py imports the module to trigger registration without
involving _adapters.py (which is reserved for adapter packages)."
```

---

### Task 3: Add SplitterConfig to pydantic Config

**Goal:** Expose splitter selection in YAML via an optional `splitter:` block defaulting to `"heuristic"`. All existing example configs continue to parse with no edits.

**Files:**
- Modify: `src/kinoforge/core/config.py` (add `SplitterConfig` model + field on `Config`)
- Extend: `tests/core/test_config.py` (tests C-1, C-2, C-3 from spec §8)

**Acceptance Criteria:**
- [ ] Building `Config(engine=..., models=[...], compute=...)` without a `splitter` argument yields `cfg.splitter.kind == "heuristic"`.
- [ ] `load_config()` against YAML containing `splitter:\n  kind: heuristic` parses identically.
- [ ] `load_config()` against YAML containing `splitter:\n  kind: custom_unregistered_thing` parses successfully (the unknown-kind error surfaces at `generate()` via `registry.get_splitter`, not at config load time — matches today's behaviour for engine/provider names).
- [ ] All four example configs (`local-fake.yaml`, `diffusers.yaml`, `wan.yaml`, `hosted.yaml`) still parse — confirmed by re-running `tests/test_examples.py`.

**Verify:** `pixi run test -- tests/core/test_config.py tests/test_examples.py -q` reports green; `pixi run typecheck` clean.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_config.py`:

```python
def test_config_splitter_defaults_to_heuristic():
    # Bug: pydantic default missing or wrong key; orchestrator falls through
    # to UnknownAdapter at runtime for every config that omits the block.
    cfg = _minimal_config()  # use the existing test helper for a valid Config.
    assert cfg.splitter.kind == "heuristic"


def test_config_splitter_explicit_heuristic_parses():
    # Bug: the schema rejects the explicit default form, forcing users to
    # omit the block to avoid validation errors.
    yaml_text = _minimal_yaml() + "\nsplitter:\n  kind: heuristic\n"
    cfg = load_config_from_string(yaml_text)
    assert cfg.splitter.kind == "heuristic"


def test_config_splitter_unknown_kind_parses_at_load_time():
    # Bug: Config validation couples the schema to global registry state,
    # so import order or test isolation flakes the loader. The unknown-kind
    # error must surface at generate() time, not at config load.
    yaml_text = _minimal_yaml() + "\nsplitter:\n  kind: bespoke_xyz\n"
    cfg = load_config_from_string(yaml_text)
    assert cfg.splitter.kind == "bespoke_xyz"
```

(Use whatever `_minimal_config()` / `_minimal_yaml()` / `load_config_from_string()` helpers already exist in `tests/core/test_config.py`. If only `load_config()` against a file exists, use `tmp_path / "cfg.yaml"` and write the YAML body to it before loading.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test -- tests/core/test_config.py -q`
Expected: the three new tests fail with `AttributeError: 'Config' object has no attribute 'splitter'`.

- [ ] **Step 3: Add `SplitterConfig` and thread it onto `Config`**

In `src/kinoforge/core/config.py`, add a new model — best location is immediately before the `Config` class definition (around line 199):

```python
class SplitterConfig(BaseModel):
    """Splitter selection block (optional in YAML; defaults to heuristic).

    Attributes:
        kind: The registry key of the splitter to use. Unknown kinds are
            permitted at load time and surface as ``UnknownAdapter`` at
            ``generate()``, matching engine/provider behaviour.
    """

    kind: str = "heuristic"
```

Then add the field to `Config` (after the existing `lifecycle_cfg` field near line 214):

```python
    splitter: SplitterConfig = Field(default_factory=SplitterConfig)
```

`default_factory` (not `default=SplitterConfig()`) guarantees each `Config` instance gets a fresh `SplitterConfig` — protects against accidental shared mutable state.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test -- tests/core/test_config.py tests/test_examples.py -q`
Expected: all config + example tests pass.

- [ ] **Step 5: Run pre-commit**

Run: `pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py`
Expected: ruff + ruff-format + mypy all Passed.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "feat(splitter): add optional splitter block to Config

Defaults to {kind: heuristic} via SplitterConfig + default_factory.
Unknown kinds parse at load time (surface at generate via UnknownAdapter)
matching engine/provider behaviour. All four example configs unchanged."
```

---

### Task 4: Wire splitter into `orchestrator.generate()` step 6

**Goal:** Replace the DEFERRED stub in `orchestrator.generate()` with a real splitter resolution + call. Attach `validated.assets` to segment 0 only. Drop the splitter from the README Roadmap.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (replace the DEFERRED comment block around line 404; thread `segments_override` into `stage.run`)
- Extend: `tests/core/test_orchestrator.py` (tests O-1 through O-4 from spec §8)
- Modify: `README.md` (mention the splitter axis in the Extending / Roadmap section; remove the splitter line from the Deferred list)
- Modify: `PROGRESS.md` (add a Post-MVP section recording the new tasks + commit refs)

**Acceptance Criteria:**
- [ ] A multi-paragraph prompt passed through `orchestrator.generate()` causes the stage to receive an `N`-element `segments_override` list, where `N` matches the paragraph count.
- [ ] With `mode == "i2v"` and a multi-paragraph prompt + one `init_image` asset, segment 0 has the asset attached and segments 1..N-1 have `assets == []`.
- [ ] A single-paragraph prompt + `i2v` + asset produces exactly 1 Segment carrying the asset — today's single-segment behaviour preserved (regression guard).
- [ ] A `Config` with no `splitter:` block successfully runs end-to-end with the `"heuristic"` default.
- [ ] The DEFERRED comment block around `orchestrator.py:404` is removed.
- [ ] `PROGRESS.md` records the four new tasks with commit refs.
- [ ] `README.md` mentions the splitter axis in Extending; the splitter line is removed from the Deferred / Roadmap list.

**Verify:** `pixi run pre-commit run --all-files` green; `pixi run test-cov` reports coverage ≥ 90%; `pixi run test -q` reports 357 prior + the new tests all passing.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Extend `tests/core/test_orchestrator.py` with four new tests. Use the existing test scaffolding (LocalProvider + FakeEngine + LocalArtifactStore + the existing `_fake_cfg(...)` or equivalent helper). If a spy on `GenerateClipStage.run` doesn't already exist, use `unittest.mock.patch.object` to capture the `segments_override` kwarg.

```python
def test_orchestrator_multi_paragraph_splits_into_n_segments(tmp_path):
    # Bug: splitter not wired into generate() — multi-paragraph prompts
    # silently collapse to one segment and the marquee feature does nothing.
    captured: dict[str, object] = {}

    real_run = GenerateClipStage.run

    def _spy(self, request, *, segments_override=None):  # type: ignore[no-untyped-def]
        captured["segments_override"] = segments_override
        return real_run(self, request, segments_override=segments_override)

    with patch.object(GenerateClipStage, "run", _spy):
        cfg = _local_fake_cfg(tmp_path)
        store = LocalArtifactStore(tmp_path / "store")
        request = GenerationRequest(
            prompt="paragraph one\n\nparagraph two\n\nparagraph three",
            mode="t2v",
        )
        orchestrator.generate(cfg, request, store=store, run_id="r1")

    segments = captured["segments_override"]
    assert segments is not None
    assert [s.prompt for s in segments] == [
        "paragraph one",
        "paragraph two",
        "paragraph three",
    ]


def test_orchestrator_attaches_assets_to_segment_zero_only(tmp_path):
    # Bug: assets accidentally copied to every segment — every clip in a
    # multi-paragraph run gets the same init_image stamped on it, which
    # is wrong for narrative continuity (issue #02 will inject tail
    # frames into segments 1..N-1 properly).
    captured: dict[str, object] = {}

    real_run = GenerateClipStage.run

    def _spy(self, request, *, segments_override=None):  # type: ignore[no-untyped-def]
        captured["segments_override"] = segments_override
        return real_run(self, request, segments_override=segments_override)

    with patch.object(GenerateClipStage, "run", _spy):
        cfg = _local_fake_cfg(tmp_path, supported_modes={"i2v"})
        store = LocalArtifactStore(tmp_path / "store")
        init = ConditioningAsset(
            kind="image",
            role="init_image",
            ref=Artifact(filename="init.png", uri="file:///tmp/init.png", meta={}),
        )
        request = GenerationRequest(
            prompt="frame one\n\nframe two",
            mode="i2v",
            assets=[init],
        )
        orchestrator.generate(cfg, request, store=store, run_id="r2")

    segments = captured["segments_override"]
    assert segments is not None
    assert len(segments) == 2
    assert segments[0].assets == [init]
    assert segments[1].assets == []


def test_orchestrator_single_paragraph_regression(tmp_path):
    # Bug: the splitter wiring regresses today's single-segment + assets
    # happy path. With one paragraph + one asset, exactly one Segment
    # carrying the asset must reach the stage.
    captured: dict[str, object] = {}

    real_run = GenerateClipStage.run

    def _spy(self, request, *, segments_override=None):  # type: ignore[no-untyped-def]
        captured["segments_override"] = segments_override
        return real_run(self, request, segments_override=segments_override)

    with patch.object(GenerateClipStage, "run", _spy):
        cfg = _local_fake_cfg(tmp_path, supported_modes={"i2v"})
        store = LocalArtifactStore(tmp_path / "store")
        init = ConditioningAsset(
            kind="image",
            role="init_image",
            ref=Artifact(filename="init.png", uri="file:///tmp/init.png", meta={}),
        )
        request = GenerationRequest(
            prompt="just one paragraph",
            mode="i2v",
            assets=[init],
        )
        orchestrator.generate(cfg, request, store=store, run_id="r3")

    segments = captured["segments_override"]
    assert segments is not None
    assert len(segments) == 1
    assert segments[0].prompt == "just one paragraph"
    assert segments[0].assets == [init]


def test_orchestrator_default_splitter_resolved_at_runtime(tmp_path):
    # Bug: when the config omits splitter:, generate() blows up looking
    # for a missing field instead of resolving the heuristic default.
    cfg = _local_fake_cfg(tmp_path)  # built WITHOUT a splitter override.
    store = LocalArtifactStore(tmp_path / "store")
    request = GenerationRequest(prompt="alpha\n\nbeta", mode="t2v")
    artifact = orchestrator.generate(cfg, request, store=store, run_id="r4")
    assert artifact.uri.endswith(".bin") or artifact.uri  # store URI populated.
```

(Adapt imports — at the top of the file add `from unittest.mock import patch`, plus `ConditioningAsset`, `Artifact` from `kinoforge.core.interfaces` if not already imported.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test -- tests/core/test_orchestrator.py -q`
Expected: the four new tests fail (`segments_override` arrives as `None` because the orchestrator still uses the single-segment DEFERRED stub).

- [ ] **Step 3: Implement the wiring**

Open `src/kinoforge/core/orchestrator.py`. Replace the existing DEFERRED comment block at step 6 (around line 404–409):

```python
    # ------------------------------------------------------------------
    # Step 6 — splitter stub (DEFERRED: GenerateClipStage builds 1 segment)
    # ------------------------------------------------------------------
    # The splitter lives inside GenerateClipStage.run(); we do not need
    # to split here.  The stage uses the request directly and constructs
    # exactly one Segment from the validated request.
    # DEFERRED: multi-segment splitter will replace this when implemented.
```

with:

```python
    # ------------------------------------------------------------------
    # Step 6 — split the validated prompt into ordered segments
    # ------------------------------------------------------------------
    from kinoforge.core.interfaces import Segment  # local import: avoid widening top-level cycle

    validated = validate_request(profile, request, accepted_kinds=accepted_kinds)
    splitter = registry.get_splitter(cfg.splitter.kind)()
    prompt_segments = splitter.split(validated.prompt, profile, {})

    # Attach assets to segment 0 only. Continuity (#02) will fill segments
    # 1..N-1 with previous-frame conditioning when implemented.
    if prompt_segments and validated.assets:
        prompt_segments[0] = dataclasses.replace(
            prompt_segments[0], assets=list(validated.assets)
        )
```

Two ancillary edits in the same file:

1. Add `import dataclasses` to the top-of-file imports if it is not already imported.
2. The existing step-5 `validate_request(...)` call (around line 401) becomes redundant — it is now performed inside the new block. Delete the prior direct call so `validate_request` runs exactly once per `generate()`.
3. Pass the segments into the stage at the bottom of `generate()`. Find the line `artifact = stage.run(request)` (around line 480) and change it to:

```python
    artifact = stage.run(request, segments_override=prompt_segments)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test -- tests/core/test_orchestrator.py -q`
Expected: all orchestrator tests pass (existing + the four new ones).

- [ ] **Step 5: Run the full suite + coverage**

Run: `pixi run test-cov`
Expected: all tests pass; coverage report ≥ 90% across `src/`.

- [ ] **Step 6: Update README + PROGRESS**

In `README.md`, locate the "Roadmap" / "Deferred layers" section. Remove the line that lists the prompt splitter as a deferred layer. Add a one-line mention in the Extending section, e.g.:

```markdown
- **Splitter axis** — implement `Splitter` (`core/interfaces.py`) and call `register_splitter("name", lambda: MySplitter())` to plug an LLM-semantic or scene-detect strategy alongside the default `HeuristicSplitter`.
```

In `PROGRESS.md`, append a new section after the existing checklist:

```markdown
## Post-MVP

### Phase 10 — prompt splitter (deferred layer #1 from handoff §7)
- [x] Task 1: Splitter ABC + register/get registry helpers — commit <SHA from Task 1>
- [x] Task 2: HeuristicSplitter + core self-registration trigger — commit <SHA from Task 2>
- [x] Task 3: SplitterConfig optional block (defaults to heuristic) — commit <SHA from Task 3>
- [x] Task 4: Orchestrator wiring + asset attachment + README/PROGRESS — commit <SHA from this task>

Tracking-issue draft at `.tracking-issues/01-prompt-splitter.md` may now be deleted or closed.
```

Replace `<SHA from Task N>` with the actual short hashes from `git log --oneline -5`.

- [ ] **Step 7: Run final pre-commit**

Run: `pixi run pre-commit run --all-files`
Expected: ruff + ruff-format + mypy + every hook Passed.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py README.md PROGRESS.md
git commit -m "feat(splitter): wire HeuristicSplitter into orchestrator.generate()

Replaces the DEFERRED single-segment stub at step 6 with a real
registry-resolved splitter call. Assets attach to segment 0 only —
continuity (#02) will fill segments 1..N-1 with tail-frame conditioning
later. README + PROGRESS updated; tracking-issue draft #01 closeable."
```

---

## Self-Review

Spec coverage walk-through (`docs/superpowers/specs/2026-05-29-prompt-splitter-design.md` §-by-§):
- §3 ABC contract → Task 1 (declaration + abstract-method test).
- §4 HeuristicSplitter + self-registration → Task 2 (behaviour + registration trigger via core/__init__.py).
- §5 Registry plumbing → Task 1.
- §6 Config → Task 3.
- §7 Orchestrator integration → Task 4.
- §8 Tests — every numbered test (T1–T12, O1–O4, C1–C3) is allocated to a task.
- §9 Files touched → covered across Tasks 1–4 (interfaces.py + registry.py in T1; splitter.py + __init__.py in T2; config.py in T3; orchestrator.py + README + PROGRESS in T4).
- §10 Out of scope — preserved as comments/notes inside Task 2 (`profile`/`params` unused) and Task 4 (continuity #02 banner in the attach-segment-0 comment).
- §11 Risks — example-config regression covered by Task 3 verify against `tests/test_examples.py`; splitter mutation of caller params covered by T8 in Task 2.

No placeholder strings, no "similar to Task N", no missing code blocks. Types match: `Splitter`, `Segment`, `ModelProfile`, `SplitterConfig`, `UnknownAdapter` — all defined where first used and reused with identical names downstream.

No user-gate language detected in the source brief or any task body. No `userGate: true` tagging required.
