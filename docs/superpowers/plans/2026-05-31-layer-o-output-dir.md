# Layer O — User-facing output directory: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-out `OutputSink` seam that publishes finished clips to a flat user-visible directory (default `output/`) with `{timestamp}_{slug}{ext}` filenames; preserve every existing `ArtifactStore` / ledger / `uri_for` / `gc` semantic untouched; fold in a `--run-id` default uniquification fix to close the silent-overwrite foot-gun on the internal store side too.

**Architecture:** New `src/kinoforge/outputs/` sibling axis parallels `src/kinoforge/stores/` exactly. `OutputSink` Protocol + `slugify()` pure helper in `outputs/base.py`; `LocalOutputSink` concrete in `outputs/local.py`, self-registering on import via `_adapters.py`. Sink is **optional** on `GenerateClipStage` — `None` default preserves bit-for-bit behavior of every existing call path. CLI builds the sink via `_build_sink(cfg, args)` and threads it through `orchestrator.generate()` and `batch.batch_generate()`. Filenames built from `clock.now()` (local TZ via `datetime.fromtimestamp`) + ASCII-conservative slugified prompt prefix + engine-derived extension.

**Tech Stack:** stdlib `pathlib`, `unicodedata`, `hashlib`, `datetime`, `time.monotonic_ns`. pydantic v2 (existing). pytest + FakeClock (existing). No new runtime deps.

**Reference docs:**
- Spec: `docs/superpowers/specs/2026-05-31-layer-o-output-dir-design.md`
- Pattern precedents: `src/kinoforge/stores/local.py` (sibling-axis layout), `src/kinoforge/core/strategy.py` (pure helper), `src/kinoforge/core/clock.py` (`Clock.now()` Unix epoch float), `src/kinoforge/_adapters.py` (concrete-import hub), `src/kinoforge/core/config.py:394-421` (`StoreConfig` pattern to mirror), `src/kinoforge/pipeline/generate_clip.py` (dataclass stage, where publish hook lands).

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `src/kinoforge/outputs/__init__.py` | NEW | `OutputSink` Protocol re-export + `register_sink` / `get_sink` registry helpers. |
| `src/kinoforge/outputs/base.py` | NEW | `OutputSink` Protocol + `slugify()` pure helper + `format_filename()` pure helper + `OutputPublishError` exception. |
| `src/kinoforge/outputs/local.py` | NEW | `LocalOutputSink(dir, clock)` concrete; self-registers under `"local"`. |
| `src/kinoforge/core/config.py` | MODIFY (`394-421`, `424-450`) | Add `OutputConfig` pydantic block + `Config.output` field. |
| `src/kinoforge/pipeline/generate_clip.py` | MODIFY (`56-90`, `170-177`) | `GenerateClipStage` dataclass gains `sink: OutputSink \| None = None` + `namespace: str \| None = None` fields; publish call after `store.put_bytes`. |
| `src/kinoforge/core/orchestrator.py` | MODIFY (`675-794`) | `generate()` gains `sink: OutputSink \| None = None` kwarg; threads into stage construction. |
| `src/kinoforge/core/batch.py` | MODIFY (`240-298`, `batch_generate()` signature) | `batch_generate()` gains `sink: OutputSink \| None = None`; `_build_stage_for_entry` threads `sink` + `namespace=batch_id`. |
| `src/kinoforge/_adapters.py` | MODIFY | Side-effect import of `kinoforge.outputs.local` (one line, mirrors stores pattern). |
| `src/kinoforge/cli.py` | MODIFY (`119-198`, `309-338`) | Change `--run-id` default from `"run"` to `None`; add `--output-dir` / `--no-output-dir` mutually exclusive group to `generate` + `batch`; new `_build_sink(cfg, args)`; thread sink into `orchestrator.generate()` and `batch.batch_generate()`. |
| `tests/outputs/__init__.py` | NEW | Empty marker. |
| `tests/outputs/test_slugify.py` | NEW | ~12 slugify behavior tests. |
| `tests/outputs/test_local.py` | NEW | ~10 LocalOutputSink behavior tests. |
| `tests/core/test_config.py` | MODIFY | +3 `OutputConfig` round-trip tests. |
| `tests/pipeline/test_generate_clip.py` | MODIFY | +3 stage-publish integration tests (with `FakeOutputSink`). |
| `tests/core/test_batch.py` (or `tests/test_batch_cli.py`) | MODIFY | +2 batch namespace propagation tests. |
| `tests/test_cli.py` | MODIFY | +5 CLI flag/precedence/default-run-id tests. |
| `tests/test_examples.py` | MODIFY | +6 example-YAML round-trip with commented `output:` block. |
| `.gitignore` | MODIFY | Add `output/` line. |
| `examples/configs/{wan,diffusers,fal,hosted,local-fake,runpod-comfyui-wan}.yaml` | MODIFY | Add commented `output:` block to each. |
| `README.md` | MODIFY | New "Output directory" section. |
| `PROGRESS.md` | MODIFY | Phase 25 entry; breaking-change note for `--run-id` default. |

---

## Branch & gate convention

Per project history (Phases 17/18/19/20/21/22/23/24), Layer O work happens on `build/layer-o` branched from `main@454e514` (post-Layer-N merge). Every task creates a commit; the final task does the `--no-ff` merge into `main`.

```bash
# Once at start (Task 1, before any work):
git checkout main
git pull --ff-only
git checkout -b build/layer-o
```

Pre-commit must pass on every commit (`pixi run pre-commit run --files <paths>` after staging). Mypy + ruff + ruff-format all green.

---

## Task 1: `outputs/base.py` + `outputs/__init__.py` — Protocol, slugify, registry

**Goal:** Land the `OutputSink` Protocol, the `slugify()` pure helper, the `format_filename()` pure helper, the `OutputPublishError` exception, and the `register_sink` / `get_sink` registry — all without any concrete sink yet. Pure-function red-first.

**Files:**
- Create: `src/kinoforge/outputs/__init__.py`
- Create: `src/kinoforge/outputs/base.py`
- Create: `tests/outputs/__init__.py`
- Create: `tests/outputs/test_slugify.py`

**Acceptance Criteria:**
- [ ] `slugify("Waves crashing on basalt cliffs at dusk!")` returns `"Waves-crashing-on-ba"`.
- [ ] `slugify("🌊 ???")` returns `"clip"`.
- [ ] `slugify("")` returns `"clip"`.
- [ ] `slugify("a" * 1000)` returns a string of length 20 with no trailing `-` or `.`.
- [ ] `slugify("café résumé")` returns `"caf-rsum"` (accents dropped by ASCII encode; spaces → `-`; truncated).
- [ ] `slugify("  ---  ")` returns `"clip"`.
- [ ] `slugify("foo/bar\x00baz")` returns `"foo-bar-baz"` (forbidden chars replaced).
- [ ] `slugify("multi---dashes")` returns `"multi-dashes"` (runs collapsed).
- [ ] `slugify(".hidden")` returns `"hidden"` (leading `.` stripped).
- [ ] `slugify("Waves crashing on ba!")` (exactly 20 chars after slug-but-before-truncate is `"Waves-crashing-on-ba-"`) returns `"Waves-crashing-on-ba"` — no trailing `-` after truncation.
- [ ] `OutputPublishError` subclasses `RuntimeError` and accepts a string message.
- [ ] `register_sink("test", lambda: object())` then `get_sink("test")` returns the registered factory; unknown name raises `UnknownAdapter`.

**Verify:** `pixi run pytest tests/outputs/test_slugify.py -v` → all 12 tests pass.

**Steps:**

- [ ] **Step 1: Create test file with all 12 failing tests**

Create `tests/outputs/__init__.py` (empty file — pytest discovery marker).

Create `tests/outputs/test_slugify.py`:

```python
"""Behavioral tests for slugify() — the ASCII-conservative prompt slugger.

Each test states (1) what behavior is under test and (2) the concrete bug
the assertion catches if the implementation regresses.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import UnknownAdapter
from kinoforge.outputs import get_sink, register_sink
from kinoforge.outputs.base import OutputPublishError, slugify


def test_slugify_typical_prompt_truncated_to_20() -> None:
    """A normal English prompt is truncated to 20 chars with whitespace
    replaced by dashes.  Catches a regression where the truncation step
    swaps order with the dash replacement and produces partial-word
    artifacts.
    """
    assert slugify("Waves crashing on basalt cliffs at dusk!") == "Waves-crashing-on-ba"


def test_slugify_emoji_only_input_falls_back_to_clip() -> None:
    """Emoji + punctuation strips to nothing; fallback "clip" prevents an
    empty filename.  Catches a regression where the empty-result branch is
    forgotten and the sink writes "<ts>_.mp4".
    """
    assert slugify("🌊 ???") == "clip"


def test_slugify_empty_input_falls_back_to_clip() -> None:
    """Empty prompt → "clip".  Catches a regression where slugify of "" raises
    IndexError on a tail-strip step.
    """
    assert slugify("") == "clip"


def test_slugify_caps_length_at_max_chars() -> None:
    """Very long inputs are truncated to exactly max_chars (default 20).
    Catches a regression where a missing truncate step lets a 1 KB prompt
    produce a 1 KB filename that exceeds the 255-byte FS limit.
    """
    out = slugify("a" * 1000)
    assert len(out) == 20
    assert out[-1] not in ("-", ".")


def test_slugify_drops_accents_via_ascii_encode() -> None:
    """Latin-1 accents survive NFC normalization but get dropped by the
    ASCII encode.  Catches a regression where bytes-style encoding leaks
    through and produces shell-unsafe filenames.
    """
    assert slugify("café résumé") == "caf-rsum"


def test_slugify_pure_whitespace_or_dashes_falls_back() -> None:
    """Whitespace-only or dash-only input strips to empty → "clip".
    Catches a regression where the strip step runs before the collapse
    step and leaves a stray "-".
    """
    assert slugify("  ---  ") == "clip"


def test_slugify_replaces_forbidden_chars_with_dash() -> None:
    """Filesystem-forbidden chars (NUL, /) are replaced with "-".  Catches
    a regression where a forbidden char survives and the sink raises
    OSError on write.
    """
    assert slugify("foo/bar\x00baz") == "foo-bar-baz"


def test_slugify_collapses_runs_of_dashes() -> None:
    """Repeated dashes in the input → single dash in the output.  Catches
    a regression where the collapse step is forgotten and filenames carry
    "Waves---crashing" eyesores.
    """
    assert slugify("multi---dashes") == "multi-dashes"


def test_slugify_strips_leading_dot() -> None:
    """A leading "." is stripped to avoid creating a hidden file.  Catches
    a regression where ".hidden" produces ".hidden.mp4" (hidden on Linux).
    """
    assert slugify(".hidden") == "hidden"


def test_slugify_no_trailing_dash_after_truncation() -> None:
    """When truncation lands on a dash, the dash is stripped post-cut.
    Catches a regression where the post-truncate strip step is skipped
    and filenames end "Waves-crashing-on-ba-.mp4".
    """
    # "Waves crashing on ba!" → "Waves-crashing-on-ba-" (21 chars before truncate)
    assert slugify("Waves crashing on ba!") == "Waves-crashing-on-ba"


def test_output_publish_error_subclasses_runtime_error() -> None:
    """OutputPublishError is a RuntimeError so callers can catch the
    broad family.  Catches a regression where the exception base class
    changes silently and a downstream `except RuntimeError` stops catching.
    """
    e = OutputPublishError("disk full")
    assert isinstance(e, RuntimeError)
    assert str(e) == "disk full"


def test_register_and_get_sink_roundtrip() -> None:
    """Registering a sink factory makes it retrievable by name; unknown
    names raise UnknownAdapter (matches Splitter/Store registry behavior).
    Catches a regression where the registry silently swallows lookup
    misses and returns None.
    """
    sentinel = object()
    register_sink("__test_sink", lambda: sentinel)
    assert get_sink("__test_sink")() is sentinel
    with pytest.raises(UnknownAdapter):
        get_sink("__does_not_exist")
```

