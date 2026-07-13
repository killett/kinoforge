# Modal Ephemeral Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `--ephemeral` fully supported for `(diffusers|comfyui, modal)` — opaque app naming, capability gate, ephemeral-index discovery, and sweeper reap — per spec `docs/superpowers/specs/2026-07-12-modal-ephemeral-parity-design.md`.

**Architecture:** Three phases. EM1 (Tasks 0–2): opaque `kinoforge-eph-{8hex}` app naming under STRICT_POLICY + capability-table entries + live proof of `--ephemeral --no-reuse`. EM2 (Tasks 3–5): DRY-lift the `EphemeralIndex.add` into a shared CLI helper called by generate/upscale/interpolate + live cross-CLI warm-attach proof on Modal. EM3 (Tasks 6–8): `ModalProvider.probe_runtime` (via the M5 `/util` route) + a `note_endpoints` priming seam threaded by the reaper + live sweeper-reap proof.

**Tech Stack:** kinoforge CLI, `ModalProvider` (`src/kinoforge/providers/modal/`), `EphemeralSession`/`EPHEMERAL_CAPABILITIES` (`src/kinoforge/core/ephemeral.py`), `EphemeralIndex` (`src/kinoforge/core/warm_reuse/ephemeral_index.py`), reaper/sweeper (`src/kinoforge/core/{reaper,reaper_actor,sweeper}.py`), `ModalUtilEndpoint` (`src/kinoforge/providers/modal/util.py`), pytest.

**User decisions (already made):**
- Residue contract: accept the opaque stopped-app lingering in `modal app list` (documented carve-out); no attempt at true app deletion.
- Scope: full parity (naming + capability + index + sweeper), ONE spec, phased EM1→EM2→EM3.
- Ephemeral app name LOCKED: `kinoforge-eph-{8hex}` (`secrets.token_hex(4)`).
- Index-add helper lives in the CLI layer, not the orchestrator.
- `probe_runtime` builds on the M5 Modal util probe (`GET /util`); no new in-container surface.
- EM live smokes are `--ephemeral` runs → MUST NOT be logged to `successful-generations.md` (evidence in live tests + PROGRESS only).

---

## Phase EM1 — correctness + honesty

### Task 0: Opaque Modal app name under ephemeral

**Goal:** `ModalProvider.create_instance` deploys as `kinoforge-eph-{8hex}` when an ephemeral session is active (no subcommand/timestamp/alias in the name); non-ephemeral naming byte-identical to today.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py` (imports + `create_instance`, lines ~9–13 and ~80–141)
- Test: `tests/providers/modal/test_provider.py` (append two tests)

**Acceptance Criteria:**
- [ ] Under `EphemeralSession(enabled=True)`, the deployed app name matches `^kinoforge-eph-[0-9a-f]{8}$`, and `Instance.id` == the `eph-{8hex}` token (so ledger/index/destroy all key off the opaque id).
- [ ] `_deployments` is keyed by the opaque id; `destroy_instance(opaque_id)` resolves the right app name.
- [ ] Without a session (or with `enabled=False`), name/id/behavior byte-identical to today (`kinoforge-{spec.run_id}`, `Instance.id == spec.run_id`) — existing tests stay green.

**Verify:** `pixi run test -- tests/providers/modal/test_provider.py -v` → all pass including 2 new.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/modal/test_provider.py` (reuse the existing `fake_factory`/`fake_deploy` closure pattern from `test_create_instance_deploys_and_returns_endpoint`, line 29):

```python
def test_create_instance_uses_opaque_name_under_ephemeral():
    """Bug caught: ephemeral Modal apps named kinoforge-{run_id} leak the
    subcommand + local timestamp (e.g. upscale-20260712-200409) into
    `modal app list`, where stopped apps linger forever."""
    import re

    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer

    captured = {}

    def fake_factory(req, modal_mod):
        captured["req"] = req
        return ("APP", "SERVERFN")

    def fake_deploy(app, server_fn):
        return "https://ws--kinoforge-eph-server.modal.run"

    provider = ModalProvider(app_factory=fake_factory, deployer=fake_deploy)
    spec = InstanceSpec(
        image="python:3.13-slim",
        offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
        run_id="upscale-20260712-200409",  # the leaky id ephemeral must hide
        provision_script="echo hi",
        run_cmd=["python", "-m", "server"],
        lifecycle=Lifecycle(idle_timeout_s=300),
    )
    with EphemeralSession(enabled=True):
        inst = provider.create_instance(spec)

    assert re.fullmatch(r"eph-[0-9a-f]{8}", inst.id), inst.id
    assert "upscale" not in inst.id and "2026" not in inst.id
    # the Modal app request + deployments record carry the opaque id too
    assert captured["req"].run_id == inst.id
    rec = provider._deployments[inst.id]
    assert rec["name"] == f"kinoforge-{inst.id}"


def test_create_instance_name_unchanged_without_ephemeral():
    """Bug caught: opaque naming accidentally applied to normal runs would
    break warm-attach ledger keys and every log/teardown that names the app."""
    from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer

    def fake_factory(req, modal_mod):
        return ("APP", "SERVERFN")

    def fake_deploy(app, server_fn):
        return "https://ws--kinoforge-run777-server.modal.run"

    provider = ModalProvider(app_factory=fake_factory, deployer=fake_deploy)
    spec = InstanceSpec(
        image="python:3.13-slim",
        offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
        run_id="run777",
        provision_script="echo hi",
        run_cmd=["python", "-m", "server"],
        lifecycle=Lifecycle(idle_timeout_s=300),
    )
    inst = provider.create_instance(spec)
    assert inst.id == "run777"
    assert provider._deployments["run777"]["name"] == "kinoforge-run777"
```

Note: `EphemeralSession(enabled=True)` used as a context manager restores the previous active session on exit (`__exit__`), and with no registered stores its scrub loop is a no-op — safe in unit tests.

- [ ] **Step 2: Run tests — verify the first fails**

Run: `pixi run test -- tests/providers/modal/test_provider.py -k opaque -v`
Expected: FAIL — `inst.id == "upscale-20260712-200409"` (no opaque naming yet). The `unchanged` test passes already (guards the refactor).

- [ ] **Step 3: Implement opaque naming**

In `src/kinoforge/providers/modal/__init__.py`:

Add imports (module top, after `import time`):

```python
import secrets
```

and with the other kinoforge imports:

```python
from kinoforge.core.ephemeral import EphemeralSession
```

(`kinoforge.core.ephemeral` imports only stdlib + TYPE_CHECKING types — no import cycle.)

In `create_instance`, right after the `spec.offer is None` validation (line ~98) and before `volume_mount = ...`, insert:

```python
        # Ephemeral runs must not leak the subcommand/timestamp-bearing
        # run_id into the app name: `modal app stop` only STOPS an app, and
        # stopped apps linger in `modal app list` forever. Mirror RunPod's
        # pod_name_includes_alias handling (runpod/__init__.py:814-821)
        # with an opaque token. The opaque id becomes the Instance.id so
        # ledger (memory-only), ephemeral-index, destroy and probe all key
        # off one consistent identifier.
        _eph = EphemeralSession.current()
        if _eph is not None and not _eph.policy.pod_name_includes_alias:
            app_run_id = f"eph-{secrets.token_hex(4)}"
        else:
            app_run_id = spec.run_id
```

Then replace every use of `spec.run_id` from that point down with `app_run_id`:

```python
        req = ModalAppRequest(
            run_id=app_run_id,
            ...            # all other fields unchanged
        )
        app, server_fn = self._app_factory(req, self._modal_mod())
        url = self._deployer(app, server_fn)
        self._deployments[app_run_id] = {
            "app": app,
            "url": url,
            "name": f"kinoforge-{app_run_id}",
        }
        return Instance(
            id=app_run_id,
            ...            # all other fields unchanged
        )
```

(The store-side `run_id` used for artifact namespacing + `delete_run` is a
separate orchestrator parameter and is NOT touched by this change.)

- [ ] **Step 4: Run tests — verify pass**

Run: `pixi run test -- tests/providers/modal/test_provider.py -v`
Expected: ALL pass (2 new + existing suite — `test_create_instance_deploys_and_returns_endpoint` proves non-ephemeral naming intact).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_provider.py
git commit -m "feat(modal): opaque kinoforge-eph-{8hex} app name under ephemeral (no run_id leak)"
```

```json:metadata
{"files": ["src/kinoforge/providers/modal/__init__.py", "tests/providers/modal/test_provider.py"], "verifyCommand": "pixi run test -- tests/providers/modal/test_provider.py -v", "acceptanceCriteria": ["ephemeral create names app kinoforge-eph-{8hex} and Instance.id is the eph token", "deployments keyed by opaque id; destroy resolves it", "non-ephemeral naming byte-identical (existing tests green)"], "modelTier": "standard"}
```

---

### Task 1: Capability entries + preflight message

**Goal:** `--ephemeral` passes preflight for `(diffusers, modal)` and `(comfyui, modal)`; the refusal message for still-unsupported combos names modal among the pod providers.

**Files:**
- Modify: `src/kinoforge/core/ephemeral.py:81-97` (`EPHEMERAL_CAPABILITIES`)
- Modify: `src/kinoforge/cli/_main.py:233-248` (`_preflight_error_block`)
- Test: `tests/core/test_ephemeral.py` (extend)

**Acceptance Criteria:**
- [ ] `EPHEMERAL_CAPABILITIES[("diffusers", "modal")] is True` and `[("comfyui", "modal")] is True`.
- [ ] `_preflight_error_block` output lists modal in the pod-provider alternatives.
- [ ] Existing `test_capability_table_contents` updated if it pins the full table; suite green.

**Verify:** `pixi run test -- tests/core/test_ephemeral.py -v` → pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_ephemeral.py`:

```python
def test_modal_combos_are_ephemeral_capable() -> None:
    """Bug caught: forgetting the table flip keeps --ephemeral refused on
    Modal even after the opaque-naming work shipped (preflight gates on
    this exact lookup, cli/_main.py:_preflight_ephemeral)."""
    from kinoforge.core.ephemeral import EPHEMERAL_CAPABILITIES

    assert EPHEMERAL_CAPABILITIES[("diffusers", "modal")] is True
    assert EPHEMERAL_CAPABILITIES[("comfyui", "modal")] is True
```

- [ ] **Step 2: Run — verify fail**

Run: `pixi run test -- tests/core/test_ephemeral.py -k modal_combos -v`
Expected: FAIL with `KeyError: ('diffusers', 'modal')`.

- [ ] **Step 3: Implement**

In `src/kinoforge/core/ephemeral.py`, inside `EPHEMERAL_CAPABILITIES` after the `("comfyui", "skypilot")` / `("diffusers", "skypilot")` entries add:

```python
    ("comfyui", "modal"): True,
    ("diffusers", "modal"): True,
```

In `src/kinoforge/cli/_main.py` `_preflight_error_block`, change the two pod-engine lines:

```python
        "    engine: comfyui       (any pod-based provider: runpod, skypilot, modal, local)\n"
        "    engine: diffusers     (any pod-based provider: runpod, skypilot, modal, local)\n"
```

If `test_capability_table_contents` asserts the full table (dict equality or key set), extend its expectation with the two modal entries.

- [ ] **Step 4: Run — verify pass**

Run: `pixi run test -- tests/core/test_ephemeral.py -v`
Expected: PASS (all, including any updated table test).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/ephemeral.py src/kinoforge/cli/_main.py tests/core/test_ephemeral.py
git commit -m "feat(ephemeral): enable (diffusers|comfyui, modal) in capability table + preflight text"
```

```json:metadata
{"files": ["src/kinoforge/core/ephemeral.py", "src/kinoforge/cli/_main.py", "tests/core/test_ephemeral.py"], "verifyCommand": "pixi run test -- tests/core/test_ephemeral.py -v", "acceptanceCriteria": ["both modal combos True in EPHEMERAL_CAPABILITIES", "preflight error block names modal", "capability-table test updated + green"], "modelTier": "standard"}
```

---

### Task 2: EM1 live proof — `--ephemeral --no-reuse` upscale on Modal

**Goal:** Live-prove the EM1 contract: ephemeral Modal upscale runs end-to-end with an opaque app name, no persistent kinoforge records, scrubbed store, and a surviving `output/` artifact.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_modal_ephemeral_em1.py` (RED scaffold, committed BEFORE spend)
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] RED scaffold committed before the live invocation.
- [ ] `pixi run preflight` exits 0 before spend.
- [ ] Live run exits 0; `output/*upscaled*.mp4` artifact present (output/ is the sole ephemeral-exempt zone).
- [ ] `modal app list` (via `pixi run -e live-modal modal app list`) shows the run's app as `kinoforge-eph-{8hex}`, state stopped; NO `kinoforge-upscale-<timestamp>` app from this run.
- [ ] `pixi run kinoforge list` → `No running instances.` AND `No instances recorded in ledger.` (memory-only run id — nothing to forget).
- [ ] Artifact-store side: no `<store>/<run_id>/` directory survives (store scrub ran).
- [ ] PROGRESS.md updated; NO `successful-generations.md` entry (ephemeral runs are barred from that file).

