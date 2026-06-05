# Layer S — `kinoforge status` reads the ledger

**Status:** Approved (brainstorm validated 2026-06-05).
**Closes:** PROGRESS:120 — "`cli._cmd_status` queries in-process provider state only, not the ledger."
**Scope guard:** Does NOT close PROGRESS:127 (cloud-ledger CLI routing); that stays a separate layer.

## 1. Decisions locked in brainstorm

| # | Decision | Choice |
|---|---|---|
| Q1 | Scope of fix | A+B: ledger-first dispatch *and* richer ledger-derived output |
| Q2 | Provider-failure exit codes | B: `KeyError` ⇒ exit 0 (stale); network/SDK ⇒ exit 2 (transient) |
| Q3 | Output format | A: multi-line `key=value`, alphabetised, one block per id |
| Q4 | Field set | `id`, `provider`, `provider_status`, `created_at`, `age_h`, `cost_rate_usd_per_hr`, `accrued_spend_usd`, `idle_timeout_s`, `max_age_s`, `last_heartbeat` (if present), `endpoints` (on provider success) |
| Q5 | `idle_timeout_s` / `max_age_s` source | A+C: extend `Ledger.record()` to persist them; `--config` optional on `status` for legacy-entry fallback |
| Q6 | Migration of legacy ledger entries | A: soft display (`<not in ledger>` or cfg fallback); no migration code |
| Q7 | Provider construction | A: sibling parity — `registry.get_provider(name)()` |
| Q8 | Cross-process cloud-ledger | A: out of scope; PROGRESS:127 stays open |
| Q9 | Stale-entry recovery for operators | B: status prints advisory + ship `kinoforge forget <id>` subcommand |
| Q10 | Testing posture | Offline. Synthetic ledger fixtures + `FakeProvider` + `FakeClock` + `capsys`. |

Task slicing: **3 tasks** (T1 ledger schema, T2 `_cmd_status` rewrite + `--config` plumbing, T3 `forget` subcommand). One `--no-ff` merge at the end.

## 2. Motivation

`_cmd_status` (cli.py:550-579) ignores the ledger. It always instantiates `LocalProvider()` and asks `get_instance(args.id)`. Every non-local instance (RunPod, SkyPilot, anything wired via Layer N or Layer Q) yields `instance '<id>' not found` even when the ledger has the entry — the operator's only introspection command silently lies about cloud state.

Sibling commands `_cmd_stop` (cli.py:582), `_cmd_destroy` (cli.py:612), `_cmd_reap`, and `_cmd_gc` already do ledger-first dispatch: look up the entry, read the `provider` field, dispatch via `registry.get_provider(name)()`. Status is the odd one out.

Beyond fixing the dispatch bug, the ledger holds operationally useful facts that the status command never surfaces today: instance age, accrued spend estimate, configured idle / max-age limits. Surfacing them turns `status` into the operator's go-to introspection tool — and when the provider lookup fails, the ledger view still tells the operator what they need to know.

## 3. Architecture

Three units, single layer, fully offline-testable.

### 3.1 Unit 1 — `Ledger.record()` schema extension (`src/kinoforge/core/lifecycle.py`)

New signature:

```python
def record(
    self,
    instance: Instance,
    *,
    idle_timeout_s: int | None = None,
    max_age_s: int | None = None,
) -> None:
    """Persist instance with optional lifecycle policy snapshot.

    Keys are persisted only when non-None. Legacy entries written before
    this layer are read back unchanged (`Ledger.entries()` does not patch
    them; consumers degrade per Section 3.2).
    """
```

Persisted JSON entry shape (additive — every existing key unchanged):

```json
{
  "id": "ia66l3rlto5x66",
  "provider": "runpod",
  "status": "ready",
  "created_at": 1717635791.0,
  "cost_rate_usd_per_hr": 0.35,
  "idle_timeout_s": 900,
  "max_age_s": 14400,
  "last_heartbeat": 1717636791.0,
  "endpoints": {"http": "https://…"},
  "metadata": {}
}
```

Updated call site (one):

