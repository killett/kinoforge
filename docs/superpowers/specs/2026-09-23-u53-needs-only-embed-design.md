# U53: embed only what the pod imports

**Status:** design approved 2026-09-23, not yet planned.
**Scope:** bring every shipped RunPod diffusers config back under RunPod's
`podFindAndDeployOnDemand` env-payload ceiling by embedding only the modules each pod
actually imports.
**Non-scope:** the encoding is not changed. The outer gzip stays (§2 says why the filed
fix direction was backwards). Modal configs are untouched. See §8.

## 1. The question, and the answer

*Eight of thirteen shipped RunPod diffusers configs are past the ~101 KB point where
`podFindAndDeployOnDemand` returns a raw HTTP 500 with no GraphQL error body. What
shrinks them?*

Not the encoding. **The payload is 95.5% embedded Python source, and every RunPod
diffusers pod carries three modules it never imports.**

`_render_embed_lines` (`src/kinoforge/engines/diffusers/__init__.py:138`) walks a
*package directory* and embeds every `.py` file in it. Configs declare
`embed_modules: ["kinoforge.engines.diffusers.servers"]`, so each pod receives the whole
`servers/` package — including `minimax_h3_server.py`, `_lora.py` and `_av_io.py`, which
exist for the MiniMax-H3 server. H3 runs on Modal only. No RunPod diffusers config
imports any of the three.

Measured on `runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale`, the worst config:

| | bytes |
|---|---|
| rendered env total | 127,917 |
| `KINOFORGE_PROVISION_SCRIPT` value | 124,588 (97.4%) |
| raw provision script | 126,690 |
| of which inner module blobs | 120,948 (95.5%), 14 files, 269,573 B of source |

Dropping the three unimported modules removes 39,024 B of encoded blob and ~39.4 KB of
rendered env from **every** RunPod diffusers config:

| config | baseline | needs-only | saved |
|---|---|---|---|
| wan-2_2-14b-t2v-flashvsr-1080p-upscale | 127,917 | 88,575 | 39,342 |
| wan-2_2-14b-t2v-flashvsr-upscale | 127,917 | 88,575 | 39,342 |
| flashvsr-x4-torch26-upscale | 127,899 | 88,553 | 39,346 |
| flashvsr-1080p-upscale | 127,871 | 88,521 | 39,350 |
| flashvsr-x4-upscale | 127,871 | 88,521 | 39,350 |
| rife-60fps-interpolate | 115,935 | 76,497 | 39,438 |
| wan-2_2-14b-t2v-spandrel-upscale | 112,893 | 73,423 | 39,470 |
| spandrel-x2-upscale | 112,831 | 73,369 | 39,462 |
| wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke | 89,393 | 49,987 | 39,406 |
| wan-2_1-1_3b-t2v-strength-grid | 89,393 | 49,987 | 39,406 |
| wan-2_2-14b-t2v | 89,389 | 49,979 | 39,410 |
| wan-2_2-14b-t2v-lora-flexible-warm-reuse-release | 89,397 | 49,991 | 39,406 |
| wan-2_2-14b-t2v-strength-grid | 89,397 | 49,991 | 39,406 |

All thirteen land under 101,000 B. Headroom ranges from **12,425 B** (the two
wan+flashvsr configs, the tightest) to **51,021 B** (`wan-2_2-14b-t2v`). The "baseline"
column is `_BASELINE_BYTES` from
`tests/providers/test_env_payload_ceiling.py`; the measurement method here reproduces it
to within 26 B, so the two are directly comparable.

## 2. The filed fix direction was backwards — do not follow it

U53's row and the snapshot's ranking (`PROGRESS.md`) both say: *"drop the outer gzip
(near-zero payoff today) and spend the reclaimed margin"*, reasoning that
`gzip`-of-`base64`-of-`gzip` buys almost nothing because base64 text is already close to
incompressible.

**Measured, that is false.** On the worst config:

| | bytes |
|---|---|
| plain base64, no outer gzip | 168,920 |
| current gzip → base64 | 124,588 |
| **outer gzip saves** | **44,332 (26.2%)** |

Base64 packs 64 symbols into 8-bit bytes — ~6 bits of entropy per byte — so gzip
recovers almost exactly the expected 25%. **The outer gzip is load-bearing.** Dropping
it takes the worst config from 127,917 B to ~172,200 B and makes the breach substantially
worse. It stays.

## 3. The fix

Replace the whole-package `embed_modules` entry with explicit `embed_files` entries
naming only the server modules the pod imports.

No new mechanism is required. `embed_files` already exists for precisely this case —
`src/kinoforge/core/config.py:510-515` documents it as *"Single-file embeds for dotted
module paths whose leaf is a .py file … Use when the on-pod runtime needs a specific
module but embedding its whole package would bust the 64KB env-var ceiling"* — and
`_render_embed_single_file` (`engines/diffusers/__init__.py:98`) already touches ancestor
`__init__.py` files the same way the package embed does.