**Verify:** `pixi run kinoforge list` → no instances + empty ledger; `pixi run -e live-modal modal app list | grep kinoforge-eph` → stopped opaque app.

**Steps:**

- [ ] **Step 1: RED scaffold (commit before spend)**

Create `tests/live/test_modal_ephemeral_em1.py`:

```python
"""LIVE EM1: --ephemeral --no-reuse FlashVSR 1080p upscale on Modal. Driven
manually via the CLI; this file records the contract."""

import pytest

pytestmark = pytest.mark.live

UPSCALE_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral upscale "
    "--config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml "
    "--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_"
    "Photorealistic-cinem.mp4 "
    "--no-reuse"
)


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS EM1 entry")
def test_modal_ephemeral_em1_contract():
    raise AssertionError(
        "run UPSCALE_CMD live; assert: opaque kinoforge-eph-{8hex} app (stopped), "
        "1080x1080 artifact in output/, empty ledger, store run dir scrubbed"
    )
```

```bash
git add tests/live/test_modal_ephemeral_em1.py
git commit -m "test(live): RED scaffold for Modal ephemeral EM1 smoke (pre-spend)"
```

- [ ] **Step 2: Preflight + fixture check**

Run: `pixi run preflight` → exit 0.
Run: `ls output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` → present.

- [ ] **Step 3: Live run (background) + monitor**

Run `UPSCALE_CMD` (from the scaffold, verbatim) in the background, tee to the scratchpad. Note: `--ephemeral` is a GLOBAL flag — it precedes the `upscale` subcommand. While running, poll utilisation per CLAUDE.md (util probe or `/util` curl once the `.modal.run` URL appears in the log; the URL may be TTY-wrapped across lines and contains `--` — de-wrap before regexing, lesson from the §27 monitor miss). GPU 0% for ≥3 consecutive polls with a generation in flight → capture log, destroy, fail fast.

- [ ] **Step 4: Assert the five EM1 postconditions**

```bash
# 1. artifact survived (output/ exempt zone)
ls -t output/*upscaled*.mp4 | head -1
# 2. opaque app name, stopped
pixi run -e live-modal modal app list | rg "kinoforge-eph-"
# and NO timestamped app from this run:
pixi run -e live-modal modal app list | rg "kinoforge-upscale-2026" && echo LEAK || echo CLEAN
# 3. ledger empty
pixi run kinoforge list
# 4. store scrub: no run dir left (store root per cfg store.kind=local default)
#    inspect the artifact-store root used by the CLI ctx — expect no dir for this run id
```

Dims check on the artifact (1080×1080) via imageio as in §27 (ffprobe is not on PATH in this container).

- [ ] **Step 5: PROGRESS + commit**

Update `PROGRESS.md` (EM1 live-green line under the workstream pointer). Do NOT touch `successful-generations.md`.

```bash
git add PROGRESS.md
git commit -m "docs(progress): Modal ephemeral EM1 live-green (opaque app, scrubbed store, empty ledger)"
```

```json:metadata
{"files": ["tests/live/test_modal_ephemeral_em1.py", "PROGRESS.md"], "verifyCommand": "pixi run kinoforge list && pixi run -e live-modal modal app list | rg kinoforge-eph-", "acceptanceCriteria": ["RED scaffold committed pre-spend", "preflight exit 0", "run exit 0 + output/ artifact present", "opaque stopped app in modal app list; no timestamped app", "kinoforge list: no instances + empty ledger", "store run dir scrubbed", "PROGRESS updated; NO successful-generations entry"], "userGate": true, "tags": ["user-gate"], "gateScope": "live-smoke", "modelTier": "standard"}
```

---

## Phase EM2 — ephemeral-index parity

### Task 3: Shared `_ephemeral_index_add` helper, called by all three handlers

**Goal:** Extract the generate-only `EphemeralIndex.add` block into a CLI-layer helper and call it from `_cmd_generate`, `_cmd_upscale`, `_cmd_interpolate`, so ephemeral upscale/interpolate pods (any provider) become discoverable.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (new helper; edit `_cmd_generate` ~616–635, `_cmd_upscale` ~767–784, `_cmd_interpolate` ~868–881)
- Test: `tests/test_ephemeral_index_add_helper.py` (new)

**Acceptance Criteria:**
- [ ] Helper `_ephemeral_index_add(ctx, cfg, instance)` no-ops when no session is active or `instance is None`; adds a row (with endpoints + provider from the instance) when active.
- [ ] `_cmd_generate` behavior unchanged (inline block replaced by helper call inside the same cold-create gate).
- [ ] `_cmd_upscale` and `_cmd_interpolate` call the helper under the gate `returned_instance is not None and instance is None and not args.no_reuse`.
- [ ] AST invariant `tests/test_ephemeral_index_write_gated.py` still green (the add stays inside an `if` mentioning `EphemeralSession.current()`).

**Verify:** `pixi run test -- tests/test_ephemeral_index_add_helper.py tests/test_ephemeral_index_write_gated.py -v` → pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ephemeral_index_add_helper.py`:

```python
"""_ephemeral_index_add: session-gated, provider-agnostic index row writer."""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import Instance
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex


def _mk_instance() -> Instance:
    return Instance(
        id="eph-deadbeef",
        provider="modal",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://x--kinoforge-eph-deadbeef-y.modal.run"},
    )


def _mk_ctx_cfg(tmp_path: Path):
    """Real local store + the cheap modal cfg (cap-key/wak derivable)."""
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml"))
    ctx = SessionContext(state_dir=tmp_path)  # adapt to the real ctor —
    # mirror how tests/integration/test_ephemeral_cross_session_warm_reuse.py
    # builds its ctx/store; the helper only needs ctx.store() to work.
    return ctx, cfg


