# Layer L-T4 — batch streaming logs

Closes the streaming-per-entry-log-lines follow-up deferred at commit
`38d5394` ("note streaming-log deferral as Layer L follow-up"). The
`kinoforge batch` CLI today prints a manifest header at start and a
summary table at end, with no mid-run output. Operators watching a 20-
entry batch cannot tell whether the third entry is mid-flight or stuck.
This layer adds a streaming-event seam in `core/batch.py` and two CLI
formatters that consume it.

The work is fully offline-tested. No live cloud spend.

---

## 1. Goals + scope

**In scope:**

- Per-entry streaming events emitted from `batch_generate()` via a
  callback hook (`on_event: Callable[[BatchEvent], None] | None`).
- Two CLI formatters consuming the hook: human-readable (default) and
  JSONL (machine-readable, pipe-friendly).
- A third `none` formatter that preserves pre-Layer-L-T4 behaviour
  (operator opts out of mid-run lines, keeps the final summary table).
- A new core module `core/batch_events.py` for the event dataclass and
  the lock-protected emitter.
- A model-extract refactor — `BatchEntry`, `BatchManifest`,
  `BatchOutcome`, `BatchResult` move from `core/batch.py` to a new
  `core/batch_models.py` to dodge an import cycle. No semantic change.

**Out of scope:**

- Per-entry progress ticks inside `stage.run` (heartbeats from
  long-running engines). The two-event vocabulary (`entry_start`,
  `entry_finish`) is intentionally minimal; progress ticks would
  require hooking into stage internals and grow the contract.
- Batch-level events (`batch_start`, `batch_finish`). The CLI already
  prints a header and renders a summary; library users can wrap
  `batch_generate` themselves if they want batch-level signals.
- TUI progress bars, Slack webhooks, Prometheus metrics. All become
  trivial once the seam exists; ship them in later layers if demand
  materializes.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Callback hook in `core/`; CLI consumes it. | Preserves `core` print-free invariant (`tests/test_core_invariant.py`). Matches existing seam pattern (PROGRESS:87). Future consumers (Slack, Prometheus, TUI) cost nothing extra. |
| D2 | Bundle both formatters in this layer. | Operator value on day one. Formatters are ~50 LOC each; no rationale to defer. |
| D3 | Minimal event vocabulary: `entry_start` + `entry_finish`. | Smallest contract surface. `entry_finish` carries `status` enum (`ok`/`fail`/`interrupted`/`aborted`), which discriminates without inflating event kinds. |
| D4 | Internal `threading.Lock` serializes the user callback. | Callback author does not have to think about thread-safety. Matches `logging.Handler` pattern. Multi-line human strings never interleave on stdout. |
| D5 | Lean+entry event payload. | `BatchEvent` carries the universal fields (`kind`, `batch_id`, `idx`, `run_id`, `ts`) plus `entry: BatchEntry` on `entry_start` and `status`/`duration_s`/`uri`/`error` on `entry_finish`. Formatters see prompt/mode without closing over the manifest. |
| D6 | Build-time fail emits both events back-to-back. | Preserves the invariant `start_count == finish_count == len(entries)`. Consumers do not need a special branch for "finish without start". |
| D7 | Single CLI flag: `--stream-format={human,jsonl,none}`, default `human`. | Visible behaviour change ships the layer to existing users. JSONL replaces the summary table with one JSONL line (`kind="batch_summary"`). |
| D8 | Stream + summary → stdout, errors → stderr. | Matches the existing `_cmd_batch` convention. JSONL is pipeable (`kinoforge batch ... \| jq .`). |
| D9 | Extract `BatchEntry`/`BatchManifest`/`BatchOutcome`/`BatchResult` to `core/batch_models.py`. | Breaks the import cycle between `batch.py` and `batch_events.py`. ~150 LOC moved, no semantic change. |
| D10 | Timestamps use `datetime.now()` (local TZ). | Project rule (`feedback_local_timezone_only` in user memory). |

---

## 3. Architecture

### 3.1 Module map

