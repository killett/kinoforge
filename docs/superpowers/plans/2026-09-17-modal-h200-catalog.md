# Modal H200 Catalog Row — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one row — `("H200", 141, 4.54)` — to kinoforge's static Modal GPU catalog, so a config can request a card larger than 80 GB, without changing which GPU any existing config books.

**Architecture:** `src/kinoforge/providers/modal/_catalog.py` holds `_MODAL_GPUS`, a hand-maintained tuple of `(modal-gpu-string, vram_gb, usd_per_hr)`. Modal is serverless and has no live offers API, so this table is the *only* thing deciding what kinoforge can book there. The tuple is expanded into `Offer` objects, filtered by `kinoforge.core.offers.filter_offers`, and the provider books `candidates[0]`. This plan adds one row and two regression guards. **It is entirely offline — no network, no GPU, no spend.**

**Tech Stack:** Python 3.13, pixi (task runner + env), pytest, ruff, mypy, pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-17-minimax-h3-t2va-design.md` — this plan implements **Sub-project A** only.

## Context for a fresh session

You do not need the conversation that produced this plan. You need these facts:

- **Why this exists.** A later sub-project adds MiniMax-H3, a video model whose bf16 weights are ~115 GB. Every card currently in the Modal catalog tops out at 80 GB, so H3 is unbookable. This plan removes that blocker and nothing else. **Do not implement H3 here.**
- **The catalog is stale.** Its docstring says `snapshot 2026-07-08`. Modal has since added H200, B200 and B300.
- **You are adding H200 ONLY.** See Task 1's "Why not B200/B300" — this is a deliberate decision with a named failure mode behind it, not an oversight. Adding B200/B300 in this plan is a **plan violation**.
- **Repo conventions.** `pixi run test` runs pytest. Google-style docstrings and type hints on all functions. Conventional Commits. Run `pixi run pre-commit run --all-files` before committing; never `--no-verify`.

## Global Constraints

- **`vram_gb` must be a number the PROVIDER states, never one inferred from a datasheet or a model name.** This is the U48 defect: SkyPilot reported a CUDA version from a lookup that could not succeed, so every offer carried the same fabricated constant and any config leaving `min_cuda` at its default had its entire catalog filtered away. A wrong `vram_gb` fails identically — silently, inside `filter_offers`, with no error to read. **Modal's docs state H200 = 141 GB verbatim. They do not state B200 or B300 VRAM.**
- **The `id` string is sent to Modal verbatim.** `Offer.gpu_type` flows to `ModalAppRequest.gpu` (`src/kinoforge/providers/modal/__init__.py:310`) and then to Modal's `gpu=` parameter (`src/kinoforge/providers/modal/_app.py:135`). A typo surfaces only as a live create failure, i.e. as wasted money. Modal's accepted spellings include `"H200"` exactly.
- **Modal offers are `mode="serverless"`, so `max_usd_per_hr` does NOT filter them.** `filter_offers` only applies the price ceiling when `o.mode == "pod"` (`src/kinoforge/core/offers.py:65`). A config with `max_usd_per_hr: 4.00` will **not** reject a $4.54 H200. Price cannot protect you here; ranking is the only protection.
- **`placement.accelerators` is a preference RANKING, not an allowlist.** `filter_offers` keeps every offer that passes `min_vram_gb`/`min_cuda` and merely sorts listed types first. Adding H200 therefore adds it as a *candidate* to configs that never asked for it. This is the whole risk of this change.
- **Do not regenerate the launch-payload goldens to make a test pass.** If `tests/providers/test_launch_payload_goldens.py` fails after your change, that is a real regression — a config now books a different GPU. Stop and report it.
- **No live spend in this plan.** No `kinoforge generate`, no Modal API calls, no `pixi run preflight` needed.

**User decisions (already made):**
- "Add H200 to the catalog, run bf16 outright" — chosen over A100-80GB-with-offload and INT8 quantized weights.
- "Do A and B before C" — this catalog change ships and commits before any prefetch or H3 work.
- H200 only; B200/B300 deferred because Modal does not publish their VRAM.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/kinoforge/providers/modal/_catalog.py` | The static Modal offer table | Modify: +1 row, re-date snapshot comment |
| `tests/providers/modal/test_catalog.py` | Catalog contents and filter behaviour | Modify: +2 tests |
| `tests/providers/modal/test_catalog_no_regression.py` | Cross-config guard: shipped configs still book the same GPU | Create |
| `PROGRESS.md` | Session-resume source of truth | Modify: record Sub-project A done |

