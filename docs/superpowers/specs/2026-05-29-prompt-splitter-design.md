# Prompt Splitter — Design

**Date:** 2026-05-29
**Status:** Validated (user-approved 2026-05-29)
**Tracks:** Deferred layer #1 from `handoff_20260530-014826.md` §7; tracking-issue draft `/workspace/.tracking-issues/01-prompt-splitter.md`.

## 1. Purpose

Add a fifth swappable axis to kinoforge — the **splitter** — that turns a long-form prompt into an ordered `list[Segment]` for downstream packaging by `strategy.decide` and dispatch by `GenerateClipStage`. Wires the long-form pipeline's marquee deferred seam (handoff §10.4) without committing to a specific splitting strategy.

A pluggable ABC + registry pattern matches the existing axes (provider / source / engine / store). A heuristic default ships in core; LLM-semantic and scene-detect strategies plug in later as adapters under `splitters/<name>/`.

## 2. Architecture

```
GenerationRequest                    Splitter axis (new)
        │
        v
validate_request  ──▶  splitter.split  ──▶  attach assets to seg 0  ──▶  stage.run(..., segments_override=...)
                                                                                  │
                                                                                  v
                                                                            strategy.decide
                                                                                  │
                                                                                  v
                                                                              pool.map
```

The splitter sits in `orchestrator.generate()` between step 5 (`validate_request`) and step 9 (`stage.run`). The orchestrator owns asset attachment so the splitter contract stays prompt-only and independent of `ConditioningAsset` / role rules.

Self-registration on import follows the established axis pattern: `core/splitter.py` registers `"heuristic"` at module load. Future plugins live under `splitters/<name>/__init__.py` and get triggered via the single concrete-import line in `_adapters.py`.

## 3. Splitter ABC

Lives in `src/kinoforge/core/interfaces.py` alongside the other ABCs.

```python
class Splitter(ABC):
    """Convert a long-form prompt into ordered segments.

    Pure contract: deterministic, side-effect-free, no I/O. The output list
    must contain at least one Segment. Each Segment carries only ``prompt``;
    ``assets`` and ``params`` default to empty. Asset attachment and
    param-merging are handled by the orchestrator and ``strategy.decide``
    respectively.
    """

    name: str

    @abstractmethod
    def split(
        self,
        prompt: str,
        profile: ModelProfile,
        params: dict,
    ) -> list[Segment]:
        ...
```

**Postconditions:**
1. `len(result) >= 1`.
2. Every `Segment.prompt` is non-empty after stripping whitespace.
3. `Segment.assets == []` and `Segment.params == {}` for every returned segment (defaults).
4. Order is preserved (input narrative order maps to output index order).

## 4. Default implementation: `HeuristicSplitter`

Lives in `src/kinoforge/core/splitter.py`. Treated as part of core (analogous to `SequentialPool` in `core/pool.py`), not an adapter package.

**Rule:** split the prompt on blank-line boundaries. Each paragraph becomes one Segment.

```python
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n+")

class HeuristicSplitter(Splitter):
    name = "heuristic"

    def split(
        self,
        prompt: str,
        profile: ModelProfile,
        params: dict,
    ) -> list[Segment]:
        chunks = [c.strip() for c in _PARAGRAPH_BREAK.split(prompt)]
        chunks = [c for c in chunks if c]
        if not chunks:
            raise ValueError("prompt yielded zero non-empty segments")
        return [Segment(prompt=c) for c in chunks]
```

**Behavioural notes:**
- `profile` and `params` are accepted for ABC compliance but unused by the heuristic. Documented as "available for future strategies".
- Single-paragraph prompt → list of length 1 → today's single-segment behaviour preserved exactly.
- Runs of `\n\n\n…` collapse to one break (the regex requires at least two newlines with optional whitespace between).
- Whitespace inside a paragraph (`\n` without a blank line) is preserved verbatim.
- All-whitespace prompt → `ValueError`. Upstream `validate_request` should already reject empty prompts; raising here is defensive.

Module footer self-registers:

```python
registry.register_splitter("heuristic", lambda: HeuristicSplitter())
```

**Registration trigger.** `core/splitter.py` is part of `core`, not an adapter package, so it is NOT imported via `_adapters.py`. The registration fires when the module first loads. To guarantee it loads before any `registry.get_splitter("heuristic")` call, `src/kinoforge/core/__init__.py` adds `from kinoforge.core import splitter  # noqa: F401` (or equivalent). This keeps `_adapters.py` strictly the dock for concrete adapter packages, preserving the §3.1 invariant phrasing in the handoff: `_adapters.py` is where adapter packages are imported, and `core/__init__.py` is where in-core defaults wire themselves up.

## 5. Registry additions

