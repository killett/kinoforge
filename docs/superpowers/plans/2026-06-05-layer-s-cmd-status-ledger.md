# Layer S — `kinoforge status` reads the ledger — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `kinoforge status <id>` to read the ledger as the source of truth, dispatch to the recorded provider, surface rich ledger-derived facts, and ship `kinoforge forget <id>` for operator-driven stale-entry cleanup.

**Architecture:** Three tasks, three commits, fully offline-tested. T1 extends `Ledger.record()` with optional `idle_timeout_s`/`max_age_s` kwargs and persists them in the JSON entry. T2 rewrites `_cmd_status` to do ledger-first lookup → provider dispatch (sibling parity), prints an alphabetised `key=value` block, distinguishes stale-ledger (exit 0 + advisory) from transient provider failure (exit 2). T3 adds `kinoforge forget <id>` as the operator's targeted-cleanup recovery command.

**Tech Stack:** Python 3.13, stdlib `argparse` / `json` / `datetime`, pydantic `Config`, existing `Ledger` + `ArtifactStore` + `LocalArtifactStore` + `registry`. No new runtime deps. `FakeProvider` + `FakeClock` + `LocalArtifactStore(tmp_path)` + `capsys` for tests.

**Spec:** `docs/superpowers/specs/2026-06-05-layer-s-cmd-status-ledger-design.md`

---

## Spec deviations resolved during planning

These are translation details. Spec stays authoritative on decisions; plan reflects actual codebase shape.

| Spec said | Actual codebase | Plan resolution |
|---|---|---|
| Update `LifecycleManager.warm_reuse_or_create` call site | `warm_reuse_or_create` (`core/lifecycle.py:307`) is a module-level function that never calls `ledger.record`. The sole production call site is `cli.py:307` inside `_cmd_deploy`, where `cfg = load_config(args.config)` (`cli.py:279`) is in scope and `cfg.lifecycle()` gives us idle/max values. | T1 updates `cli.py:307`, not `warm_reuse_or_create`. Tests for `Ledger.record` go through direct ledger construction (matches `tests/core/test_lifecycle_sweeper.py` pattern). |
| `status` / `forget` take positional `id` | Existing siblings (`stop`, `destroy`) use `--id ID` flag (`cli.py:222-227`). Spec's positional form is a deviation from house style. | T2 keeps `status --id`. T3 uses `forget --id`. |
| `last_heartbeat` is persisted on entries | Current `Ledger.record` persists only `id`, `provider`, `tags`, `created_at`, `cost_rate_usd_per_hr` (`core/lifecycle.py:459-465`). `last_heartbeat` is forward-compatible — `_build_ledger_block` checks `if "last_heartbeat" in entry` so it surfaces when present, omits when absent. | T2 tests cover the conditional-surfacing path by synthesising an entry with `last_heartbeat` set. PROGRESS Phase 33 entry documents production-side persistence as a future addition (out of scope for Layer S). |

---

## File structure

| File | Role | Change |
|---|---|---|
| `src/kinoforge/core/lifecycle.py` | `Ledger.record()` signature + persisted JSON shape | Modify (T1) |
| `src/kinoforge/cli.py:307` | `_cmd_deploy` ledger-record call site | Modify (T1) |
| `src/kinoforge/cli.py:217-219` | `status` subparser | Modify (T2): add optional `--config`/`-c` |
| `src/kinoforge/cli.py:550-579` | `_cmd_status` body | Rewrite (T2) |
| `src/kinoforge/cli.py` (new helpers) | `_build_ledger_block`, `_ledger_field_or_cfg`, `_print_block` | Create (T2) |
| `src/kinoforge/cli.py` (new subparser + dispatch + impl) | `forget` subcommand | Create (T3) |
| `tests/core/test_lifecycle.py` (or new `test_ledger_record.py`) | `Ledger.record` schema extension tests | Create or extend (T1) |
| `tests/test_cli.py` | `_cmd_status` / `_build_ledger_block` / `forget` tests | Extend (T2 + T3) |
| `PROGRESS.md` | Phase 33 entry | Modify (T3) |
| `README.md` | "Operator commands" `status` / `forget` example | Modify (T3) |

---

## Task 1 — Extend `Ledger.record` schema with `idle_timeout_s` + `max_age_s`

**Goal:** Persist optional `idle_timeout_s` and `max_age_s` keys on every new ledger entry so the operator's `kinoforge status` can surface lifecycle policy without re-loading the YAML config.

**Files:**
- Modify: `src/kinoforge/core/lifecycle.py:443-467` — `Ledger.record` signature + entry dict
- Modify: `src/kinoforge/cli.py:305-307` — `_cmd_deploy` thread `cfg.lifecycle()` values into the call
- Test: `tests/core/test_lifecycle.py` — three new tests (legacy entry read, new-kwargs round-trip, omit-on-None round-trip)

**Acceptance Criteria:**
- [ ] `Ledger.record(instance, idle_timeout_s=900, max_age_s=14400)` persists both keys; round-trip via `Ledger.entries()[0]` returns them as `int`.
- [ ] `Ledger.record(instance)` (no kwargs) persists neither key; entry dict has no `idle_timeout_s` / `max_age_s` field. Legacy backwards-compat lock.
- [ ] `Ledger.entries()` reads a hand-rolled legacy entry (missing both keys) without raising; returned dict simply lacks them.
- [ ] `_cmd_deploy` threads `cfg.lifecycle().idle_timeout_s` and `cfg.lifecycle().max_age_s` into `ledger.record`. Test asserts that after `kinoforge deploy`, the ledger entry contains both keys with the configured values.
- [ ] All 5 existing `tests/core/test_lifecycle_sweeper.py` call sites and 2 existing `tests/test_cli.py` call sites still pass without modification (backwards-compat via default-None kwargs).

**Verify:** `pixi run pytest tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py tests/test_cli.py -v` → all green; new tests pass; existing tests unchanged.

**Steps:**

- [ ] **Step 1: Write the failing tests** in `tests/core/test_lifecycle.py` (append to existing file; create a new test class if helpful):