```
src/kinoforge/
  core/
    batch.py                      MODIFIED  ~-150 / +60 LOC net
    batch_models.py               NEW       extracted from batch.py
    batch_events.py               NEW       BatchEvent + _LockedEmitter
  cli/
    _commands.py                  MODIFIED  ~-15 LOC (summary moves out)
    _main.py                      MODIFIED  +6 LOC (--stream-format)
    batch_formatters.py           NEW       Human/Jsonl/NoOp formatters
tests/
  core/
    test_batch_events.py          NEW       ~8 tests
    test_batch.py                 MODIFIED  +5 ACs (on_event behaviour)
  cli/
    test_batch.py                 MODIFIED  +4 ACs (--stream-format)
```

### 3.2 Invariants preserved

- **Core print-free.** `core/batch.py` never calls `print` and never
  writes to `sys.stdout` / `sys.stderr`. Output happens only through
  the user-supplied `on_event` callback. The CLI is the only printer.
  Enforced today by convention; this layer continues to honour it.
- **Core-import-ban.** `tests/test_core_invariant.py` scans `core/` for
  imports of `kinoforge.{providers,sources,engines}`. New `core/`
  files contain zero such imports. No allowlist change needed.
- **Backward compatibility.** `batch_generate(... on_event=None)` is
  byte-identical to today's behaviour. All 1297 existing tests pass
  with only the import-line refactor caused by D9 (model extract).

### 3.3 Data contract

```python
# src/kinoforge/core/batch_events.py
from datetime import datetime
from typing import Callable, Literal
from pydantic import BaseModel, ConfigDict

from kinoforge.core.batch_models import BatchEntry

EventKind = Literal["entry_start", "entry_finish"]
EntryStatus = Literal["ok", "fail", "interrupted", "aborted"]


class BatchEvent(BaseModel):
    """Streaming event emitted by batch_generate via on_event."""

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    batch_id: str
    idx: int               # 0-based index into manifest.entries
    run_id: str            # entry.run_id or str(idx)
    ts: datetime           # LOCAL timezone

    # entry_start only
    entry: BatchEntry | None = None

    # entry_finish only
    status: EntryStatus | None = None
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None


BatchEventCallback = Callable[[BatchEvent], None]
```

**Field nullability rules** (asserted in `test_batch_events.py`):

| Field | `entry_start` | `entry_finish` |
|---|---|---|
| `kind`/`batch_id`/`idx`/`run_id`/`ts` | always set | always set |
| `entry` | set | `None` |
| `status` | `None` | always set |
| `duration_s` | `None` | always set |
| `uri` | `None` | set iff `status=="ok"` |
| `error` | `None` | set iff `status` in `{fail,interrupted,aborted}` |

### 3.4 Lock-protected emitter

```python
class _LockedEmitter:
    """Serializes user callback under a Lock; no-op when callback is None."""

    def __init__(self, on_event: BatchEventCallback | None) -> None:
        self._cb = on_event
        self._lock = threading.Lock()
        self._started_idxs: set[int] = set()

    def __call__(self, event: BatchEvent) -> None:
        with self._lock:
            if event.kind == "entry_start":
                self._started_idxs.add(event.idx)
            if self._cb is not None:
                self._cb(event)

    def has_started(self, idx: int) -> bool:
        with self._lock:
            return idx in self._started_idxs
```

The emitter doubles as the `started_idxs` book-keeper so
`_mark_remaining_after_fatal` can distinguish "in flight (already
started, emit `entry_finish(interrupted)`)" from "never started (emit
`entry_start` + `entry_finish(aborted)` back-to-back)".

When `on_event=None`, the lock acquire still runs for `entry_start`
events to keep `_started_idxs` accurate, but the user callback is
skipped — overhead is one uncontended lock acquire per entry, which is
negligible compared to the network round-trips inside `stage.run`.

---

## 4. Emission sites in `batch_generate`

Five sites emit events. Lock-protected via the emitter from §3.4.

### 4.1 Build-time fail (main thread)

`core/batch.py` line 729 today catches `Exception` from
`_build_stage_for_entry` and records a `BatchOutcome(status="fail")`
without touching the executor. New flow emits start+finish:

