# Modal Milestone 5 — Warm-reuse + HF-cache live proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove, live on the cheap Wan 2.1 1.3B / A10 config, that Modal supports cross-CLI warm-reuse (a second `kinoforge generate` attaches to the still-alive container) and HF Volume weight-caching (a fresh container skips the download), closing the last two open Modal threads.

**Architecture:** Verify-then-prove. The warm-reuse machinery (`core/warm_reuse/`) is already provider-agnostic and the `HF_HOME=/cache/hf` + named-Volume wiring already ships; the unknown is whether a *Modal* instance flows through the index-write + provider-resolution path. Task 0 characterizes that offline and closes any gap with a small provider-agnostic patch. Tasks 1–2 add a pre-spend RED live scaffold and the controller-driven 3-run live proof. No new provider abstraction.

**Tech Stack:** Python 3.13, Modal SDK (`live-modal` env), diffusers `wan_t2v_server`, the `core/warm_reuse/` EphemeralIndex + matcher, pytest.

**User decisions (already made):**
- **Proof scope: both** warm-attach AND cold-cache (the two halves of the two open threads). Selected 2026-07-12.
- **Model tier: Wan 2.1 1.3B / A10** throughout — reuse `examples/configs/modal-wan-t2v-1_3b.yaml` (§22). Cheap; the cache time-delta is modest at 1.3B, so the proof is the download step's presence/absence, not wall-clock. Selected 2026-07-12.
- **Run C via destroy-then-fresh-deploy** (deterministic; the real preemption-recovery scenario), not waiting out the 20-min scaledown. Selected 2026-07-12.
- **Phase-0 write-side gap fix is in-scope** for this milestone, not split out. Selected 2026-07-12.

Spec: `docs/superpowers/specs/2026-07-12-modal-milestone5-warm-reuse-hf-cache-design.md`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py` (create) | Offline: a `provider="modal"` index row round-trips write→discover; `registry.get_provider("modal")` resolves on the attach path | 0 |
| `src/kinoforge/cli/_commands.py` (modify, only if Task 0 surfaces a gap) | Self-register the modal provider on the generate/warm-attach path (mirror the runpod/skypilot imports) | 0 |
| `tests/live/test_modal_warm_reuse_hf_cache.py` (create) | RED live scaffold (`pytest.mark.live`), the 3-run sequence documented, committed before spend | 1 |
| `successful-generations.md`, `PROGRESS.md` (modify) | Log §26 + snapshot after the live proof | 2 |

Ordering: **0 → 1 → 2 (live)**. Task 1 documents the exact CLI sequence Task 0's cfg enables; Task 2 needs both.

---

### Task 0: Offline — Modal warm-reuse write→discover round-trip + provider registration

**Goal:** An offline test proving a Modal-provider instance round-trips through the `EphemeralIndex` (write on run 1 → discover on run 2, carrying its `.modal.run` endpoint) and that `registry.get_provider("modal")` resolves on the warm-attach path; close any Modal-specific gap the test surfaces with the smallest provider-agnostic patch.

**Files:**
- Create: `tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py`
- Modify (only if a gap is found): `src/kinoforge/cli/_commands.py` (around lines 479-480, the provider self-register block)

**Acceptance Criteria:**
- [ ] A `provider="modal"` `EphemeralIndexRow` with `endpoints={"8000": "https://…modal.run"}` written under an `EphemeralSession` is discoverable via `EphemeralIndex.rows_by_kinoforge_key(cap12)` after the session tears down, and the discovered row preserves the exact `.modal.run` endpoint (proves the URL survives the cross-process boundary — it is NOT rebuildable from ports like RunPod's proxy URL).
- [ ] `kinoforge.core.registry.get_provider("modal")` returns a `ModalProvider` instance (proves the provider is resolvable by name on the attach path).
- [ ] The `_cmd_generate` provider self-register block (`_commands.py:479-480`, currently `runpod` + `skypilot` only) includes `modal`, OR the implementer has confirmed + documented that modal self-registration already happens on the warm-attach path via another import. If it does not, add `import kinoforge.providers.modal  # noqa: F401 — self-register`.
- [ ] Full offline suite stays green.