def test_helper_noops_without_session(tmp_path):
    """Bug caught: an ungated helper would leak index rows into normal runs,
    violating the ephemeral-only discovery contract (AST invariant)."""
    from kinoforge.cli._commands import _ephemeral_index_add

    ctx, cfg = _mk_ctx_cfg(tmp_path)
    _ephemeral_index_add(ctx, cfg, _mk_instance())
    assert EphemeralIndex(store=ctx.store()).rows() == []


def test_helper_indexes_modal_instance_under_session(tmp_path):
    """Bug caught: upscale/interpolate ephemeral pods invisible to the next
    CLI process (the pre-lift state: add() was inlined in _cmd_generate only)."""
    from kinoforge.cli._commands import _ephemeral_index_add

    ctx, cfg = _mk_ctx_cfg(tmp_path)
    with EphemeralSession(enabled=True):
        _ephemeral_index_add(ctx, cfg, _mk_instance())
    rows = EphemeralIndex(store=ctx.store()).rows()
    assert len(rows) == 1
    assert rows[0].provider == "modal"
    assert rows[0].endpoints["8000"].endswith(".modal.run")
    assert rows[0].id == "eph-deadbeef"


def test_helper_noops_on_none_instance(tmp_path):
    """Bug caught: hosted-path (no compute instance) upscale would crash on
    instance.endpoints."""
    from kinoforge.cli._commands import _ephemeral_index_add

    ctx, cfg = _mk_ctx_cfg(tmp_path)
    with EphemeralSession(enabled=True):
        _ephemeral_index_add(ctx, cfg, None)
    assert EphemeralIndex(store=ctx.store()).rows() == []
```

(`_mk_ctx_cfg` note: match the ctx-construction idiom used by
`tests/integration/test_ephemeral_cross_session_warm_reuse.py` — the assertion
surface is the index rows, not the ctx shape.)

- [ ] **Step 2: Run — verify fail**

Run: `pixi run test -- tests/test_ephemeral_index_add_helper.py -v`
Expected: FAIL — `ImportError: cannot import name '_ephemeral_index_add'`.

- [ ] **Step 3: Implement the helper + three call sites**

In `src/kinoforge/cli/_commands.py`, add (module level, near the other `_cfg_*` helpers):

```python
def _ephemeral_index_add(
    ctx: SessionContext, cfg: Config, instance: Instance | None
) -> None:
    """Index a surviving ephemeral pod for cross-CLI discovery.

    No-op when no EphemeralSession is active or when the run produced no
    compute instance. Under STRICT_POLICY the ledger entry lives only in
    session.in_memory_ledger; this disk row is what lets the NEXT CLI
    process discover the surviving pod (spec 2026-06-27, extended to
    upscale/interpolate + modal by spec 2026-07-12-modal-ephemeral-parity).
    """
    from kinoforge.core.warm_reuse.ephemeral_index import (
        EphemeralIndex,
        EphemeralIndexRow,
    )

    if instance is None:
        return
    if EphemeralSession.current() is not None:
        EphemeralIndex(store=ctx.store()).add(
            EphemeralIndexRow(
                id=instance.id,
                warm_attach_key=_cfg_warm_attach_key(cfg),
                kinoforge_key=cfg.capability_key().derive()[:12],
                endpoints=dict(instance.endpoints),
                provider=instance.provider,
                created_at_local=datetime.now().isoformat(),
            )
        )
```

In `_cmd_generate` (lines ~616–635): delete the inline import + `if EphemeralSession.current() ...` add-block and replace with:

```python
        _ephemeral_index_add(ctx, cfg, returned_instance)
```

(still inside the existing `if returned_instance is not None and instance is None and not single:` gate — the `emit_record_path` block below it stays).

In `_cmd_upscale`, at the END of the existing T11 stamp block (after `ledger.touch(...)`, line ~784, same indentation as `ledger.touch`):

```python
        _ephemeral_index_add(ctx, cfg, returned_instance)
```

In `_cmd_interpolate`, replace `del returned_instance` (line ~881) with:

```python
    if returned_instance is not None and instance is None and not args.no_reuse:
        _ephemeral_index_add(ctx, cfg, returned_instance)
```

(Observed while planning, NOT in scope to fix here: `_cmd_interpolate` also
never ledger-stamps its cold-created pod for non-ephemeral warm scan — a
pre-existing gap independent of ephemeral; leave as-is.)

- [ ] **Step 4: Run — verify pass (helper + invariant + regressions)**

Run: `pixi run test -- tests/test_ephemeral_index_add_helper.py tests/test_ephemeral_index_write_gated.py -v`
Expected: PASS. The AST invariant passes because the helper's `add` sits inside `if EphemeralSession.current() is not None:`.

Run: `pixi run test` (full offline suite)
Expected: green — generate-path integration tests (`tests/integration/test_ephemeral_cross_session_warm_reuse.py` etc.) unaffected.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_commands.py tests/test_ephemeral_index_add_helper.py
git commit -m "feat(ephemeral): shared _ephemeral_index_add helper; upscale/interpolate now index ephemeral pods"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "tests/test_ephemeral_index_add_helper.py"], "verifyCommand": "pixi run test -- tests/test_ephemeral_index_add_helper.py tests/test_ephemeral_index_write_gated.py -v", "acceptanceCriteria": ["helper session-gated + None-safe", "generate inline block replaced, behavior unchanged", "upscale + interpolate call helper under cold-create gate", "AST invariant green"], "modelTier": "standard"}
```

---

### Task 4: Warm-attach discovery accepts a Modal index row (offline)

**Goal:** Prove the matcher/scan path warm-attaches from a Modal EphemeralIndexRow (endpoint replay, no RunPod-proxy assumption).

**Files:**
- Test: `tests/integration/test_ephemeral_modal_row_discovery.py` (new)
- Modify (only if the test exposes a provider-specific assumption): `src/kinoforge/core/warm_reuse/matcher.py`

**Acceptance Criteria:**
- [ ] A Modal row (`provider="modal"`, `.modal.run` endpoint) added to the index is surfaced by `_scan_warm_candidates`/matcher with its endpoints replayed onto the candidate instance, health-preflight seam stubbed exactly like the existing RunPod-row test.
- [ ] Zero production changes expected; any change made is minimal and named in the commit.