```python
import json
from pathlib import Path

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


def _make_instance(iid: str = "i-1") -> Instance:
    return Instance(
        id=iid,
        provider="fake",
        status="ready",
        endpoints={},
        hardware={},
        tags={},
        metadata={},
        created_at=1717635791.0,
        cost_rate_usd_per_hr=0.35,
    )


def test_record_persists_idle_timeout_s_and_max_age_s(tmp_path: Path) -> None:
    """Ledger.record with both new kwargs writes them into the JSON entry."""
    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id="_lifecycle")

    ledger.record(_make_instance(), idle_timeout_s=900, max_age_s=14400)

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["idle_timeout_s"] == 900
    assert entries[0]["max_age_s"] == 14400


def test_record_omits_new_keys_when_kwargs_none(tmp_path: Path) -> None:
    """Backwards-compat: record() without kwargs writes the legacy entry shape.

    Bug-catch: if a default-None kwarg accidentally persisted as `null`, legacy
    consumers that switch on `key in entry` would flip behavior.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id="_lifecycle")

    ledger.record(_make_instance())

    on_disk = json.loads(
        (tmp_path / "_lifecycle" / "ledger.json").read_text()
    )
    entry = on_disk[0]
    assert "idle_timeout_s" not in entry
    assert "max_age_s" not in entry


def test_entries_reads_legacy_entry_without_new_keys(tmp_path: Path) -> None:
    """A ledger.json written before this layer must read cleanly.

    Bug-catch: if the new fields became required, this would KeyError.
    """
    target = tmp_path / "_lifecycle" / "ledger.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            [
                {
                    "id": "legacy-1",
                    "provider": "runpod",
                    "tags": {},
                    "created_at": 1700000000.0,
                    "cost_rate_usd_per_hr": 0.35,
                }
            ]
        )
    )

    ledger = Ledger(store=LocalArtifactStore(tmp_path), run_id="_lifecycle")
    entries = ledger.entries()

    assert len(entries) == 1
    assert entries[0]["id"] == "legacy-1"
    assert "idle_timeout_s" not in entries[0]
    assert "max_age_s" not in entries[0]
```

- [ ] **Step 2: Run tests to confirm RED**

Run: `pixi run pytest tests/core/test_lifecycle.py::test_record_persists_idle_timeout_s_and_max_age_s tests/core/test_lifecycle.py::test_record_omits_new_keys_when_kwargs_none tests/core/test_lifecycle.py::test_entries_reads_legacy_entry_without_new_keys -v`

Expected: First two FAIL with `TypeError: Ledger.record() got an unexpected keyword argument 'idle_timeout_s'`. Third passes (legacy read is already lenient).

- [ ] **Step 3: Modify `Ledger.record` signature + entry dict** in `src/kinoforge/core/lifecycle.py:443-467`:

```python
    def record(
        self,
        instance: Instance,
        *,
        idle_timeout_s: int | None = None,
        max_age_s: int | None = None,
    ) -> None:
        """Append an instance entry to the ledger.

        Reads the current ledger (or starts fresh if none exists), appends
        the new entry, and writes back atomically within this call under
        an outer cross-process lock.

        Args:
            instance: The :class:`~kinoforge.core.interfaces.Instance` to
                record.  Fields ``id``, ``provider``, ``tags``,
                ``created_at``, and ``cost_rate_usd_per_hr`` are stored.
            idle_timeout_s: Optional lifecycle policy snapshot — when
                non-None, persisted into the entry so `kinoforge status`
                can surface it without re-loading the YAML config.
            max_age_s: Optional lifecycle policy snapshot — same purpose
                as ``idle_timeout_s``.
        """
        with self._store.acquire_lock(
            f"ledger/{self._run_id}", ttl_s=self._mutate_ttl_s
        ):
            entries = self._read_entries()
            entry: dict = {  # type: ignore[type-arg]
                "id": instance.id,
                "provider": instance.provider,
                "tags": dict(instance.tags),
                "created_at": instance.created_at,
                "cost_rate_usd_per_hr": instance.cost_rate_usd_per_hr,
            }
            if idle_timeout_s is not None:
                entry["idle_timeout_s"] = int(idle_timeout_s)
            if max_age_s is not None:
                entry["max_age_s"] = int(max_age_s)
            entries.append(entry)
            self._write_entries(entries)
```

- [ ] **Step 4: Update `_cmd_deploy` call site** at `src/kinoforge/cli.py:305-307`:

Replace:
```python
        if result.instance is not None:
            ledger = _ledger(state_dir)
            ledger.record(result.instance)
```
With:
```python
        if result.instance is not None:
            ledger = _ledger(state_dir)
            lc = cfg.lifecycle()
            ledger.record(
                result.instance,
                idle_timeout_s=lc.idle_timeout_s,
                max_age_s=lc.max_age_s,
            )
```

- [ ] **Step 5: Add a `_cmd_deploy`-side regression test** in `tests/test_cli.py` (E2E lock that the call site actually threads the values, not just that `Ledger.record` accepts them):

```python
def test_cmd_deploy_persists_lifecycle_policy_into_ledger(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """`kinoforge deploy` records idle_timeout_s + max_age_s into the ledger.

    Bug-catch: a future refactor of _cmd_deploy that drops the kwargs would
    leave a silent gap — `kinoforge status` would fall back to `<not in ledger>`
    even when --config wasn't supplied.
    """
    # Choose any existing fake-engine + local-provider config from examples/.
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        Path("examples/configs/local-fake.yaml").read_text()
    )
    state_dir = tmp_path / "state"

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "deploy", "--config", str(cfg_path)]
    )

    assert rc == 0
    ledger_path = state_dir / "_lifecycle" / "ledger.json"
    data = json.loads(ledger_path.read_text())
    assert len(data) == 1
    entry = data[0]
    # local-fake.yaml has lifecycle: idle_timeout_s: 900, max_age_s: 14400
    # (substitute the actual values from examples/configs/local-fake.yaml).
    assert "idle_timeout_s" in entry
    assert "max_age_s" in entry
    assert isinstance(entry["idle_timeout_s"], int)
    assert isinstance(entry["max_age_s"], int)
```

Note for implementer: read `examples/configs/local-fake.yaml` first to confirm the actual `lifecycle.idle_timeout_s` / `lifecycle.max_age_s` values to hard-code in the assertion (or skip the value-equality assertion and only assert key presence + type). `kinoforge_cli_main` is the existing test helper / direct `kinoforge.cli.main` import.

- [ ] **Step 6: Run all four new tests + the full lifecycle / cli regression** to confirm GREEN

Run: `pixi run pytest tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py tests/test_cli.py -v`

Expected: All previously-passing tests still green. New tests green.

- [ ] **Step 7: Run pre-commit on touched files** (project policy)

Run: `pixi run pre-commit run --files src/kinoforge/core/lifecycle.py src/kinoforge/cli.py tests/core/test_lifecycle.py tests/test_cli.py`