Add to `src/kinoforge/core/registry.py`, symmetric to the engine / provider / store helpers:

```python
def register_splitter(name: str, factory: Callable[[], Splitter]) -> None:
    """Register a Splitter factory under ``name``. Re-registration overwrites."""

def get_splitter(name: str) -> Callable[[], Splitter]:
    """Return the registered factory. Raises ``UnknownAdapter`` on miss."""
```

Zero-arg factory keeps construction lazy and re-import idempotent. Unknown-name lookup raises `UnknownAdapter` (existing error class) — surfaces at `generate()`/`deploy()` time, not config load time, mirroring how engines/providers behave today.

## 6. Config model

New optional `splitter:` block in pydantic v2 `Config`:

```python
class SplitterConfig(BaseModel):
    kind: str = "heuristic"

class Config(BaseModel):
    ...
    splitter: SplitterConfig = SplitterConfig()
```

YAML form:

```yaml
splitter:
  kind: heuristic   # optional; this is the default
```

All four example configs (`local-fake.yaml`, `diffusers.yaml`, `wan.yaml`, `hosted.yaml`) keep working with no edits — `splitter` defaults are applied. A test confirms each example still parses.

## 7. Orchestrator integration

`generate()` step 6 (currently a DEFERRED stub at `orchestrator.py:404`) becomes:

```python
from kinoforge.core.interfaces import Segment

splitter = registry.get_splitter(cfg.splitter.kind)()
prompt_segments = splitter.split(validated.prompt, profile, params={})

if prompt_segments and validated.assets:
    prompt_segments[0] = dataclasses.replace(
        prompt_segments[0], assets=list(validated.assets)
    )

segments_override = prompt_segments
```

Passed to the stage:

```python
artifact = stage.run(request, segments_override=segments_override)
```