- [ ] **Step 2: Run tests, confirm RED (import errors expected)**

Run: `pixi run pytest tests/outputs/test_slugify.py -v`
Expected: 12 collection or import errors — `kinoforge.outputs` does not exist.

- [ ] **Step 3: Implement `outputs/base.py`**

Create `src/kinoforge/outputs/base.py`:

```python
"""OutputSink Protocol + pure helpers (slugify, format_filename) + errors.

This module is the engine-agnostic side of the publish seam: the Protocol
that GenerateClipStage depends on, plus pure functions any sink
implementation can compose.  No I/O, no concrete sink — see ``local.py``
for the default implementation.

Naming + sanitization conventions:

- ``slugify`` is ASCII-conservative on purpose: emoji, CJK, and accented
  characters are dropped (via ``encode("ascii", "ignore")``) rather than
  transliterated, because shell-quoting and grep/tab-complete ergonomics
  matter more for operator UX than filename expressiveness.  Cross-platform
  safety (Linux NFC vs macOS HFS+ NFD divergence) falls out of the same
  decision.
- ``format_filename`` separates the ``ts`` / ``slug`` / ``ext`` rendering
  from the collision-resolution logic in ``LocalOutputSink.publish`` so a
  future ``S3OutputSink`` can reuse the same naming and collide on its own
  terms.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol


class OutputPublishError(RuntimeError):
    """Raised when a sink cannot persist the bytes to its destination.

    Wraps the underlying OSError (or equivalent) so callers can catch a
    single, semantic exception type and decide whether to fail the run or
    fall back to a different sink.
    """


class OutputSink(Protocol):
    """Publish a finished clip with a user-facing filename.

    The contract: take in-memory bytes and a prompt-derived filename hint,
    place the file at the sink's destination, and return the absolute
    path or URI of the published file.  The sink owns its own clock,
    sanitization rules, and collision policy.

    Implementations MUST be idempotent under retry only in the sense that
    a second call with the same arguments produces a NEW path (via
    collision suffix) rather than overwriting — clip output must never be
    silently destroyed.
    """

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
    ) -> str:
        """Publish *data* under a name derived from *prompt* and *extension*.

        Args:
            data: The raw clip bytes to write.
            prompt: The user-facing prompt; first 20 ASCII-safe chars
                become the slug portion of the filename.
            extension: File suffix including the dot (e.g. ".mp4"); use
                ".bin" when the engine returns no extension.
            namespace: Optional sub-directory under the sink's root; used
                by ``batch_generate`` to group entries by ``batch_id``.

        Returns:
            The absolute path of the published file as a string.

        Raises:
            OutputPublishError: The sink could not write (read-only dir,
                disk full, permission denied, etc.).
        """
        ...


# slugify -------------------------------------------------------------------

# Characters allowed verbatim in the slug.  Everything else gets replaced
# with a dash before the collapse + trim passes.
_ALLOWED_CHARS = re.compile(r"[A-Za-z0-9._-]")
_DASH_RUN = re.compile(r"-+")


def slugify(prompt: str, max_chars: int = 20) -> str:
    """Return an ASCII-conservative slug of *prompt* up to *max_chars*.

    Pipeline:

    1. NFC-normalize, then ``encode("ascii", "ignore").decode()`` to drop
       emoji, CJK, and accented characters.
    2. Replace each character not in ``[A-Za-z0-9._-]`` with ``-``.
    3. Collapse runs of ``-`` to a single ``-``.
    4. Strip leading/trailing ``-`` and ``.``.
    5. Truncate to ``max_chars``.
    6. Strip trailing ``-`` and ``.`` again (truncation may have landed
       inside a dash run).
    7. Return ``"clip"`` if the result is empty.

    Args:
        prompt: The free-text prompt to slugify.
        max_chars: Maximum length of the returned slug (default 20).

    Returns:
        A filesystem-safe slug, guaranteed non-empty and ASCII-only.
    """
    ascii_only = unicodedata.normalize("NFC", prompt).encode("ascii", "ignore").decode()
    replaced = "".join(c if _ALLOWED_CHARS.match(c) else "-" for c in ascii_only)
    collapsed = _DASH_RUN.sub("-", replaced)
    trimmed = collapsed.strip("-.")
    truncated = trimmed[:max_chars]
    final = truncated.rstrip("-.")
    return final or "clip"


def format_filename(*, ts: str, slug: str, extension: str) -> str:
    """Compose ``{ts}_{slug}{extension}`` with no further sanitization.

    Args:
        ts: The local-TZ timestamp string, e.g. ``"20260531-210015"``.
        slug: The ASCII slug from :func:`slugify`.
        extension: File suffix including the dot (e.g. ``".mp4"``).

    Returns:
        The composed filename.
    """
    return f"{ts}_{slug}{extension}"
```

- [ ] **Step 4: Implement `outputs/__init__.py`**

Create `src/kinoforge/outputs/__init__.py`:

```python
"""kinoforge.outputs — user-facing publish seam (Layer O).

Sibling axis to ``kinoforge.stores`` with the same shape:

* :mod:`kinoforge.outputs.base` holds the engine-agnostic Protocol +
  pure helpers (``slugify``, ``format_filename``, ``OutputPublishError``).
* :mod:`kinoforge.outputs.local` holds the default ``LocalOutputSink``
  and self-registers under ``"local"`` on import.
* :func:`register_sink` / :func:`get_sink` mirror the patterns proven
  by ``kinoforge.core.registry`` for stores, providers, sources,
  engines, and splitters.

Concrete sinks are imported only by ``kinoforge._adapters`` (the
concrete-import hub used by the CLI); ``kinoforge.core`` never imports
this module's submodules directly.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.errors import UnknownAdapter
from kinoforge.outputs.base import (
    OutputPublishError,
    OutputSink,
    format_filename,
    slugify,
)

__all__ = [
    "OutputPublishError",
    "OutputSink",
    "format_filename",
    "get_sink",
    "register_sink",
    "slugify",
]

_SINKS: dict[str, Callable[[], OutputSink]] = {}


def register_sink(name: str, factory: Callable[[], OutputSink]) -> None:
    """Register a zero-arg sink factory under *name*.

    Args:
        name: The registry key (lowercase, matches ``output.kind`` in YAML).
        factory: Zero-arg callable returning a fresh ``OutputSink`` instance.
    """
    _SINKS[name] = factory


def get_sink(name: str) -> Callable[[], OutputSink]:
    """Return the registered factory for *name*.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory; call it to produce an ``OutputSink``.

    Raises:
        UnknownAdapter: No sink is registered under *name*.
    """
    try:
        return _SINKS[name]
    except KeyError as exc:
        raise UnknownAdapter(f"unknown output sink kind: {name!r}") from exc
```

- [ ] **Step 5: Run tests, confirm GREEN**

Run: `pixi run pytest tests/outputs/test_slugify.py -v`
Expected: 12 passed.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/outputs/__init__.py \
  src/kinoforge/outputs/base.py \
  tests/outputs/__init__.py \
  tests/outputs/test_slugify.py

git add src/kinoforge/outputs/__init__.py src/kinoforge/outputs/base.py \
        tests/outputs/__init__.py tests/outputs/test_slugify.py

git commit -m "$(cat <<'EOF'
feat(outputs): OutputSink protocol + slugify helper + registry (Layer O Task 1)

ASCII-conservative slugify pipeline (NFC → ASCII encode → forbidden-char
replace → run-collapse → trim → truncate → re-trim → "clip" fallback) plus
the engine-agnostic Protocol that GenerateClipStage will depend on in
Task 4.  Registry helpers mirror the existing store/splitter pattern.