Expected: All hooks pass.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/lifecycle.py src/kinoforge/cli.py tests/core/test_lifecycle.py tests/test_cli.py
git commit -m "feat(lifecycle): persist idle_timeout_s + max_age_s on Ledger.record (Phase 33 T1)"
```

```json:metadata
{"files": ["src/kinoforge/core/lifecycle.py", "src/kinoforge/cli.py", "tests/core/test_lifecycle.py", "tests/test_cli.py"], "verifyCommand": "pixi run pytest tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py tests/test_cli.py -v", "acceptanceCriteria": ["Ledger.record persists idle_timeout_s + max_age_s when supplied", "Ledger.record omits the keys when kwargs are None", "Ledger.entries reads legacy entries without raising", "_cmd_deploy threads cfg.lifecycle() values into ledger.record", "existing test_lifecycle_sweeper + test_cli call sites pass unchanged"]}
```

---

## Task 2 — Rewrite `_cmd_status` for ledger-first dispatch + rich `key=value` output

**Goal:** Replace the current local-provider-only `_cmd_status` with a ledger-first implementation that prints an alphabetised `key=value` block of ledger-derived facts, dispatches to the recorded provider for live status / endpoints, and distinguishes stale-ledger (exit 0 + advisory) from transient provider failure (exit 2).

**Files:**
- Modify: `src/kinoforge/cli.py:217-219` — `status` subparser (add `--config`/`-c` optional flag)
- Modify: `src/kinoforge/cli.py:550-579` — `_cmd_status` body (full rewrite)
- Create: `src/kinoforge/cli.py` (new helpers) — `_build_ledger_block`, `_ledger_field_or_cfg`, `_print_status_block`
- Test: `tests/test_cli.py` — fourteen new tests covering the spec's test matrix items #4-#17

**Acceptance Criteria:**
- [ ] `_build_ledger_block` returns the spec-defined field set in alphabetical key order; ledger-supplied `idle_timeout_s`/`max_age_s` win over `cfg`; `cfg` wins over `<not in ledger>`.
- [ ] Negative `age_h` (clock skew: `created_at > now`) is clamped to `0.0`.
- [ ] `created_at` (and `last_heartbeat`, when present) are formatted with `datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")` (local-timezone ISO 8601 with colon-separated offset).
- [ ] `_cmd_status` with ledger missing id → stderr `instance '<id>' not found in ledger`; exit 1.
- [ ] Provider success path: prints sorted `key=value` block including `provider_status=<instance.status>` and `endpoints=<json.dumps(provider.endpoints(id))>`; exit 0.
- [ ] Provider `get_instance` raises `KeyError`: prints sorted block with `provider_status=unknown (stale ledger — provider has no record)`; appends `advisory: ledger entry is stale — run 'kinoforge forget --id <id>'` as the LAST stdout line; exit 0.
- [ ] Provider construction raises `UnknownAdapter`: prints sorted block with `provider_status=unknown (unknown provider: <name>)`; exit 2.
- [ ] Any other provider exception: prints sorted block with `provider_status=unknown (provider lookup failed: <ExcClass>)`; exit 2.
- [ ] `endpoints()` raises while `get_instance` succeeds: prints `endpoints=unknown (<ExcClass>)`; exit stays 0.
- [ ] `status --id ID -c PATH` (and `--config PATH`) parses; `cfg.lifecycle()` fills missing legacy entry fields.
- [ ] Advisory line printed exactly once on the stale path.

**Verify:** `pixi run pytest tests/test_cli.py -v -k 'status or build_ledger_block or print_status_block'` → all green; existing tests untouched.

**Steps:**

- [ ] **Step 1: Update the `status` subparser** at `src/kinoforge/cli.py:217-219`:

Replace:
```python
    # status
    p_status = sub.add_parser("status", help="show status of one instance")
    p_status.add_argument("--id", required=True, metavar="ID")
```
With:
```python
    # status
    p_status = sub.add_parser("status", help="show status of one instance")
    p_status.add_argument("--id", required=True, metavar="ID")
    p_status.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        metavar="PATH",
        help="optional config; fills missing legacy ledger fields",
    )
```

- [ ] **Step 2: Write failing tests for `_build_ledger_block`** in `tests/test_cli.py` (group under a new section comment):

```python
# ---------------------------------------------------------------------------
# Layer S — _build_ledger_block helper (pure)
# ---------------------------------------------------------------------------


from kinoforge.cli import _build_ledger_block


def _legacy_entry() -> dict:
    return {
        "id": "i-legacy",
        "provider": "runpod",
        "tags": {},
        "created_at": 1717635791.0,
        "cost_rate_usd_per_hr": 0.35,
    }


def _new_entry() -> dict:
    e = _legacy_entry()
    e["id"] = "i-new"
    e["idle_timeout_s"] = 900
    e["max_age_s"] = 14400
    return e


def test_build_ledger_block_legacy_entry_no_cfg_shows_sentinel() -> None:
    """Legacy entry + no cfg → idle_timeout_s / max_age_s fall back to sentinel.

    Bug-catch: if the fallback chain skipped the sentinel and emitted "None",
    operators would see a confusing 'None' string with no actionable signal.
    """
    block = _build_ledger_block(_legacy_entry(), cfg=None, now=1717635791.0)

    assert block["idle_timeout_s"] == "<not in ledger>"
    assert block["max_age_s"] == "<not in ledger>"


def test_build_ledger_block_legacy_entry_with_cfg_fills_from_lifecycle(
    tmp_path: Path,
) -> None:
    """Legacy entry + cfg → cfg.lifecycle() values fill the gap.

    Bug-catch: spec requires entry > cfg > sentinel precedence. If cfg won
    over entry, new-shape entries would silently lose their snapshot.
    """
    cfg_yaml = tmp_path / "cfg.yaml"
    cfg_yaml.write_text(Path("examples/configs/local-fake.yaml").read_text())
    cfg = load_config(cfg_yaml)

    block = _build_ledger_block(_legacy_entry(), cfg=cfg, now=1717635791.0)
    lc = cfg.lifecycle()

    assert block["idle_timeout_s"] == str(lc.idle_timeout_s)
    assert block["max_age_s"] == str(lc.max_age_s)


def test_build_ledger_block_new_entry_ignores_cfg() -> None:
    """Entry-supplied idle / max win over cfg.

    Bug-catch: a future operator who edits the YAML lifecycle limits AFTER
    spinning a pod must still see the pod's snapshot, not the new YAML.
    """
    cfg_yaml = Path("examples/configs/local-fake.yaml")
    cfg = load_config(cfg_yaml)

    block = _build_ledger_block(_new_entry(), cfg=cfg, now=1717635791.0)

    assert block["idle_timeout_s"] == "900"
    assert block["max_age_s"] == "14400"


def test_build_ledger_block_clamps_negative_age_to_zero() -> None:
    """`created_at > now` (clock skew on resume) clamps age_h to 0.0.

    Bug-catch: negative age would propagate to a negative accrued_spend_usd
    and confuse the operator.
    """
    block = _build_ledger_block(
        _legacy_entry(),
        cfg=None,
        now=1717635791.0 - 3600.0,  # 1h before created_at
    )

    assert block["age_h"] == "0.0"
    assert block["accrued_spend_usd"] == "0.0000"


def test_build_ledger_block_created_at_is_local_iso8601() -> None:
    """`created_at` formatted via .astimezone().isoformat(timespec='seconds').

    Bug-catch: if the formatter used .utcfromtimestamp / .isoformat() without
    offset, operators in non-UTC zones would misread the timestamp.  CLAUDE.md
    + memory both insist on local timezone.
    """
    block = _build_ledger_block(_legacy_entry(), cfg=None, now=1717635791.0)

    import re
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
        block["created_at"],
    ), block["created_at"]


def test_build_ledger_block_surfaces_last_heartbeat_when_present() -> None:
    """`last_heartbeat` (when present in entry) is formatted like created_at.

    Bug-catch: spec is forward-compat — production doesn't persist this yet,
    but the surface must be wired so the operator-visible side lights up the
    moment a future layer starts persisting it.
    """
    entry = _legacy_entry()
    entry["last_heartbeat"] = 1717636791.0

    block = _build_ledger_block(entry, cfg=None, now=1717636791.0)

    assert "last_heartbeat" in block
    import re
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
        block["last_heartbeat"],
    ), block["last_heartbeat"]


def test_build_ledger_block_omits_last_heartbeat_when_absent() -> None:
    """No `last_heartbeat` key → block omits the field entirely.

    Bug-catch: emitting `last_heartbeat=None` would be a usability regression.
    """
    block = _build_ledger_block(_legacy_entry(), cfg=None, now=1717635791.0)

    assert "last_heartbeat" not in block
```