**Verify:** `pixi run test -- tests/integration/test_ephemeral_modal_row_discovery.py -v` → pass.

**Steps:**

- [ ] **Step 1: Write the test (mirror the RunPod-row test)**

Copy the structure of `tests/integration/test_ephemeral_cross_session_warm_reuse.py` (the ephemeral-index scan test whose `/health` preflight seam was stubbed in commit `45c4806`). New file `tests/integration/test_ephemeral_modal_row_discovery.py` with one substantive difference — the row:

```python
row = EphemeralIndexRow(
    id="eph-cafef00d",
    warm_attach_key=<wak the mirrored test derives from its cfg>,
    kinoforge_key=<cap key12 the mirrored test derives>,
    endpoints={"8000": "https://acct--kinoforge-eph-cafef00d-fn.modal.run"},
    provider="modal",
    created_at_local="2026-07-12T21:00:00",
)
```

Assert (as the mirrored test does for RunPod): the scan returns a candidate with `id == "eph-cafef00d"` and `endpoints["8000"]` equal to the row's `.modal.run` URL (replay, not rebuild — Modal URLs are NOT rebuildable, M5 lesson `1cb4299`).

Docstring bug statement: "Bug caught: a RunPod-proxy URL-pattern assumption in the scan/preflight would make Modal ephemeral rows undiscoverable or rebuild a wrong URL."

- [ ] **Step 2: Run — expect pass (or fix the exposed gap)**

Run: `pixi run test -- tests/integration/test_ephemeral_modal_row_discovery.py -v`
Expected: PASS with zero production changes. If it FAILS on a provider-specific assumption, fix minimally in the named seam and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_ephemeral_modal_row_discovery.py
git commit -m "test(ephemeral): modal index row discovered + endpoint-replayed by warm scan"
```

```json:metadata
{"files": ["tests/integration/test_ephemeral_modal_row_discovery.py"], "verifyCommand": "pixi run test -- tests/integration/test_ephemeral_modal_row_discovery.py -v", "acceptanceCriteria": ["modal row surfaced by warm scan with endpoints replayed verbatim", "zero (or minimal, named) production change"], "modelTier": "standard"}
```

---

### Task 5: EM2 live proof — bare `--ephemeral` cross-CLI warm-attach on Modal

**Goal:** Live-prove two consecutive bare-`--ephemeral` Modal runs where the second warm-attaches the first's live app via the ephemeral index; then tear down explicitly.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_modal_ephemeral_em2_warm_attach.py` (RED scaffold, pre-spend)
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] RED scaffold committed before spend; `pixi run preflight` exit 0.
- [ ] RUN 1 (bare `--ephemeral` generate, cheap 1.3B/A10 cfg, standard prompt file): exits 0, artifact in output/, app live after exit, index row present.
- [ ] RUN 2 (same cfg/prompt, separate CLI process): log shows warm-attach to RUN 1's app (no new deploy); wall-clock decisively below RUN 1's.
- [ ] Teardown: `pixi run kinoforge destroy --id <eph-id>` (or sweeper) stops the app; `pixi run -e live-modal modal app list` shows it stopped; index row removed; `pixi run kinoforge list` clean.
- [ ] PROGRESS updated; NO successful-generations entry.

**Verify:** RUN 2 log contains the warm-attach line naming RUN 1's `eph-` id; post-teardown `kinoforge list` → no instances + empty ledger.

**Steps:**

- [ ] **Step 1: RED scaffold (commit before spend)**

Create `tests/live/test_modal_ephemeral_em2_warm_attach.py`:

```python
"""LIVE EM2: bare --ephemeral cross-CLI warm-attach on Modal (Wan 2.1 1.3B/A10).
Driven manually via the CLI; this file records the contract."""

import pytest

pytestmark = pytest.mark.live

GEN_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral generate "
    "--config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)"'
)
# Run GEN_CMD twice as SEPARATE processes. Expect run 2 to warm-attach run 1's
# kinoforge-eph-{8hex} app via the ephemeral index (no new deploy).


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS EM2 entry")
def test_modal_ephemeral_em2_contract():
    raise AssertionError(
        "run GEN_CMD twice; assert run2 warm-attaches run1's eph app "
        "(no deploy, faster), then destroy + verify stopped + index row gone"
    )
```

```bash
git add tests/live/test_modal_ephemeral_em2_warm_attach.py
git commit -m "test(live): RED scaffold for Modal ephemeral EM2 warm-attach smoke (pre-spend)"
```

- [ ] **Step 2: Preflight, RUN 1, evidence**

`pixi run preflight` → 0. Run `GEN_CMD`; record wall-clock + the `eph-` id from the log; after exit confirm the app is live (`modal app list` state deployed) and the index row exists (`kinoforge list` should surface ephemeral-index pods per `b28311a`; otherwise read the index file through the store root).

- [ ] **Step 3: RUN 2, warm-attach evidence**

Run `GEN_CMD` again (new process). Capture the warm-attach log line naming RUN 1's id and the wall-clock delta. Poll util during both runs per CLAUDE.md.

- [ ] **Step 4: Teardown + verify**

```bash
pixi run kinoforge destroy --id <eph-id>
pixi run kinoforge list
pixi run -e live-modal modal app list | rg "kinoforge-eph-"
```

Expect: no instances + empty ledger; the app state stopped; index row removed (destroy path calls `ephemeral_index.remove` via `lifecycle.py:794-795`).

- [ ] **Step 5: PROGRESS + commit**

```bash
git add PROGRESS.md
git commit -m "docs(progress): Modal ephemeral EM2 live-green (cross-CLI warm-attach via index)"
```

```json:metadata
{"files": ["tests/live/test_modal_ephemeral_em2_warm_attach.py", "PROGRESS.md"], "verifyCommand": "rg 'warm' <run2 log> && pixi run kinoforge list", "acceptanceCriteria": ["RED scaffold pre-spend + preflight 0", "run1 leaves live app + index row", "run2 warm-attaches run1 app (log line + faster wall-clock)", "explicit destroy -> app stopped + index row gone + ledger clean", "PROGRESS updated; NO successful-generations entry"], "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["run1", "cold"], ["run2", "warm-attach"]], "gateScope": "live-smoke", "modelTier": "standard"}
```

