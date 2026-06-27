# README rewrite — design

**Date:** 2026-06-27
**Author:** Dr. Twinklebrane (operator) + Claude (drafting)
**Status:** validated; ready for implementation plan
**Scope:** replace stale repo-root `README.md` with a focused entry-point document; relocate the existing deep-dive content into per-topic files under `docs/`.

## Problem

The current `README.md` (2032 lines) accreted incrementally across ~12 phases / Layers / B-tasks. It works as a reference for an operator who already knows kinoforge's internal taxonomy, but it fails the three jobs the operator actually wants from a README:

1. **First-touch onboarding.** No table of contents; no clearly marked "do this first" path.
2. **Lookup-while-stuck.** Subcommands, pixi tasks, and failure-mode fixes are scattered across deep-dive sections.
3. **Navigation.** Every major topic is at the top level; nothing distinguishes the 5-minute install path from the 50-page LoRA-swap deep dive.

## Solution

A **two-tier documentation surface**:

- **`README.md`** — focused entry-point (~400 lines). Quickstart + install + cheatsheet + credentials + brief operator-concept tour with links + symptom-driven troubleshooting + license. New operators succeed here; returning operators use it as a TOC.
- **`docs/<topic>.md`** — one file per operator concept. Absorbs the existing deep-dive sections verbatim, organized by **what the operator is trying to do**, not by internal Layer name.

No deep-dive content is deleted. Every existing `##` and `###` block in the current README is either retained in the new README, moved 1:1 to a `docs/` file, or compressed to a 1–2-sentence summary with a link.

## README outline (target ~400 lines)

```
# kinoforge
  one-paragraph what-it-is

## Table of contents
  - Quickstart
  - Installation
  - Command cheatsheet
  - Configuration at a glance
  - Credentials
  - Operator concepts (links to docs/<topic>.md)
  - Troubleshooting
  - Contributing / extending
  - Releasing
  - License

## Quickstart (tiered ladder)
  Step 1 — verify install
    examples/configs/local-fake.yaml; FakeEngine + LocalProvider; no creds; ~30 s
  Step 2 — first real video
    examples/configs/fal.yaml; FAL_KEY only; ~1–2 min; a few cents
  Step 3 — production
    examples/configs/runpod-comfyui-wan-t2v-14b-2_2.yaml
    pointer to docs/engines.md + docs/credentials.md + docs/warm-reuse.md

## Installation
  Prerequisites: git, pixi (link to install), POSIX shell
  Clone + `pixi install`
  Optional feature envs: `live-skypilot`, `live-hosted`
  Optional system tools: `aria2c` (faster model downloads)
  Verify: `pixi run kinoforge --help`; `pixi run test`

## Command cheatsheet
  Table 1 — `kinoforge` subcommands (one row per verb)
  Table 2 — pixi tasks (test / lint / format / typecheck / smoke-* /
              preflight / probe-hosted / probe-watchdog / release)

## Configuration at a glance
  Three-block YAML shape: engine / models / compute
  Brief paragraph + pointer to docs/configuration.md
  Table of canonical example configs from examples/configs/

## Credentials
  .env workflow (retained in README — short, every operator hits this)
  Known-keys table (FAL_KEY, CIVITAI_TOKEN, HF_TOKEN, RUNPOD_API_KEY,
                   REPLICATE_API_TOKEN, RUNWAYML_API_SECRET, LUMAAI_API_KEY)
  Precedence (shell > .env); deeper auth-strategy detail → docs/credentials.md

## Operator concepts
  One paragraph per concept + link to its docs/ file. Concepts:
    Lifecycle (status / reap / sweeper / heartbeat)   → docs/lifecycle.md
    Warm reuse (B4 + LoRA-flexible + grid lora_swap)  → docs/warm-reuse.md
    Engines (comfyui, diffusers, hosted, fal,
             replicate, runway, bedrock, veo, keyframe) → docs/engines.md
    Cost and spend (cost dashboard, Replicate throttle) → docs/cost-and-spend.md
    Batch + grid                                       → docs/batch-and-grid.md
    Cloud stores (cloud-backed ledger, multi-host)     → docs/cloud-stores.md
    Output layout                                      → docs/output-layout.md
    Breaking changes                                   → docs/breaking-changes.md
    Roadmap                                            → docs/roadmap.md

## Troubleshooting
  Symptom → likely cause → fix table (~10 entries; high-frequency only).
  Pointer to docs/troubleshooting.md for the full catalogue.

## Contributing / extending
  One paragraph + link to docs/extending.md and AGENTS.md.

## Releasing
  Pointer to docs/releasing.md (full procedure lives there).

## License
  SPDX + LICENSE link.
```

