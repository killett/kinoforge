# kinoforge

kinoforge is a configuration-driven video-generation orchestrator. It abstracts over GPU compute
providers (RunPod, SkyPilot, local), generation engines (ComfyUI, Diffusers, hosted APIs), and
model sources (HuggingFace, CivitAI, plain HTTPS) behind a single YAML config file and a small
CLI. Swapping providers, engines, or model sources requires only a config edit — no code changes,
no branching on provider names in core logic.

## Table of contents

- [Quickstart](#quickstart)
- [Installation](#installation)
- [Command cheatsheet](#command-cheatsheet)
- [Grid examples — verified](#grid-examples--verified)
  - [Wan 2.1 1.3B — strength sweep](#wan-21-13b--strength-sweep)
  - [Wan 2.1 1.3B — LoRA stack swap](#wan-21-13b--lora-stack-swap)
  - [Wan 2.1 1.3B — prompt sweep](#wan-21-13b--prompt-sweep)
  - [Wan 2.1 mixed path + generate](#wan-21-mixed-path--generate)
  - [Wan 2.1 model sweep — 1.3B vs 5B vs 14B](#wan-21-model-sweep--13b-vs-5b-vs-14b)
  - [Wan 2.2 14B — strength sweep](#wan-22-14b--strength-sweep)
  - [Wan 2.2 14B — LoRA stack swap](#wan-22-14b--lora-stack-swap)
  - [Wan 2.2 14B — prompt sweep](#wan-22-14b--prompt-sweep)
  - [Wan 2.2 14B mixed path + generate](#wan-22-14b-mixed-path--generate)
- [Configuration at a glance](#configuration-at-a-glance)
- [Credentials](#credentials)
- [Operator concepts](#operator-concepts)
  - Lifecycle → [docs/lifecycle.md](docs/lifecycle.md)
  - Warm-reuse → [docs/warm-reuse.md](docs/warm-reuse.md)
  - Engines → [docs/engines.md](docs/engines.md)
  - Cost and spend → [docs/cost-and-spend.md](docs/cost-and-spend.md)
  - Batch and grid → [docs/batch-and-grid.md](docs/batch-and-grid.md)
  - Cloud stores → [docs/cloud-stores.md](docs/cloud-stores.md)
  - Output layout → [docs/output-layout.md](docs/output-layout.md)
  - Breaking changes → [docs/breaking-changes.md](docs/breaking-changes.md)
  - Roadmap → [docs/roadmap.md](docs/roadmap.md)
- [Troubleshooting](#troubleshooting)
- [Contributing / extending](#contributing--extending)
- [Releasing](#releasing)
- [License](#license)

## Quickstart

### Step 1 — verify install (no credentials, ~30 s)

```bash
pixi run kinoforge generate \
  --config examples/configs/local-fake.yaml \
  --prompt "ocean waves at sunset" \
  --mode t2v
```

Uses `FakeEngine` + `LocalProvider`. No cloud account needed. Confirms the install works end-to-end.

### Step 2 — first real video (`FAL_KEY` only, ~1–2 min, few cents)

```bash
cp .env.example .env
# Edit .env: set FAL_KEY=<your key from fal.ai>
pixi run kinoforge generate \
  --config examples/configs/fal.yaml \
  --prompt "ocean waves at sunset" \
  --mode t2v \
  --no-reuse
```

Sends the job to fal.ai's hosted inference. The `--no-reuse` flag tears down the session when done.

### Step 3 — production (RunPod + Wan 14B)

Use `examples/configs/runpod-comfyui-wan-t2v-14b-2_2.yaml` as the starting point, then read:

- [docs/engines.md](docs/engines.md) — choose ComfyUI vs. Diffusers vs. hosted
- [docs/credentials.md](docs/credentials.md) — set `RUNPOD_API_KEY`, `HF_TOKEN`, `CIVITAI_TOKEN`
- [docs/warm-reuse.md](docs/warm-reuse.md) — understand pod warm-reuse and `--no-reuse`

## Installation

**Prerequisites:** git, [pixi](https://pixi.sh), POSIX shell.

```bash
git clone https://github.com/emmykillett/kinoforge.git
cd kinoforge
pixi install
```

**Optional feature environments** (install only when needed):

```bash
pixi install -e live-skypilot   # SkyPilot + gcloud + AWS CLI
pixi install -e live-hosted     # Replicate / Runway / Luma SDKs
```

**Optional system tools:** `aria2c` — multi-connection model weight downloads (recommended for
large models ≥10 GB).

**Verify:**

```bash
pixi run kinoforge --help
pixi run test
```

## Command cheatsheet

### CLI subcommands

| Verb | Purpose | Common invocation |
|------|---------|-------------------|
| `deploy` | Provision compute and deploy engine | `pixi run kinoforge deploy --config cfg.yaml` |
| `provision` | Provision an existing instance | `pixi run kinoforge provision --config cfg.yaml` |
| `doctor` | Validate config and credentials | `pixi run kinoforge doctor --config cfg.yaml` |
| `generate` | Run a generation job | `pixi run kinoforge generate --config cfg.yaml --prompt "…" --mode t2v` |
| `list` | List running instances from ledger | `pixi run kinoforge list` |
| `status` | Show status of one instance | `pixi run kinoforge status --id <id>` |
| `stop` | Stop an instance | `pixi run kinoforge stop --id <id>` |
| `destroy` | Destroy an instance | `pixi run kinoforge destroy --id <id>` |
| `forget` | Remove a stale ledger entry | `pixi run kinoforge forget --id <id>` |
| `reap` | Reap instances matching policy | `pixi run kinoforge reap` |
| `gc` | Garbage-collect stored artifacts | `pixi run kinoforge gc` |
| `cost` | Show cost report | `pixi run kinoforge cost` |
| `pod lora ls` | List LoRA inventory on a live pod | `pixi run kinoforge pod lora ls --id <id>` |
| `sweeper start` | Start the reap daemon | `pixi run kinoforge sweeper start` |
| `sweeper stop` | Stop the reap daemon | `pixi run kinoforge sweeper stop` |
| `sweeper status` | Read sweeper liveness | `pixi run kinoforge sweeper status` |
| `sweeper metrics` | Fetch sweeper metrics | `pixi run kinoforge sweeper metrics` |
| `batch` | Run a batch of generation jobs | `pixi run kinoforge batch --config cfg.yaml` |
| `grid` | Run a parameter grid of jobs | `pixi run kinoforge grid --config cfg.yaml` |

### Pixi tasks

| Task | Purpose | Notes |
|------|---------|-------|
| `pixi run test` | Run unit tests | Fast; no cloud creds required |
| `pixi run test-cov` | Run tests with coverage report | Adds `--cov` flag |
| `pixi run test-live` | Run live integration tests | Needs `KINOFORGE_LIVE_TESTS=1` + cloud creds |
| `pixi run test-live-skypilot` | Run SkyPilot live tests | Requires `live-skypilot` env |
| `pixi run probe-hosted` | Probe hosted provider endpoints | Requires `live-hosted` env |
| `pixi run preflight` | Check creds + zero active pods | Run before any live spend |
| `pixi run release` | Cut a release | See [docs/releasing.md](docs/releasing.md) |
| `pixi run probe-watchdog` | Probe pod watchdog tool | Diagnostics only |
| `pixi run lint` | Lint with ruff | `ruff check .` |
| `pixi run format` | Format with ruff | `ruff format .` |
| `pixi run typecheck` | Type-check with mypy | `mypy .` |
| `pixi run pre-commit` | Run pre-commit on staged files | |
| `pixi run pre-commit-all` | Run pre-commit on all files | |
| `pixi run pre-commit-install` | Install git pre-commit hook | Run once per checkout |
| `pixi run smoke-local` | Local CPU smoke suite | No cloud required |
| `pixi run smoke-21b-live` | Live smoke: Wan 2.1 21B | Costs ~$0.10–0.30 |
| `pixi run smoke-wan22-live` | Live smoke: Wan 2.2 14B | Costs ~$0.10–0.30 |
| `pixi run smoke-leak-sweep` | Sweep for cost/resource leaks | Runs leak-detection checks |

## Grid examples — verified

`kinoforge grid` composes N generations into one side-by-side mp4 with
per-cell captions. The 9 examples below each ship a committed spec YAML
under `examples/configs/grids/` (built from official, repo-verified
LoRA refs + prompts) and a committed live evidence file under
`tests/live/_grid_examples/` proving the example ran end-to-end on
RunPod. Schema details live in [`docs/batch-and-grid.md`](docs/batch-and-grid.md).

### Wan 2.1 1.3B — strength sweep

Sweep LoRA strength `{0.5, 1.0, 1.5}` on Static Rotation + Pokemon
Sprite. Generate cells force `--no-reuse`, so 3 cold-boot RTX A5000
pods (~$0.30).

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml \
        --out output/wan21-1_3b-strength.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_strength.json`](tests/live/_grid_examples/wan21_strength.json).

### Wan 2.1 1.3B — LoRA stack swap

3 distinct stacks (Static only / Pokemon only / both) on a single warm
pod via server-side `/lora/set_stack` swap. Cell-to-cell wall ~35s vs
~10min cold-boot. ~$0.003.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml \
        --out output/wan21-1_3b-loras-swap.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_loras_swap.json`](tests/live/_grid_examples/wan21_loras_swap.json).

### Wan 2.1 1.3B — prompt sweep

Override `prompt:` per cell to field-realistic / field-dreamlike /
forest text. 3 cold-boot RTX A5000 pods (generate cells force
`--no-reuse`). ~$0.10.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml \
        --out output/wan21-1_3b-prompt.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_prompt.json`](tests/live/_grid_examples/wan21_prompt.json).

### Wan 2.1 mixed path + generate

2 `path:` cells reuse mp4 fixtures from the strength + prompt sweeps
above + 1 `generate:` cell renders fresh. Path cells skip compute.
~$0.04.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml \
        --out output/wan21-mixed-path.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_mixed_path.json`](tests/live/_grid_examples/wan21_mixed_path.json).

### Wan 2.1 model sweep — 1.3B vs 5B vs 14B

3 different `config:` per cell → 3 distinct CapabilityKeys → 3 parallel
pods (1.3B diffusers + 5B comfyui + 14B comfyui). No LoRAs. ~$0.50.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-model-sweep.grid.yaml \
        --out output/wan21-model-sweep.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_model_sweep.json`](tests/live/_grid_examples/wan21_model_sweep.json).

### Wan 2.2 14B — strength sweep

Arcane LoRA pair high+low strengths `{0.5, 1.0, 1.5}` on A100 80GB
PCIe ($1.49/hr). 3 cold-boot pods (generate cells force `--no-reuse`).
~$1.10.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-strength-sweep.grid.yaml \
        --out output/wan22-14b-strength.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_strength.json`](tests/live/_grid_examples/wan22_strength.json).

### Wan 2.2 14B — LoRA stack swap

Arcane high-only / low-only / both-stacked on a single warm A100 80GB
pod via `/lora/set_stack`. Tests the MoE pair (high_noise + low_noise
branches). Group wall ~8.5min for 3 cells. ~$0.20.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-loras-swap.grid.yaml \
        --out output/wan22-14b-loras-swap.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_loras_swap.json`](tests/live/_grid_examples/wan22_loras_swap.json).

### Wan 2.2 14B — prompt sweep

3 different prompts (field-realistic / field-dreamlike / dawn-flight)
on Arcane LoRA stack, A100. Cell 0 verified live; cells 1+2 blocked
by Wan 2.2 14B A100 cold-boot reliability variance (~50% of fresh
pods stall on HF weight DL or pip install). ~$1.35.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml \
        --out output/wan22-14b-prompt.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_prompt.json`](tests/live/_grid_examples/wan22_prompt.json).

### Wan 2.2 14B mixed path + generate

2 `path:` cells (Wan 2.2 fixtures from the strength + prompt sweeps
above) + 1 fresh `generate:` cell. ~$0.35.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml \
        --out output/wan22-14b-mixed-path.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_mixed_path.json`](tests/live/_grid_examples/wan22_mixed_path.json).

## Configuration at a glance

A kinoforge YAML config has three top-level blocks. **`engine`** declares the inference backend
(e.g. `comfyui`, `diffusers`, `fake`, `hosted`) and its parameters. **`models`** lists the base
checkpoint, optional LoRAs, and VAE — each with a `source` URI that kinoforge resolves
automatically from HuggingFace, CivitAI, or plain HTTPS. **`compute`** names the cloud provider
(e.g. `runpod`, `skypilot`, `local`) and its resource requirements (GPU type, VRAM, disk, image).

Canonical example configs in `examples/configs/`:

| File | Use-case |
|------|---------|
| `local-fake.yaml` | Offline smoke test; FakeEngine + LocalProvider; no credentials |
| `fal.yaml` | Hosted inference via fal.ai; `FAL_KEY` only |
| `runpod-comfyui-wan-t2v-14b-2_2.yaml` | RunPod + ComfyUI + Wan 2.2 14B t2v (recommended production config) |
| `runpod-diffusers-wan-t2v-14b-2_2.yaml` | RunPod + Diffusers + Wan 2.2 14B t2v |
| `hosted.yaml` | Multi-provider hosted comparison (Replicate / Runway / Luma) |
| `nova-reel.yaml` | AWS Nova Reel hosted engine |
| `luma-ray.yaml` | Luma Ray hosted engine |
| `skypilot-lambda.yaml` | SkyPilot on Lambda Labs GPU |

Full reference (all keys, precedence, override flags): [docs/configuration.md](docs/configuration.md).

## Credentials

Copy the example file and fill in the keys you need:

```bash
cp .env.example .env
chmod 600 .env
```

kinoforge loads `.env` automatically on startup. Shell-exported variables take precedence over
`.env` values. Never commit `.env`.

**Known keys:**

| Variable | Required for | Description |
|----------|-------------|-------------|
| `FAL_KEY` | fal.ai hosted | API key from [fal.ai](https://fal.ai) dashboard |
| `CIVITAI_TOKEN` | CivitAI model downloads | Personal token from CivitAI account settings |
| `HF_TOKEN` | Gated HuggingFace models | HuggingFace access token (gated models only) |
| `RUNPOD_API_KEY` | RunPod provider | API key from [runpod.io](https://runpod.io) console |
| `REPLICATE_API_TOKEN` | Replicate hosted | API token from [replicate.com](https://replicate.com) account |
| `RUNWAYML_API_SECRET` | Runway hosted | API secret from [runwayml.com](https://runwayml.com) dashboard |
| `LUMAAI_API_KEY` | Luma hosted | API key from [lumalabs.ai](https://lumalabs.ai) developer portal |

Precedence: shell environment > `.env` file. See [docs/credentials.md](docs/credentials.md) for
deeper auth-strategy detail (per-provider auth flows, KMS-backed secrets, CI/CD patterns).

## Operator concepts

**Lifecycle** ([docs/lifecycle.md](docs/lifecycle.md)) — every kinoforge instance transitions
through a defined state machine (idle → provisioned → deployed → generating → done/error). The
lifecycle engine enforces budget caps, idle timeouts, and max-lifetime limits automatically.

**Warm-reuse** ([docs/warm-reuse.md](docs/warm-reuse.md)) — by default, pods survive after a
generation completes so the next call can attach without paying the cold-boot tax (~10 min on
RunPod). Pass `--no-reuse` for one-shot runs to auto-destroy on completion.

**Engines** ([docs/engines.md](docs/engines.md)) — kinoforge supports ComfyUI, Diffusers, FakeEngine
(offline testing), and hosted Bearer providers (Replicate, Runway, Luma, fal.ai, Nova Reel).
Each engine exposes the same `generate()` interface; switching is a config-only change.

**Cost and spend** ([docs/cost-and-spend.md](docs/cost-and-spend.md)) — `kinoforge cost` pulls
runtime spend from provider APIs and (optionally) BigQuery billing export. Budget caps in the
YAML config stop runaway pods before they accrue surprise charges.

**Batch and grid** ([docs/batch-and-grid.md](docs/batch-and-grid.md)) — `batch` runs a list of
independent jobs sequentially or in parallel; `grid` expands a parameter sweep (LoRA strengths,
prompts, resolutions) into a full factorial run with a single config file.

**Cloud stores** ([docs/cloud-stores.md](docs/cloud-stores.md)) — output artifacts and the run
ledger can be stored locally, on GCS, or on S3. The `cfg.store` block controls the backend;
switching backends migrates the ledger automatically.

**Output layout** ([docs/output-layout.md](docs/output-layout.md)) — generated videos, metadata
JSON, and sidecar files follow a deterministic directory hierarchy keyed on run-id, mode, and
model slug, making outputs reproducible and diff-friendly.

**Breaking changes** ([docs/breaking-changes.md](docs/breaking-changes.md)) — a versioned log of
config-schema and CLI changes that require operator action on upgrade.

**Roadmap** ([docs/roadmap.md](docs/roadmap.md)) — planned engines, providers, and capability axes
for upcoming releases.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `pixi run kinoforge: command not found` | `pixi install` not run | Run `pixi install` from repo root |
| `AuthError` on first generate | Missing `.env` or unset key | `cp .env.example .env`; `chmod 600 .env`; fill required keys |
| `kinoforge list` shows pod that no longer exists | Provider destroyed it out-of-band | `pixi run kinoforge forget --id <id>` |
| Pod still alive after `generate` completes | Default is warm-reuse | Pass `--no-reuse` for one-shots, or `pixi run kinoforge destroy --id <id>` post-hoc |
| `ProxyWarmupTimeout` on first POST | RunPod proxy 502 race during pod warmup | Auto-retried; if persistent, `pixi run kinoforge status --id <id>` then `destroy` |
| ComfyUI poll timeout (~10 min default) | Wan 14B t2v can take ~6 min/step | Raise `engine.comfyui.poll_timeout_s` in YAML |
| `cli-loras-bypass-vault` WARNING | `--loras` override fired while `vault.loras` was non-empty | Intentional; vault file on disk is unchanged |
| `error: cfg.store ({…}) differs from sidecar` | Switched to/from cloud ledger backend | See [docs/cloud-stores.md](docs/cloud-stores.md) |
| `gcp_status: export-not-ready` on `kinoforge cost` | <24 h since BigQuery billing export was enabled | Wait for first table (up to 24 h) |
| GPU sits 0% during live smoke | Worker died silently | Kill pod immediately; see [docs/troubleshooting.md](docs/troubleshooting.md) deep section |

Full catalogue: [docs/troubleshooting.md](docs/troubleshooting.md).

## Contributing / extending

Contributions are welcome. New engines and providers follow the plugin pattern described in
[docs/extending.md](docs/extending.md) — implement the relevant abstract base class, add a YAML
`type:` discriminator, and the orchestrator picks it up automatically. For agent-driven
development workflow and subagent conventions, see [AGENTS.md](AGENTS.md).

## Releasing

See [docs/releasing.md](docs/releasing.md) for the full release checklist and version-bump procedure.

## License

SPDX-License-Identifier: MIT — see [LICENSE](LICENSE).