---

### Task 1: Add the H200 row to the Modal catalog

**Goal:** `MODAL_GPU_CATALOG` contains an H200 offer with Modal's stated 141 GB and $4.54/hr, so a `min_vram_gb` above 80 can be satisfied.

**Files:**
- Modify: `src/kinoforge/providers/modal/_catalog.py:4` (docstring date), `:14-22` (`_MODAL_GPUS`)
- Test: `tests/providers/modal/test_catalog.py`

**Acceptance Criteria:**
- [ ] `MODAL_GPU_CATALOG` contains an offer with `id == "H200"`, `vram_gb == 141`, `cost_rate_usd_per_hr == 4.54`, `mode == "serverless"`
- [ ] A `Placement(min_vram_gb=120)` returns a non-empty offer list whose every entry has `vram_gb >= 120`
- [ ] Before this change, that same `Placement` returned an empty list (assert the old behaviour is genuinely gone)
- [ ] A `Placement(min_vram_gb=16, accelerators=("H200",))` ranks H200 first while keeping the cheaper cards as fallbacks
- [ ] The module docstring's snapshot date reads `2026-09-17`
- [ ] No B200 or B300 row is added

**Verify:** `pixi run python -m pytest tests/providers/modal/test_catalog.py -v` → all tests PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/modal/test_catalog.py`:

```python
def test_h200_row_matches_modal_published_specs():
    # Bug caught: a vram_gb inferred from a datasheet rather than stated by
    # Modal. That is the U48 shape — a constant the provider never supplied,
    # which filter_offers then applies silently. Modal's GPU docs state
    # H200 = "141 GB"; its pricing page states $0.001261/sec = $4.54/hr.
    by_id = {o.id: o for o in MODAL_GPU_CATALOG}
    assert "H200" in by_id
    assert by_id["H200"].vram_gb == 141
    assert by_id["H200"].cost_rate_usd_per_hr == 4.54
    assert by_id["H200"].mode == "serverless"


def test_h200_is_the_only_card_above_80gb():
    # Bug caught: helpfully adding B200/B300 alongside H200. Modal's docs do
    # NOT state their VRAM, so any number written for them is invented.
    big = {o.id for o in MODAL_GPU_CATALOG if o.vram_gb > 80}
    assert big == {"H200"}


def test_a_request_above_80gb_is_now_satisfiable():
    # Bug caught: the row exists but filter_offers still drops it, so the
    # catalog looks right and nothing can actually book it. Before this
    # change this returned [] — that emptiness is what blocked MiniMax-H3.
    offers = modal_offers(Placement(min_vram_gb=120))
    assert offers, "no Modal offer satisfies min_vram_gb=120"
    assert all(o.vram_gb >= 120 for o in offers)
    assert offers[0].id == "H200"


def test_h200_ranks_first_when_a_config_asks_for_it_by_name():
    # Bug caught: a config naming H200 gets something else at candidates[0].
    # The test above passes trivially because H200 is the only card over
    # 120 GB; this one exercises the RANKING path with a low vram floor, so
    # H200 must beat seven cheaper cards that all clear the bar.
    offers = modal_offers(Placement(min_vram_gb=16, accelerators=("H200",)))
    assert offers[0].id == "H200"
    assert len(offers) > 1, "expected the cheaper cards to remain as fallbacks"
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `pixi run python -m pytest tests/providers/modal/test_catalog.py -v`

Expected: all **four** new tests FAIL. `test_h200_row_matches_modal_published_specs` fails on `assert "H200" in by_id`; `test_a_request_above_80gb_is_now_satisfiable` fails on the empty-list assertion; `test_h200_is_the_only_card_above_80gb` fails comparing `set()` to `{"H200"}`; `test_h200_ranks_first_when_a_config_asks_for_it_by_name` fails because `offers[0].id` is `"T4"` (H200 is absent, so the unknown-accelerator path leaves the catalog in its natural order). The three pre-existing tests still PASS.

**Verified against HEAD `0d600860` on 2026-09-17** — these are the measured pre-change values, not predictions: `"H200" in by_id` is `False`; `{o.id for o in MODAL_GPU_CATALOG if o.vram_gb > 80}` is `set()`; `modal_offers(Placement(min_vram_gb=120))` is `[]`; and `modal_offers(Placement(min_vram_gb=16, accelerators=("H200",)))` returns 7 offers led by `"T4"`.