## docs/ file inventory

One file per operator concept. Each absorbs the listed current-README sections verbatim, with a header line `(Moved from README §<old-section-name> on 2026-06-27.)` so inbound links from `PROGRESS.md`, specs, and plans degrade gracefully into a "see new home" pointer.

| New file | Absorbs current README sections |
|---|---|
| `docs/configuration.md` | §Configuration; §Per-job spec & params (incl. `spec.*` per engine, `params:` vs `spec.params:`, `validate_spec` failure); §HuggingFace ref grammar |
| `docs/credentials.md` | §Credentials deep dive; §Auth strategies; §Faster downloads (aria2c); §Cloud bootstrap (Layer W+α) |
| `docs/lifecycle.md` | §Operator commands (status, heartbeat persistence Layer U); §Reaping orphan pods (Layer V); §Sweeper daemon (B1/W); §Forget; §Interrupting a generation; §Configurable ComfyUI poll timeout |
| `docs/warm-reuse.md` | §Operator warm-reuse (B4); §LoRA-flexible warm-reuse (Wan 2.2 + Diffusers) incl. eviction policy, per-pod swap lock, `--loras`, `--dry-run-swap`, `pod lora ls`, failure modes, `lora_swap_re_probe_after_s`, deferred limitations; §Default test LoRAs (Wan 2.1 1.3B + Wan 2.2); §Smoke test pyramid; §`kinoforge grid` `lora_swap:` cells |
| `docs/engines.md` | §Real providers — fal.ai; §Hosted Bearer providers (Replicate / Runway); §Bedrock Video (Nova Reel, Luma Ray v2); §Keyframe stage (i2v, flf2v, implementation note); §Real providers — RunPod; §Diffusers inference-server response contract; §Hosted response URL — `url_path`; §Cross-engine prompt routing; §Engine asset wiring |
| `docs/cost-and-spend.md` | §Cost dashboard (B2/X) incl. balance read-out, caching, Prometheus textfile, Replicate throttle |
| `docs/batch-and-grid.md` | §Batch generation incl. streaming output; §`kinoforge grid` (top-level `generate:` / `path:` variants — `lora_swap:` lives in warm-reuse.md) |
| `docs/cloud-stores.md` | §Cloud-backed ledger; §Multi-host setup; §Migration from a local ledger; §Multi-node coordination; §Remote provisioning; §Cloud stores |
| `docs/extending.md` | §Extending: add a provider/source/engine (ComputeProvider, ModelSource, GenerationEngine, Splitter, ArtifactStore) |
| `docs/output-layout.md` | §Output directory incl. configuring it and `--run-id` change |
| `docs/troubleshooting.md` | NEW. Deeper symptom catalogue (covers every failure mode named in the current README + the failure-mode tables already there) |
| `docs/breaking-changes.md` | §Breaking changes (Layer T cloud `store.kind`; Layer M `engine.hosted.model`) + room for future entries |
| `docs/roadmap.md` | §Roadmap (deferred layers and their seams) |
| `docs/releasing.md` | §Releasing |

## Command cheatsheet — exact shape

**Table 1 — `kinoforge` subcommands.** Source of truth: `sub.add_parser(...)` calls in `src/kinoforge/cli/_main.py`. Columns: `Verb | Purpose (verbatim from add_parser help=) | Common invocation`.

Verbs to include (full set from `_main.py`): `deploy`, `provision`, `doctor`, `generate`, `list`, `status`, `stop`, `destroy`, `forget`, `reap`, `gc`, `cost`, `pod lora ls`, `sweeper {start,stop,status,metrics}`, `batch`, `grid`.

**Table 2 — pixi tasks.** Source: `[tasks]` block in `pixi.toml`. Columns: `Task | Purpose | Notes`.