---

## Phase EM3 — sweeper reap parity

### Task 6: `ModalProvider.probe_runtime` + `note_endpoints` + reaper threading

**Goal:** Give Modal the runtime-probe substrate: app existence from `list_instances`, util from the M5 `/util` route, with the reaper priming the `.modal.run` URL from the index row's endpoints (Modal URLs are not rebuildable cross-process).

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py` (new `note_endpoints` + `probe_runtime`; import `datetime`, `RuntimeProbe`, `ModalUtilEndpoint`)
- Modify: `src/kinoforge/core/reaper_actor.py:380-390` (`_probe_with_cache` primes the seam)
- Test: `tests/providers/modal/test_probe_runtime.py` (new)

**Acceptance Criteria:**
- [ ] App absent from active list → `RuntimeProbe(found=False, …)` (→ GC_404 downstream).
- [ ] App active + `/util` snapshot → `found=True`, `gpu_util_pct`/`cpu_pct`/`container_uptime_s` mapped field-for-field from `UtilSnapshot`, `cost_per_hr=None`.
- [ ] App active + no known URL → `found=True`, util fields None, `error` set (no false reap).
- [ ] App active + `/util` raises (`TransportError`) → caught: `found=True`, util None, `error` set.
- [ ] Lister failure propagates (reaper converts to `PROBE_FAILED`; probe never fabricates `found=False`).
- [ ] `note_endpoints(id, endpoints)` primes the URL cache; `_probe_with_cache` calls it when present (duck-typed; RunPod unaffected).

**Verify:** `pixi run test -- tests/providers/modal/test_probe_runtime.py -v` → pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/modal/test_probe_runtime.py`:

```python
"""ModalProvider.probe_runtime: found/absent/partial mapping + URL priming."""

from __future__ import annotations

import pytest

from kinoforge.providers.modal import ModalProvider


def _active_lister(name: str):
    return lambda: [{"description": name, "state": "deployed"}]


def test_absent_app_probes_found_false():
    """Bug caught: a dead app never GC'd (row loops PROBE_FAILED/LIVE forever)."""
    provider = ModalProvider(lister=lambda: [])
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None
    assert probe.found is False
    assert probe.gpu_util_pct is None


def test_active_app_with_primed_url_maps_util_snapshot(monkeypatch):
    """Bug caught: field mismatch UtilSnapshot->RuntimeProbe silently breaks
    STALL_REAP thresholds (gpu/cpu None or swapped)."""
    from kinoforge.core.util_endpoints import UtilSnapshot

    provider = ModalProvider(lister=_active_lister("kinoforge-eph-cafef00d"))
    provider.note_endpoints(
        "eph-cafef00d", {"8000": "https://a--kinoforge-eph-cafef00d-f.modal.run"}
    )

    snap = UtilSnapshot(
        gpu_util_percent=87.0,
        cpu_percent=42.0,
        memory_percent=10.0,
        disk_percent=None,
        uptime_seconds=321,
    )
    monkeypatch.setattr(
        "kinoforge.providers.modal.util.ModalUtilEndpoint.read_util",
        lambda self, instance_id: snap,
    )
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None and probe.found is True
    assert probe.gpu_util_pct == 87.0
    assert probe.cpu_pct == 42.0
    assert probe.container_uptime_s == 321.0
    assert probe.cost_per_hr is None


def test_active_app_without_url_is_partial_probe():
    """Bug caught: probe fabricating found=False (or raising) when the URL is
    unknown would GC a LIVE app's row / spam PROBE_FAILED."""
    provider = ModalProvider(lister=_active_lister("kinoforge-eph-cafef00d"))
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None and probe.found is True
    assert probe.gpu_util_pct is None
    assert probe.error  # names the missing-URL condition


def test_util_transport_error_is_partial_probe(monkeypatch):
    """Bug caught: a flaky /util 5xx crashing the probe -> PROBE_FAILED noise
    instead of a conservative partial probe."""
    from kinoforge.core.errors import TransportError

    provider = ModalProvider(lister=_active_lister("kinoforge-eph-cafef00d"))
    provider.note_endpoints(
        "eph-cafef00d", {"8000": "https://a--kinoforge-eph-cafef00d-f.modal.run"}
    )

    def _boom(self, instance_id):
        raise TransportError("modal /util returned HTTP 502")

    monkeypatch.setattr(
        "kinoforge.providers.modal.util.ModalUtilEndpoint.read_util", _boom
    )
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None and probe.found is True
    assert probe.error and "502" in probe.error


def test_lister_failure_propagates():
    """Bug caught: swallowing a lister crash into found=False would GC live
    rows on a transient `modal app list` failure."""

    def _broken():
        raise RuntimeError("modal CLI absent")

    provider = ModalProvider(lister=_broken)
    with pytest.raises(RuntimeError):
        provider.probe_runtime("eph-cafef00d")
```

- [ ] **Step 2: Run — verify fail**

Run: `pixi run test -- tests/providers/modal/test_probe_runtime.py -v`
Expected: FAIL — `AttributeError: note_endpoints` (base `probe_runtime` returns None → first assert fails too).

- [ ] **Step 3: Implement provider methods**

In `src/kinoforge/providers/modal/__init__.py` add imports:

```python
from datetime import datetime
from collections.abc import Mapping
from kinoforge.core.runtime_probe import RuntimeProbe
```

Add methods to `ModalProvider` (after `destroy_instance`):