- [ ] **Step 3: Run tests to confirm RED**

Run: `pixi run pytest tests/test_cli.py -v -k 'build_ledger_block'`

Expected: All FAIL with `ImportError: cannot import name '_build_ledger_block' from 'kinoforge.cli'`.

- [ ] **Step 4: Implement `_build_ledger_block` + `_ledger_field_or_cfg`** in `src/kinoforge/cli.py` (place above `_cmd_status`):

```python
from datetime import datetime

from kinoforge.core.config import Config


def _ledger_field_or_cfg(entry: dict, key: str, cfg: Config | None) -> str:
    """Return entry-supplied value, else cfg.lifecycle() value, else sentinel.

    Args:
        entry: Ledger entry dict (may be a legacy entry missing newer keys).
        key: One of ``"idle_timeout_s"`` or ``"max_age_s"``.
        cfg: Optional Config for fallback when entry lacks the key.

    Returns:
        Stringified value, or ``"<not in ledger>"`` when neither source has it.
    """
    value = entry.get(key)
    if value is not None:
        return str(value)
    if cfg is not None:
        lc = cfg.lifecycle()
        return str(getattr(lc, key))
    return "<not in ledger>"


def _build_ledger_block(
    entry: dict,
    *,
    cfg: Config | None,
    now: float,
) -> dict[str, str]:
    """Build the ledger-derived portion of `kinoforge status` output.

    Pure: no I/O, no clock reads. All time inputs flow through ``now``.

    Args:
        entry: A ledger entry dict (possibly legacy-shaped).
        cfg: Optional config used as fallback for lifecycle policy fields.
        now: Wall-clock seconds-since-epoch used for age / spend calculations.

    Returns:
        An ordered dict of ``{field: stringified_value}``.  ``last_heartbeat``
        is included only when the entry has it.
    """
    out: dict[str, str] = {}
    out["id"] = str(entry.get("id", "?"))
    out["provider"] = str(entry.get("provider", "?"))
    created_at_raw = float(entry.get("created_at", now))
    age_h = max(0.0, (now - created_at_raw) / 3600.0)
    out["created_at"] = (
        datetime.fromtimestamp(created_at_raw)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    out["age_h"] = f"{age_h:.1f}"
    rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
    out["cost_rate_usd_per_hr"] = f"{rate:.4f}"
    out["accrued_spend_usd"] = f"{age_h * rate:.4f}"
    out["idle_timeout_s"] = _ledger_field_or_cfg(entry, "idle_timeout_s", cfg)
    out["max_age_s"] = _ledger_field_or_cfg(entry, "max_age_s", cfg)
    hb = entry.get("last_heartbeat")
    if hb is not None:
        out["last_heartbeat"] = (
            datetime.fromtimestamp(float(hb))
            .astimezone()
            .isoformat(timespec="seconds")
        )
    return out
```

- [ ] **Step 5: Run helper tests to confirm GREEN**

Run: `pixi run pytest tests/test_cli.py -v -k 'build_ledger_block'`

Expected: All 7 tests PASS.

- [ ] **Step 6: Write failing tests for `_cmd_status`** (append to `tests/test_cli.py`):