```python
# src/kinoforge/core/lifecycle.py, LifecycleManager.warm_reuse_or_create
self._ledger.record(
    instance,
    idle_timeout_s=int(self._idle_timeout_s) if self._idle_timeout_s else None,
    max_age_s=int(self._max_age_s) if self._max_age_s else None,
)
```

Test fixtures and `BudgetTracker` tests that construct ledgers directly do not need the new kwargs (defaults preserve behaviour).

### 3.2 Unit 2 — `_cmd_status` ledger-first rewrite (`src/kinoforge/cli.py`)

New control flow:

```
entry = next((e for e in ledger.entries() if e.get("id") == args.id), None)
if entry is None:
    print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
    return 1

cfg = load_config(args.config) if getattr(args, "config", None) else None
ledger_block = _build_ledger_block(entry, cfg=cfg, now=time.time())

provider_name = str(entry.get("provider", "local"))
try:
    provider = registry.get_provider(provider_name)()
except UnknownAdapter as exc:
    provider_block = {"provider_status": f"unknown (unknown provider: {provider_name})"}
    _print_block(ledger_block, provider_block)
    return 2

try:
    instance = provider.get_instance(args.id)
except KeyError:
    provider_block = {"provider_status": "unknown (stale ledger — provider has no record)"}
    _print_block(ledger_block, provider_block,
                 advisory=f"advisory: ledger entry is stale — run 'kinoforge forget {args.id}'")
    return 0
except Exception as exc:  # noqa: BLE001 — explicit transient-error surface
    provider_block = {"provider_status": f"unknown (provider lookup failed: {exc.__class__.__name__})"}
    _print_block(ledger_block, provider_block)
    return 2

provider_block = {"provider_status": instance.status}
try:
    provider_block["endpoints"] = json.dumps(provider.endpoints(args.id))
except Exception as exc:  # noqa: BLE001
    provider_block["endpoints"] = f"unknown ({exc.__class__.__name__})"
_print_block(ledger_block, provider_block)
return 0
```

`_print_block(ledger_block, provider_block, *, advisory=None)` merges the two dicts, prints `key=value` lines in alphabetical order (one per stdout line), then — if `advisory` is set — prints the advisory line *after* the sorted block on stdout.

#### `_build_ledger_block` pure helper

```python
def _build_ledger_block(
    entry: dict,
    *,
    cfg: Config | None,
    now: float,
) -> dict[str, str]:
    """Return the ledger-derived portion of `kinoforge status` output.

    Pure: no I/O, no clock reads. All time inputs flow through `now`.

    Fallback order for `idle_timeout_s` / `max_age_s`:
      1. entry[key] when present and non-None
      2. cfg.lifecycle().<field> when cfg is supplied
      3. literal "<not in ledger>"
    """
    out: dict[str, str] = {}
    out["id"] = str(entry.get("id", "?"))
    out["provider"] = str(entry.get("provider", "?"))
    created_at_raw = float(entry.get("created_at", now))
    age_h = max(0.0, (now - created_at_raw) / 3600.0)
    out["created_at"] = (
        datetime.fromtimestamp(created_at_raw).astimezone().isoformat(timespec="seconds")
    )
    out["age_h"] = f"{age_h:.1f}"
    rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
    out["cost_rate_usd_per_hr"] = f"{rate:.4f}"
    out["accrued_spend_usd"] = f"{age_h * rate:.4f}"
    out["idle_timeout_s"] = _ledger_field_or_cfg(entry, "idle_timeout_s", cfg)
    out["max_age_s"] = _ledger_field_or_cfg(entry, "max_age_s", cfg)
    if "last_heartbeat" in entry and entry["last_heartbeat"] is not None:
        out["last_heartbeat"] = (
            datetime.fromtimestamp(float(entry["last_heartbeat"]))
            .astimezone()
            .isoformat(timespec="seconds")
        )
    return out


def _ledger_field_or_cfg(entry: dict, key: str, cfg: Config | None) -> str:
    value = entry.get(key)
    if value is not None:
        return str(value)
    if cfg is not None:
        lc = cfg.lifecycle()
        return str(getattr(lc, key))
    return "<not in ledger>"
```

#### Parser change

`status` subparser gains `--config`/`-c` (optional, `type=Path`):

