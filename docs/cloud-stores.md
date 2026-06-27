# Cloud stores and multi-host coordination

(Moved from README §Cloud-backed ledger, §Multi-host setup, §Migration from a local ledger, §Multi-node coordination, §Remote provisioning, §Cloud stores on 2026-06-27. See [../README.md](../README.md).)

## Cloud-backed ledger

When `store.kind` is `s3` or `gcs` in your `kinoforge.yaml`, the instance
ledger (the list of running pods, their providers, their lifecycle policy
snapshots) is persisted in the configured artifact store — not on the host
that ran `kinoforge deploy`. The ledger lives at
`<store-uri>/_lifecycle/ledger.json`.

On first run of a cfg-bearing command (`deploy`, `provision`, `generate`,
`gc`, `batch`), kinoforge writes a sidecar at `<state-dir>/store.json`
recording which store backs the ledger. Subsequent no-config commands
(`list`, `stop`, `destroy`, `forget`, `reap`) read the sidecar and
construct the matching store transparently — no `--config` flag needed.

```yaml
# kinoforge.yaml
engine:
  kind: fake  # or hosted / diffusers / comfyui / fal
  precision: fp16
models:
  - kind: base
    name: m
    ref: fake://m
    target: checkpoints
store:
  kind: s3
  bucket: kf-prod
  prefix: kinoforge
```

```bash
# Host A — first command writes the sidecar
$ kinoforge deploy --config kinoforge.yaml
[instance overview] No running instances.
deployed: instance='i-abc'

# Host B — once it has its own sidecar (see "Multi-host setup" below),
# `kinoforge list` reads the same S3 ledger as Host A
$ kinoforge deploy --dry-run --config kinoforge.yaml  # writes Host-B sidecar
$ kinoforge list
  i-abc  provider=runpod
```

If you change `cfg.store` and re-run a cfg-bearing command, kinoforge
hard-errors with `error: cfg.store ({...}) differs from sidecar ({...});
remove <path> or revert cfg.store to switch`. Remove `state_dir/store.json`
to explicitly opt into the switch — but read the migration steps below first.

## Multi-host setup

The sidecar is per-host: every host's `.kinoforge/store.json` must be
written before its first state-mutating command. **The first command per
host MUST be cfg-bearing** (e.g. `kinoforge deploy --dry-run --config
kinoforge.yaml`) so the sidecar gets written. A no-config command on a
fresh host with no sidecar falls back to a local `state_dir` ledger,
meaning kinoforge will not see the instances tracked in the shared
cloud ledger, and the duplicate-instance guard in `kinoforge deploy`
may not fire.

This is a documented v1 constraint. A future layer will add
`--store-uri s3://kf-prod` (or `KINOFORGE_STORE_URI`) so that any
command can bootstrap its own sidecar from a single flag.

## Migration from a local ledger

If you previously used a cloud `store.kind` for artifacts but the
ledger lived locally (pre-Layer-T behaviour), kinoforge will refuse
to switch to a cloud-backed ledger while in-flight pods are still
recorded locally. The error is:

```
error: refusing to switch to cloud store (s3) while local ledger has
entries; run `kinoforge destroy` on each local-tracked instance, then
re-run
```

Migration steps:

1. `kinoforge list` — inventory in-flight instances tracked locally.
2. `kinoforge destroy --id <id>` for each — empties the local ledger.
3. Upgrade to the Layer T release.
4. `kinoforge deploy --config kinoforge.yaml` — writes the sidecar,
   opens a fresh cloud-backed ledger.

## Multi-node coordination

Once the sidecar wires every host at the same store, multi-node
deployments where several `kinoforge` workers point at one shared
artifact store (S3 or GCS) are coordinated by a lease-based mutex
returned from `ArtifactStore.acquire_lock(key, *, ttl_s)`. Local-disk
stores use `fcntl.flock`; S3 uses conditional PUT (`If-None-Match: *`);
GCS uses native `if_generation_match=0`.

Two surfaces use the lock automatically:

1. **Profile discovery** (`JsonProfileCache.resolve_or_discover`) — only one
   worker probes the live model for a given `CapabilityKey`; followers read
   the cached profile.
2. **Ledger mutations** (`Ledger.record`, `Ledger.forget`) — read-modify-write
   stays atomic across workers; entries cannot be lost to concurrent updates.
   Under Layer T's cloud-backed ledger, this is the mechanism that lets
   two CLI invocations on different hosts both land their entries.

Semantics are best-effort: a holder that dies mid-hold has its lease expire
after `ttl_s`, at which point another acquirer can steal. There are no
fencing tokens. Sized TTLs absorb modest clock skew.

Tune via constructor kwargs (no YAML surface):

```python
JsonProfileCache(store, discover_ttl_s=600.0)
Ledger(store, mutate_ttl_s=60.0)
```

## Remote provisioning

Engines that talk to a remote pod (ComfyUI, Diffusers on RunPod / SkyPilot)
bootstrap via `engine.render_provision(cfg)`. The engine emits a self-
contained bash script that clones its repo, installs dependencies, downloads
weights, and launches the inference HTTP server. The orchestrator validates
declared credential env vars, attaches the rendered payload to
`InstanceSpec`, and the provider injects it via its native boot-script
mechanism (RunPod base64-encoded env var + `dockerArgs`; SkyPilot
`Task.setup` / `Task.run`).

After the pod boots, `engine.provision(instance, cfg)` polls an engine-
specific ready endpoint (ComfyUI: `/system_stats`; Diffusers: `/health`)
until HTTP-200, the pod status flips terminal, or
`cfg.lifecycle.boot_timeout` (default 900s) elapses. Failures raise
`ProvisionFailed` (terminal status) or `ProvisionTimeout` (deadline).

No SSH required. Local users see zero behavioural change — engines branch
on `instance.provider == "local"` and run the existing local bootstrap.

Credentials referenced by the script (e.g. `$HF_TOKEN`) are lifted from
the configured `CredentialProvider` onto `spec.env` by the orchestrator.
The script string never carries plaintext token values.

## Cloud stores

Kinoforge ships three `ArtifactStore` backends: `local`, `s3`, and `gcs`.
Configure via the top-level `store:` block:

```yaml
store:
  kind: s3                # or gcs / local
  bucket: my-bucket
  encryption:
    mode: kms             # or "default" (provider-managed)
    kms_key_id: arn:aws:kms:us-east-1:123456789012:key/abc
  signed_url_default_ttl_s: 3600
```

The `kms_key_id` form is cloud-specific:

- **S3:** an AWS KMS ARN — `arn:aws:kms:<region>:<account>:key/<uuid>`
- **GCS:** a Cloud KMS resource name — `projects/<proj>/locations/<loc>/keyRings/<ring>/cryptoKeys/<key>`

Operators that need encrypted artifact storage can opt into
provider-managed encryption (`mode: default` — the silent default) or
customer-managed keys (`mode: kms`). See [`CLOUD-CREDS.md`](CLOUD-CREDS.md) for the
KMS bootstrap path (`pixi run cloud:bootstrap-kms`).

Callers can hand out time-limited URLs without sharing creds:

```python
url = store.signed_url("run-1", "out.mp4", op="GET", ttl_s=600)
```

`ttl_s` defaults to `store.signed_url_default_ttl_s` (default 3600 s).
`LocalArtifactStore` does not support signed URLs (no transport-layer
auth for local files) and raises `NotImplementedError`.