```python
# ---------------------------------------------------------------------------
# Layer S — _cmd_status ledger-first dispatch
# ---------------------------------------------------------------------------


import json as _json
from kinoforge.core import registry
from kinoforge.core.interfaces import ComputeProvider, Instance


class _StatusFakeProvider:
    """Test double for `kinoforge status` provider dispatch.

    Per-test overrides of `get_instance_impl` and `endpoints_impl` let each
    test target a single branch in `_cmd_status`.
    """

    name = "fake-status"

    def __init__(self) -> None:
        self.get_instance_impl = lambda iid: Instance(
            id=iid,
            provider=self.name,
            status="ready",
            endpoints={"http": "https://example/"},
            hardware={},
            tags={},
            metadata={},
            created_at=0.0,
            cost_rate_usd_per_hr=0.0,
        )
        self.endpoints_impl = lambda iid: {"http": "https://example/"}

    # ComputeProvider methods used by _cmd_status:
    def get_instance(self, instance_id: str) -> Instance:
        return self.get_instance_impl(instance_id)

    def endpoints(self, instance_id: str) -> dict:
        return self.endpoints_impl(instance_id)

    # Unused but required by the ABC if registered as a full ComputeProvider —
    # implementer must check the ABC and stub the remaining methods to raise
    # NotImplementedError.  Spec compliance: this fake is registered under a
    # distinct name and never exercised outside _cmd_status.


@pytest.fixture
def status_fake_provider():
    """Register a fake provider, yield the instance, tear down."""
    inst = _StatusFakeProvider()
    registry.register_provider("fake-status", lambda: inst)
    try:
        yield inst
    finally:
        # Implementer: registry currently lacks `unregister_provider`; if
        # absent, reach into the private dict to pop "fake-status" so the
        # registration does not leak across tests.  If a helper exists,
        # use it instead.
        registry._PROVIDERS.pop("fake-status", None)  # type: ignore[attr-defined]


def _seed_ledger_with(tmp_path: Path, entry: dict) -> Path:
    """Write a ledger.json under tmp_path/_lifecycle/ and return state_dir."""
    state_dir = tmp_path / "state"
    target = state_dir / "_lifecycle" / "ledger.json"
    target.parent.mkdir(parents=True)
    target.write_text(_json.dumps([entry]))
    return state_dir


def _runpod_entry(iid: str = "i-runpod") -> dict:
    return {
        "id": iid,
        "provider": "fake-status",
        "tags": {},
        "created_at": 1717635791.0,
        "cost_rate_usd_per_hr": 0.35,
        "idle_timeout_s": 900,
        "max_age_s": 14400,
    }


def test_cmd_status_id_absent_from_ledger_exits_1(
    tmp_path: Path, capsys
) -> None:
    """status --id <unknown> → exit 1 + stderr 'not found in ledger'.

    Bug-catch: this is the ONE precondition for the entire status workflow.
    A regression here would let stale references silently succeed.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-existing"))

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-missing"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "not found in ledger" in captured.err


def test_cmd_status_provider_success_prints_full_block_exit_0(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """Happy path: ledger entry + provider returns Instance → exit 0 + full block.

    Bug-catch: prior to Layer S the LocalProvider-only dispatch would always
    return 'not found' for any non-local entry.  This lock proves the new
    dispatch path actually runs.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-runpod"]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "provider_status=ready" in captured.out
    assert "endpoints=" in captured.out
    assert "provider=fake-status" in captured.out


def test_cmd_status_keyerror_prints_advisory_exit_0(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """Provider KeyError → stale ledger advisory + exit 0.

    Bug-catch: returning exit 1 here would block scripts that expect "the
    instance is gone, ledger is stale, move on" as a successful outcome.
    """
    def raise_keyerror(iid: str) -> Instance:
        raise KeyError(iid)
    status_fake_provider.get_instance_impl = raise_keyerror

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-runpod"]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "stale ledger" in captured.out
    assert "advisory:" in captured.out
    assert "kinoforge forget --id i-runpod" in captured.out
    # Advisory printed exactly once
    assert captured.out.count("advisory:") == 1


def test_cmd_status_transient_error_exits_2(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """Provider raises non-KeyError → transient outcome → exit 2.

    Bug-catch: exit 0 here would mask real outages from monitoring scripts.
    """
    def raise_runtime(iid: str) -> Instance:
        raise RuntimeError("simulated network failure")
    status_fake_provider.get_instance_impl = raise_runtime

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-runpod"]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "provider lookup failed: RuntimeError" in captured.out


def test_cmd_status_unknown_adapter_exits_2(
    tmp_path: Path, capsys
) -> None:
    """Ledger entry references a provider name not in the registry → exit 2.

    Bug-catch: silently returning 0 would hide broken installs.
    """
    entry = _runpod_entry()
    entry["provider"] = "this-provider-does-not-exist"
    state_dir = _seed_ledger_with(tmp_path, entry)

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-runpod"]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown provider" in captured.out


def test_cmd_status_endpoints_raises_still_exit_0(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """`endpoints()` raises while get_instance succeeds → endpoints=unknown(...) + exit 0.

    Bug-catch: ancillary endpoint discovery failure must not turn a healthy
    `ready` instance into an apparent outage.
    """
    def raise_endpoints(iid: str) -> dict:
        raise RuntimeError("endpoint api down")
    status_fake_provider.endpoints_impl = raise_endpoints

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-runpod"]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "provider_status=ready" in captured.out
    assert "endpoints=unknown (RuntimeError)" in captured.out


def test_cmd_status_output_is_alphabetised_key_value(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """Stdout lines are sorted alphabetically; advisory (if any) is the last line.

    Bug-catch: unsorted output would break operator scripts that grep/awk by
    line number.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "status", "--id", "i-runpod"]
    )

    assert rc == 0
    out_lines = [
        ln for ln in capsys.readouterr().out.splitlines()
        if "=" in ln and not ln.startswith("[instance overview]")
    ]
    keys = [ln.split("=", 1)[0] for ln in out_lines]
    assert keys == sorted(keys), keys


def test_cmd_status_short_alias_dash_c_parses(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """`status --id ID -c PATH` parses (mirrors the documented quickstart).

    Bug-catch: argparse mis-wiring of the short alias would error before
    _cmd_status ever runs.
    """
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(Path("examples/configs/local-fake.yaml").read_text())
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = kinoforge_cli_main(
        [
            "--state-dir", str(state_dir),
            "status", "--id", "i-runpod", "-c", str(cfg_path),
        ]
    )

    assert rc == 0


def test_cmd_status_legacy_entry_with_cfg_fills_lifecycle(
    tmp_path: Path, capsys, status_fake_provider
) -> None:
    """Legacy entry (no idle/max keys) + --config → cfg.lifecycle() fills the block.

    Bug-catch: legacy ledger entries from before Layer S would otherwise show
    '<not in ledger>' for every operator with a YAML on hand.
    """
    legacy = _runpod_entry()
    del legacy["idle_timeout_s"]
    del legacy["max_age_s"]
    state_dir = _seed_ledger_with(tmp_path, legacy)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(Path("examples/configs/local-fake.yaml").read_text())

    rc = kinoforge_cli_main(
        [
            "--state-dir", str(state_dir),
            "status", "--id", "i-runpod", "--config", str(cfg_path),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "idle_timeout_s=" in captured.out
    assert "<not in ledger>" not in captured.out
```

- [ ] **Step 7: Run `_cmd_status` tests to confirm RED**

Run: `pixi run pytest tests/test_cli.py -v -k 'cmd_status'`

Expected: All FAIL — current `_cmd_status` always tries `LocalProvider().get_instance(args.id)` and returns 1, so most assertions miss; `--config` arg doesn't exist yet on the parser.