**Verify:** `pixi run pytest tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the round-trip + registration test** `tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py`. This mirrors `tests/integration/test_ephemeral_cross_session_warm_reuse.py` (the proven RunPod round-trip) but pins the Modal specifics — a `.modal.run` endpoint (non-deterministic, index-carried) and modal provider resolution:

```python
"""Offline: a Modal warm-reuse instance round-trips write->discover.

Milestone 5. The index write (cli/_commands.py:625, gated on an active
EphemeralSession) is provider-agnostic; this pins that a provider="modal"
row survives the cross-process boundary carrying its .modal.run endpoint
(which — unlike RunPod's proxy URL — cannot be rebuilt from ports, so the
stored endpoint is the ONLY recovery path), and that the modal provider is
resolvable by name on the attach side.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.registry import get_provider
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.providers.modal import ModalProvider
from kinoforge.stores.local import LocalArtifactStore

_MODAL_URL = "https://emmykillett--kinoforge-generate-x-build-27e651.modal.run"
_CAP = "cap0mod4l789"


def test_modal_index_row_roundtrips_with_modal_run_endpoint(tmp_path: Path) -> None:
    # Bug caught: a Modal row that loses its .modal.run endpoint across the
    # process boundary -> run 2 cold-boots (URL is not port-rebuildable).
    store = LocalArtifactStore(tmp_path)

    # Process #1: write the Modal discovery row under an EphemeralSession.
    with EphemeralSession(enabled=True):
        EphemeralIndex(store=store).add(
            EphemeralIndexRow(
                id="modal-run-1",
                warm_attach_key="wak-modal",
                kinoforge_key=_CAP,
                endpoints={"8000": _MODAL_URL},
                provider="modal",
                created_at_local="2026-07-12T10:00:00",
            )
        )

    # Process #2: fresh session; disk index survives; discover by cap key.
    with EphemeralSession(enabled=True):
        rows = EphemeralIndex(store=store).rows_by_kinoforge_key(_CAP)

    assert len(rows) == 1, f"expected 1 modal discovery row, got {len(rows)}"
    assert rows[0].id == "modal-run-1"
    assert rows[0].provider == "modal"
    assert rows[0].endpoints == {"8000": _MODAL_URL}


def test_modal_provider_resolves_by_name() -> None:
    # Bug caught: the warm-attach path calls registry.get_provider(cfg
    # .compute.provider); if "modal" is not registered there it raises and
    # the attach dies. Importing kinoforge.providers.modal self-registers it.
    provider = get_provider("modal")
    assert isinstance(provider, ModalProvider)
```

- [ ] **Step 2: Run — confirm the round-trip test passes and the registration test's status.** `pixi run pytest tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py -v`. The round-trip test should pass (the index is provider-agnostic). The registration test tells you whether `get_provider("modal")` already works from a bare import.

- [ ] **Step 3: Investigate the warm-attach provider-registration path.** The generate command self-registers providers for preflight at `src/kinoforge/cli/_commands.py:479-480`:

```python
        import kinoforge.providers.runpod  # noqa: F401 — self-register
        import kinoforge.providers.skypilot  # noqa: F401 — self-register
```

Modal is absent. Confirm whether a Modal warm-attach (`_resolve_warm_instance` / the B3 `_scan_warm_candidates` discovery path, which call `kinoforge.core.registry.get_provider(cfg.compute.provider)`) can resolve `"modal"` without it. Grep for existing modal self-registration on the generate path: `rg -n "import kinoforge.providers.modal" src/kinoforge/cli/ src/kinoforge/core/`. If the only registration is inside `_cmd_generate`'s cold-create branch (not reachable on the warm-attach branch), that is the gap.

- [ ] **Step 4: If a gap exists, close it (minimal, provider-agnostic).** Add the modal self-register next to the others so it is registered on every generate invocation (warm-attach included):

```python
        import kinoforge.providers.modal  # noqa: F401 — self-register
        import kinoforge.providers.runpod  # noqa: F401 — self-register
        import kinoforge.providers.skypilot  # noqa: F401 — self-register
```

(Alphabetical keeps the block tidy. If the block is guarded by `--skip-preflight` such that warm-attach could bypass it, hoist the modal import to the top of `_cmd_generate` instead — verify which branch the warm-attach path actually takes before choosing.)

- [ ] **Step 5: Run — confirm PASS + no regressions.** `pixi run pytest tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py tests/cli/test_resolve_warm_instance_endpoints.py -q` → PASS. Then the broader guard: `pixi run test 2>&1 | tail -3` → `0 failed`.

- [ ] **Step 6: Commit:**

```bash
pixi run pre-commit run --files tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py src/kinoforge/cli/_commands.py
git add tests/core/warm_reuse/test_modal_warm_reuse_roundtrip.py src/kinoforge/cli/_commands.py
git commit -m "test(warm-reuse): Modal index round-trip + provider self-register on generate path"
```

(If Task 0 required NO code change, drop `src/kinoforge/cli/_commands.py` from the `add`/commit and use message `test(warm-reuse): characterize Modal warm-reuse index round-trip + provider resolution`.)

---

### Task 1: RED live scaffold (committed before any spend)

**Goal:** A `pytest.mark.live` scaffold documenting the exact 3-run warm-reuse + cold-cache sequence, committed BEFORE the live spend (durability rule) so a mid-spend crash never loses it.

**Files:**
- Create: `tests/live/test_modal_warm_reuse_hf_cache.py`

**Acceptance Criteria:**
- [ ] Marked `pytest.mark.live` so `pixi run test` (`-m 'not live'`) DESELECTS it (offline suite stays green).
- [ ] Documents the three CLI invocations (cold → warm-attach → destroy → cold-cache) as constants and carries an `xfail` contract body (mirrors the M4 scaffold `tests/live/test_modal_rife_60fps.py`).

**Verify:** `pixi run pytest tests/live/test_modal_warm_reuse_hf_cache.py -q` (default env) → `1 xfailed` (or deselected under `-m 'not live'`); it must NOT run live.

**Steps:**

- [ ] **Step 1: Read the M4 scaffold** to mirror the house style exactly: `sed -n '1,40p' tests/live/test_modal_rife_60fps.py` (module-level `pytestmark = pytest.mark.live`, CLI-invocation string constants, an `@pytest.mark.xfail` contract test raising `AssertionError`).

- [ ] **Step 2: Write** `tests/live/test_modal_warm_reuse_hf_cache.py`:

```python
"""LIVE Milestone 5: Modal warm-reuse + HF Volume cache on Wan 2.1 1.3B / A10.

