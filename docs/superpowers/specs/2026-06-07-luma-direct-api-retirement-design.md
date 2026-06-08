# Layer 5a — Luma direct-API retirement (deletion-only)

**Status:** Design — approved through brainstorming on 2026-06-07.
**Author:** Claude (Opus 4.7), Emmy Killett.
**Predecessors:** Phase 43 Layer 4 (Bearer-provider comparison smokes), commit
`4515ac4` shipped the `LumaEngine` (direct Dream Machine video API).
**Successor:** Layer 5b (`LumaAgentsImageEngine` for UNI-1 keyframes — separate
brainstorm and separate spec).

## 1. Context

Luma retired the direct Dream Machine developer video API in 2026. The legacy
URL `https://lumalabs.ai/dream-machine/api/keys` now `308`-redirects to
`/app` (the consumer subscription dashboard, no API). Luma video models
(`ray-2`, `ray-flash-2`, `ray-3`) remain reachable only through:

1. The consumer Dream Machine subscription UI at `lumalabs.ai/app` (no API).
2. Amazon Bedrock (`luma.ray-v2:0`) — handled by `BedrockVideoEngine` and
   `examples/configs/luma-ray.yaml` (Phase 42).
3. Replicate (`luma/ray-flash-2`) — handled by `ReplicateEngine` (Phase 43).
4. fal.ai (`fal-ai/luma-dream-machine/ray-2`) — handled by `FalEngine`
   (Phase 19).

The `kinoforge.engines.luma` package committed in Phase 43 talks to the
retired endpoint and is therefore dead code. The Bearer Luma live smoke
(Phase 43 Task 13) was DEFERRED at the time on a `403` from
`api.lumalabs.ai` — the deferral is now permanent, because the API does not
exist anymore. The `LUMAAI_API_KEY` env var still mints valid Luma Agents
keys (UNI-1 image generation) and will be reused unchanged by Layer 5b.

Project memory `project_luma_video_retirement_2026.md` is the authoritative
record. This spec closes the carry-forward it describes.

## 2. Goal

Remove every code path that calls the retired direct API, including its unit
tests and its comparison-batch example YAML. Leave the `LUMAAI_API_KEY` env
var and `BedrockVideoEngine` / `luma-ray.yaml` / `test_luma_ray_live.py`
untouched (they target a different provider, Amazon Bedrock).

Add a README tombstone so a future reader searching for `luma` in the
Hosted-Bearer-providers section finds the retirement notice and the three
still-live ways to reach Luma video, plus a forward pointer to Layer 5b.

## 3. Scope

### 3.1 In scope

- Source-tree deletion of the `engines/luma/` package and its tests.
- Deletion of the comparison-batch YAML that targets the dead engine.
- Edit of every site that references the engine: `_adapters.py`,
  `core/config.py`'s `KNOWN_ENGINES`, the vendor-confinement invariant
  scan, and the comparison-batch test's engine-kind allowlist.
- Sweep of three test files that use `provider="luma"` as a free-form
  `LocalOutputSink` label — rename to `provider="replicate"` so future
  readers do not infer that a Luma direct engine still exists.
- README tombstone replacing the Luma row in
  `## Hosted Bearer providers (Replicate / Runway / Luma)`. Heading drops
  "/ Luma". Echo line `LUMAAI_API_KEY=...` stays, re-commented as
  "(used by Layer 5b UNI-1 keyframe engine)".
- `PROGRESS.md` Layer 5a entry under a new `### Phase 44` heading; the
  carry-forward note in `project_luma_video_retirement_2026.md` flips to
  CLOSED on commit.

### 3.2 Out of scope (will NOT happen in this layer)

- `LumaAgentsImageEngine` (Layer 5b — separate brainstorm).
- Any change to the `LUMAAI_API_KEY` env var name or `.env.example`
  Luma block (the block was trimmed in `0657799`; key is reused as-is).
- `BedrockVideoEngine`, `examples/configs/luma-ray.yaml`,
  `tests/live/test_luma_ray_live.py` — different provider entirely.
- Phase 43 historical entries in `PROGRESS.md` (frozen; Layer 5a appends a
  Phase 44 block).
- Completing the Phase 43 comparison-batch capstone (Task 15 stays
  deferred; it also depends on the Fal retrofit which is Task 7 / Layer 4
  carry-forward).
- Resurrecting any direct Luma video API.

## 4. File-level changes

```
DELETE   src/kinoforge/engines/luma/__init__.py
DELETE   tests/engines/test_luma.py
DELETE   examples/configs/comparison/luma-t2v.yaml

EDIT     src/kinoforge/_adapters.py
            drop `import kinoforge.engines.luma  # noqa: F401`
EDIT     src/kinoforge/core/config.py
            drop `"luma"` from `KNOWN_ENGINES` (line 81 at this writing)
EDIT     tests/test_core_invariant.py
            drop `SRC_ROOT / "engines" / "luma"` from the vendor-SDK
            confinement-scan list (line 126 at this writing)
EDIT     tests/test_examples.py
            tighten the comparison-YAML kind set from
            `{"replicate", "runway", "luma"}` to `{"replicate", "runway"}`
            (line 124 at this writing)
EDIT     tests/pipeline/test_generate_clip.py
            rename `provider="luma"` → `provider="replicate"` at lines 1480
            and 1488 (string literal; not an engine call)
EDIT     tests/outputs/test_local.py
            rename `provider="luma"` → `provider="replicate"` at lines 295,
            312, 315 (string literal; not an engine call)
            (model="ray-2" → model="seedance-1-lite" at the same sites
            to keep the test label coherent with the new provider)
EDIT     tests/outputs/test_format_filename.py
            rename `provider="luma"` → `provider="replicate"` at line 23