You will also see this on stderr during the RED run, and it is **expected and correct**:

```
[placement] accelerator 'H200' matches nothing in this provider's catalog, so it
ranks no higher than a GPU you did not ask for; did you mean 'H100'?
```

That is the U43 guard doing its job — it distinguishes a misspelled accelerator from a stocked-out one. It disappears once Task 1 Step 3 lands. Do not chase it.

**Do not skip this step.** A test that passes before the implementation is testing nothing, and the empty-list assertion is the whole point of this task.

- [ ] **Step 3: Add the row**

In `src/kinoforge/providers/modal/_catalog.py`, change `_MODAL_GPUS` from:

```python
_MODAL_GPUS: tuple[tuple[str, int, float], ...] = (
    ("T4", 16, 0.59),
    ("L4", 24, 0.80),
    ("A10", 24, 1.10),
    ("L40S", 48, 1.95),
    ("A100-40GB", 40, 2.10),
    ("A100-80GB", 80, 2.50),
    ("H100", 80, 3.95),
)
```

to:

```python
_MODAL_GPUS: tuple[tuple[str, int, float], ...] = (
    ("T4", 16, 0.59),
    ("L4", 24, 0.80),
    ("A10", 24, 1.10),
    ("L40S", 48, 1.95),
    ("A100-40GB", 40, 2.10),
    ("A100-80GB", 80, 2.50),
    ("H100", 80, 3.95),
    # H200 is the only card here above 80 GB, and it is deliberately the only
    # one. Modal's GPU docs state its capacity verbatim ("141 GB"); they do NOT
    # state B200's or B300's, and a vram_gb that the provider never published is
    # the U48 defect — a fabricated constant that filter_offers applies
    # silently, with no error to read. Add B200/B300 only when Modal itself
    # publishes their VRAM.
    ("H200", 141, 4.54),
)
```

- [ ] **Step 4: Re-date the snapshot comment**

In the same file, line 4, change:

```
(snapshot 2026-07-08, https://modal.com/pricing). Offers are ``mode="serverless"``
```

to:

```
(snapshot 2026-09-17, https://modal.com/pricing). Offers are ``mode="serverless"``
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `pixi run python -m pytest tests/providers/modal/test_catalog.py -v`

Expected: all 7 tests PASS (3 pre-existing + 4 new).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/modal/_catalog.py tests/providers/modal/test_catalog.py
git commit -m "feat(modal): add the H200 row to the GPU catalog

The catalog's largest card was H100 at 80 GB, so no config could express a
requirement above that. Adds H200 at Modal's published 141 GB / \$4.54 per hr.

H200 only. Modal's docs do not state B200 or B300 VRAM, and writing a
vram_gb the provider never published is the U48 defect — filter_offers
applies it silently and the whole catalog can vanish with no error."
```

---

### Task 2: Guard every shipped Modal config against a GPU change

**Goal:** Prove that adding H200 did not change which GPU any of the five shipped Modal configs books, and freeze that so a future catalog row cannot change it silently.

**Why this is a separate task:** Task 1 proves the row is *present and usable*. This proves it is *harmless*. They fail for different reasons and deserve separate commits. The risk is real and non-obvious: `filter_offers` keeps every offer that clears `min_vram_gb`/`min_cuda` and only *ranks* by `placement.accelerators`, and the `max_usd_per_hr` ceiling is skipped entirely for serverless offers. So H200 becomes a silent candidate for configs that never asked for it, protected only by ranking.

**Files:**
- Create: `tests/providers/modal/test_catalog_no_regression.py`
- Read-only: `examples/configs/modal-*.yaml` (five files)

**Acceptance Criteria:**
- [ ] For all five shipped `examples/configs/modal-*.yaml`, `modal_offers(placement)[0].id` equals the config's first declared accelerator
- [ ] The test derives the expectation from each config's own `accelerators` list — it does not hardcode five GPU names, so it keeps working when a config's preference changes
- [ ] The test fails loudly if a config declares no `accelerators` list, rather than silently passing
- [ ] `tests/providers/test_launch_payload_goldens.py` passes with **no golden regenerated** (`git status --short tests/providers/golden/launch_payloads/` is empty)