Driven manually via the CLI; this file records the 3-run contract. Mirrors
the M4 RIFE live scaffold (tests/live/test_modal_rife_60fps.py). Marked
`live` so the default suite (`-m 'not live'`) skips it.

Sequence (all separate CLI invocations; default warm-reuse, NO --no-reuse
until teardown):
  RUN_A  cold boot, deploy, fetch 1.3B weights -> Volume, write index row.
  RUN_B  within the idle window -> warm-attach to RUN_A's live container
         (NO new image build, NO new deploy URL, wall-clock << RUN_A).
  destroy the app (named Volume survives), then:
  RUN_C  fresh deploy that SKIPS the weight download (weights already on
         /cache/hf); fresh boot present, download absent, boot < RUN_A.
Teardown: destroy + verify `kinoforge list` and `modal app list` clean.
"""

import pytest

pytestmark = pytest.mark.live

_CFG = "examples/configs/modal-wan-t2v-1_3b.yaml"

RUN_A_COLD = (
    "pixi run -e live-modal kinoforge generate "
    f"--config {_CFG} "
    "--prompt-file examples/configs/prompts/field-realistic.txt"
)
RUN_B_WARM = (
    "pixi run -e live-modal kinoforge generate "
    f"--config {_CFG} "
    "--prompt-file examples/configs/prompts/field-dreamlike.txt"
)
DESTROY = "pixi run kinoforge destroy --id <run-a-app-id>"
RUN_C_COLD_CACHE = (
    "pixi run -e live-modal kinoforge generate "
    f"--config {_CFG} "
    "--prompt-file examples/configs/prompts/forest.txt --no-reuse"
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations §26"
)
def test_modal_warm_reuse_hf_cache_contract():
    raise AssertionError(
        "run RUN_A_COLD -> RUN_B_WARM (assert warm-attach, no redeploy, "
        "faster) -> DESTROY -> RUN_C_COLD_CACHE (assert fresh boot skips "
        "the weight download); frame-QA the distinct prompts"
    )
```

Confirm the prompt files exist: `ls examples/configs/prompts/field-realistic.txt examples/configs/prompts/field-dreamlike.txt examples/configs/prompts/forest.txt` (they back the §20 4-prompt matrix). If a filename differs, correct the constant to the real path; do not invent one. Also confirm the CLI flag is `--prompt-file` for `kinoforge generate`: `pixi run kinoforge generate --help 2>&1 | rg -i "prompt"` — if the flag differs (e.g. `--prompt`), fix all three constants.

- [ ] **Step 3: Confirm the offline suite skips it:** `pixi run test 2>&1 | tail -3` → `0 failed`, file deselected; and `pixi run pytest tests/live/test_modal_warm_reuse_hf_cache.py -q 2>&1 | tail -3` → `1 xfailed` (no `kinoforge` subprocess spawned).

- [ ] **Step 4: Commit the RED scaffold BEFORE any spend:**

```bash
pixi run pre-commit run --files tests/live/test_modal_warm_reuse_hf_cache.py
git add tests/live/test_modal_warm_reuse_hf_cache.py
git commit -m "test(live): RED scaffold for Modal warm-reuse + HF-cache (M5 proof, pre-spend)"
```

---

### Task 2: Live 3-run warm-reuse + cold-cache proof + frame-QA + teardown + §26

**Goal:** Run the live sequence on Modal (Wan 2.1 1.3B / A10): cold boot, warm-attach, destroy, cold-cache re-boot; prove warm-attach (no redeploy) and the HF Volume cache-hit (download skipped); frame-QA; verify teardown; log `successful-generations.md` §26 and update `PROGRESS.md`.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (Modal M5 warm-reuse + HF-cache live proof). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new §26), `PROGRESS.md` (RESUME SNAPSHOT + SINGLE NEXT ACTION)

**Acceptance Criteria:**
- [ ] `pixi run preflight` → PASS before spend.
- [ ] **RUN_A (cold):** completes; valid 480×480 mp4 published; the EphemeralIndex row was written (endpoints `{"8000": <modal.run>}` + `kinoforge_key`) — confirm via the index/store or the `generated: uri=` + a follow-up index read.
- [ ] **RUN_B (warm-attach):** a SEPARATE CLI invocation attaches to RUN_A's live container — orchestrator log shows warm-attach, NO new `Building image`, NO new `.modal.run` deploy URL, and wall-clock materially < RUN_A. Output is a valid 480×480 mp4 from the second prompt.
- [ ] **RUN_C (cold-cache):** after an explicit `kinoforge destroy` of RUN_A's app (named Volume survives), a fresh `generate` cold-boots a NEW container whose log shows the Wan-1.3B weight download SKIPPED / served from `/cache/hf` (cache-hit), and boot < RUN_A's cold boot. (At 1.3B the time saving is modest — the binary presence/absence of the download is the proof.)
- [ ] Frame-QA on ≥5 frames of the two distinct-prompt outputs (RUN_A, RUN_B): coherent, prompt-adherent, no artifacts (⚠️-flag if not clearly HQ).
- [ ] After teardown: `pixi run kinoforge list` → no instances AND `modal app list` → no running kinoforge app.
- [ ] `successful-generations.md` §26 written (warm-reuse + HF-cache on Modal, recipe + the measured warm-vs-cold + cache-hit evidence).

**Verify:** `pixi run kinoforge list` → No running instances + empty ledger; §26 present with the warm-attach + cache-hit log evidence.

**Steps:**

- [ ] **Step 1: Preflight** — `pixi run preflight` → PASS. (`kinoforge`/monitoring self-loads `.env`; for the bare `modal` CLI use `set -a; . /workspace/.env; set +a` first.)

- [ ] **Step 2: RUN_A — cold boot** (default warm-reuse, NO `--no-reuse`). Stream to a log; monitor the deploy + app state (Modal has no util probe — poll app liveness + the orchestrator log, per the live-smoke rule):

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-wan-t2v-1_3b.yaml \
  --prompt-file examples/configs/prompts/field-realistic.txt \
  2>&1 | tee /tmp/m5_runA.log
```

Capture: the deployed `.modal.run` URL, the app id, the cold-boot wall-clock, `generated: uri=`. Confirm the index row exists (read `EphemeralIndex` from the session store, or `rg` the store dir).

- [ ] **Step 3: RUN_B — warm-attach** (fresh CLI process, within the 20-min idle window, second prompt, still NO `--no-reuse`):

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-wan-t2v-1_3b.yaml \
  --prompt-file examples/configs/prompts/field-dreamlike.txt \
  2>&1 | tee /tmp/m5_runB.log
```

Assert from `m5_runB.log`: a warm-attach line (attaching to RUN_A's id), NO `Building image`, NO new deploy URL, wall-clock ≪ RUN_A. `rg -n "warm|attach|Building image|modal.run|generated" /tmp/m5_runB.log`.

- [ ] **Step 4: Destroy RUN_A's app, then RUN_C — cold-cache.** The named Volume survives app deletion, so the fresh deploy reuses the cached weights:

```bash
pixi run kinoforge destroy --id <run-a-app-id>
pixi run kinoforge list   # confirm gone before RUN_C
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-wan-t2v-1_3b.yaml \
  --prompt-file examples/configs/prompts/forest.txt --no-reuse \
  2>&1 | tee /tmp/m5_runC.log
```

Assert from `m5_runC.log`: a FRESH boot (new deploy) BUT the Wan-1.3B weight fetch is skipped / cache-hit (weights already on `/cache/hf`) and boot < RUN_A. Look for the HF cache-hit / absence of the download progress lines: `rg -n "download|cache|Fetching|weights|Building image|boot" /tmp/m5_runC.log`. If instead the download re-runs in full, that is a cache MISS — capture it and treat as a deviation (check `HF_HOME` reached the container env, and that the Volume committed after RUN_A).

- [ ] **Step 5: Frame-QA** — extract ~5 frames from RUN_A and RUN_B outputs and read them (contact-sheet or a few frames each); confirm coherent, prompt-adherent, no artifacts. Record the verdict.

```bash
for OUT in $(ls -t output/*.mp4 | head -2); do
  SCR="$(mktemp -d)"
  pixi run ffmpeg -hide_banner -loglevel error -i "$OUT" \
    -vf "select='not(mod(n\,16))'" -vsync vfr -frames:v 5 "$SCR/qa_%d.png"
  echo "$OUT -> $SCR"
done
# then Read the qa_*.png frames
```

- [ ] **Step 6: Verify teardown** — after RUN_C (`--no-reuse`) exits:

```bash
pixi run kinoforge list
set -a; . /workspace/.env; set +a
pixi run -e live-modal modal app list | grep -i kinoforge || echo "no kinoforge apps"
```

Expected: `No running instances.` + `No instances recorded in ledger.` AND no running Modal app. Destroy any leftover with `pixi run kinoforge destroy --id <id>`.

- [ ] **Step 7: Log §26 + update PROGRESS; commit** (follow the §24/§25 schema in `successful-generations.md`: stack triple = `Modal / DiffusersEngine (Wan 2.1 T2V-1.3B) / Wan-AI/Wan2.1-T2V-1.3B-Diffusers`, mode = t2v, new axis = "cross-CLI warm-reuse + HF Volume weight-cache on Modal (M5)"; include the three commands, the warm-attach evidence (RUN_B: no redeploy + speed delta), the cache-hit evidence (RUN_C: download skipped + boot delta), the two artifact rows with ffprobe + SHA, the frame-QA verdict, and teardown confirmation):

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs(gen): Modal M5 warm-reuse + HF-cache live-green (Wan 2.1 1.3B/A10) + frame-QA"
```

---

## Self-Review

**Spec coverage:** Phase 0 offline write→match trace + gap fix (spec §"Phase 0") → Task 0. RED live scaffold before spend (spec §Testing) → Task 1. Live 3-run proof: cold + warm-attach + cold-cache, frame-QA, teardown, §26 (spec §"The live sequence" + §"Acceptance criteria") → Task 2. Run-C via destroy-then-fresh-deploy (spec decision) → Task 2 Step 4. Both-scenarios scope + 1.3B tier + presence/absence-not-wall-clock proof (spec decisions) → Task 2 ACs. Modal-URL-not-port-rebuildable risk (spec §Known risks #2) → Task 0 Step 1 first test. No-util-probe monitoring (spec §Known risks #4) → Task 2 Step 2. All covered.

**Placeholder scan:** No TBD/TODO. Task 0/1 test bodies are complete runnable code. Task 0 Steps 3-4 are explicit investigate-then-conditionally-patch instructions with the exact file:line and the exact import line to add — a real fork, not a placeholder. `<run-a-app-id>` in Task 2 is a runtime value captured in Step 2, not a placeholder.

**Type consistency:** `EphemeralIndexRow(id=, warm_attach_key=, kinoforge_key=, endpoints=, provider=, created_at_local=)` matches the constructor used at `_commands.py:627`. `EphemeralIndex(store=…)` + `.add(...)` + `.rows_by_kinoforge_key(...)` match `ephemeral_index.py`. `get_provider("modal")` + `ModalProvider` match the registry + `providers/modal`. Cfg path `examples/configs/modal-wan-t2v-1_3b.yaml` and the three prompt-file paths are consistent across Tasks 1-2.