EDIT     README.md
            replace the Luma row in `## Hosted Bearer providers (Replicate
            / Runway / Luma)` with a 1-paragraph tombstone:
                The direct Luma Dream Machine video API was retired in
                2026. Reach Luma video via AWS Bedrock
                (`luma.ray-v2:0`, see Bedrock Video section) or Replicate
                (`luma/ray-flash-2`, see the Replicate row above). UNI-1
                image-keyframe support via `LumaAgentsImageEngine` is
                planned in Layer 5b.
            strip "Luma" from the section heading (now
            "## Hosted Bearer providers (Replicate / Runway)") and from
            any prose that lists the trio.
            keep the `echo 'LUMAAI_API_KEY=luma-zzzzz' >> .env` line, but
            re-comment it as "(used by Layer 5b UNI-1 image keyframes;
            direct video API retired)".
EDIT     PROGRESS.md
            add a new `### Phase 44 — Layer 5a (Luma direct-API retirement)`
            block at the end of the Post-MVP section; flip the
            `project_luma_video_retirement_2026` carry-forward note from
            "pending" to "CLOSED by Phase 44"; update the resume pointer if
            it still references the dead Luma block.
```

Reference line numbers are advisory at the time of writing; the
implementer should grep for the symbols before editing rather than trusting
the numbers verbatim.

The `provider="luma"` → `provider="replicate"` sweep may cascade into
downstream assertions within the same test (expected-filename strings,
hash-derived ids, golden-output literals). Re-run the relevant test files
after the rename and update any assertion that compares against a `luma`
substring; do not stop at the constructor call sites.

The PROGRESS Phase 43 carry-forward currently reads "3 of 15 comparison
YAMLs landed". After this layer's deletion of `comparison/luma-t2v.yaml`,
the count is 2 of 15. Update the Phase 43 carry-forward line accordingly
(this is a stale-fact fix, not a rewrite of historical entries — the same
line already records Task 10 as PARTIAL).

## 5. Commit plan

Two commits, both Conventional-Commits-styled, both in a single PR /
layer:

1. `chore(engines): retire LumaEngine — direct Dream Machine API ended 2026`
   - Every code and test deletion or edit listed in §4 except the README
     and `PROGRESS.md` changes.
   - Atomic: `git revert <sha>` restores LumaEngine, its tests, the
     `_adapters.py` import, the invariant entry, and the engine-kind
     allowlist in one step.
2. `docs(readme,progress): mark Luma direct API retired (Layer 5a)`
   - README tombstone, heading edit, echo-line recomment.
   - `PROGRESS.md` Phase 44 entry, carry-forward flip.

Commit 1 must precede commit 2 so the README tombstone is never out of
step with the missing code.

## 6. Verification

All offline; no live spend.

| Step | Command | Pass criterion |
|---|---|---|
| Static | `pixi run lint` | `ruff check` clean |
| Static | `pixi run format` | no diff |
| Static | `pixi run typecheck` | `mypy` 0 errors |
| Suite | `pixi run test` | every test green; total count drops by the deleted `test_luma.py` size (commit `4515ac4` reports 14 tests in that file — verify at execute time) |
| Hook | `pixi run pre-commit run --all-files` | green |
| Invariant | `tests/test_core_invariant.py` | confinement-scan still passes after `engines/luma` is removed from its list |
| Adapter sanity | `pixi run python -c "import kinoforge._adapters"` | clean import, no traceback (the deleted import does not dangle) |
| Loud-failure manual check | `pixi run kinoforge generate --config /tmp/luma-stub.yaml` where the stub has `engine.kind: luma` | raises `UnknownAdapter` at config load (no traceback through registry); manual one-off, not a regression test |

If any of these fail, the commit must NOT land; debug the regression and
re-run before pushing.

## 7. Rollback contract

- `git revert` on commit 1 restores all deleted files, the import line, the
  `KNOWN_ENGINES` entry, the invariant list entry, and the `provider=`
  label sweeps. Tests pass again with no further action.
- `git revert` on commit 2 restores the README Luma row and the
  `PROGRESS.md` Phase 44 entry.
- No state outside git changes during the layer. No env var is touched. No
  cloud resource is created or destroyed. No DB migration. The layer is
  trivially reversible from end of execution back to the pre-layer SHA.

## 8. Resumability

The layer is atomic enough that the durability rule (commit RED scaffolds
before any live spend) does not apply — there is no live spend. If the
session dies mid-edit:

- Working-tree state can be discarded with `git checkout -- .` because there
  is no scaffold to lose; the layer's value is in the eventual commit, not
  in any intermediate artifact.
- The implementer reads PROGRESS, this spec, and `git log --oneline -20`
  on resume, then continues from the first unchecked task in the plan.

## 9. Non-goals reaffirmed

This spec does NOT:

- Add `LumaAgentsImageEngine` or any UNI-1 image-keyframe code (Layer 5b).
- Modify any Bedrock-side Luma Ray code or YAML.
- Modify the `LUMAAI_API_KEY` env var or its `.env.example` block.
- Modify any Phase 43 historical PROGRESS entry.
- Address the Phase 43 comparison-batch capstone (Task 15) or the Fal
  retrofit (Task 7 carry-forward).

## 10. Open questions

None. Two design axes were resolved during brainstorming:

- **Transport for Layer 5b:** `urllib` (no new dep). Recorded here only as
  a forward-compatibility note; Layer 5b's spec will restate it.
- **Layer split:** delete (5a) and add (5b) ship as two separate layers
  to keep `git bisect` clean if either half regresses.
- **Free-form `provider="luma"` test labels:** swept to `"replicate"` in
  the same commit as the code deletion (option 2b at design time).