```python
p_status = sub.add_parser("status", help="…")
p_status.add_argument("id")
p_status.add_argument("--config", "-c", type=Path, default=None,
                      help="optional; fallback for missing ledger fields")
```

### 3.3 Unit 3 — `kinoforge forget <id>` (`src/kinoforge/cli.py`)

New subcommand:

```python
def _cmd_forget(args: argparse.Namespace, state_dir: Path) -> int:
    ledger = _ledger(state_dir)
    if not any(e.get("id") == args.id for e in ledger.entries()):
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1
    ledger.forget(args.id)
    print(f"forgot: {args.id}")
    return 0
```

Wired into `_build_parser`:

```python
p_forget = sub.add_parser("forget", help="remove instance from local ledger")
p_forget.add_argument("id")
```

Wired into `main` dispatch alongside `gc`:

```python
elif cmd == "forget":
    return _cmd_forget(args, state_dir)
```

## 4. Output format

Alphabetised `key=value`, one per line; advisory (if any) printed last. Example success path:

```
accrued_spend_usd=0.8400
age_h=2.4
cost_rate_usd_per_hr=0.3500
created_at=2026-06-05T14:23:11-07:00
endpoints={"http": "https://abc.proxy.runpod.net"}
id=ia66l3rlto5x66
idle_timeout_s=900
max_age_s=14400
provider=runpod
provider_status=ready
```

Example stale-ledger path (exit 0):

```
accrued_spend_usd=0.8400
age_h=2.4
cost_rate_usd_per_hr=0.3500
created_at=2026-06-05T14:23:11-07:00
id=ia66l3rlto5x66
idle_timeout_s=900
max_age_s=14400
provider=runpod
provider_status=unknown (stale ledger — provider has no record)
advisory: ledger entry is stale — run 'kinoforge forget ia66l3rlto5x66'
```

## 5. Error matrix for `_cmd_status`

| Condition | stdout | stderr | exit |
|---|---|---|---|
| Ledger empty / id absent | — | `instance '<id>' not found in ledger` | 1 |
| Entry present, `provider` value not in registry (`UnknownAdapter`) | ledger block + `provider_status=unknown (unknown provider: <name>)` | — | 2 |
| Entry present, provider ctor succeeds, `get_instance` → `KeyError` | ledger block + `provider_status=unknown (stale ledger — provider has no record)` + advisory line | — | 0 |
| Entry present, `get_instance` → any non-KeyError exception | ledger block + `provider_status=unknown (provider lookup failed: <ExcClass>)` | — | 2 |
| Entry present, `get_instance` returns Instance, `endpoints()` raises | full ledger block + `provider_status=<status>` + `endpoints=unknown (<ExcClass>)` | — | 0 |
| Entry present, `get_instance` returns Instance, `endpoints()` succeeds | full ledger block + `provider_status=<status>` + `endpoints=<json>` | — | 0 |

## 6. Edge cases

1. **`endpoints()` raises while `get_instance` succeeds:** wrapped separately. `provider_status` still printed; `endpoints` line shows `unknown (<ExcClass>)`. Exit stays 0.
2. **`cost_rate_usd_per_hr` absent on legacy entry:** treated as `0.0`. Mirrors `_print_instance_overview` (cli.py:150).
3. **`created_at` absent:** defensive — treated as `now`, age clamps to `0.0`. Mirrors `_print_instance_overview:147`.
4. **Clock skew (`created_at > now`):** age clamped via `max(0.0, …)`. Spend likewise.
5. **Empty endpoints dict:** prints `endpoints={}`.
6. **`--config` points at unreadable file:** `load_config` raises before `_cmd_status` is reached. No special handling.
7. **`kinoforge forget <id>` on already-forgotten id:** returns 1 + "not found in ledger" stderr. Non-idempotent on purpose (matches `_cmd_stop`/`_cmd_destroy`).

## 7. Testing posture

Offline. No real cloud, no real SDK. All tests use:

- `LocalArtifactStore(tmp_path)` for the ledger fixture.
- A test-local `FakeProvider` registered into `kinoforge.core.registry`; per-test behaviour set on the instance. Test fixture also tears the registration down via a `try…finally` to prevent bleed.
- `FakeClock` (`kinoforge.core.clock`) for deterministic `now`.
- `capsys` for stdout/stderr capture.