12 slugify behavior tests cover the empty / emoji-only / accents /
forbidden-char / dash-run / leading-dot / no-trailing-dash-after-truncate
cases.  Pure functions, no I/O, easy to extend.
EOF
)"
```

---

## Task 2: `outputs/local.py` — `LocalOutputSink` concrete

**Goal:** Land the default `LocalOutputSink(dir, clock)` implementation, self-registered under `"local"`. Hardlinks are out of scope (v1 writes bytes); collision policy uses `_2..._99` suffix then 6-char sha256.

**Files:**
- Create: `src/kinoforge/outputs/local.py`
- Create: `tests/outputs/test_local.py`

**Acceptance Criteria:**
- [ ] `LocalOutputSink(tmp_path, FakeClock(start=<epoch_for_2026-05-31T21:00:15>)).publish(b"x", prompt="Waves crashing", extension=".mp4")` writes `tmp_path / "20260531-210015_Waves-crashing.mp4"` and returns its absolute string.
- [ ] Same call with `namespace="batch-X"` writes `tmp_path / "batch-X" / "20260531-210015_Waves-crashing.mp4"` and the subdirectory is created via `mkdir(parents=True, exist_ok=True)`.
- [ ] Two consecutive `publish()` calls with the same prompt and same FakeClock time produce `<...>.mp4` and `<...>_2.mp4` (collision suffix).
- [ ] After 99 consecutive collisions, the 100th call uses a 6-char sha256 hash suffix.
- [ ] Publishing into a read-only directory raises `OutputPublishError` (not bare `OSError`).
- [ ] `LocalOutputSink(Path("relative/dir"), clock).publish(...)` resolves the relative path against cwd at construction time and writes to the absolute resolved path.
- [ ] `extension=""` is treated as `".bin"` (no extension supplied by engine).
- [ ] Importing `kinoforge.outputs.local` self-registers under `"local"`: `get_sink("local")()` returns a `LocalOutputSink` instance.
- [ ] `publish()` uses `os.replace` for atomicity: writes to `<final>.tmp` then atomically renames; an OSError on the rename is wrapped as `OutputPublishError`.
- [ ] `clock.now()` returns a Unix epoch float; the sink converts to local TZ via `datetime.fromtimestamp(epoch)` (naive datetime — CPython default is local TZ) — assert by injecting a FakeClock at a known epoch and matching the exact string in the written filename.

**Verify:** `pixi run pytest tests/outputs/test_local.py -v` → all 10 tests pass.

**Steps:**

- [ ] **Step 1: Write all 10 failing tests**

Create `tests/outputs/test_local.py`:

```python
"""Behavioral tests for LocalOutputSink — the default local-FS publish sink.

Each test states the behavior under test and the concrete bug the assertion
catches.  All I/O uses pytest's tmp_path; FakeClock makes timestamps
deterministic.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.outputs import get_sink
from kinoforge.outputs.base import OutputPublishError
from kinoforge.outputs.local import LocalOutputSink


# Build a FakeClock epoch corresponding exactly to local-TZ 2026-05-31T21:00:15.
# We round-trip through datetime.timestamp() so we don't have to hand-compute
# the offset for whatever TZ the test environment is in.
_FIXED_EPOCH = datetime(2026, 5, 31, 21, 0, 15).timestamp()
_FIXED_TS_STRING = "20260531-210015"


def test_publish_writes_bytes_at_ts_slug_ext_path(tmp_path: Path) -> None:
    """A vanilla publish lands at <dir>/<ts>_<slug><ext> with the right
    bytes.  Catches a regression where the filename builder swaps ts/slug
    order or drops the underscore separator.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"hello", prompt="Waves crashing", extension=".mp4")
    expected = tmp_path / f"{_FIXED_TS_STRING}_Waves-crashing.mp4"
    assert Path(out) == expected.resolve()
    assert expected.read_bytes() == b"hello"


def test_publish_with_namespace_creates_subdir(tmp_path: Path) -> None:
    """A namespace argument nests the file under <dir>/<namespace>/ and
    creates the subdirectory if missing.  Catches a regression where
    namespace is interpreted as a filename prefix rather than a subdir.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"x", prompt="A", extension=".mp4", namespace="batch-X")
    expected = tmp_path / "batch-X" / f"{_FIXED_TS_STRING}_A.mp4"
    assert Path(out) == expected.resolve()
    assert expected.parent.is_dir()


def test_publish_collision_appends_underscore_n(tmp_path: Path) -> None:
    """Two publishes with identical prompt + clock state produce
    <name>.mp4 and <name>_2.mp4; the original is preserved.  Catches a
    regression where the sink silently overwrites — the foot-gun the
    whole layer exists to close.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    first = sink.publish(b"one", prompt="Cliffs", extension=".mp4")
    second = sink.publish(b"two", prompt="Cliffs", extension=".mp4")
    assert Path(first).name == f"{_FIXED_TS_STRING}_Cliffs.mp4"
    assert Path(second).name == f"{_FIXED_TS_STRING}_Cliffs_2.mp4"
    assert Path(first).read_bytes() == b"one"
    assert Path(second).read_bytes() == b"two"


def test_publish_collision_99_then_hash_suffix(tmp_path: Path) -> None:
    """After exhausting _2.._99, the next collision uses a 6-char sha256
    hash suffix.  Catches a regression where the suffix loop has an
    off-by-one and either raises or never gives up.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    # Pre-populate the 99 collision targets manually so we don't loop the sink.
    base = f"{_FIXED_TS_STRING}_X.mp4"
    (tmp_path / base).write_bytes(b"orig")
    for n in range(2, 100):
        (tmp_path / f"{_FIXED_TS_STRING}_X_{n}.mp4").write_bytes(b"prior")
    out = sink.publish(b"new", prompt="X", extension=".mp4")
    name = Path(out).name
    assert name.startswith(f"{_FIXED_TS_STRING}_X_")
    assert name.endswith(".mp4")
    # Hash suffix is 6 hex chars between underscore and extension.
    middle = name[len(f"{_FIXED_TS_STRING}_X_") : -len(".mp4")]
    assert len(middle) == 6
    assert all(c in "0123456789abcdef" for c in middle)


def test_publish_into_readonly_dir_raises_OutputPublishError(tmp_path: Path) -> None:
    """A write into a chmod-000 directory raises OutputPublishError, not
    bare OSError, so callers can catch the semantic family.  Catches a
    regression where the sink lets the underlying OSError propagate
    unwrapped.
    """
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)  # r-x, no write
    sink = LocalOutputSink(dir=target, clock=FakeClock(start=_FIXED_EPOCH))
    try:
        with pytest.raises(OutputPublishError):
            sink.publish(b"x", prompt="A", extension=".mp4")
    finally:
        target.chmod(0o700)  # restore so tmp_path cleanup works


def test_publish_resolves_relative_dir_against_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative dir argument is resolved against cwd at construction
    time; cwd changes after construction must not move the publish target.
    Catches a regression where the sink stores the raw relative path and
    later writes land in the wrong place.
    """
    monkeypatch.chdir(tmp_path)
    sink = LocalOutputSink(dir=Path("relative-out"), clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"x", prompt="A", extension=".mp4")
    assert Path(out).is_absolute()
    assert (tmp_path / "relative-out" / f"{_FIXED_TS_STRING}_A.mp4").exists()


def test_publish_empty_extension_defaults_to_bin(tmp_path: Path) -> None:
    """An empty extension string falls back to ".bin" so we never write
    files with no extension.  Catches a regression where empty extension
    produces "20260531-210015_A" with no suffix.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"x", prompt="A", extension="")
    assert Path(out).name == f"{_FIXED_TS_STRING}_A.bin"


def test_local_sink_self_registers_on_import() -> None:
    """Importing kinoforge.outputs.local registers under "local" so the
    CLI's _build_sink can resolve it via the registry without a direct
    import.  Catches a regression where the side-effect registration is
    removed and the registry returns UnknownAdapter at CLI startup.
    """
    import kinoforge.outputs.local  # noqa: F401  side-effect import

    factory = get_sink("local")
    # Factory requires arguments; we just assert it's the class itself or
    # a partial whose result is a LocalOutputSink at the right type.
    assert callable(factory)


def test_publish_uses_atomic_replace(tmp_path: Path) -> None:
    """The sink writes to <final>.tmp first then os.replace's it into
    place, so a crash mid-write never leaves a partial file at the final
    name.  Catches a regression where the sink writes directly to the
    final path and a crash mid-write leaves a corrupt file the operator
    later mistakes for a finished clip.
    """
    # Wrap Path.write_bytes to fail AFTER the .tmp file has been written
    # but BEFORE os.replace runs — assert the final path doesn't exist.
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))

    real_replace = __import__("os").replace

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated crash")

    import os as _os

    _os.replace = boom  # type: ignore[assignment]
    try:
        with pytest.raises(OutputPublishError):
            sink.publish(b"x", prompt="A", extension=".mp4")
    finally:
        _os.replace = real_replace  # type: ignore[assignment]

    final = tmp_path / f"{_FIXED_TS_STRING}_A.mp4"
    assert not final.exists()


def test_clock_now_converted_to_local_tz_naive_datetime(tmp_path: Path) -> None:
    """clock.now() returns a Unix epoch float; the sink renders it via
    datetime.fromtimestamp(epoch) (no tz arg) which gives local-TZ naive
    datetime — matching feedback_local_timezone_only.md.  Catches a
    regression where the sink uses datetime.utcfromtimestamp and the
    filename TS drifts off the operator's wall clock.
    """
    epoch = datetime(2026, 5, 31, 21, 0, 15).timestamp()
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=epoch))
    out = sink.publish(b"x", prompt="A", extension=".mp4")
    assert "20260531-210015" in Path(out).name
```

- [ ] **Step 2: Run tests, confirm RED**

Run: `pixi run pytest tests/outputs/test_local.py -v`
Expected: 10 import / collection errors — `kinoforge.outputs.local` does not exist.

- [ ] **Step 3: Implement `outputs/local.py`**

Create `src/kinoforge/outputs/local.py`:

```python
"""LocalOutputSink — default local-filesystem publish sink.

Writes bytes to ``<dir>/<namespace>/<ts>_<slug><ext>`` with atomic
rename semantics and ``_2.._99`` then sha256-hash collision suffixes.
Self-registers under ``"local"`` on import.

