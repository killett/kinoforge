# Troubleshooting

(New as of 2026-06-27 README rewrite. Top-tier short table lives in [../README.md](../README.md#troubleshooting); this file is the deeper symptom catalogue.)

## Install and environment

| Symptom | Likely cause | Fix |
|---|---|---|
| `pixi run kinoforge: command not found` | `pixi install` not run yet after clone | Run `pixi install` from the repo root, then retry. |
| `aria2c: command not found` warning in logs | Optional system accelerator absent — kinoforge falls back to stdlib transport silently | `sudo apt install aria2` (Debian/Ubuntu) or `brew install aria2` (macOS). No config needed. See [credentials.md](credentials.md#faster-downloads-aria2c). |
| pre-commit hook aborts with `pixi.lock not staged` | Editing `pixi.toml` triggers automatic `pixi.lock` regen; hook stash-conflict fires if only `pixi.toml` was staged | Stage both files: `git add pixi.toml pixi.lock` before committing. See [../CLAUDE.md](../CLAUDE.md#pre-commit-stages-pixilock). |
| `ModuleNotFoundError: replicate` (or `runwayml`) when running hosted engines | Replicate / Runway SDKs live only in the `live-hosted` pixi feature env | Use `pixi run -e live-hosted kinoforge ...` for all hosted-provider commands. See [engines.md](engines.md#hosted-bearer-providers-replicate--runway). |
| `gcloud`/`aws` command not found | Cloud CLI binaries live only in the `live-skypilot` env | Use `pixi run -e live-skypilot gcloud ...` or `pixi run -e live-skypilot aws ...`. See [../CLAUDE.md](../CLAUDE.md#cloud-cli-invocation-gcloud-aws-sky). |

## Credentials and .env

| Symptom | Likely cause | Fix |
|---|---|---|
| `AuthError: RUNPOD_API_KEY not set` (or similar) | `.env` file missing or key absent | `cp .env.example .env && chmod 600 .env`, then fill in the key. See [credentials.md](credentials.md#credentials). |
| Shell-set env var does not match what's in `.env` | Shell exports always take precedence over `.env` values | CI / prod exports win; use `kinoforge --env-file /other/.env` for an explicit override. See [credentials.md](credentials.md#precedence). |
| HF gated model download returns HTTP 401 | `HF_TOKEN` absent or expired | Add a valid `HF_TOKEN` to `.env`. See [credentials.md](credentials.md#known-keys). |
| CivitAI download returns 401 | `CIVITAI_TOKEN` absent | Add `CIVITAI_TOKEN` to `.env`. Required for gated/private CivitAI models. See [credentials.md](credentials.md#known-keys). |
| AWS / GCP SDK reports `Unable to locate credentials` outside `pixi run` | `pixi.toml [activation.env]` wires credential paths only inside `pixi run`; bare shell does not set them | Invoke via `pixi run python -m ...` or export `AWS_SHARED_CREDENTIALS_FILE` / `GOOGLE_APPLICATION_CREDENTIALS` manually. See [../CLAUDE.md](../CLAUDE.md#cloud-cli-invocation-gcloud-aws-sky). |

## Compute and provider lifecycle

| Symptom | Likely cause | Fix |
|---|---|---|
| `kinoforge list` shows a pod that no longer exists on the provider | Provider destroyed the pod out-of-band (OOM kill, billing stop, expiry) while ledger was not updated | `kinoforge forget --id <id>` to clear the stale entry. See [lifecycle.md](lifecycle.md#kinoforge-forget---id-id----clear-a-stale-ledger-entry). |
| Pod still alive and billing after `kinoforge generate` completes | Default behaviour is warm-reuse — pod stays warm for the next call | Pass `--no-reuse` for one-shot runs, or destroy post-hoc: `kinoforge destroy --id <id>`. See [warm-reuse.md](warm-reuse.md#operator-warm-reuse-b4) and [../CLAUDE.md](../CLAUDE.md#live-smoke-teardown-pass---no-reuse-verify-post-run). |
| GPU utilisation at 0% for ≥3 consecutive probes during a live smoke | Worker process crashed silently — generation is not progressing | Kill the pod immediately: capture last 100 log lines, then `kinoforge destroy --id <id>`. See [../CLAUDE.md](../CLAUDE.md#live-smoke-monitoring-poll-dont-wait). |
| `kinoforge status` reports `provider_status=unknown (stale ledger)` | Ledger entry is orphaned — provider has no record of the id | Run `kinoforge forget --id <id>`. See [lifecycle.md](lifecycle.md#operator-commands). |
| `ORPHAN_REAP` verdict reported by `kinoforge reap` but pod not destroyed | `ORPHAN_REAP` requires explicit opt-in — default policy does not act on it | Pass `--include-orphans` alongside `--apply`: `kinoforge reap -c cfg.yaml --apply --include-orphans`. See [lifecycle.md](lifecycle.md#reaping-orphan-pods). |
| `kinoforge generate` refuses to attach with verdict `HEARTBEAT_UNKNOWN` | Heartbeat thread state unknown; reuse blocked by default | Pass `--force-attach` to override salvageable verdicts. See [warm-reuse.md](warm-reuse.md#--force-attach-matrix). |

## Engine and generation failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `ProxyWarmupTimeout` on first POST to a new pod | RunPod proxy 502 race during the first ~60s after `wait_for_ready` returns | The client auto-retries; if persistent, check pod health with `kinoforge status --id <id>` then `kinoforge destroy --id <id>` and retry cold. See [engines.md](engines.md#real-providers--runpod). |
| ComfyUI poll times out (default 600s) on Wan 14B t2v | Wan 14B t2v can take ~6 min per step; default ceiling too low | Raise `engine.comfyui.poll_timeout_s` in your YAML. See [lifecycle.md](lifecycle.md#configurable-comfyui-poll-timeout). |
| `LoraSwapDegradedPodError` | Pod entered half-state mid-swap; ledger marks `status=degraded` | Reaper destroys the pod on the next sweep; matcher routes subsequent jobs elsewhere or cold-boots. Use `kinoforge destroy --id <id>` for immediate cleanup. See [warm-reuse.md](warm-reuse.md#failure-modes-operator-one-liners). |
| `LoraSwapVramOomError` | LoRA stack exceeds VRAM; pod rolls back to previous adapter set | Pod itself is healthy — try a smaller stack or a different pod. See [warm-reuse.md](warm-reuse.md#failure-modes-operator-one-liners). |
| `cli-loras-bypass-vault` WARNING on stderr | `--loras` flag fired while `vault.loras` was non-empty | Intentional audit warning — vault on disk is unchanged; the override applied only for this run. See [warm-reuse.md](warm-reuse.md#kinoforge-generate---loras----cli-lora-stack-override). |
| Runway returns HTTP 403 `Model variant not available` | Model variant gated per-account; surfaces as `KinoforgeError`, not raw 403 | Switch to `gen4.5` (generally available) or request access for the gated variant. See [engines.md](engines.md#live-smoke-prompt-size--model-entitlement-caveats). |

## Cost and spend leaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `error: cfg.store ({...}) differs from sidecar` on deploy | Cloud-ledger backend switched while in-flight pods are still recorded locally | Destroy all local-tracked instances first (`kinoforge destroy --id <id>`), then remove `state_dir/store.json` and re-deploy. See [cloud-stores.md](cloud-stores.md#migration-from-a-local-ledger). |
| `gcp_status: export-not-ready` on `kinoforge cost` | BigQuery billing export enabled less than 24h ago; first table not yet landed | Wait for the first table (6–24h lag). The report returns partial data with this status in the meantime. See [../PROGRESS.md](../PROGRESS.md). |
| Replicate spend warning: `> 90% of throttle` | `KINOFORGE_REPLICATE_THROTTLE_AT_USD` threshold approached | Reduce Replicate run cadence or raise the threshold via `KINOFORGE_REPLICATE_THROTTLE_AT_USD=N`. See [cost-and-spend.md](cost-and-spend.md#replicate-throttle-warning). |
| Pod keeps billing after `kinoforge generate` exits (mid-run crash, no `--no-reuse`) | Warm-reuse keeps pod alive; crash before teardown leaves it running | Run `kinoforge reap -c cfg.yaml --apply` to destroy idle/overage pods, or `kinoforge sweeper start` for continuous background reaping. See [lifecycle.md](lifecycle.md#sweeper-daemon-b1--layer-w). |