### Test matrix

| # | Test | Asserts |
|---|---|---|
| 1 | `Ledger.record` writes new keys when supplied | round-trip JSON has `idle_timeout_s`, `max_age_s` |
| 2 | `Ledger.record` omits new keys when None | round-trip JSON has neither key (backwards-compat) |
| 3 | `Ledger.entries()` reads legacy entry (missing new keys) | no KeyError; entry dict missing them |
| 4 | `_build_ledger_block` legacy entry + no cfg | both fields → `<not in ledger>` |
| 5 | `_build_ledger_block` legacy entry + cfg supplied | both fields → `cfg.lifecycle()` values |
| 6 | `_build_ledger_block` new-shape entry + no cfg | both fields → entry values; cfg ignored |
| 7 | `_build_ledger_block` clock-skew (`created_at > now`) | `age_h` clamped to `0.0` |
| 8 | `_cmd_status` id absent | stderr msg + exit 1 |
| 9 | `_cmd_status` provider success path | sorted lines, exit 0, endpoints printed |
| 10 | `_cmd_status` KeyError path | advisory line + exit 0 |
| 11 | `_cmd_status` network/SDK error path | unknown line w/ ExcClass + exit 2 |
| 12 | `_cmd_status` `UnknownAdapter` for provider name | unknown line + exit 2 |
| 13 | `_cmd_status` `endpoints()` raises while `get_instance` OK | `endpoints=unknown (<ExcClass>)`; exit 0 |
| 14 | `_cmd_status` output is alphabetised `key=value`, one per line | regex / line-order assertion |
| 15 | `_cmd_status` ISO8601 local-tz on `created_at` | regex `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}` |
| 16 | `_cmd_status` advisory printed exactly once on stale path | count check |
| 17 | `_cmd_status` accepts `-c PATH` short alias | parser smoke |
| 18 | `_cmd_forget` removes single entry | post-call `entries()` no longer contains id |
| 19 | `_cmd_forget` absent id → exit 1 | stderr msg |
| 20 | `_cmd_forget` non-idempotent (second call → exit 1) | regression lock |
| 21 | `_build_parser` registers `forget` subcommand w/ id positional | parser smoke |

## 8. Task slicing

| Task | Files | Tests | Commit summary |
|---|---|---|---|
| **T1 — Ledger schema extension** | `src/kinoforge/core/lifecycle.py` | #1-3 above | `feat(lifecycle): persist idle_timeout_s + max_age_s in Ledger.record` |
| **T2 — `_cmd_status` ledger-first rewrite + `--config` plumbing** | `src/kinoforge/cli.py` | #4-17 above | `feat(cli): kinoforge status reads ledger; rich block + provider dispatch` |
| **T3 — `kinoforge forget <id>` subcommand** | `src/kinoforge/cli.py` | #18-21 above | `feat(cli): kinoforge forget <id> removes single ledger entry` |

Final integration: README "Operator commands" section gets a short `status` / `forget` example; PROGRESS Phase 33 entry; `--no-ff` merge.

## 9. Out of scope (documented gaps)

- **PROGRESS:127 — cloud-ledger CLI routing.** `_ledger(state_dir)` continues to construct `LocalArtifactStore(state_dir)` unconditionally. Closing this requires touching every ledger-using command (`stop`, `destroy`, `reap`, `gc`, `status`, `forget`) plus a lock-acquisition strategy for cloud-backed reads. Own layer.
- **Provider construction with cfg-dependent secrets.** `_cmd_status` uses sibling parity (`registry.get_provider(name)()`). Providers that need cfg/secret injection beyond `EnvCredentialProvider` defaults already fail in `stop`/`destroy` today; fixing it is a provider-construction refactor unrelated to status.
- **`--json` output mode for `status`.** Multi-line `key=value` ships v1. JSON output via `--json` flag is a future layer.
- **Backfill migration helper.** Soft display (Q6=A) is the chosen migration stance. No `kinoforge ledger migrate` subcommand.
- **`status` sweep over all ledger entries** (no `id` arg). Today's command is single-id. A `--all` flag is an obvious future addition; deferred.