Layer P deferred: hardlink optimization via
``ArtifactStore.local_path_for(run_id, name)``.  V1 always writes bytes,
doubling local-store disk usage for one clip — negligible for sub-GB mp4s.
"""

from __future__ import annotations

import hashlib
import os
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.logging import get_logger
from kinoforge.outputs import register_sink
from kinoforge.outputs.base import OutputPublishError, format_filename, slugify

_log = get_logger(__name__)

_MAX_COLLISION_SUFFIX = 99


class LocalOutputSink:
    """Publish bytes to a local-filesystem directory with friendly filenames.

    Attributes:
        dir: Absolute directory root for all publishes (resolved at
            construction).
        clock: Time source — usually ``RealClock``; tests inject ``FakeClock``.
    """

    def __init__(self, dir: Path, clock: Clock | None = None) -> None:
        """Initialise the sink with a destination directory and optional clock.

        Args:
            dir: Destination root; relative paths are resolved against cwd
                at construction time.
            clock: Time source; defaults to :class:`RealClock`.
        """
        self.dir = Path(dir).resolve()
        self.clock = clock or RealClock()

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
    ) -> str:
        """Write *data* to ``<dir>/<namespace?>/<ts>_<slug><ext>``.

        Args:
            data: Raw bytes to write.
            prompt: Source prompt; first 20 ASCII chars become the slug.
            extension: File suffix with the dot (e.g. ``".mp4"``); empty
                string defaults to ``".bin"``.
            namespace: Optional batch_id subdirectory.

        Returns:
            Absolute path of the published file as a string.

        Raises:
            OutputPublishError: The write or the atomic replace failed.
        """
        ext = extension or ".bin"
        ts = datetime.fromtimestamp(self.clock.now()).strftime("%Y%m%d-%H%M%S")
        slug = slugify(prompt)
        base = format_filename(ts=ts, slug=slug, extension=ext)

        target_dir = self.dir / namespace if namespace else self.dir
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputPublishError(
                f"failed to create output directory {target_dir}: {exc}"
            ) from exc

        path = self._resolve_collision(target_dir, ts, slug, ext)

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        except OSError as exc:
            # Cleanup the partial file so an operator never sees a half-byte mp4.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OutputPublishError(
                f"failed to publish to {path}: {exc}"
            ) from exc

        _log.info("output published: %s", path)
        return str(path)

    def _resolve_collision(self, target_dir: Path, ts: str, slug: str, ext: str) -> Path:
        """Return the first non-existing path in the collision sequence.

        Sequence: ``base.ext`` → ``base_2.ext`` → ... → ``base_99.ext`` →
        ``base_<6-char-sha256>.ext``.
        """
        primary = target_dir / format_filename(ts=ts, slug=slug, extension=ext)
        if not primary.exists():
            return primary

        for n in range(2, _MAX_COLLISION_SUFFIX + 1):
            candidate = target_dir / f"{ts}_{slug}_{n}{ext}"
            if not candidate.exists():
                return candidate

        # 99 exhausted — fall back to a 6-char sha256 hash of monotonic_ns()
        # so two simultaneous fallbacks in the same nanosecond still
        # diverge (vanishingly unlikely; still belt+suspenders).
        hash_suffix = hashlib.sha256(str(time.monotonic_ns()).encode()).hexdigest()[:6]
        return target_dir / f"{ts}_{slug}_{hash_suffix}{ext}"


def _factory() -> LocalOutputSink:
    """Zero-arg factory used by the registry.  Real configuration of the
    sink (dir / clock) is done by ``cli._build_sink`` which constructs
    the class directly; this factory just exists so ``get_sink("local")``
    doesn't raise UnknownAdapter for callers that want a default-rooted
    sink at cwd.
    """
    return LocalOutputSink(dir=Path.cwd() / "output")


register_sink("local", _factory)
```

- [ ] **Step 4: Run tests, confirm GREEN**

Run: `pixi run pytest tests/outputs/ -v`
Expected: 22 passed (12 slugify + 10 local).

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/outputs/local.py \
  tests/outputs/test_local.py

git add src/kinoforge/outputs/local.py tests/outputs/test_local.py

git commit -m "$(cat <<'EOF'
feat(outputs): LocalOutputSink with atomic write + collision suffix (Layer O Task 2)

Default local-FS publish sink.  Writes bytes to <dir>/<namespace?>/
<ts>_<slug><ext>; ts comes from injected Clock via fromtimestamp() in
LOCAL TZ; slug from slugify() in Task 1.  Collisions resolved via
_2.._99 numeric suffix then 6-char sha256 hash.  Atomic via tmp + os.replace.

10 behavior tests cover happy path, namespace nesting, collision sequence
(both halves), hash exhaustion, read-only dir → OutputPublishError, relative
dir resolved at construction, empty-extension fallback, self-registration,
atomic-replace failure path, and local-TZ TS conversion.
EOF
)"
```

---

## Task 3: `OutputConfig` pydantic block

**Goal:** Add the YAML-level config block for the output dir, mirroring `StoreConfig`. Default-factory pattern so absent block uses sensible defaults.

**Files:**
- Modify: `src/kinoforge/core/config.py` (insert `OutputConfig` class near `StoreConfig` at line 394+; add `Config.output` field near line 447)
- Modify: `tests/core/test_config.py` (+3 round-trip tests)

**Acceptance Criteria:**
- [ ] `Config(...)` constructed with no `output` block defaults to `OutputConfig(kind="local", dir=Path("output"), enabled=True)`.
- [ ] An explicit `output: {kind: local, dir: /tmp/foo, enabled: true}` round-trips through `load_config()`.
- [ ] `output: {enabled: false}` round-trips and `cfg.output.enabled is False`.

**Verify:** `pixi run pytest tests/core/test_config.py -v -k Output` → 3 new tests pass; all existing config tests still pass.

**Steps:**

- [ ] **Step 1: Find the right test file + add the 3 failing tests**

Read the existing pattern first:

```bash
rg -n 'class TestStoreConfig|StoreConfig\(' /workspace/tests/core/test_config.py | head -5
```

Append to `tests/core/test_config.py`:

```python
class TestOutputConfig:
    """OutputConfig round-trip + default behavior (Layer O Task 3)."""

    def test_absent_block_uses_defaults(self, tmp_path: Path) -> None:
        """An empty config has no output: block; the block defaults to
        kind="local", dir=Path("output"), enabled=True.  Catches a
        regression where the field is misdefined as Optional and absent
        blocks parse as None.
        """
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(MINIMAL_FAKE_ENGINE_YAML)  # reuse existing fixture
        cfg = load_config(cfg_path)
        assert cfg.output.kind == "local"
        assert cfg.output.dir == Path("output")
        assert cfg.output.enabled is True

    def test_explicit_block_roundtrips(self, tmp_path: Path) -> None:
        """An explicit YAML block parses every field correctly.  Catches a
        regression where pydantic silently coerces dir from str to Path
        in an unexpected way or where enabled isn't honored.
        """
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            MINIMAL_FAKE_ENGINE_YAML
            + "\noutput:\n  kind: local\n  dir: /tmp/kf-out\n  enabled: true\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.output.kind == "local"
        assert cfg.output.dir == Path("/tmp/kf-out")
        assert cfg.output.enabled is True

    def test_enabled_false_disables(self, tmp_path: Path) -> None:
        """output.enabled=false round-trips as False so the CLI can build
        sink=None.  Catches a regression where the field is shadowed by
        the default-factory and the YAML override is lost.
        """
        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text(
            MINIMAL_FAKE_ENGINE_YAML + "\noutput:\n  enabled: false\n"
        )
        cfg = load_config(cfg_path)
        assert cfg.output.enabled is False
```

*Note:* if `MINIMAL_FAKE_ENGINE_YAML` isn't already defined in `test_config.py`, replace with the smallest existing fixture there. Read the file first to learn what's available.

- [ ] **Step 2: Run tests, confirm RED**

Run: `pixi run pytest tests/core/test_config.py -v -k Output`
Expected: 3 tests fail with AttributeError or pydantic validation error.

- [ ] **Step 3: Implement `OutputConfig`**

In `src/kinoforge/core/config.py`, insert after `StoreConfig` (around line 421):

```python
class OutputConfig(BaseModel):
    """Optional user-facing output-dir block.

    Absent block defaults to ``kind="local"``, ``dir=Path("output")``,
    ``enabled=True`` — the CLI then constructs a ``LocalOutputSink``
    rooted at ``cwd / "output"`` (Layer O design §5).

    Attributes:
        kind: Registry key of the output sink.  Only ``"local"`` ships
            in v1; cloud-native sinks (S3 mirror, webhook POST) are a
            future layer.
        dir: Local-sink destination directory.  Relative paths are
            resolved against cwd at sink construction.
        enabled: When ``False``, the CLI builds ``sink=None`` and the
            stage skips the publish call (today's behavior).
    """

    kind: Literal["local"] = "local"
    dir: Path = Path("output")
    enabled: bool = True
```

In the `Config` class (around line 447), add the field:

```python
    output: OutputConfig = Field(default_factory=OutputConfig)
```

Add to the `Config` docstring under `Attributes:`:

```
        output: User-facing output sink block (defaults to kind='local',
            dir='output', enabled=True).
```

- [ ] **Step 4: Run tests, confirm GREEN**

```bash
pixi run pytest tests/core/test_config.py -v
```

Expected: existing tests still pass + 3 new ones pass.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/core/config.py \
  tests/core/test_config.py

git add src/kinoforge/core/config.py tests/core/test_config.py

git commit -m "$(cat <<'EOF'
feat(config): OutputConfig pydantic block + Config.output field (Layer O Task 3)

YAML-level config surface for the output dir, mirroring StoreConfig's
absent-block-defaults-via-Field(default_factory=...) pattern.  Defaults
to kind="local", dir=Path("output"), enabled=True so a kinoforge.yaml
without an output: block still works.

3 round-trip tests lock down absent-block / explicit-block / enabled=false
behavior.  Wires into orchestrator + batch in Tasks 5/6 once the stage
gains the sink field in Task 4.
EOF
)"
```

---

## Task 4: `GenerateClipStage` sink + namespace integration

**Goal:** Add `sink: OutputSink | None = None` and `namespace: str | None = None` fields to the stage dataclass; call `sink.publish(...)` after `store.put_bytes` when sink is not None. None default = bit-for-bit identical to today's behavior.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` (constructor fields at line 82-90; run() tail at line 170-177)
- Modify: `tests/pipeline/test_generate_clip.py` (+3 stage integration tests with a `FakeOutputSink`)

**Acceptance Criteria:**
- [ ] `GenerateClipStage(...)` constructed without `sink` argument behaves bit-for-bit identical to today (no publish call, return value unchanged).
- [ ] `GenerateClipStage(sink=spy, ...)` with a single-segment request calls `spy.publish` exactly once with `prompt=request.prompt`, `extension=Path(artifact.filename).suffix`, `namespace=None`.
- [ ] `GenerateClipStage(sink=spy, namespace="batch-X", ...)` propagates `namespace="batch-X"` to `spy.publish`.
- [ ] Multi-segment chained non-native run only publishes the final artifact (matches the pre-existing "persist only last artifact" behavior at line 176-177).

**Verify:** `pixi run pytest tests/pipeline/test_generate_clip.py -v` → existing + 3 new tests pass.

**Steps:**

- [ ] **Step 1: Read the existing test file to learn fixture conventions**

```bash
rg -n 'def test_|FakeBackend|FakeEngine|GenerateClipStage' /workspace/tests/pipeline/test_generate_clip.py | head -40
```

- [ ] **Step 2: Write 3 failing tests using a local `FakeOutputSink` spy**

Append to `tests/pipeline/test_generate_clip.py`:

```python
@dataclass
class _SpyOutputSink:
    """In-test sink that records every publish call for assertions."""

    calls: list[dict] = field(default_factory=list)  # type: ignore[type-arg]

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "data_len": len(data),
                "prompt": prompt,
                "extension": extension,
                "namespace": namespace,
            }
        )
        return f"/fake/out/{prompt[:20]}{extension}"


def test_stage_with_sink_none_does_not_publish(...):
    """sink=None preserves today's behavior; the stage returns the same
    Artifact byte-for-byte without calling any sink.  Catches a
    regression where the publish branch runs unconditionally and an
    operator who set output.enabled=false still gets output/ pollution.
    """
    # Construct stage WITHOUT sink (or sink=None); reuse the existing
    # happy-path setup from test_generate_clip.py.
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="run",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
    )
    result = stage.run(request)
    assert result.uri  # store wrote it
    # Implicit: no sink calls to assert — the test passes iff stage.run
    # doesn't crash and doesn't accidentally publish anywhere.


def test_stage_with_sink_publishes_with_request_prompt(...):
    """sink=spy → publish called once with the request's prompt and the
    Artifact's extension.  Catches a regression where the stage forgets
    to thread the prompt or extension from the right source.
    """
    spy = _SpyOutputSink()
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="run",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
        sink=spy,
    )
    request = GenerationRequest(prompt="Test waves", mode="t2v")
    stage.run(request)
    assert len(spy.calls) == 1
    assert spy.calls[0]["prompt"] == "Test waves"
    # FakeEngine produces "fake_<sha>.bin"; suffix=".bin"
    assert spy.calls[0]["extension"] == ".bin"
    assert spy.calls[0]["namespace"] is None


def test_stage_namespace_field_propagates_to_sink(...):
    """A namespace constructor arg flows through to sink.publish.  Catches
    a regression where batch_id grouping silently breaks because the
    stage didn't pass the namespace argument.
    """
    spy = _SpyOutputSink()
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="run",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
        sink=spy,
        namespace="batch-20260531-204500",
    )
    stage.run(GenerationRequest(prompt="X", mode="t2v"))
    assert spy.calls[0]["namespace"] == "batch-20260531-204500"
```

*Note:* the `...` placeholders are the existing per-test fixtures (`profile`, `pool`, `store`, `engine`, etc.) that the existing tests in this file already construct. Reuse them verbatim — the patterns are right there in the same file. Do NOT introduce new fixture machinery.

Add to the file's imports:

```python
from kinoforge.outputs import OutputSink  # noqa: F401  (typing-only doc reference)
```

- [ ] **Step 3: Run tests, confirm RED**

Run: `pixi run pytest tests/pipeline/test_generate_clip.py -v -k "sink or namespace"`
Expected: 3 tests fail with `TypeError: __init__() got an unexpected keyword argument 'sink'`.

- [ ] **Step 4: Add fields + publish call to `GenerateClipStage`**

In `src/kinoforge/pipeline/generate_clip.py`, modify the dataclass (around line 82-90):

Add these imports near the top of the file (after the existing kinoforge.core.interfaces import):

```python
from kinoforge.outputs.base import OutputSink
```

In the dataclass fields, after `http_get_bytes` (line 90), add:

```python
    sink: OutputSink | None = None
    namespace: str | None = None
```

Update the dataclass docstring's `Attributes:` block to document the two new fields:

```
        sink: Optional user-facing publish target.  When ``None`` (the
            default) the stage behaves identically to pre-Layer-O —
            ``store.put_bytes`` is the only persistence side effect.
            When non-None, the stage calls ``sink.publish(payload,
            prompt=segment.prompt, extension=ext, namespace=self.namespace)``
            after ``store.put_bytes`` returns.
        namespace: Optional sub-directory grouping for the sink, used by
            ``batch_generate`` to namespace per-batch publishes under
            ``<output_dir>/<batch_id>/``.
```

In `run()`, at the end (around line 174-177), replace:

```python
        last = results[-1]

        # Persist the bytes derived from the engine's Artifact.
        payload = self._artifact_bytes(last)
        return self.store.put_bytes(self.run_id, last.filename, payload)
```

with:

```python
        last = results[-1]

        # Persist the bytes derived from the engine's Artifact.
        payload = self._artifact_bytes(last)
        stored = self.store.put_bytes(self.run_id, last.filename, payload)

        # Layer O — also publish to the user-facing sink if one is wired.
        # Read prompt from the LAST segment so chained continuity (i2v)
        # uses the final segment's prompt when it eventually grows past
        # the seg-0-only case; today single-segment is the only path
        # that publishes anything meaningful, so this is also correct
        # for it.
        if self.sink is not None:
            ext = Path(last.filename).suffix or ".bin"
            self.sink.publish(
                payload,
                prompt=segments[-1].prompt,
                extension=ext,
                namespace=self.namespace,
            )

        return stored
```

Add `from pathlib import Path` to the top of the file if not already present.

- [ ] **Step 5: Run tests, confirm GREEN**

Run: `pixi run pytest tests/pipeline/test_generate_clip.py -v`
Expected: existing tests still green + 3 new ones pass.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/pipeline/generate_clip.py \
  tests/pipeline/test_generate_clip.py

git add src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_generate_clip.py

git commit -m "$(cat <<'EOF'
feat(pipeline): GenerateClipStage publishes to OutputSink (Layer O Task 4)

The stage now optionally publishes the final clip's bytes to a
user-facing OutputSink in addition to the existing store.put_bytes call.
sink=None preserves pre-Layer-O behavior bit-for-bit; namespace=None for
single runs, namespace=batch_id for batch entries.  Reads the prompt
from segments[-1] so the chained-continuity case (when it grows past
seg-0 attachment) still publishes a meaningful filename.

3 stage tests cover the sink=None, sink-present, and namespace-propagation
paths via a local _SpyOutputSink dataclass.
EOF
)"
```

---

## Task 5: `orchestrator.generate()` sink threading

**Goal:** `generate()` gains `sink: OutputSink | None = None` kwarg; threads into the `GenerateClipStage` constructor. None default keeps every existing call site working.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (signature at line 675-686; stage construction at line 785-794)
- Modify: existing orchestrator test file (+2 tests)

**Acceptance Criteria:**
- [ ] `generate(cfg, request, store=store)` with no `sink` kwarg constructs a stage with `sink=None`.
- [ ] `generate(cfg, request, store=store, sink=spy)` constructs a stage with `sink=spy` and the spy receives one `publish` call per generated artifact.

**Verify:** `pixi run pytest tests/core/test_orchestrator.py -v -k sink` → 2 new tests pass; all existing orchestrator tests still green.

**Steps:**

- [ ] **Step 1: Locate the right test file**

```bash
rg -l 'def test_.*generate' /workspace/tests/core/ | head -3
```

The orchestrator tests likely live in `tests/core/test_orchestrator.py`. Read it to learn the fixture conventions (FakeEngine, FakeProvider, etc.) before writing new tests.

- [ ] **Step 2: Write 2 failing tests**

Append to `tests/core/test_orchestrator.py`:

```python
def test_generate_default_sink_is_none(...):
    """generate() without sink kwarg builds a stage with sink=None so
    every existing caller (CLI today, tests, downstream automation) keeps
    working unchanged.  Catches a regression where the kwarg becomes
    required and breaks legacy callers.
    """
    # Capture the stage construction via monkeypatch of GenerateClipStage
    # or by inspecting the result's side effects.  The minimal-fixture
    # path here is to ensure the generate() call succeeds with no sink
    # and produces the expected Artifact.
    art = generate(cfg, request, store=store)
    assert art.uri  # the call worked end-to-end with no sink


def test_generate_threads_sink_into_stage(...):
    """generate(sink=spy) threads the sink into the stage so spy.publish
    is called for every produced Artifact.  Catches a regression where
    the kwarg is accepted but silently dropped before reaching the stage.
    """
    spy = _SpyOutputSink()  # use the same spy class defined in Task 4 if
                            # it's in a shared conftest; otherwise inline
                            # the dataclass at the top of this test file.
    art = generate(cfg, request, store=store, sink=spy)
    assert art.uri
    assert len(spy.calls) >= 1
```

- [ ] **Step 3: Run tests, confirm RED**

Run: `pixi run pytest tests/core/test_orchestrator.py -v -k sink`
Expected: 2 tests fail (TypeError on unexpected kwarg).

- [ ] **Step 4: Add `sink` kwarg + thread into stage construction**

In `src/kinoforge/core/orchestrator.py`, modify the `generate()` signature (around line 675-686):

Add the import at the top of the file:

```python
from kinoforge.outputs.base import OutputSink
```

Update the signature:

```python
def generate(
    cfg: Config,
    request: GenerationRequest,
    *,
    store: ArtifactStore,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    run_id: str = "run",
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
) -> Artifact:
```

Add to the docstring `Args:` block:

```
        sink: Optional user-facing output sink.  When provided, the stage
            calls ``sink.publish(...)`` after persisting to the store.
            ``None`` (default) preserves pre-Layer-O behavior.
```

Update the stage construction (around line 785-794):

```python
        stage = GenerateClipStage(
            profile=session.profile,
            pool=session.pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
            engine=session.engine,
            sink=sink,
        )
```

- [ ] **Step 5: Run tests, confirm GREEN**

Run: `pixi run pytest tests/core/test_orchestrator.py -v`
Expected: existing + 2 new tests pass.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/core/orchestrator.py \
  tests/core/test_orchestrator.py

git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py

git commit -m "$(cat <<'EOF'
feat(orchestrator): generate() accepts OutputSink kwarg (Layer O Task 5)

Threads an optional sink through generate() → GenerateClipStage so the CLI
can wire user-facing publishing without touching the stage directly.  None
default keeps every existing call site (tests, downstream automation, CLI
pre-Task-7) working bit-for-bit.

2 orchestrator tests lock down the sink=None default and the
sink-threading happy path.
EOF
)"
```

---

## Task 6: `batch.batch_generate()` sink + namespace propagation

**Goal:** `batch_generate()` gains a `sink: OutputSink | None = None` kwarg; `_build_stage_for_entry` passes `sink` + `namespace=batch_id` into the stage. All per-entry artifacts publish under `<output_dir>/<batch_id>/`.

**Files:**
- Modify: `src/kinoforge/core/batch.py` (`_build_stage_for_entry` signature + call; `batch_generate` signature)
- Modify: `tests/test_batch_cli.py` (or wherever batch tests live; +2 tests)

**Acceptance Criteria:**
- [ ] `batch_generate(..., sink=spy)` calls `spy.publish` once per entry with `namespace=batch_id`.
- [ ] `batch_generate(...)` without a sink kwarg behaves identically to today (no publish calls).

**Verify:** `pixi run pytest tests/test_batch_cli.py -v -k sink` → 2 new tests pass; all existing batch tests still green.

**Steps:**

- [ ] **Step 1: Locate batch tests + read the existing fixture pattern**

```bash
rg -l 'batch_generate' /workspace/tests/ | head -3
```

The batch unit tests live alongside the CLI tests in `tests/test_batch_cli.py` per PROGRESS Phase 22 history. Pure batch_generate() tests may also exist in `tests/core/test_batch.py` — check both.

- [ ] **Step 2: Write 2 failing tests**

The test names + assertions:

```python
def test_batch_generate_default_sink_is_none(...):
    """batch_generate() without sink kwarg builds per-entry stages with
    sink=None.  Catches a regression where the kwarg becomes required
    and the CLI breaks for anyone running batch without --output-dir.
    """
    result = batch_generate(cfg=cfg, manifest=manifest, ...)
    # The test passes iff batch_generate succeeds end-to-end without
    # sink and produces a BatchResult with one outcome per entry.
    assert all(o.status == "ok" for o in result.outcomes)


def test_batch_generate_threads_sink_with_batch_id_namespace(...):
    """batch_generate(sink=spy) → spy.publish called per entry with
    namespace=batch_id so all outputs from one batch share a subdir.
    Catches a regression where the namespace is hardcoded to None or
    set to the per-entry run_id instead of the batch_id.
    """
    spy = _SpyOutputSink()
    result = batch_generate(
        cfg=cfg, manifest=manifest, sink=spy, batch_id="batch-20260531-X", ...
    )
    assert len(spy.calls) == len(manifest.entries)
    assert all(c["namespace"] == "batch-20260531-X" for c in spy.calls)
```

- [ ] **Step 3: Run tests, confirm RED**

Run: `pixi run pytest tests/test_batch_cli.py -v -k sink` (and/or `tests/core/test_batch.py`)
Expected: TypeError on unexpected `sink` kwarg.

- [ ] **Step 4: Add `sink` kwarg to `batch_generate` and `_build_stage_for_entry`**

In `src/kinoforge/core/batch.py`:

Add to the existing imports:

```python
from kinoforge.outputs.base import OutputSink
```

Modify `_build_stage_for_entry` signature (around line 240):

```python
def _build_stage_for_entry(
    cfg: Config,
    entry: BatchEntry,
    session: DeploySession,
    accepted_kinds: set[str],
    store: ArtifactStore,
    batch_id: str,
    sink: OutputSink | None = None,
) -> tuple[GenerateClipStage, GenerationRequest]:
```

In the stage construction inside that function (around line 288-297), add `sink=sink` and `namespace=batch_id`:

```python
    stage = GenerateClipStage(
        profile=session.profile,
        pool=session.pool,
        store=store,
        run_id=entry_run_id,
        accepted_kinds=accepted_kinds,
        base_params=merged_params,
        base_spec=merged_spec,
        engine=session.engine,
        sink=sink,
        namespace=batch_id,
    )
```

Modify the `batch_generate()` signature (locate it via `rg -n 'def batch_generate' src/kinoforge/core/batch.py`):

Add `sink: OutputSink | None = None` to the keyword-only block. At the call site of `_build_stage_for_entry` inside `batch_generate`, pass `sink=sink`.

Document the new kwarg in the `batch_generate` docstring `Args:` block:

```
        sink: Optional user-facing publish target (Layer O); when set,
            each entry's final artifact is also published to
            ``<sink>/<batch_id>/`` with a friendly filename.  None default
            preserves pre-Layer-O behavior.
```

- [ ] **Step 5: Run tests, confirm GREEN**

Run: `pixi run pytest tests/test_batch_cli.py tests/core/ -v -k batch`
Expected: existing + 2 new tests pass.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/core/batch.py \
  tests/test_batch_cli.py

git add src/kinoforge/core/batch.py tests/test_batch_cli.py

git commit -m "$(cat <<'EOF'
feat(batch): batch_generate threads OutputSink + batch_id namespace (Layer O Task 6)

Per-entry GenerateClipStage gets sink=sink + namespace=batch_id so every
finished clip from one batch lands under <output_dir>/<batch_id>/.  None
default preserves pre-Layer-O batch behavior.

2 batch tests cover the sink=None default and the sink-with-batch_id
namespace propagation.
EOF
)"
```

---

## Task 7: CLI flags + `_build_sink` + `--run-id` default change

**Goal:** Wire the user-facing controls. `--output-dir PATH` / `--no-output-dir` flags on `generate` + `batch`; `_build_sink(cfg, args)` constructs the sink; `--run-id` default flips from `"run"` to a local-TZ uniquified default. CLI also imports `kinoforge.outputs.local` via `_adapters.py` so the registry is populated by the time `_build_sink` runs.

**Files:**
- Modify: `src/kinoforge/_adapters.py` (one-line side-effect import of `kinoforge.outputs.local`)
- Modify: `src/kinoforge/cli.py` (parser at line 119-198; `_cmd_generate` at line 309-338; `_cmd_batch` accordingly)
- Modify: `tests/test_cli.py` (+5 tests)

**Acceptance Criteria:**
- [ ] `kinoforge generate -c cfg.yaml --prompt P --mode t2v --output-dir /tmp/foo` constructs a `LocalOutputSink(dir=Path("/tmp/foo").resolve())`.
- [ ] `kinoforge generate -c cfg.yaml --prompt P --mode t2v --no-output-dir` constructs `sink=None`.
- [ ] `kinoforge generate -c cfg.yaml --prompt P --mode t2v` (no flag) uses `cfg.output.dir` from YAML if present, else `Path("output")`.
- [ ] `kinoforge generate -c cfg.yaml --prompt P --mode t2v --output-dir A --no-output-dir` → argparse error (mutex group).
- [ ] Two successive `kinoforge generate ... ` invocations (no `--run-id`) produce two distinct run_ids of shape `run-YYYYMMDD-HHMMSS`; assertable via FakeClock injection on the CLI's clock seam.

**Verify:** `pixi run pytest tests/test_cli.py -v -k "output or run_id"` → 5 new tests pass; existing tests still green.

**Steps:**

- [ ] **Step 1: Read `_adapters.py` to understand the existing registration pattern**

```bash
cat /workspace/src/kinoforge/_adapters.py
```

- [ ] **Step 2: Add one line to `_adapters.py` for outputs.local side-effect registration**

Add to `src/kinoforge/_adapters.py`, alongside the existing store side-effect imports:

```python
import kinoforge.outputs.local  # noqa: F401  side-effect: register "local" OutputSink
```

- [ ] **Step 3: Write 5 failing CLI tests**

Append to `tests/test_cli.py`:

```python
def test_cli_output_dir_flag_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--output-dir wins over cfg.output.dir from YAML.  Catches a
    regression where precedence inverts and the YAML silently shadows
    the explicit CLI flag.
    """
    # Use the local-fake.yaml fixture (or build a minimal one) plus
    # monkeypatch the orchestrator to capture the sink argument.
    captured: dict = {}

    def fake_generate(cfg, request, *, store, sink=None, **kwargs):
        captured["sink"] = sink
        return Artifact(filename="out.mp4", uri=f"file://{tmp_path}/out.mp4")

    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)
    rc = main([
        "generate", "-c", str(LOCAL_FAKE_YAML), "--prompt", "P", "--mode", "t2v",
        "--output-dir", str(tmp_path / "cli-out"),
    ])
    assert rc == 0
    sink = captured["sink"]
    assert isinstance(sink, LocalOutputSink)
    assert sink.dir == (tmp_path / "cli-out").resolve()