Thirteen RunPod diffusers configs change. Each config's existing `embed_files` entries
(`kinoforge.core.errors`, `kinoforge.core.scale_target`, …) are unaffected.

**Only the `kinoforge.engines.diffusers.servers` package embed changes.** The
`kinoforge.upscalers.flashvsr`, `kinoforge.interpolators.rife` and
`kinoforge.upscalers.spandrel` package embeds stay whole-package, exactly as today. All
three dropped modules live in `servers/`, so this is where the entire 39 KB win is; those
other packages carry their own controller-side `_engine.py` alongside the pod-side
`_runtime.py`, and separating them is a different change with a different risk profile.
Scoping to `servers/` keeps this increment's blast radius equal to its measured benefit.

### 3.1 What every RunPod diffusers pod needs

`wan_t2v_server` is the HTTP host on **all thirteen**, including the upscale- and
interpolate-only configs: those set `upscale_only: true`, which emits
`KINOFORGE_SKIP_WAN_LOAD=1` so the server starts without the eager `WanPipeline` load and
serves `/upscale` or `/interpolate` only. Their provision scripts end in
`python -m kinoforge.engines.diffusers.servers.wan_t2v_server`, verified by rendering
them. **`wan_t2v_server.py` therefore stays everywhere — it is not dead weight on the
upscale-only configs**, and an earlier reading of this design that dropped it there was
wrong.

`wan_t2v_server`'s full AST import closure over `kinoforge.*` is 13 modules and excludes
all three dropped files. A textual search of `wan_t2v_server.py` for `_av_io`, `_lora`
and `minimax_h3` returns only the diffusers pipe attribute `_lora_loadable_modules` and
test filenames — no reference to any of the three modules.

## 4. The guard test

This change trades a create-time HTTP 500 for a possible boot-time `ImportError`:
under-embed one module and the pod boots, reports ready, then dies serving. That safety
property needs a mechanism, not a hand-maintained list.

A new test computes each config's pod-side import closure — rooted at the entry points
its rendered provision script actually runs (`python -m …` lines) — and asserts it in both
directions, **restricted to modules under `kinoforge.engines.diffusers.servers`**, matching
§3's scope:

* **nothing in the closure is unembedded** — catches the under-embed that kills a pod at
  boot;
* **nothing embedded is outside the closure** — stops the fat creeping back, which is how
  the breach arrived in the first place.

**Nested imports count; the package restriction does the narrowing.** An earlier draft of
this section said *module-level imports only*, reasoning that `wan_t2v_server` imports the
flashvsr / rife / spandrel / seedvr2 runtimes lazily inside functions and a nested-inclusive
closure would demand all of them. **That rule is wrong and would ship a bug.**
`wan_t2v_server`'s only *module-level* `kinoforge` import is
`servers._video_io` (line 63); `servers._util_stats` is imported lazily inside a function —
yet every pod needs it, because it backs the `/util` route that CLAUDE.md's live-smoke
polling rule depends on. A module-level-only closure drops `_util_stats` and breaks `/util`
on all thirteen configs.

The correct rule is the **full closure — nested imports included — restricted to
`servers/`**. The restriction alone does all the narrowing the module-level rule was
reaching for, because the lazily-imported feature runtimes live in `upscalers.*` and
`interpolators.*`, outside the restriction. Measured, that rule gives exactly:

| entry point | closure inside `servers/` |
|---|---|
| `wan_t2v_server` | `wan_t2v_server`, `_util_stats`, `_video_io` |
| `minimax_h3_server` | `minimax_h3_server`, `_lora`, `_av_io`, `_util_stats`, `__init__` |

Three of the seven files in `servers/` for a RunPod pod; the three dropped modules appear
only in H3's closure. Feature-lazy modules outside `servers/` stay declared per config, as
now, and stay exercised by the per-feature live smokes.

The closure must skip `if TYPE_CHECKING:` blocks — those imports never execute, and
counting them would demand embedding controller-side modules the pod never loads.

Restricting the assertion to `servers/` also keeps the second direction tractable: the
`upscalers.*` / `interpolators.*` package embeds deliberately ship a controller-side
`_engine.py` next to the pod-side `_runtime.py`, so "nothing embedded is outside the
closure" would fire on them for a reason this increment is not fixing.

**Why a static closure is sound here specifically.** The only `importlib.import_module`
calls in the embedded set resolve an operator-supplied env-var stub path
(`KINOFORGE_DIFFUSERS_LOAD_STUB`, `wan_t2v_server.py:1054` and
`minimax_h3_server.py:442`) and a third-party `diffsynth` module
(`upscalers/flashvsr/_runtime.py:362`). Neither is a `kinoforge.*` module an AST walk
would miss. This assumption is checked, not assumed, and the guard should say so in its
docstring so a future dynamic import does not silently invalidate it.

## 5. Retire the exemption