- [ ] **Step 8: Implement `_print_status_block` + rewrite `_cmd_status`** in `src/kinoforge/cli.py`. Replace lines 550-579:

```python
def _print_status_block(
    ledger_block: dict[str, str],
    provider_block: dict[str, str],
    *,
    advisory: str | None = None,
) -> None:
    """Print a merged + sorted `key=value` block to stdout.

    Args:
        ledger_block: Output of :func:`_build_ledger_block`.
        provider_block: Provider-derived fields
            (``provider_status`` and optionally ``endpoints``).
        advisory: Optional advisory line; printed AFTER the sorted block when set.
    """
    merged = {**ledger_block, **provider_block}
    for key in sorted(merged):
        print(f"{key}={merged[key]}")
    if advisory is not None:
        print(advisory)


def _cmd_status(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``status`` subcommand: read ledger, dispatch to recorded provider.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code per the Layer S design contract:
            * 0 — provider success OR stale ledger (KeyError) OR endpoints-only failure.
            * 1 — ledger entry absent.
            * 2 — unknown provider in entry OR non-KeyError exception from provider.
    """
    from kinoforge.core.config import load_config
    from kinoforge.core import registry

    ledger = _ledger(state_dir)
    entry = next(
        (e for e in ledger.entries() if e.get("id") == args.id), None
    )
    if entry is None:
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1

    cfg = load_config(args.config) if getattr(args, "config", None) else None
    ledger_block = _build_ledger_block(entry, cfg=cfg, now=time.time())

    provider_name = str(entry.get("provider", "local"))
    try:
        provider = registry.get_provider(provider_name)()
    except UnknownAdapter:
        provider_block = {
            "provider_status": f"unknown (unknown provider: {provider_name})",
        }
        _print_status_block(ledger_block, provider_block)
        return 2

    try:
        instance = provider.get_instance(args.id)
    except KeyError:
        provider_block = {
            "provider_status": "unknown (stale ledger — provider has no record)",
        }
        _print_status_block(
            ledger_block,
            provider_block,
            advisory=(
                f"advisory: ledger entry is stale — "
                f"run 'kinoforge forget --id {args.id}'"
            ),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — explicit transient-error surface
        provider_block = {
            "provider_status": f"unknown (provider lookup failed: {exc.__class__.__name__})",
        }
        _print_status_block(ledger_block, provider_block)
        return 2

    provider_block = {"provider_status": instance.status}
    try:
        provider_block["endpoints"] = json.dumps(provider.endpoints(args.id))
    except Exception as exc:  # noqa: BLE001
        provider_block["endpoints"] = f"unknown ({exc.__class__.__name__})"

    _print_status_block(ledger_block, provider_block)
    return 0
```

Add the necessary imports at the top of `cli.py` if not already present: `import json`, `import time`, `from kinoforge.core.config import Config`. (Lazy-import `load_config` and `registry` inside the function to keep CLI startup fast — pattern matches `_cmd_deploy`.)

- [ ] **Step 9: Run all 16 Layer S tests** (`_build_ledger_block` + `_cmd_status`) to confirm GREEN

Run: `pixi run pytest tests/test_cli.py -v -k 'status or build_ledger_block or print_status_block'`

Expected: All PASS.

- [ ] **Step 10: Run full test suite** to confirm nothing else regressed

Run: `pixi run pytest -x`

Expected: All previously-passing tests still green. Test count: previous 1198 + ~10 new from T1 + 16 new from T2 = ~1224 passed.

- [ ] **Step 11: Run pre-commit on touched files**

Run: `pixi run pre-commit run --files src/kinoforge/cli.py tests/test_cli.py`

Expected: All hooks pass.

- [ ] **Step 12: Commit**

```bash
git add src/kinoforge/cli.py tests/test_cli.py
git commit -m "feat(cli): kinoforge status reads ledger + rich key=value block (Phase 33 T2)"
```

```json:metadata
{"files": ["src/kinoforge/cli.py", "tests/test_cli.py"], "verifyCommand": "pixi run pytest tests/test_cli.py -v -k 'status or build_ledger_block or print_status_block'", "acceptanceCriteria": ["_build_ledger_block returns spec-defined fields in alphabetical order with documented fallback precedence", "negative age_h is clamped to 0.0", "created_at uses local-timezone ISO8601 with colon-separated offset", "_cmd_status ledger-missing path returns 1 + stderr message", "provider success path prints provider_status + endpoints + exit 0", "KeyError path prints advisory exactly once + exit 0", "UnknownAdapter path prints unknown-provider line + exit 2", "non-KeyError exceptions print unknown line w/ ExcClass + exit 2", "endpoints() failure prints endpoints=unknown(...) + exit stays 0", "status --id ID -c PATH parses + cfg fills missing legacy fields"]}
```

---

## Task 3 — Ship `kinoforge forget --id <id>` recovery subcommand + README + PROGRESS

**Goal:** Add a single-id ledger-only `forget` subcommand so operators can clear the stale entries that `_cmd_status` advises them about. Document `status` and `forget` in README. Update PROGRESS Phase 33.

**Files:**
- Modify: `src/kinoforge/cli.py` — `_build_parser` (add `forget` subcommand), `main` dispatch (add `cmd == "forget"` branch), `_cmd_forget` impl
- Modify: `tests/test_cli.py` — four new tests covering #18-#21 from the spec test matrix
- Modify: `README.md` — "Operator commands" section gains `status` / `forget` example
- Modify: `PROGRESS.md` — append "Phase 33 — Layer S" entry; flip PROGRESS:120 carry-forward to CLOSED; update "Single next action" pointer + test count + budget

**Acceptance Criteria:**
- [ ] `kinoforge forget --id ID` with existing ledger entry: removes it; stdout `forgot: <id>`; exit 0.
- [ ] `kinoforge forget --id ID` with absent ledger entry: stderr `instance '<id>' not found in ledger`; exit 1.
- [ ] Calling `forget` twice on the same id (second call after the first succeeds) returns exit 1 with the absent-entry message. Non-idempotent regression lock per spec §6.
- [ ] `_build_parser` registers the `forget` subcommand with `--id ID` flag (matches `stop`/`destroy` style).
- [ ] README contains a fenced example showing `kinoforge status --id <id>` output AND `kinoforge forget --id <id>` cleanup.
- [ ] PROGRESS Phase 33 entry lists T1/T2/T3 commit SHAs, key design decisions, and flips the PROGRESS:120 carry-forward to CLOSED. PROGRESS:127 stays OPEN with a note that Layer S explicitly did not address it.