```python
    # -- sweeper-ephemeral-reap substrate ------------------------------------
    def note_endpoints(self, instance_id: str, endpoints: Mapping[str, str]) -> None:
        """Prime the URL cache for a cross-process probe (reaper seam).

        Modal ``.modal.run`` URLs are NOT rebuildable from the app name
        (M5 lesson, commit 1cb4299) — in the sweeper process the only
        source is the EphemeralIndexRow's persisted endpoints, threaded
        here by ``reaper_actor._probe_with_cache``. Never overwrites a
        live deployment record.
        """
        url = endpoints.get("8000")
        if url and instance_id not in self._deployments:
            self._deployments[instance_id] = {
                "app": None,
                "url": url,
                "name": f"kinoforge-{instance_id}",
            }

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        """Live runtime probe: app existence + /util snapshot.

        Outcomes:
          * app not in the active list → ``found=False`` (reaper: GC_404)
          * active, URL known, /util ok → fully populated probe
          * active, URL unknown or /util raised → ``found=True`` with util
            fields None + ``error`` set (partial probe — conservative, the
            reaper cannot false-reap on it)

        A lister failure PROPAGATES (reaper classifies PROBE_FAILED) —
        fabricating ``found=False`` there would GC rows of live apps.
        """
        from kinoforge.providers.modal.util import ModalUtilEndpoint

        now_local = datetime.now().isoformat()
        active_ids = {inst.id for inst in self.list_instances()}
        if pod_id not in active_ids:
            return RuntimeProbe(
                pod_id=pod_id,
                found=False,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
            )
        rec = self._deployments.get(pod_id)
        url = rec.get("url") if rec else None
        if not url:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
                error="no endpoint known for /util (note_endpoints not primed)",
            )
        try:
            snapshot = ModalUtilEndpoint(
                resolve_endpoint=lambda _id: url
            ).read_util(pod_id)
        except Exception as exc:  # noqa: BLE001 — TransportError et al.
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
                error=f"{type(exc).__name__}: {exc}",
            )
        if snapshot is None:  # /util 404 — route gone though app active
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
                error="/util returned 404 on an active app",
            )
        return RuntimeProbe(
            pod_id=pod_id,
            found=True,
            container_uptime_s=float(snapshot.uptime_seconds)
            if snapshot.uptime_seconds is not None
            else None,
            gpu_util_pct=snapshot.gpu_util_percent,
            cpu_pct=snapshot.cpu_percent,
            cost_per_hr=None,
            probed_at_local=now_local,
        )
```

- [ ] **Step 4: Thread endpoints in the reaper**

In `src/kinoforge/core/reaper_actor.py` `_probe_with_cache` (line ~384), inside the `try`, before the probe call:

```python
    try:
        note = getattr(provider, "note_endpoints", None)
        if note is not None and row.endpoints:
            note(row.id, row.endpoints)
        result: RuntimeProbe | None | str = provider.probe_runtime(row.id)
```

(RunPod/SkyPilot/Local have no `note_endpoints` → `getattr` yields None → unchanged.)

- [ ] **Step 5: Run — verify pass**

Run: `pixi run test -- tests/providers/modal/test_probe_runtime.py tests/providers/modal/test_provider.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/modal/__init__.py src/kinoforge/core/reaper_actor.py tests/providers/modal/test_probe_runtime.py
git commit -m "feat(modal): probe_runtime + note_endpoints reaper seam (sweeper-ephemeral-reap substrate)"
```

```json:metadata
{"files": ["src/kinoforge/providers/modal/__init__.py", "src/kinoforge/core/reaper_actor.py", "tests/providers/modal/test_probe_runtime.py"], "verifyCommand": "pixi run test -- tests/providers/modal/test_probe_runtime.py -v", "acceptanceCriteria": ["absent app -> found=False", "active+util -> full field mapping", "unknown URL / util raise / util 404 -> partial probe with error, found=True", "lister failure propagates", "reaper primes note_endpoints duck-typed"], "modelTier": "standard"}
```

---

### Task 7: Sweeper end-to-end classification of Modal ephemeral rows (offline)

**Goal:** Prove a Modal EphemeralIndexRow flows sweep → probe → verdict → action: GC_404 for a gone app; STALL_REAP → `destroy_instance` for a persistently idle one.

**Files:**
- Test: `tests/integration/test_sweeper_reaps_ephemeral_modal.py` (new)

**Acceptance Criteria:**
- [ ] Row whose app is absent → verdict GC_404 → row removed, NO `destroy_instance` call.
- [ ] Row whose app is active with GPU/CPU below stall thresholds for the required consecutive samples → STALL_REAP → `destroy_instance("eph-…")` called → row removed.
- [ ] Zero production changes (Task 6 provided the substrate; `provider_util_supported("modal")` is already True from `35c2068`).

**Verify:** `pixi run test -- tests/integration/test_sweeper_reaps_ephemeral_modal.py -v` → pass.

**Steps:**

- [ ] **Step 1: Write the test**

Mirror `tests/integration/test_sweeper_reaps_ephemeral_stall.py` (the RunPod version) — same store/threshold/stall-history scaffolding, two substantive differences: the index row has `provider="modal"` + a `.modal.run` endpoint, and the registry factory is monkeypatched to return a `ModalProvider` built with a fake lister (controls active/absent) and a monkeypatched `ModalUtilEndpoint.read_util` returning idle snapshots (gpu 0.0 / cpu 0.0). Two tests:

```python
def test_gone_modal_app_row_is_gc404(...):
    """Bug caught: Modal rows stuck at SKIP_NO_PROBE forever (no probe
    substrate) — dead rows accumulate and the index never converges."""
    # fake lister returns []; sweep once; assert row removed and the fake
    # stopper was NOT called.

def test_idle_modal_app_is_stall_reaped(...):
    """Bug caught: an orphaned bare-ephemeral Modal app idling forever,
    invisible to kinoforge (memory-only ledger) and never reaped."""
    # fake lister returns the active app; read_util -> 0%/0% snapshots;
    # feed enough sweep ticks to fill stall_history (required =
    # ceil(stall_window_s / heartbeat_interval_s), reaper.py:311);
    # assert destroy fired (fake stopper called with "kinoforge-eph-...")
    # and the row was removed.
```

Use the RunPod test's exact mechanism for driving `SweeperLoop`/`sweep` ticks and thresholds; assert against the fake `stopper` seam of `ModalProvider` (constructor-injected) rather than log text.

- [ ] **Step 2: Run — verify pass**