`tests/providers/test_env_payload_ceiling.py` currently records a pre-existing breach:
`_BASELINE_BYTES` holds eight over-ceiling entries, and `_SAFE_AS_OF_2026_09_22` is a
frozen five-name list of the configs that were under it.

After the fix:

* `_BASELINE_BYTES` re-baselines ~39 KB lower for all thirteen;
* `_SAFE_AS_OF_2026_09_22` is **deleted** and its parametrised test replaced by an
  unconditional one: *every* RunPod diffusers pod config measures under
  `_RUNPOD_CEILING_BYTES`. That is strictly stronger than the frozen name list — the list
  existed only to stop a `_BASELINE_BYTES` bump widening tolerance for the five
  then-safe configs, and an unconditional assertion cannot be widened by a bump at all.
  The module docstring's "recorded pre-existing breach" section goes with it.

Retiring the exemption is what closes U53. A re-baseline alone would only move the
ratchet.

## 6. Goldens

All 32 launch-payload goldens move, plus the separate `_golden_provision.json`. Order is
load-bearing and has bitten before: run `pixi run pre-commit run --all-files` **first**,
then `tools/snapshot_launch_payloads.py` — regenerating before formatting produces
goldens that the formatter then invalidates.

## 7. Documentation corrections

Three places assert something this design measures as false. All three change in the same
pass, because a stale note here is what cost a session on 2026-07-05.

1. **`CLAUDE.md`, "Known infra gotchas"** — *"~74 KB script → ~72 KB base64, ~4×
   headroom"*. Replace with the measured behaviour: the outer gzip saves ~26%, and
   headroom was **negative** for 8 of 13 shipped configs until this change. State what
   the real lever is (what gets embedded), not the encoding.
2. **`src/kinoforge/providers/runpod/__init__.py:1317-1322`** — the same claim duplicated
   as a source comment on `_encode_provision_script`.
3. **`PROGRESS.md`** — U53's row and the suggested-order entry at the end of the RESUME
   SNAPSHOT both carry the inverted fix direction from §2. Correct in place; the row's
   diagnosis of *where* the bytes are remains accurate, only the prescription was wrong.

## 8. Non-scope

* **The encoding is unchanged.** Consolidating the per-file gzip+base64 blobs into one
  tar.gz would recover a further few KB of cross-file redundancy that the per-file passes
  throw away (~6.5 KB measured, but against an intermediate drop set, not the final one —
  treat it as indicative only). Unnecessary once §3 lands: a second structural change with
  its own golden churn, for headroom this design does not need.
* **Modal configs are untouched.** The H3 configs genuinely need
  `minimax_h3_server.py`, `_lora.py` and `_av_io.py`, and Modal's boot payload has its
  own separate 32,768 B Secret cap and chunking path.
* **Not fetching source at boot.** Installing kinoforge from a git ref or presigned URL
  would retire the ceiling as a class of problem rather than inching under it, but needs
  network and auth at boot plus a fallback. Considered and deferred.
* **U50, U52's models-dir seam, and the two-LoRA-implementations debt** are adjacent to
  these files and stay out.

## 9. Live proof

The offline measurement proves the payload shrinks. Only a live create proves the 500
stops, and only a live boot proves the three dropped modules were genuinely unused.

1. **Red, at $0.** Attempt a create on `runpod-diffusers-spandrel-x2-upscale` (112,831 B)
   *before* the fix and record the response. If it raw-500s, that is the red half of the
   proof for free — GraphQL rejects the body before booking any hardware. If it succeeds,
   the ~101 KB figure is looser than recorded and the spec says so rather than quietly
   dropping the claim.
2. **Green.** After the fix, create on the same config and run upscale-only against a
   fixture clip — prefer a **tracked** one, `examples/configs/grids/_fixtures/wan21_strength_cell0.mp4`
   (480²/33f), since `output/flashvsr-fixture-41f-480sq.mp4` (480²/41f) sits under the
   gitignored `output/` tree and is routinely swept. Upscale-only is ~4 min / ~$0.08 per
   CLAUDE.md — the cheapest vehicle that still exercises
   the server's import path without the dropped modules. Poll `/util` for
   `gpuUtilPercent` every 60–90 s, never `est_spend`. Frame-QA the output before
   reporting green. Pass `--no-reuse` and verify teardown with `kinoforge list` **after**
   the orchestrator exits.
3. The other twelve configs share the same three dropped modules and the same entry
   point; §4's guard covers them.

## 10. Residual risk, stated plainly

* **The ceiling is empirical.** ~101 KB is where it broke in practice on 2026-07-05, not
  an RunPod-documented constant. The worst config keeps 12,425 B of headroom — real, but
  not generous. The guard in §4 is what stops it eroding again.
* **The live proof covers one config.** The other twelve rest on the guard test plus a
  shared entry point.
* **A future dynamic `kinoforge.*` import breaks §4's soundness** without failing it. The
  guard's docstring must name that assumption so the next reader can re-check it.