**Verify:** `pixi run pytest tests/test_cli.py -v -k 'forget' && pixi run pytest -x` → all green, full suite passes.

**Steps:**

- [ ] **Step 1: Write failing tests for `_cmd_forget`** in `tests/test_cli.py` (append after Layer S `_cmd_status` tests):

```python
# ---------------------------------------------------------------------------
# Layer S — kinoforge forget --id <id>
# ---------------------------------------------------------------------------


def test_cmd_forget_removes_existing_entry(tmp_path: Path, capsys) -> None:
    """`forget --id ID` removes the entry; stdout 'forgot: ID'; exit 0.

    Bug-catch: a no-op forget that returned 0 without mutating the ledger
    would leave operators chasing the same stale id forever.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-target"))

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "forget", "--id", "i-target"]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "forgot: i-target" in captured.out
    on_disk = _json.loads(
        (state_dir / "_lifecycle" / "ledger.json").read_text()
    )
    assert all(e.get("id") != "i-target" for e in on_disk)


def test_cmd_forget_absent_id_exits_1(tmp_path: Path, capsys) -> None:
    """`forget --id <missing>` → stderr 'not found in ledger'; exit 1.

    Bug-catch: silent success on absent ids would mask script bugs that
    pass a wrong instance id.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-other"))

    rc = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "forget", "--id", "i-missing"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "not found in ledger" in captured.err


def test_cmd_forget_is_non_idempotent(tmp_path: Path, capsys) -> None:
    """Second forget on the same id (post-success) returns exit 1.

    Bug-catch: idempotent `forget` would diverge from the design decision
    in spec §6 and from sibling commands `stop` / `destroy`.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-twice"))

    rc1 = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "forget", "--id", "i-twice"]
    )
    rc2 = kinoforge_cli_main(
        ["--state-dir", str(state_dir), "forget", "--id", "i-twice"]
    )

    assert rc1 == 0
    assert rc2 == 1


def test_build_parser_registers_forget_with_id_flag() -> None:
    """`_build_parser()` accepts `forget --id ID` and exposes args.cmd == 'forget'.

    Bug-catch: parser wiring is the only thing the dispatch in `main()` relies
    on.  A typo in subparser name would route forget to argparse error.
    """
    from kinoforge.cli import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["forget", "--id", "i-foo"])
    assert ns.cmd == "forget"
    assert ns.id == "i-foo"
```

- [ ] **Step 2: Run forget tests to confirm RED**

Run: `pixi run pytest tests/test_cli.py -v -k 'forget'`

Expected: All FAIL — `forget` subcommand doesn't exist yet; parser errors out before `_cmd_forget` runs.

- [ ] **Step 3: Add the `forget` subparser** to `_build_parser` in `src/kinoforge/cli.py` (place it right after the `destroy` subparser, lines ~226-227):

```python
    # forget
    p_forget = sub.add_parser(
        "forget", help="remove an instance entry from the local ledger"
    )
    p_forget.add_argument("--id", required=True, metavar="ID")
```

- [ ] **Step 4: Implement `_cmd_forget`** in `src/kinoforge/cli.py` (place above the entry-point `main` function, alongside other `_cmd_*` impls):

```python
def _cmd_forget(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``forget`` subcommand: remove one ledger entry.

    Args:
        args: Parsed CLI arguments (uses ``args.id``).
        state_dir: Path to the state directory.

    Returns:
        Exit code: 0 on successful removal; 1 when the id is absent.
    """
    ledger = _ledger(state_dir)
    if not any(e.get("id") == args.id for e in ledger.entries()):
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1
    ledger.forget(args.id)
    print(f"forgot: {args.id}")
    return 0
```

- [ ] **Step 5: Wire dispatch** in `main()` at `src/kinoforge/cli.py:757-759` — add a `cmd == "forget"` branch alongside `gc`:

After:
```python
    if args.cmd == "gc":
        return _cmd_gc(args, state_dir)
```
Insert:
```python
    if args.cmd == "forget":
        return _cmd_forget(args, state_dir)
```

- [ ] **Step 6: Run forget tests to confirm GREEN**

Run: `pixi run pytest tests/test_cli.py -v -k 'forget'`

Expected: All 4 tests PASS.

- [ ] **Step 7: Update README** (`README.md`) — find the "Operator commands" or "CLI" section (use `rg -n '## ' README.md` to locate; if no operator section exists, append one). Add:

````markdown
### `kinoforge status --id <id>` — introspect one instance

`kinoforge status` reads the local ledger first and dispatches to the
provider recorded for that instance. The output is an alphabetised block
of `key=value` lines covering ledger-side facts (age, accrued spend,
lifecycle policy) plus live `provider_status` and `endpoints` from the
provider.

```
$ kinoforge status --id ia66l3rlto5x66
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

When the provider has no record of the id (stale ledger), `status`
exits 0 and appends an advisory:

```
provider_status=unknown (stale ledger — provider has no record)
advisory: ledger entry is stale — run 'kinoforge forget --id ia66l3rlto5x66'
```

Transient provider failures (network outage, SDK 5xx) exit 2.

Pass `--config PATH` (or `-c PATH`) to fill missing lifecycle fields on
legacy entries written before Layer S.

### `kinoforge forget --id <id>` — clear a stale ledger entry

Removes a single entry from the local ledger without touching the
upstream provider. Use when `kinoforge status` reports
`provider_status=unknown (stale ledger ...)`. Pairs naturally with
`kinoforge gc` for sweep-style cleanup.

```
$ kinoforge forget --id ia66l3rlto5x66
forgot: ia66l3rlto5x66
```
````

- [ ] **Step 8: Update PROGRESS.md** — append a new "Phase 33 — Layer S" entry under the post-MVP section in the spirit of the existing layer entries (use Phase 32 as a template; locate via `rg -n '^### Phase 32' PROGRESS.md`):

```markdown
### Phase 33 — Layer S (`kinoforge status` reads the ledger)