**Verify:** `pixi run python -m pytest tests/providers/modal/test_catalog_no_regression.py tests/providers/test_launch_payload_goldens.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write the guard test**

Create `tests/providers/modal/test_catalog_no_regression.py`:

```python
"""Behavior: adding a catalog row must not change what a shipped config books.

``filter_offers`` keeps every offer clearing ``min_vram_gb``/``min_cuda`` and
only RANKS by ``placement.accelerators`` (U36: it is a preference ordering, not
an allowlist). The ``max_usd_per_hr`` ceiling is skipped for ``mode=
"serverless"`` offers, which every Modal offer is. So a new, larger, pricier
card becomes a silent candidate for configs that never asked for it, and the
only thing keeping it out of ``candidates[0]`` is the ranking.

The provider books ``candidates[0]`` (``providers/modal/__init__.py:262``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.providers.modal._catalog import modal_offers

_MODAL_CONFIGS = sorted(Path("examples/configs").glob("modal-*.yaml"))


def test_the_shipped_modal_configs_are_actually_discovered():
    # Bug caught: a glob that matches nothing makes every parametrized test
    # below vacuously pass, so the guard silently protects nothing.
    assert len(_MODAL_CONFIGS) >= 5


@pytest.mark.parametrize("cfg_path", _MODAL_CONFIGS, ids=lambda p: p.stem)
def test_shipped_config_still_books_its_first_choice(cfg_path: Path) -> None:
    cfg = load_config(str(cfg_path))
    assert cfg.compute is not None  # noqa: S101 — every shipped Modal config has one
    accelerators = cfg.compute.placement.accelerators

    # A config with no stated preference has no protection from ranking, so a
    # new card could take candidates[0]. Fail rather than pass vacuously.
    assert accelerators, (
        f"{cfg_path.name} declares no placement.accelerators, so a newly added "
        "catalog row could silently become its booked GPU"
    )

    offers = modal_offers(cfg.placement())
    assert offers, f"{cfg_path.name} matched no Modal offer at all"
    assert offers[0].id == accelerators[0], (
        f"{cfg_path.name} would now book {offers[0].id!r}, not its first "
        f"declared choice {accelerators[0]!r}"
    )
```

**Note on the two type surfaces** (this repo has both, and confusing them fails `mypy --strict`): `cfg.compute.placement` is a `PlacementConfig` — the YAML shape, which is what carries `.accelerators`. `modal_offers` wants an `interfaces.Placement`, which `cfg.placement()` (`src/kinoforge/core/config.py:1650`) adapts to. `tests/test_modal_config.py:59,93` already uses `modal_offers(cfg.placement())`; follow that. Both test functions also need `-> None` annotations.

- [ ] **Step 2: Run it**

Run: `pixi run python -m pytest tests/providers/modal/test_catalog_no_regression.py -v`

Expected: PASS for all five configs plus the discovery test. All five declare an `accelerators` list whose first entry stays rank 1, because H200 is unlisted and therefore sorts after every listed type.

**If any config FAILS here, stop.** It means H200 displaced a cheaper card at $4.54/hr on a config that never asked for it. Do not "fix" it by reordering the catalog — report it, because the same displacement would happen on the next card added.

- [ ] **Step 3: Confirm the golden ratchet is untouched**

Run: `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -v`

Expected: PASS, with no golden file modified. Confirm with:

```bash
git status --short tests/providers/golden/launch_payloads/
```

Expected: empty output.

**If a golden moved, that is a real regression, not a stale snapshot.** That test's own docstring says: "never regenerate to make this test pass." A moved golden means a config's wire payload changed, i.e. it books a different GPU. Stop and report.

- [ ] **Step 4: Run the whole suite**

Run: `pixi run test`

Expected: green. This catches anything elsewhere that asserted on catalog length or contents.

- [ ] **Step 5: Commit**

```bash
git add tests/providers/modal/test_catalog_no_regression.py
git commit -m "test(modal): freeze which GPU each shipped Modal config books

filter_offers ranks rather than allowlists, and skips the price ceiling for
serverless offers — so a new catalog row becomes a silent candidate for every
config that clears min_vram_gb, however expensive it is.

Derives each expectation from the config's own accelerators list rather than
hardcoding GPU names, and fails loudly on a config that declares none."
```

---

### Task 3: Record Sub-project A as done

**Goal:** `PROGRESS.md` tells the next session that A is complete and B is next, per this repo's session-resume protocol.

**Why:** `CLAUDE.md` makes `PROGRESS.md` the source of truth for where the build is, and requires it be updated and committed after each task. A fresh session reads it before doing anything.

**Files:**
- Modify: `PROGRESS.md` (the `## RESUME SNAPSHOT` section)

**Acceptance Criteria:**
- [ ] A dated entry under `## RESUME SNAPSHOT` states Sub-project A is complete, names the commits, and names Sub-project B as the next action
- [ ] It records that B200/B300 were deliberately deferred, with the reason, so a later session does not "finish the job" by adding them
- [ ] The snapshot heading's date is updated to `2026-09-17`
- [ ] No claim of live spend — this sub-project spent $0

**Verify:** `rg -n 'Sub-project A' PROGRESS.md` → returns the new entry

**Steps:**

- [ ] **Step 1: Locate the snapshot**

Run: `rg -n '^## RESUME SNAPSHOT' PROGRESS.md`

The section is thousands of lines into the file and **moves whenever the sections above it grow** — always locate it this way, never by a remembered line number. `PROGRESS.md` exceeds the single-read limit; do not attempt a full-file read.

- [ ] **Step 2: Insert the entry**

Immediately below the `## RESUME SNAPSHOT` heading line, insert:

```markdown
### SESSION 2026-09-17 (second) — MiniMax-H3 Sub-project A done, $0 spent

**Sub-project A of `docs/superpowers/specs/2026-09-17-minimax-h3-t2va-design.md`
is COMPLETE, offline, for $0.** The Modal catalog now carries
`("H200", 141, 4.54)`, so a config can request more than 80 GB — which is what
blocked MiniMax-H3, whose bf16 weights are ~115 GB.

**H200 is the ONLY card above 80 GB in the catalog, deliberately.** Modal's docs
state H200 = 141 GB verbatim but do NOT state B200's or B300's VRAM. A `vram_gb`
the provider never published is the U48 defect — `filter_offers` applies it
silently and can drop the entire catalog with no error to read. **Do not "finish
the job" by adding B200/B300 until Modal publishes their VRAM.** A test
(`test_h200_is_the_only_card_above_80gb`) enforces this.

**The real risk was never the row, it was the ranking.** `filter_offers` keeps
every offer clearing `min_vram_gb` and only RANKS by `accelerators` (U36), and
it skips the `max_usd_per_hr` ceiling entirely for `mode="serverless"` offers —
which every Modal offer is. So a $4.54/hr card became a silent candidate for
configs that never asked for it. All five shipped `modal-*.yaml` still book
their first declared choice, now frozen by
`tests/providers/modal/test_catalog_no_regression.py`, and the launch-payload
goldens did not move.

**Next action: Sub-project B** — the HF Volume prefetch path. It pulls
`FL2VA/*` (144 GB) onto `kinoforge-hf-cache` from a T4 rather than an H200,
turning a ~$3.94 fetch into ~$0.51 and making a failed fetch cost cents. It is a
prerequisite for C, not an optimisation: without it the $20 budget buys three or
four attempts at H3 instead of six to eight. It is also the cheapest place to
discover whether the `minimax-h3-community-license-agreement` gates downloads.

```

- [ ] **Step 3: Re-date the snapshot heading**

Change the `## RESUME SNAPSHOT` heading's parenthetical to read `(updated 2026-09-17 — read this, then STOP; below is history)`.

- [ ] **Step 4: Verify**

Run: `rg -n 'Sub-project A' PROGRESS.md`

Expected: at least one hit inside the new entry.

- [ ] **Step 5: Run pre-commit and commit**

```bash
pixi run pre-commit run --all-files
git add PROGRESS.md
git commit -m "docs: record MiniMax-H3 Sub-project A complete

Modal catalog carries H200 (141 GB, \$4.54/hr); no shipped config changed
which GPU it books, frozen by a new guard test. \$0 spent — offline only.

Records why B200/B300 stay out, so a later session does not add them: Modal
does not publish their VRAM, and an invented vram_gb is the U48 defect.

Next action: Sub-project B, the HF Volume prefetch."
```

---

## Done when

- `pixi run test` is green
- `pixi run pre-commit run --all-files` is green
- Three commits exist: the catalog row, the guard test, the progress note
- `git status` is clean
- **Zero dollars spent**

## What is explicitly NOT in this plan

- MiniMax-H3 itself — no server, no config, no mode, no audio (Sub-project C)
- The HF Volume prefetch (Sub-project B)
- B200 / B300 catalog rows (deferred; see Global Constraints)
- Any change to `filter_offers` — its ranking-not-allowlist behaviour is deliberate (U36) and load-bearing for other providers