```python
except Exception as build_exc:
    start_times[idx] = monotonic()
    emit(BatchEvent(
        kind="entry_start", batch_id=batch_id, idx=idx,
        run_id=entry.run_id or str(idx), ts=datetime.now(),
        entry=entry,
    ))
    outcomes_by_idx[idx] = BatchOutcome(
        run_id=entry.run_id or str(idx),
        status="fail",
        duration_s=0.0,
        error=f"{type(build_exc).__name__}: {build_exc}",
    )
    emit(BatchEvent(
        kind="entry_finish", batch_id=batch_id, idx=idx,
        run_id=entry.run_id or str(idx), ts=datetime.now(),
        status="fail", duration_s=0.0,
        error=f"{type(build_exc).__name__}: {build_exc}",
    ))
    continue
```

### 4.2 Per-entry start (worker thread)

`_run_with_clock` gains the emitter + entry + batch_id args and emits
`entry_start` immediately after `start_times[idx] = monotonic()`:

```python
def _run_with_clock(stage, initial_state, start_times, idx, *,
                    emit, entry, batch_id):
    start_times[idx] = monotonic()
    emit(BatchEvent(
        kind="entry_start", batch_id=batch_id, idx=idx,
        run_id=entry.run_id or str(idx), ts=datetime.now(),
        entry=entry,
    ))
    return stage.run(initial_state)
```

### 4.3 Per-entry ok (main thread)

`core/batch.py` line 781 records `BatchOutcome(status="ok")`. Emit
`entry_finish` immediately after:

```python
else:
    outcomes_by_idx[idx] = BatchOutcome(
        run_id=entry.run_id or str(idx),
        status="ok",
        duration_s=duration,
        uri=artifact.uri,
    )
    emit(BatchEvent(
        kind="entry_finish", batch_id=batch_id, idx=idx,
        run_id=entry.run_id or str(idx), ts=datetime.now(),
        status="ok", duration_s=duration, uri=artifact.uri,
    ))
```

### 4.4 Per-entry fail (main thread)

Same shape at `core/batch.py` line 774 — emit `entry_finish` with
`status="fail"`, `duration_s`, `error`.

### 4.5 Batch-fatal (main thread)

The directly-raising entry emits `entry_finish(status="interrupted")`
before `raise`. Then `_mark_remaining_after_fatal` walks every other
entry:

- If the entry already emitted `entry_start` (visible via
  `emit.has_started(idx)`), emit `entry_finish(status="interrupted",
  duration_s=monotonic()-start_times[idx], error="batch aborted by
  <FatalType>")`.
- Otherwise, emit `entry_start` (with the resolved `BatchEntry`) +
  `entry_finish(status="aborted", duration_s=0.0, error="batch aborted
  by <FatalType>")` back-to-back.

### 4.6 Ordering invariants

For any `idx`:

1. `entry_start(idx)` precedes `entry_finish(idx)` in callback call
   order (enforced by the lock + sequential emission within each
   path).
2. `start_count == finish_count == len(manifest.entries)` at batch
   end, on every exit path (clean / per-entry fail / build-time fail /
   batch-fatal).

---

## 5. CLI consumers

### 5.1 `cli/batch_formatters.py`

Three formatters consuming `on_event`:

```python
class HumanFormatter:
    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        prefix = f"[{event.batch_id}] [{event.idx + 1}/{event.run_id}]"
        if event.kind == "entry_start":
            mode = event.entry.mode if event.entry else "?"
            prompt = (event.entry.prompt or "")[:60] if event.entry else ""
            self._stream.write(
                f"{prefix} START mode={mode} prompt={prompt!r}\n"
            )
        else:  # entry_finish
            status = (event.status or "?").upper()
            dur = f"{event.duration_s:.1f}s" if event.duration_s is not None else "—"
            tail = event.uri or event.error or ""
            self._stream.write(f"{prefix} {status} {dur} {tail}\n")
        self._stream.flush()

    def render_summary(self, result: BatchResult) -> None:
        """Final summary table (moved verbatim from _cmd_batch).

        Auto-sizes the run_id column to the widest entry + 1; status
        column is fixed 12-wide (max label "interrupted" is 11). Prints
        the `batch-id:` and `results:` footer lines.
        """
        rid_width = max((len(o.run_id) for o in result.outcomes), default=1) + 1
        self._stream.write("\nsummary:\n")
        for o in result.outcomes:
            status_label = o.status.upper()
            duration = f"{o.duration_s:.1f}s" if o.duration_s is not None else "—"
            detail = o.uri if o.uri else (o.error or "")
            self._stream.write(
                f"  {o.run_id:<{rid_width}s} {status_label:<12s} "
                f"{duration:<8s} {detail}\n"
            )
        self._stream.write(f"batch-id: {result.batch_id}\n")
        n_ok = sum(1 for o in result.outcomes if o.status == "ok")
        n_fail = len(result.outcomes) - n_ok
        self._stream.write(
            f"results:  {n_ok}/{len(result.outcomes)} ok, {n_fail} failed\n"
        )
        self._stream.flush()


class JsonlFormatter:
    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()

    def render_summary(self, result: BatchResult) -> None:
        self._stream.write(
            json.dumps({"kind": "batch_summary", **result.to_dict()}) + "\n"
        )
        self._stream.flush()


class NoOpFormatter:
    """--stream-format=none: skip mid-run lines, keep final summary."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        pass

    def render_summary(self, result: BatchResult) -> None:
        HumanFormatter(self._stream).render_summary(result)


def build_formatter(kind: str, stream: TextIO = sys.stdout):
    return {
        "human": HumanFormatter,
        "jsonl": JsonlFormatter,
        "none": NoOpFormatter,
    }[kind](stream)
```

### 5.2 `_cmd_batch` integration

```python
formatter = build_formatter(args.stream_format)
# Header print (current line 344) stays — manifest-load context is
# operator-friendly regardless of format. Acceptable in human + none
# modes; in jsonl mode it goes to stderr to keep stdout pure JSONL.
if args.stream_format == "jsonl":
    print(f"[{batch_id}] manifest loaded: ...", file=sys.stderr)
else:
    print(f"[{batch_id}] manifest loaded: ...")

try:
    result = batch_generate(
        cfg, manifest, store=store, sink=sink,
        batch_id=batch_id, concurrent=args.concurrent,
        state_dir=ctx.state_dir,
        on_event=formatter.emit,
    )
except (BudgetExceeded, CapabilityMismatch, TeardownError) as exc:
    print(f"[{batch_id}] batch-fatal: {type(exc).__name__}: {exc}",
          file=sys.stderr)
    return 2
except KinoforgeError as exc:
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1

formatter.render_summary(result)
n_ok = sum(1 for o in result.outcomes if o.status == "ok")
return 0 if n_ok == len(result.outcomes) else 1
```

### 5.3 Argparse change

```python
batch_parser.add_argument(
    "--stream-format",
    choices=("human", "jsonl", "none"),
    default="human",
    help="Streaming output format (default: human).",
)
```

---

## 6. Tests

### 6.1 `tests/core/test_batch_events.py` (new)

- `test_event_frozen` — mutation raises pydantic `ValidationError`.
- `test_event_json_roundtrip` — `model_dump_json()` →
  `model_validate_json()` returns identity-equal object.
- `test_locked_emitter_serializes` — 4 worker threads on a
  `threading.Barrier`; each fires one event; recorded
  `(thread_id, t_enter, t_exit)` triples never overlap.
- `test_locked_emitter_none_callback_noop` — emitter with
  `on_event=None` silently accepts all events; does not raise.
- `test_started_idxs_tracking` — every `entry_start` adds to
  `_started_idxs`; `has_started` reflects state.
- `test_field_nullability_rules` — assert the §3.3 table by
  construction (start with `status` set raises; finish with `entry`
  set raises). Validators live on the model.

### 6.2 `tests/core/test_batch.py` (extended)

- **AC1** `batch_generate(... on_event=None)` byte-identical
  side-effects to today's behaviour. Use an existing happy-path test
  as the regression fixture.
- **AC2** invariant `start_count == finish_count ==
  len(manifest.entries)` across 4 exit paths: clean, per-entry fail,
  build-time fail, batch-fatal. Recording callback collects every
  emitted event; assertions count events by kind.
- **AC3** `entry_start(idx)` precedes `entry_finish(idx)` for every
  idx across all 4 exit paths.
- **AC4** lock-stress: 4 entries, fake backend that sleeps random
  jitter, recording callback that does a `time.sleep(0.01)` inside —
  recorded enter/exit triples never overlap.