Tasks to include: `kinoforge`, `test`, `test-cov`, `test-live`, `test-live-skypilot`, `probe-hosted`, `preflight`, `release`, `probe-watchdog`, `lint`, `format`, `typecheck`, `pre-commit`, `pre-commit-all`, `pre-commit-install`, `smoke-local`, `smoke-21b-live`, `smoke-wan22-live`, `smoke-leak-sweep`.

## Troubleshooting — README-level table (~10 entries)

Symptom → likely cause → fix. Each row links to the deeper section in `docs/troubleshooting.md` when there is one. Initial rows:

1. `pixi run kinoforge: command not found` → `pixi install` not yet run → `pixi install` from repo root.
2. `AuthError` on first generate → missing `.env` or unset key → `cp .env.example .env`; `chmod 600 .env`; fill required keys (see Credentials).
3. `kinoforge list` shows pod that no longer exists → provider destroyed it out-of-band → `kinoforge forget --id <id>`.
4. Pod still alive after `generate` completes → default is warm-reuse → pass `--no-reuse` for one-shots, or `kinoforge destroy --id <id>` post-hoc.
5. `ProxyWarmupTimeout` on first POST → RunPod proxy 502 race during pod warmup → already auto-retried; if persistent, `kinoforge status --id <id>` then `destroy`.
6. ComfyUI poll timeout (~10 min default) → Wan 14B t2v can take ~6 min/step → raise `engine.comfyui.poll_timeout_s` in YAML.
7. `cli-loras-bypass-vault` WARNING → `--loras` override fired while vault.loras was non-empty → intentional; vault file on disk is unchanged.
8. `error: cfg.store ({...}) differs from sidecar` → switched to / from cloud ledger → see `docs/cloud-stores.md` migration steps.
9. `gcp_status: export-not-ready` on `kinoforge cost` → < 24 h since BigQuery billing export was enabled → wait for first table to land.
10. GPU sits 0 % during a live smoke → worker died silently → kill pod immediately; see CLAUDE.md live-smoke polling rule + `docs/troubleshooting.md` deep section.

## Migration contract

- **No content deleted.** Every current `##` / `###` block ends up in either the new README or a `docs/` file.
- **Inbound link preservation.** Every `docs/` file that absorbs a moved section gets a `(Moved from README §<name> on 2026-06-27.)` header so an operator landing via a stale link (e.g. a Slack pointer to `README.md#operator-warm-reuse`) finds the new home immediately.
- **PROGRESS.md, specs, plans, and AGENTS.md** retain their existing pointer text; their references to README sections are converted to `docs/<file>.md` pointers in the same commit as the rewrite.
- **Anchors.** The README TOC links use Markdown auto-generated anchors (no manual `<a name="…">`); the `docs/` filenames are stable, kebab-case, and operator-concept-named so they are themselves anchorable.

## Non-goals

- Not rewriting the deep-dive prose. Move-then-tighten is out of scope for this rewrite; later passes can edit individual `docs/` files.
- Not changing `AGENTS.md`, `CLAUDE.md`, `DESIGN.md`, `SPEC.md`, or any spec / plan under `docs/superpowers/`.
- Not adding new content beyond the README's top-tier shape and the new `docs/troubleshooting.md`.

## Acceptance

- `README.md` ≤ 500 lines.
- README opens with a paragraph, a TOC, and a Quickstart whose Step 1 succeeds with zero credentials.
- Every `kinoforge` subcommand listed in `cli/_main.py` appears in the cheatsheet table.
- Every pixi `[tasks]` entry appears in the pixi-task table.
- Every operator concept in the README points to exactly one `docs/<topic>.md` file that exists in the same commit.
- No deep-dive section in the current README is dropped without a documented destination in `docs/`.
- `pixi run pre-commit run --all-files` is clean after the rewrite.

## References

- Current `README.md` (head of `main` at 2026-06-27, commit `5838dca`).
- `CLAUDE.md` (durability rules; live-smoke polling; teardown rule).
- `PROGRESS.md` (current workstream pointers — informs which docs/ files inherit which absorbed sections).
- `src/kinoforge/cli/_main.py` (source of truth for the subcommand table).
- `pixi.toml` `[tasks]` (source of truth for the pixi-task table).