Run: `pixi run test -- tests/integration/test_sweeper_reaps_ephemeral_modal.py -v`
Expected: PASS with zero production changes (if classification or routing fails, the gap is real — fix in the named seam and document in the commit).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_sweeper_reaps_ephemeral_modal.py
git commit -m "test(sweeper): modal ephemeral rows GC_404 + STALL_REAP end-to-end (offline)"
```

```json:metadata
{"files": ["tests/integration/test_sweeper_reaps_ephemeral_modal.py"], "verifyCommand": "pixi run test -- tests/integration/test_sweeper_reaps_ephemeral_modal.py -v", "acceptanceCriteria": ["gone app -> GC_404, row removed, no destroy", "idle app -> STALL_REAP, destroy called, row removed"], "modelTier": "standard"}
```

---

### Task 8: EM3 live proof — sweeper reaps an idle ephemeral Modal app

**Goal:** Live-prove the reap: a bare-`--ephemeral` Modal app left idle is stall-reaped by the sweeper (destroy fired, index row gone, `modal app list` shows stopped).

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_modal_ephemeral_em3_sweeper.py` (RED scaffold, pre-spend)
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] RED scaffold committed before spend; `pixi run preflight` exit 0.
- [ ] Bare `--ephemeral` 1.3B generate exits 0 leaving a live `kinoforge-eph-{8hex}` app + index row.
- [ ] Sweeper (or `kinoforge reap` one-shot loop with stall-tight thresholds, mirroring `tests/live/test_runpod_ephemeral_sweeper_smoke.py`) observes idle GPU and reaps: destroy fired, app state stopped in `modal app list`, index row removed.
- [ ] `pixi run kinoforge list` → no instances + empty ledger afterward.
- [ ] PROGRESS updated (EM3 + workstream CLOSED line); NO successful-generations entry.

**Verify:** sweeper log shows `STALL_REAP` (or `IDLE/OVERAGE_REAP`) for the `eph-` id; `modal app list` → stopped; index row gone.

**Steps:**

- [ ] **Step 1: RED scaffold (commit before spend)**

Create `tests/live/test_modal_ephemeral_em3_sweeper.py`:

```python
"""LIVE EM3: sweeper reaps an idle bare---ephemeral Modal app. Driven manually
via the CLI; this file records the contract."""

import pytest

pytestmark = pytest.mark.live

GEN_CMD = (
    "pixi run -e live-modal kinoforge --ephemeral generate "
    "--config examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)"'
)
# After GEN_CMD exits (app idle), run the sweeper with stall-tight thresholds
# (mirror tests/live/test_runpod_ephemeral_sweeper_smoke.py mechanics) and
# assert the eph app is reaped.


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS EM3 entry")
def test_modal_ephemeral_em3_contract():
    raise AssertionError(
        "run GEN_CMD, leave idle, run sweeper; assert STALL/IDLE reap of the "
        "eph app: destroy fired, modal app list -> stopped, index row gone"
    )
```

```bash
git add tests/live/test_modal_ephemeral_em3_sweeper.py
git commit -m "test(live): RED scaffold for Modal ephemeral EM3 sweeper-reap smoke (pre-spend)"
```

- [ ] **Step 2: Preflight + generate + idle**

`pixi run preflight` → 0. Run `GEN_CMD`; record the `eph-` id; confirm app live + index row present after exit.

- [ ] **Step 3: Sweep + observe reap**

Drive the sweeper against the idle app using the same invocation shape as the RunPod EM smoke (`tests/live/test_runpod_ephemeral_sweeper_smoke.py`) with thresholds tight enough to trip within a few ticks (idle GPU = 0% per the M5 util-probe live evidence). Capture the verdict log line. Bounded wait — if no reap within the configured window, capture state, destroy manually, and fail the smoke (do NOT report green).

- [ ] **Step 4: Verify postconditions**

```bash
pixi run -e live-modal modal app list | rg "kinoforge-eph-"   # stopped
pixi run kinoforge list                                        # clean
```

Index row gone (surface via `kinoforge list` ephemeral section or the index file).

- [ ] **Step 5: PROGRESS + commit**

Update `PROGRESS.md`: EM3 live-green + Modal-ephemeral-parity workstream CLOSED; RESUME SNAPSHOT + SINGLE NEXT ACTION refreshed. NO successful-generations entry.

```bash
git add PROGRESS.md
git commit -m "docs(progress): Modal ephemeral EM3 live-green (sweeper reap) — parity workstream closed"
```

```json:metadata
{"files": ["tests/live/test_modal_ephemeral_em3_sweeper.py", "PROGRESS.md"], "verifyCommand": "pixi run kinoforge list && pixi run -e live-modal modal app list | rg kinoforge-eph-", "acceptanceCriteria": ["RED scaffold pre-spend + preflight 0", "bare ephemeral run leaves live eph app + index row", "sweeper verdict reaps it (log captured)", "modal app list stopped + index row gone + ledger clean", "PROGRESS updated + workstream closed; NO successful-generations entry"], "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["live", "deployed", "idle"], ["reaped", "stopped", "STALL_REAP", "OVERAGE_REAP"]], "gateScope": "live-smoke", "modelTier": "standard"}
```

---

## Self-Review

**Spec coverage:** EM1-A → Task 0; EM1-B → Task 1; EM1 exit criteria → Task 2. EM2-C → Task 3; EM2-D → Task 4; EM2 exit criteria → Task 5. EM3-E → Task 6; EM3-F → Task 7; EM3 exit criteria → Task 8. Residue decision → documented in Task 2 assertions (stopped opaque app accepted). "No successful-generations entry" rule → embedded in Tasks 2/5/8. Spec's error-handling section → Task 6 tests (partial probes, lister propagation — the plan refines spec's "never raises" to "util leg never raises; lister leg propagates so the reaper classifies PROBE_FAILED instead of the probe fabricating found=False"; same conservative intent, now explicit).

**Placeholder scan:** No TBD/TODO. Tasks 4 and 7 mirror named existing tests (`test_ephemeral_cross_session_warm_reuse.py`, `test_sweeper_reaps_ephemeral_stall.py`) with the substantive deltas spelled out — the mirroring is deliberate (those scaffolds are store/ctx-heavy; duplicating them into the plan would drift), and each delta (row shape, provider factory, assertions) is stated. Task 3's `_mk_ctx_cfg` names its reference idiom.

**Type consistency:** `_ephemeral_index_add(ctx, cfg, instance)` consistent across Tasks 3–5. `note_endpoints(instance_id, endpoints)` + `probe_runtime(pod_id)` consistent across Tasks 6–8 and match `RuntimeProbe` (`core/runtime_probe.py:31-38`) and `UtilSnapshot` (`core/util_endpoints.py`) field names verified against source. Opaque id format `eph-[0-9a-f]{8}` consistent across Tasks 0/2/5/6/7/8.