- **AC5** build-fail path emits both events with `duration_s=0.0` and
  `error` populated.
- **AC6** batch-fatal path: directly-raising entry emits
  `entry_finish(interrupted)`; never-started entries emit start +
  `entry_finish(aborted)`; already-in-flight entries emit
  `entry_finish(interrupted)` (their start was emitted by their
  worker).

### 6.3 `tests/cli/test_batch.py` (extended)

- **AC7** `--stream-format=human` (default): for a 3-entry happy-path
  fixture, stdout contains 3 `START` lines + 3 `OK` lines + the
  summary table.
- **AC8** `--stream-format=jsonl`: every stdout line parses as one
  JSON object; the last line has `"kind": "batch_summary"`; the human
  summary table is absent.
- **AC9** `--stream-format=none`: stdout matches the pre-Layer-L-T4
  fixture (header + summary, no `START` lines).
- **AC10** `--stream-format=xyz`: argparse exit code 2 (free from
  `choices=`).
- The existing 6 `_cmd_batch` tests are substring-asserted today; they
  pass under the new `human` default without modification.
  Confirmation step happens during plan-phase, not via blanket update.

### 6.4 Invariant scan

`tests/test_core_invariant.py` continues to pass; the new core files
(`core/batch_events.py`, `core/batch_models.py`) carry no
provider/source/engine imports.

**Projected test count:** 1297 → ~1318 (+8 in `test_batch_events.py`,
+5 in `test_batch.py`, +4 in `tests/cli/test_batch.py`, +1 invariant
regression cover, -0 deletions).

---

## 7. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Existing 6 `cli/test_batch.py` tests break under the new `human` default. | Tests are substring asserts, not full-stdout equality. Spot-check during plan-phase; if any test does assert full output, switch it to `--stream-format=none` for that single test rather than blanket-updating all six. |
| `_started_idxs` mutation race between worker threads and main thread. | All mutations + reads go through `_LockedEmitter._lock`. AC4 stress test covers it. |
| Multi-line human-format strings interleave on stdout. | The lock-protected emitter holds the lock around the entire `self._cb(event)` call; the formatter writes one line + flushes inside the lock. No interleaving possible. |
| Pre-existing race on `start_times` dict (workers mutate, main reads). | Documented as out-of-scope. CPython dict atomicity covers it in practice; the `.get(idx, batch_start)` fallback masks any miss. Hardening is a future layer. |
| Behaviour change (operator-visible mid-run lines on default `kinoforge batch` invocation). | README "Batch jobs" section gains a `Streaming output` subheading documenting the new flag. PROGRESS.md Phase 35 entry notes the visible change. Operators who want pre-Layer behaviour use `--stream-format=none`. |
| Model extract (D9) breaks existing imports. | `core/batch.py` re-exports the four names from `core/batch_models.py` at module top (`from .batch_models import BatchEntry, BatchManifest, BatchOutcome, BatchResult`) so existing call sites (`from kinoforge.core.batch import BatchManifest`) keep working. Plan-phase audits the import graph before splitting. |

---

## 8. Out-of-scope / future layers

- **Per-entry progress ticks** (heartbeats from long-running stages).
  Requires hooking into `stage.run` internals. Bigger scope; later
  layer if a TUI progress bar materializes.
- **Batch-level events** (`batch_start`, `batch_finish`). Library
  users can wrap `batch_generate` themselves; CLI already prints
  header + summary.
- **Slack / Prometheus / structured-log-aggregator formatters.**
  Trivial to add now that the seam exists. Bring per demand.
- **Multi-batch streaming** (multiple `batch_generate` invocations
  multiplexed onto one stream). Out of scope; a single batch is the
  unit today.
- **`start_times` race hardening.** Pre-existing; documented in §7.

---

## 9. Acceptance summary

See §6 for the AC numbered list (AC1–AC12). The layer is "done" when
all 12 ACs pass, the existing 1297 tests still pass, `pixi run lint
test typecheck` is clean, and the PROGRESS.md Phase 35 entry is
committed.

Live spend: $0. Fully offline-tested via the existing
`FakeProvider`/`FakeEngine`/`FakeBackend` fixtures.