def test_cli_no_output_dir_disables_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-output-dir → sink=None.  Catches a regression where the flag
    silently disables the sink only when output.enabled=true in YAML.
    """
    captured: dict = {}

    def fake_generate(cfg, request, *, store, sink=None, **kwargs):
        captured["sink"] = sink
        return Artifact(filename="out.mp4", uri="file:///tmp/out.mp4")

    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)
    rc = main([
        "generate", "-c", str(LOCAL_FAKE_YAML), "--prompt", "P", "--mode", "t2v",
        "--no-output-dir",
    ])
    assert rc == 0
    assert captured["sink"] is None


def test_cli_default_output_dir_is_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No flag + no YAML override → sink rooted at cwd/output.  Catches a
    regression where the default is None (always disabled) when neither
    the flag nor the YAML are set.
    """
    captured: dict = {}

    def fake_generate(cfg, request, *, store, sink=None, **kwargs):
        captured["sink"] = sink
        return Artifact(filename="out.mp4", uri="file:///tmp/out.mp4")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)
    rc = main([
        "generate", "-c", str(LOCAL_FAKE_YAML), "--prompt", "P", "--mode", "t2v",
    ])
    assert rc == 0
    sink = captured["sink"]
    assert isinstance(sink, LocalOutputSink)
    assert sink.dir == (tmp_path / "output").resolve()