**Why assets only on segment 0:** the continuity layer (issue #02) will fill segments 1..N-1 with the previous segment's tail frame as `init_image`. Until #02 lands, downstream segments render condition-free, which matches today's behaviour for non-conditioned prompts.

Stage is **not** modified. Existing tests that supply `segments_override` directly still work.

## 8. Tests (TDD red-first)

### `tests/core/test_splitter.py` (new)

| # | Behaviour under test | Concrete bug it would catch |
|---|---|---|
| 1 | `Splitter` ABC importable from `kinoforge.core.interfaces` | Refactor removes the ABC; downstream plugins lose contract. |
| 2 | `HeuristicSplitter.split("one paragraph", ...)` returns list of length 1 with that prompt | Regex over-eagerly splits non-blank lines; breaks all existing single-segment tests. |
| 3 | `split("a\n\nb")` → 2 segments with `["a", "b"]` | Marker detection broken; multi-paragraph prompts collapse to one segment. |
| 4 | `split("a\n\n\n\nb")` → 2 segments (collapsing) | Runs of newlines emit empty middle segments instead of collapsing. |
| 5 | `split("  a  \n\n  b  ")` → `["a", "b"]` (strip) | Whitespace leaks into the prompt sent to backend. |
| 6 | `split("a\nb\n\nc")` → `["a\nb", "c"]` (single newline preserved in-paragraph) | Splitter destroys intentional line breaks inside a paragraph. |
| 7 | `split("   \n\n   ")` raises `ValueError` | All-whitespace prompt silently produces zero segments; downstream NPE. |
| 8 | Every returned Segment has `assets == []` and `params == {}` | Splitter accidentally mutates defaults or shares state across calls. |
| 9 | `registry.get_splitter("heuristic")()` returns a `HeuristicSplitter` instance | Self-registration missing; orchestrator can't resolve default. |
| 10 | `register_splitter("h2", factory)`; `get_splitter("h2")()` round-trips | Registry API broken or factory dropped. |
| 11 | `get_splitter("nope")` raises `UnknownAdapter` | Silent fallthrough on bad config; opaque failure at runtime. |
| 12 | `HeuristicSplitter` calls do not mutate `params` argument | Splitter writes back into caller's dict; future plugins inherit unsafe contract. |

### `tests/core/test_orchestrator.py` (extend)

| # | Behaviour under test | Bug caught |
|---|---|---|
| O-1 | Multi-paragraph prompt → stage receives N-segment `segments_override` (use `LocalProvider + FakeEngine + LocalArtifactStore`; assert via spy on `stage.run`) | Splitter not actually wired into `generate()`. |
| O-2 | Multi-paragraph prompt + i2v assets → assets land on segment 0 only; segments 1..N-1 have `assets == []` | Asset attachment logic broken or applied to all segments. |
| O-3 | Single-paragraph prompt + i2v assets → 1 Segment with assets attached (regression of today's path) | Splitter integration breaks the existing single-clip happy path. |
| O-4 | Config without explicit `splitter:` block → `"heuristic"` resolved | Default not applied; configs explode at runtime. |

### `tests/core/test_config.py` (extend)

| # | Behaviour under test | Bug caught |
|---|---|---|
| C-1 | `Config()` with no `splitter` field → `cfg.splitter.kind == "heuristic"` | Pydantic default missing. |
| C-2 | YAML with `splitter: {kind: heuristic}` parses identically | Schema rejects valid explicit-default form. |
| C-3 | YAML with `splitter: {kind: custom_thing}` parses successfully (unknown-kind error happens at registry lookup, not config load) | Config validates registry membership at load time; couples Config to global registry state. |

### `tests/test_examples.py` (verify, no new test)
All four example configs continue to parse — covered by existing parametrised tests; running them confirms no regression from the new optional field.

### Whole-suite acceptance
- `pixi run test` → all 357 existing + new tests green.
- `pixi run test-cov` → coverage ≥ 90%.
- `pixi run pre-commit run --all-files` → ruff + ruff-format + mypy clean.

## 9. Files touched

| File | Change |
|---|---|
| `src/kinoforge/core/interfaces.py` | Add `Splitter` ABC. |
| `src/kinoforge/core/registry.py` | Add `register_splitter` / `get_splitter` symmetric to existing axes. |
| `src/kinoforge/core/splitter.py` | New module: `HeuristicSplitter` + self-registration. |
| `src/kinoforge/core/__init__.py` | One-line `from kinoforge.core import splitter  # noqa: F401` to trigger the heuristic's self-registration. |
| `src/kinoforge/core/config.py` | Add `SplitterConfig`; thread into `Config` with default. |
| `src/kinoforge/core/orchestrator.py` | Replace step-6 DEFERRED stub with splitter resolve + call + asset attachment. |
| `tests/core/test_splitter.py` | New. |
| `tests/core/test_orchestrator.py` | Extend. |
| `tests/core/test_config.py` | Extend. |
| `PROGRESS.md` | Add post-MVP section with new tasks + commit refs. |
| `README.md` | Update "Extending" section to mention the splitter axis; remove splitter from Roadmap list. |

No new runtime dependencies. No changes to provider/engine/source/store packages. No changes to `_adapters.py` (core auto-registers).

## 10. Out of scope (explicit deferrals)

| Item | Where it lives later |
|---|---|
| LLM-semantic splitter | Adapter under `src/kinoforge/splitters/llm/`; registers via `_adapters.py`. When the first adapter splitter lands, `tests/test_core_invariant.py` extends its allowlist for the new `splitters/` directory in the same shape it already does for `providers/`, `engines/`, `sources/`, `stores/`. |
| Scene-detect splitter (e.g. via shot-boundary on a reference video) | Same pattern as above. |
| Per-segment `params` overrides (seed offset, cfg drift) | Future Splitter subclass; ABC supports it without contract change. |
| Per-segment metadata syntax (`[scene seed=42]`) | Deferred until a real need surfaces. |
| Continuity (tail-frame → next segment's `init_image`) | Tracking-issue #02. Splitter contract already leaves segments 1..N-1's `assets` empty for #02 to fill. |
| Stitching across N artifacts (concat / crossfade) | Separate concern; `GenerateClipStage` still returns `results[-1]` for now. |
| Duration-budget enforcement / estimation | Deliberately skipped — user owns segment authoring via marker placement. Future strategies may enforce. |

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Existing example configs break with new pydantic field | Field has a default + a test parametrises across all four example configs. |
| Splitter accidentally mutates caller's `params` dict | Explicit immutability test (T-12). Heuristic doesn't touch `params`. |
| Continuity (issue #02) needs a contract change to the Splitter ABC later | ABC is intentionally minimal — only `prompt`/`profile`/`params` in, `list[Segment]` out. Continuity is an orchestrator-layer concern that operates on the splitter's output. No ABC change anticipated. |
| Future LLM splitter needs network/credentials | The ABC is pure (no engine/backend reference). LLM splitter can take an injected client at construction; factory still zero-arg, with config-supplied params flowing through `Splitter.split`'s `params` argument or a constructor param read at registration time. |

## 12. Acceptance summary

A v1 splitter axis ships when:

1. `Splitter` ABC + `HeuristicSplitter` + `register_splitter`/`get_splitter` exist and self-register.
2. `Config.splitter.kind` defaults to `"heuristic"` and parses explicit blocks.
3. `orchestrator.generate()` calls the resolved splitter and attaches assets to segment 0 only.
4. All tests in §8 are red-first, then green; the existing 357-test suite stays green.
5. mypy strict + ruff + pre-commit are clean.
6. `PROGRESS.md` records the work; `README.md` describes the new axis.