- [x] Task 1: `Ledger.record` schema extension — commit `<T1 SHA>`. Persists optional `idle_timeout_s` + `max_age_s` into every new ledger entry; backwards-compat preserved (legacy entries read clean, default-None kwargs omit the keys). `_cmd_deploy` updated to thread `cfg.lifecycle()` values into the call. 3 unit tests + 1 E2E lock.
- [x] Task 2: `_cmd_status` ledger-first rewrite — commit `<T2 SHA>`. New `_build_ledger_block` pure helper + `_print_status_block` formatter + sibling-parity provider dispatch (`registry.get_provider(name)()`). Distinguishes stale-ledger (exit 0 + `kinoforge forget --id <id>` advisory) from transient provider failure (exit 2); preserves exit 0 when `endpoints()` raises but `get_instance` succeeds. `status --id ID -c PATH` adds optional `--config`/`-c` so legacy entries can fall back to `cfg.lifecycle()`. 16 tests.
- [x] Task 3: `kinoforge forget --id <id>` + README + PROGRESS — commit `<T3 SHA>`. New non-idempotent single-id ledger-only recovery subcommand. README "Operator commands" gains `status` + `forget` examples. 4 tests.
- [x] Merge to main via `--no-ff` — merge commit `<MERGE SHA>` (closes PROGRESS:120).

**Key design decisions:**
- Spec scope locked at A+B (Q1): ledger-first dispatch + rich ledger-derived output. Cloud-ledger CLI routing (PROGRESS:127) explicitly out of scope.
- Exit-code split (Q2=B): `KeyError` from provider ⇒ exit 0 (stale ledger; operator action = `forget`); any other provider exception ⇒ exit 2 (transient).
- Multi-line `key=value` alphabetised output (Q3=A): scales as fields are added, plays well with `grep`, no `jq` dependency.
- Ledger-schema extension + optional `--config` (Q5=A+C): values frozen at instance creation time, immune to later YAML edits; legacy entries fall back to cfg or `<not in ledger>` sentinel.
- Soft migration (Q6=A): no `kinoforge ledger migrate` helper; legacy entries age out fast.
- Sibling parity for provider construction (Q7=A): same shape as `stop`/`destroy`/`reap`.
- New `kinoforge forget --id <id>` (Q9=B): closes the recovery gap end-to-end; the advisory line in `_cmd_status` points to a real command.

**Test count:** 1198 passed + 8 skipped pre-Layer-S → ~1228 passed + 8 skipped post-Layer-S (+24 net new — T1: 4, T2: 16, T3: 4).

**Known follow-ups:**
- PROGRESS:127 (cloud-ledger CLI routing) still open. Layer S explicitly did not touch `_ledger(state_dir)` callers other than `status`/`forget`.
- Production-side `last_heartbeat` ledger persistence: `_build_ledger_block` surfaces the field when present, but `Ledger.record` does not yet write it. Future layer.
- `kinoforge status` over all ledger entries (`--all` flag) deferred.
- `kinoforge status --json` deferred.
```

Then update the "Known limitations & follow-ups" section's "Architectural follow-ups" bullet for PROGRESS:120 to flag it as CLOSED (annotate inline like prior closures); update the "Single next action" block to point at the next candidate layer.

- [ ] **Step 9: Run full test suite** to confirm everything still green

Run: `pixi run pytest -x`

Expected: All pre-Layer-S tests still green; T1/T2/T3 tests green; total ~1228 passed + 8 skipped.

- [ ] **Step 10: Run pre-commit on every touched file**

Run: `pixi run pre-commit run --files src/kinoforge/cli.py tests/test_cli.py README.md PROGRESS.md`

Expected: All hooks pass.

- [ ] **Step 11: Commit + tag PROGRESS:120 closed**

```bash
git add src/kinoforge/cli.py tests/test_cli.py README.md PROGRESS.md
git commit -m "feat(cli): kinoforge forget --id <id> + README + PROGRESS Phase 33 (Layer S T3, closes PROGRESS:120)"
```

- [ ] **Step 12: Final `--no-ff` merge** if Layer S was developed on a branch (mirrors Layer L/M/N/Q close-out pattern). If developed on `main` directly (per recent layer rhythm), skip — the three task commits stand on their own.

```json:metadata
{"files": ["src/kinoforge/cli.py", "tests/test_cli.py", "README.md", "PROGRESS.md"], "verifyCommand": "pixi run pytest tests/test_cli.py -v -k 'forget' && pixi run pytest -x", "acceptanceCriteria": ["forget --id ID removes existing entry, prints 'forgot: <id>', exit 0", "forget --id <missing> emits stderr 'not found in ledger', exit 1", "second forget on same id returns exit 1 (non-idempotent regression lock)", "_build_parser registers forget subcommand with --id flag", "README documents kinoforge status + forget operator workflow", "PROGRESS Phase 33 entry committed with T1/T2/T3 SHAs + PROGRESS:120 flipped to CLOSED"]}
```

---

## Self-review checklist

| Check | Result |
|---|---|
| Spec §1 decisions Q1-Q10 — every one covered by a task | ✓ — Q1/Q4 in T2 helper + body; Q2 in T2 control flow; Q3 in T2 print helper; Q5 in T1 schema + T2 cfg threading; Q6 in T2 helper fallback; Q7 in T2 dispatch; Q8 explicit out-of-scope in PROGRESS entry; Q9 in T3 + T2 advisory line; Q10 covered by every test |
| Spec §3 architecture — Unit 1/2/3 mapped to T1/T2/T3 | ✓ |
| Spec §4 output example — encoded in test #14 (alphabetised) + Step 8 README | ✓ |
| Spec §5 error matrix rows — one test per row in T2 | ✓ |
| Spec §6 edge cases #1-#7 — covered: #1 endpoints-raises test, #2-#4 in `_build_ledger_block` defensive `.get` + clamp tests, #5 nothing to test (json.dumps({}) = "{}"), #6 nothing to test (argparse layer), #7 in `test_cmd_forget_is_non_idempotent` | ✓ |
| Spec §7 test matrix #1-#21 — every line implemented across T1/T2/T3 (T1: #1-3; T2: #4-17; T3: #18-21) | ✓ |
| Spec §8 task slicing — three tasks, matches T1/T2/T3 | ✓ |
| Spec §9 out-of-scope — PROGRESS:127, provider construction, --json, migration helper, --all — flagged in PROGRESS Phase 33 entry | ✓ |
| Placeholder scan ("TBD", "TODO", "implement later", "add appropriate", "see plan") | clean |
| Type consistency (`_build_ledger_block` signature, `_print_status_block` signature, `_cmd_forget` signature) | ✓ — used identically wherever referenced |
| Method-name consistency (`get_instance`, `endpoints`, `entries`, `forget`, `record`) — all match existing ABC | ✓ |
| Argparse style (`--id` flag for status/stop/destroy/forget) | ✓ consistent across all four |
| Banner ↔ metadata invariant: no `userGate: true` tasks → no banner needed | ✓ no gate signal in user's brief |