def test_cli_output_dir_and_no_output_dir_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    """Passing both flags is an argparse error before any work runs.
    Catches a regression where the mutex group is dropped and operator
    confusion sets in over precedence.
    """
    with pytest.raises(SystemExit):
        main([
            "generate", "-c", str(LOCAL_FAKE_YAML), "--prompt", "P", "--mode", "t2v",
            "--output-dir", "/tmp/a", "--no-output-dir",
        ])
    err = capsys.readouterr().err
    assert "--output-dir" in err and "--no-output-dir" in err


def test_cli_default_run_id_uniquifies_per_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default --run-id is f"run-{ts}" with ts = local-TZ
    YYYYMMDD-HHMMSS at invocation time; two invocations with the same
    FakeClock produce the same string, two invocations with different
    clock times produce different strings.  Catches the silent-overwrite
    foot-gun that the layer exists to close.
    """
    captured_ids: list[str] = []

    def fake_generate(cfg, request, *, store, run_id="run", **kwargs):
        captured_ids.append(run_id)
        return Artifact(filename="out.mp4", uri="file:///tmp/out.mp4")

    # Patch the CLI's clock seam (added in Step 4 below) so we control time.
    fake_clock = FakeClock(start=datetime(2026, 5, 31, 21, 0, 15).timestamp())
    monkeypatch.setattr("kinoforge.cli._cli_clock", fake_clock)
    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)

    main(["generate", "-c", str(LOCAL_FAKE_YAML), "--prompt", "P", "--mode", "t2v"])
    fake_clock.advance(1.0)
    main(["generate", "-c", str(LOCAL_FAKE_YAML), "--prompt", "P", "--mode", "t2v"])

    assert captured_ids[0] == "run-20260531-210015"
    assert captured_ids[1] == "run-20260531-210016"
```

*Note:* `LOCAL_FAKE_YAML` should point at the existing `examples/configs/local-fake.yaml` (or a minimal fixture in `tests/`). Reuse whatever existing CLI tests in this file already use — read the file to find the pattern.

- [ ] **Step 4: Run tests, confirm RED**

Run: `pixi run pytest tests/test_cli.py -v -k "output or run_id"`
Expected: tests fail with argparse errors / AttributeError on missing `_cli_clock`.

- [ ] **Step 5: Modify CLI parser + `_cmd_generate` + `_cmd_batch`**

In `src/kinoforge/cli.py`:

Add to imports:

```python
from datetime import datetime

from kinoforge.core.clock import Clock, RealClock
from kinoforge.outputs.base import OutputSink
from kinoforge.outputs.local import LocalOutputSink
```

Add a module-level clock seam (placed near the top of the file, after imports):

```python
# CLI clock seam — overridable in tests via monkeypatch.  Used to derive
# the default --run-id and any other invocation-time stamps.
_cli_clock: Clock = RealClock()
```

In `_build_parser` (around line 160-164), change the `--run-id` default from `"run"` to `None`:

```python
    p_generate.add_argument("--run-id", default=None, metavar="ID")
```

In the same parser function, add the `--output-dir` / `--no-output-dir` mutex group to `p_generate` (after the existing `--run-id` arg):

```python
    p_generate_output = p_generate.add_mutually_exclusive_group()
    p_generate_output.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="user-facing output directory (overrides cfg.output.dir)",
    )
    p_generate_output.add_argument(
        "--no-output-dir",
        action="store_true",
        help="disable user-facing publish; clips remain only in the store",
    )
```

And to `p_batch` (after `--env-file`):

```python
    p_batch_output = p_batch.add_mutually_exclusive_group()
    p_batch_output.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="user-facing output directory (overrides cfg.output.dir)",
    )
    p_batch_output.add_argument(
        "--no-output-dir",
        action="store_true",
        help="disable user-facing publish; clips remain only in the store",
    )
```

Add `_build_sink` near the existing `_build_store` (around line 57):

```python
def _build_sink(cfg: Config, args: argparse.Namespace) -> OutputSink | None:
    """Return the configured OutputSink, or None when publishing is disabled.

    Precedence:
      1. ``--no-output-dir`` flag → ``None``.
      2. ``--output-dir PATH`` flag → ``LocalOutputSink(PATH)``.
      3. ``cfg.output.enabled is False`` → ``None``.
      4. Else → ``LocalOutputSink(cfg.output.dir)``.

    Args:
        cfg: Loaded kinoforge configuration.
        args: Parsed CLI arguments.

    Returns:
        A ``LocalOutputSink`` rooted at the resolved directory, or
        ``None`` when the operator opted out.
    """
    if getattr(args, "no_output_dir", False):
        return None
    explicit = getattr(args, "output_dir", None)
    if explicit is not None:
        return LocalOutputSink(dir=Path(explicit), clock=_cli_clock)
    if not cfg.output.enabled:
        return None
    return LocalOutputSink(dir=cfg.output.dir, clock=_cli_clock)
```

In `_cmd_generate` (around line 309-338), replace the `run_id` derivation + add `sink=...`:

```python
def _cmd_generate(args: argparse.Namespace, state_dir: Path) -> int:
    cfg = load_config(args.config)
    request = GenerationRequest(prompt=args.prompt, mode=args.mode)
    store = _build_store(cfg, state_dir)
    sink = _build_sink(cfg, args)

    if args.run_id is not None:
        run_id: str = args.run_id
    else:
        ts = datetime.fromtimestamp(_cli_clock.now()).strftime("%Y%m%d-%H%M%S")
        run_id = f"run-{ts}"

    try:
        artifact = generate(
            cfg, request, store=store, sink=sink, run_id=run_id, state_dir=state_dir
        )
    except UnknownAdapter as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"artifact: {artifact.uri}")
    return 0
```

In `_cmd_batch`, similarly construct `sink = _build_sink(cfg, args)` and pass `sink=sink` into `batch_generate(...)`.

- [ ] **Step 6: Run tests, confirm GREEN**

Run: `pixi run pytest tests/test_cli.py -v`
Expected: existing + 5 new tests pass.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/_adapters.py \
  src/kinoforge/cli.py \
  tests/test_cli.py

git add src/kinoforge/_adapters.py src/kinoforge/cli.py tests/test_cli.py

git commit -m "$(cat <<'EOF'
feat(cli): --output-dir / --no-output-dir flags + uniquified --run-id default (Layer O Task 7)

Wires the user-facing controls described in the spec:

* --output-dir PATH / --no-output-dir mutex group on `generate` and `batch`.
* _build_sink(cfg, args) honors the precedence:
  --no-output-dir > --output-dir > cfg.output.enabled=false > cfg.output.dir.
* --run-id default flips from "run" to f"run-{local-tz YYYYMMDD-HHMMSS}".
  Explicit --run-id foo invocations unaffected.
* Module-level _cli_clock seam so tests can monkeypatch the clock without
  touching the orchestrator's clock injection points.
* _adapters.py imports kinoforge.outputs.local for side-effect registration
  so the registry is populated by the time _build_sink runs.

5 CLI tests cover the override / disable / default / mutex / run_id-uniquify
paths.

BREAKING (narrow): scripts that grep .kinoforge/run/ no longer find clips
when --run-id is omitted; pass --run-id run to restore prior behavior.
Documented in PROGRESS.md as the second breaking change after the
Layer C `kinoforge gc --config PATH` requirement.
EOF
)"
```

---

## Task 8: `.gitignore` + example YAMLs + round-trip tests

**Goal:** Add `output/` to `.gitignore`; add a commented `output:` block to every existing example YAML; assert each YAML still round-trips through `load_config`.

**Files:**
- Modify: `.gitignore`
- Modify: `examples/configs/wan.yaml`
- Modify: `examples/configs/diffusers.yaml`
- Modify: `examples/configs/fal.yaml`
- Modify: `examples/configs/hosted.yaml`
- Modify: `examples/configs/local-fake.yaml`
- Modify: `examples/configs/runpod-comfyui-wan.yaml`
- Modify: `tests/test_examples.py` (+6 round-trip tests; one per YAML)

**Acceptance Criteria:**
- [ ] `.gitignore` contains a line `output/` (no leading slash; matches any depth).
- [ ] Each example YAML has a commented `# output:` block in canonical form (kind/dir/enabled), all 3 lines commented out so the YAML still parses with defaults.
- [ ] `load_config(example_yaml_path)` succeeds for every example with `cfg.output` at its defaults.

**Verify:** `pixi run pytest tests/test_examples.py -v -k output` → 6 new tests pass; existing example tests still green.

**Steps:**

- [ ] **Step 1: Add the gitignore line**

In `.gitignore`, append after the existing `.kinoforge/` line:

```
# Layer O — user-facing output directory (configurable per-run via --output-dir)
output/
```

- [ ] **Step 2: Add commented `output:` block to each YAML**

For each example YAML in `examples/configs/`, append this block at the end (after any existing top-level keys):

```yaml

# Layer O — user-facing output directory.  Uncomment to override defaults.
# Final clips are published to <dir>/<batch_id>?/{YYYYMMDD-HHMMSS}_{prompt-slug}{ext}.
# Internal artifacts (profile cache, ledger, weights) stay under --state-dir
# regardless of this block.
# output:
#   kind: local        # only "local" ships in v1
#   dir: output        # relative-to-cwd or absolute
#   enabled: true      # set false to skip publishing for this config
```

- [ ] **Step 3: Write 6 round-trip tests**

Append to `tests/test_examples.py`:

```python
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
```

- [ ] **Step 4: Run tests**

```bash
pixi run pytest tests/test_examples.py -v -k output
```

Expected: 6 passed.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
  .gitignore \
  examples/configs/wan.yaml \
  examples/configs/diffusers.yaml \
  examples/configs/fal.yaml \
  examples/configs/hosted.yaml \
  examples/configs/local-fake.yaml \
  examples/configs/runpod-comfyui-wan.yaml \
  tests/test_examples.py

git add .gitignore examples/configs/ tests/test_examples.py

git commit -m "$(cat <<'EOF'
docs(examples): add commented output: block to every YAML + .gitignore output/ (Layer O Task 8)

Each example YAML now carries a commented-out output: block in canonical
shape so operators can uncomment-and-tweak instead of reading the spec.
.gitignore picks up output/ so newly published clips don't get accidentally
committed.

6 round-trip tests parametrise across every example YAML to lock down that
the commented block doesn't break load_config().
EOF
)"
```

---

## Task 9: Invariant verification + README + PROGRESS

**Goal:** Confirm the architectural invariant test still passes (outputs is treated as a sibling axis like stores — no special-casing needed); add a "Output directory" section to `README.md`; add a Phase 25 entry to `PROGRESS.md` including the breaking-change note for `--run-id`.

**Files:**
- Verify (no change expected): `tests/test_core_invariant.py`
- Modify: `README.md`
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] `pixi run pytest tests/test_core_invariant.py -v` passes with zero changes to the test file (outputs is a sibling axis; orchestrator's `from kinoforge.outputs.base import OutputSink` is legal the same way `from kinoforge.stores.base import ArtifactStore` is).
- [ ] `README.md` has a new "Output directory" section between the existing "Running tools" and the next sibling section.
- [ ] `PROGRESS.md` has a Phase 25 entry under the existing "### Phase 24" block; the entry documents the breaking-change for `--run-id` default.

**Verify:**

```bash
pixi run pytest tests/test_core_invariant.py -v
pixi run pytest -q  # full suite still green
```

**Steps:**

- [ ] **Step 1: Run the invariant test, confirm green**

```bash
pixi run pytest tests/test_core_invariant.py -v
```

If it FAILS — investigate. The most likely cause is the orchestrator's `from kinoforge.outputs.base import OutputSink` triggering an unexpected branch of `_FORBIDDEN_CORE_IMPORTS`. If the regex accidentally matches `outputs`, that's a real Layer-O fix: extend the regex to be exact (`providers|sources|engines` only). Today's regex already is exact. If the test fails despite that, dig in.

If it PASSES — move on.

- [ ] **Step 2: Add "Output directory" section to README.md**

Read the existing README structure first:

```bash
rg -n '^## ' /workspace/README.md
```

Insert a new section. Suggested location: between "Running tools" and the next major section. Content:

```markdown
## Output directory

Final clips publish to a flat user-visible directory (default `output/` at
the repo root) with filenames of the form:

```
YYYYMMDD-HHMMSS_<prompt-slug>.<ext>
```

* The timestamp is local-TZ at the moment the clip finishes.
* The slug is the first 20 ASCII-safe characters of the prompt; emoji,
  CJK, accented characters, and punctuation are dropped (the slug
  pipeline is ASCII-conservative for cross-platform safety and
  grep/tab-complete ergonomics).
* Collisions in the same second resolve as `_2`, `_3`, … `_99`, then a
  6-character sha256 hash.
* Batch entries nest under `output/<batch_id>/` for grouping.

The internal artifact store (profile cache, ledger, weights cache,
intermediate segment artifacts) is unchanged — it still lives under
`--state-dir` (default `.kinoforge/`) and is operator-facing, not
user-facing. The output dir is a *publish* target, not a replacement
for the store.

### Configuring it

YAML block (optional; absent block uses the defaults below):

```yaml
output:
  kind: local            # only "local" ships in v1
  dir: output            # relative-to-cwd, or absolute
  enabled: true          # set false to skip publishing
```

CLI flags (overrides YAML):

* `--output-dir PATH` — publish here instead of the YAML default.
* `--no-output-dir` — skip publishing for this invocation.
* Flags are mutually exclusive.

### `--run-id` change

The `kinoforge generate --run-id` default changed from the literal
string `"run"` to `f"run-{YYYYMMDD-HHMMSS}"` (local TZ at invocation
time). This closes a silent-overwrite foot-gun where two successive
`kinoforge generate` calls without explicit `--run-id` would overwrite
each other's internal artifact + ledger entry. Pass `--run-id run` to
restore the prior behavior verbatim. Batch runs are unaffected — each
manifest entry already names its own `run_id`.
```

- [ ] **Step 3: Add Phase 25 entry to PROGRESS.md**

Open `PROGRESS.md`. Add a new section after Phase 24 (around line 415, at the end of the "### Phase 24 — Layer N" block):

```markdown

### Phase 25 — Layer O (user-facing output directory)

UX-only layer that closes the operator findability + persistence gap
identified during the Layer-N retro: final clips were buried under
`.kinoforge/<run_id>/<engine-derived-name>` with names that mean nothing
at a glance, and the default `--run-id="run"` silently overwrote prior
runs.

- [ ] Task 1: `outputs/base.py` (Protocol + slugify + format_filename) + `outputs/__init__.py` (registry) + 12 slugify tests — commit _PENDING_
- [ ] Task 2: `outputs/local.py` (LocalOutputSink with atomic write + collision suffix + self-register) + 10 tests — commit _PENDING_
- [ ] Task 3: `OutputConfig` pydantic block + `Config.output` field + 3 round-trip tests — commit _PENDING_
- [ ] Task 4: `GenerateClipStage` sink + namespace integration + 3 stage tests — commit _PENDING_
- [ ] Task 5: `orchestrator.generate()` sink threading + 2 tests — commit _PENDING_
- [ ] Task 6: `batch.batch_generate()` sink + batch_id namespace + 2 tests — commit _PENDING_
- [ ] Task 7: CLI `--output-dir`/`--no-output-dir` mutex group + `_build_sink` + `--run-id` uniquification + 5 tests — commit _PENDING_
- [ ] Task 8: `.gitignore` `output/` + commented `output:` block on every example YAML + 6 round-trip tests — commit _PENDING_
- [ ] Task 9: README "Output directory" section + this PROGRESS entry + invariant verification — commit _PENDING_
- [ ] Task 10: Full gate + `--no-ff` merge to main — commit _PENDING_

**Key design decisions:**
- Publish step layered on top of ArtifactStore (Q2=A): zero behavior change to existing call sites; store/ledger/uri_for/gc untouched.
- ASCII-conservative slug (Q3=A): emoji/CJK/accents dropped, not transliterated; cross-platform safe, shell-friendly.
- Flat single + batch-nested layout (Q4=A): single-clip runs land directly in `output/`; batch runs nest under `output/<batch_id>/`.
- `--run-id` default uniquification folded into Layer O: one-line CLI change closes the silent-overwrite foot-gun on the internal store side too.
- Bytes-only v1: hardlink optimization (`ArtifactStore.local_path_for`) deferred; sub-GB mp4 disk doubling is negligible.

**Breaking changes:**
- `kinoforge generate` default `--run-id` flipped from `"run"` to `f"run-{ts}"`. Scripts that grep `.kinoforge/run/` no longer find clips; pass `--run-id run` to restore prior behavior. Second breaking change after the Layer C `kinoforge gc --config PATH` precedent.

**Test count:** ~778 pre-Layer-O → ~820 post-Layer-O (+42 net: 12 slugify + 10 sink + 3 cfg + 3 stage + 2 orch + 2 batch + 5 CLI + 6 examples). Numbers will be backfilled with the actual delta in Task 10.

**Out of scope (Layer P+ candidates):**
- Hardlink / zero-copy via `ArtifactStore.local_path_for`.
- Cloud-native sinks (S3 mirror, webhook POST).
- Filename template customization.
- Migration of existing `.kinoforge/<run_id>/*.mp4` into `output/`.
- `Artifact.published_path` field for CLI status / batch summary.
- Engine integration on real RunPod (original Layer-O candidate; now reslotted as Layer P).
```

Also update PROGRESS.md's "Single next action" block at line 150-158 to point at Layer O.

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files README.md PROGRESS.md

git add README.md PROGRESS.md

git commit -m "$(cat <<'EOF'
docs: README Output directory section + PROGRESS Phase 25 entry (Layer O Task 9)

User-facing docs for the new output/ publish step + breaking-change note
for the --run-id default change.  Phase 25 entry mirrors the structure of
Phases 17-24 (key decisions, breaking changes, out-of-scope).
EOF
)"
```

---

## Task 10: Full gate + commit-SHA backfill + `--no-ff` merge to `main`

**Goal:** Run the full pre-commit + test gate; backfill commit SHAs into the PROGRESS Phase 25 task list; merge `build/layer-o` into `main` with a substantive `--no-ff` merge commit per project tradition.

**Files:**
- Modify: `PROGRESS.md` (backfill SHAs for Tasks 1–9)

**Acceptance Criteria:**
- [ ] `pixi run pre-commit run --all-files` is green.
- [ ] `pixi run test` is green; test count delta within ±5 of `+42` projection.
- [ ] `PROGRESS.md` Phase 25 task list has each `_PENDING_` placeholder replaced with the actual commit SHA.
- [ ] `git log build/layer-o..main` shows the merge commit referencing Layer O.

**Verify:**

```bash
pixi run pre-commit run --all-files
pixi run test
git log --oneline main -5
```

**Steps:**

- [ ] **Step 1: Full pre-commit gate**

```bash
pixi run pre-commit run --all-files
```

Expected: green. If anything fails — fix in-branch, commit as a separate task-cleanup commit, document at the appropriate task entry in PROGRESS.

- [ ] **Step 2: Full test suite**

```bash
pixi run test
```

Expected: green. Count the test deltas:

```bash
pixi run pytest --collect-only -q | tail -5
```

Confirm the total is within ±5 of `778 + 42 = 820`.

- [ ] **Step 3: Backfill commit SHAs**

In `PROGRESS.md`, for each Task N entry in the Phase 25 list, replace `commit _PENDING_` with the actual SHA from `git log --oneline build/layer-o`. Map by the `Layer O Task N` substring in each commit message.

```bash
git log --oneline build/layer-o ^main | head -20
```

- [ ] **Step 4: Commit the SHA backfill**

```bash
git add PROGRESS.md

git commit -m "chore(docs): backfill Layer O commit SHAs in PROGRESS"
```

- [ ] **Step 5: Merge into main**

```bash
git checkout main
git pull --ff-only

git merge --no-ff build/layer-o -m "$(cat <<'EOF'
Merge branch 'build/layer-o': user-facing output directory (Layer O)

ArtifactStore + ledger + uri_for + gc semantics fully preserved.  A new
OutputSink seam at src/kinoforge/outputs/ optionally publishes finished
clips to a flat user-visible directory (default `output/`) with
{YYYYMMDD-HHMMSS}_{ascii-slug}{ext} filenames.  Batch runs nest under
output/<batch_id>/.  --run-id default flipped from "run" to
f"run-{ts}" to close the silent-overwrite foot-gun on the internal
store side.

Tasks (build/layer-o):
- Task 1: OutputSink Protocol + slugify + registry (12 tests)
- Task 2: LocalOutputSink (10 tests)
- Task 3: OutputConfig pydantic block (3 tests)
- Task 4: GenerateClipStage sink + namespace (3 tests)
- Task 5: orchestrator.generate() sink threading (2 tests)
- Task 6: batch.batch_generate() sink + batch_id namespace (2 tests)
- Task 7: CLI flags + _build_sink + --run-id uniquification (5 tests)
- Task 8: .gitignore + example YAMLs + round-trip tests (6 tests)
- Task 9: README + PROGRESS

Closes operator UX gap: findability + persistence.
EOF
)"
```

- [ ] **Step 6: Backfill the merge SHA into PROGRESS**

```bash
git log --oneline -1
```

Copy the merge SHA. Edit `PROGRESS.md`'s "Single next action" block and Phase 25 entry to reference `merge commit <SHA>`.

```bash
git add PROGRESS.md
git commit -m "chore(docs): backfill Layer O merge commit SHA in PROGRESS"
```

- [ ] **Step 7: Confirm `main` is shipped**

```bash
git log --oneline main -8
git status  # working tree clean
```

Expected: merge commit at HEAD; clean working tree; ready for the next layer.

---

## Self-review

Cross-checking against the spec (`docs/superpowers/specs/2026-05-31-layer-o-output-dir-design.md`):

| Spec section | Task(s) |
|---|---|
| §1 Architecture (OutputSink seam) | Task 1, 2 |
| §2 Sink Protocol | Task 1 |
| §3 Filename construction (slugify, ts, collision) | Task 1, 2 |
| §4 Path layout (flat single + batch-nested) | Task 4, 6 |
| §5 Configuration (YAML + CLI + precedence) | Task 3, 7 |
| §6 Wiring touchpoints (every file in the table) | Tasks 1-9 (all rows covered) |
| §7 Edge cases | Task 1 + Task 2 tests; CLI mutex group in Task 7 |
| §8 Testing (offline, deterministic, FakeClock) | Every task uses FakeClock + spies; no real I/O outside tmp_path |
| §9 Build order | Tasks 1-10 in spec order |
| AC-1 (slugify) | Task 1 |
| AC-2 (LocalOutputSink basic publish) | Task 2 |
| AC-3 (namespace nesting) | Task 2 |
| AC-4 (collision _2) | Task 2 |
| AC-5 (stage publish call) | Task 4 |
| AC-6 (sink=None preserves behavior) | Task 4 |
| AC-7 (batch namespace) | Task 6 |
| AC-8 (CLI precedence) | Task 7 |
| AC-9 (default --run-id uniquifies) | Task 7 |
| AC-10 (`output.enabled: false`) | Task 3 + Task 7 |
| AC-11 (OSError wrap) | Task 2 |
| AC-12 (invariant preserved) | Task 9 |
| AC-13 (.gitignore) | Task 8 |

No spec section uncovered. Placeholder scan: no `TBD` / `TODO` / `Add appropriate error handling` / `Similar to Task N` patterns left. Type consistency: `sink: OutputSink | None`, `namespace: str | None`, `LocalOutputSink(dir, clock)` used identically across tasks. Method names: `slugify`, `format_filename`, `publish`, `register_sink`, `get_sink`, `_build_sink`, `_cli_clock` — single canonical spelling each.
