# Design — Make the onboarding path lead to the secure option

**Date:** 2026-08-21
**Status:** validated (approved in brainstorm)
**Brief:** operator build brief, this session
**Upstream evidence:** `docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md`
(findings **F9** and **F10**, both CONFIRMED)

**Superseded during execution (2026-08-23, Task 3):** the policy file was
renamed to `.aws/policies/skypilot-minimal.template.json`, and the
UNVALIDATED banner moved from a `_comment` key inside the JSON to a
sibling `.aws/policies/README.md` — AWS's IAM policy grammar is closed
and rejects arbitrary top-level keys. References below use the names as
of design time; see the plan document for current state.

---

## 1. Problem

Three separate defects, one shared shape — the documented path and the secure path
point in opposite directions, and the documented one wins because it is what gets
pasted.

1. **`.aws/policies/skypilot-minimal.json` is referenced by nothing.** `.env.example`
   instead instructs a new operator to attach `AmazonEC2FullAccess`,
   `AmazonS3FullAccess` and `AmazonBedrockFullAccess`, with a parenthetical to
   tighten later. `.aws/README.md` has the same shape: bootstrap step 1 says attach
   `AmazonS3FullAccess`, and the scoped policy appears 65 lines further down under
   "optional follow-up". Whichever instruction appears first is what gets used, so in
   practice the scoped policy does not exist.

2. **`roles/iam.securityAdmin` is annotated approvingly.** `.gcp/README.md:30` reads
   `roles/iam.securityAdmin ← self-grant capability; additional roles can be added
   without re-auth`. An identity that can modify IAM bindings can grant itself
   anything — that is project-owner under a quieter name, and the key sits in a
   `.env` on a laptop.

3. **Scrub discipline is inconsistent.** `.gitignore:90-91` excludes
   `.aws/kms-test-key.arn` and `.gcp/kms-test-key.name` specifically to keep key
   identifiers out of the tree, while `.aws/policies/skypilot-minimal.json` commits a
   literal KMS key UUID with every other identifier in the same file scrubbed to a
   `<PLACEHOLDER>`. A policy enforced by `.gitignore` for two files and by nothing at
   all for the rest is a policy that will be violated again.

## 2. Goal

The default onboarding path is the least-privilege one. Wide grants remain available
but are clearly labelled as a bootstrap shortcut, second. A test keeps the scrub
discipline from decaying back.

## 3. Investigation findings that changed the design

Two facts were established during the brainstorm that the brief did not assume.

### 3.1 The brief's literal invocation cannot work

The brief specifies:

```
aws iam put-user-policy --policy-document file://.aws/policies/skypilot-minimal.json
```

`skypilot-minimal.json` already carries `<AWS_ACCOUNT>` (4 sites) and
`<GCS_KMS_KEYRING>` (2 sites) placeholders. AWS rejects a malformed ARN, so this
invocation fails today, before this change. Scrubbing the KMS UUID to `<KMS_KEY_ID>`
adds a third placeholder.

**Consequence:** onboarding needs a render step. A tracked policy file cannot be both
placeholder-clean and directly attachable; the resolution is a small renderer that
substitutes at attach time and writes outside the tree.

### 3.2 Bucket names are not clean

F10's sweep reported bucket names clean. Re-running the sweep with a
`gs://`/`s3://`-anchored pattern finds **6 concrete names across 20 tracked files**,
including two shipped example configs:

| File | Concrete name |
|---|---|
| `examples/configs/bedrock-nova-reel-t2v.yaml` | kinoforge Nova Reel output bucket |
| `examples/configs/bedrock-luma-ray-t2v.yaml` | account-suffixed Bedrock video bucket |
| `docs/CLOUD-CREDS.md`, `docs/cloud-stores.md`, `PROGRESS.md` | prod + Nova Reel buckets |
| `tests/core/test_config.py`, `tests/engines/test_bedrock_video.py`, `tests/engines/test_bedrock_video_replay.py`, `tests/live/test_luma_ray_live.py`, `tests/live/test_nova_reel_live.py` | same two |
| `tests/live/_c33_probe_h_evidence.json` | pod-diagnostics bucket |
| 9 files under `docs/superpowers/` | assorted |

Existing test doubles already in use and to be preserved: `bkt`, `bucket`,
`my-bucket`, `layer-w-test`, `probe-discard`.

### 3.3 AWS account ids are clean — but only with an anchored pattern

A bare `\d{12}` pattern yields 110 hits across 20 tracked files, 53 of them hash
fragments in `pixi.lock`. Anchoring to ARN position and to a labelled
`account_id` assignment yields **zero** hits once `123456789012` (AWS's
reserved-for-documentation account) is exempted. The finding is confirmed; the
pattern must be context-anchored or the test is unshippable.

### 3.4 `compute.admin` → `compute.instanceAdmin.v1` is already the repo's own answer

`tools/cloud_perms_probe.py:315-318`:

```python
_GCP_REQUIRED_ROLES: tuple[str, ...] = (
    "roles/compute.instanceAdmin.v1",
    "roles/iam.serviceAccountUser",
)
```

The project's own permission gate has never required `compute.admin`. The drop is
grounded in existing code, not inference.

### 3.5 `roles/iam.securityAdmin` is bootstrap-only

Zero references in `src/` or `tools/`. No call to `setIamPolicy` or
`add-iam-policy-binding` anywhere in the codebase. Every grep hit is prose:
`.gcp/README.md`, `docs/CLOUD-CREDS.md`, two 2026-06 design docs, and the F9
verification section. It is needed only while binding the other roles, and never at
runtime.

### 3.6 `skypilot-minimal.json` has never been attached

Per `docs/CLOUD-CREDS.md:162-175`, quoted in F9: the policy is "NOT attached to
`kinoforge-ci` in this layer (operator opted for AWS-managed broad policies
instead)". It is a design document with a `.json` extension. Phase 53 (2026-06-17)
then abandoned GCP+AWS GPU work entirely, so this surface is dead weight rather than
an active attack surface — which sets the validation depth chosen in §9.

## 4. Decisions taken

| Decision | Choice | Rationale |
|---|---|---|
| Validation depth | **IAM simulate, no launch** | Zero compute spend; catches missing/denied actions statically against a path Phase 53 abandoned. A live launch's permission-denied iteration loop is unbounded spend on dead infrastructure. |
| Scrub-test scope | **All tracked files** | A test scoped only to the two files already scrubbed by hand catches nothing. The real leak (project id in a code default) lives in `tools/`. |
| Drift-test strictness | **Forward + marker, narrow reverse** | Forward direction needs a marker convention regardless (11 keys currently fail). Full-bidirectional over all `KINOFORGE_*` would flag dozens of internal knobs. |
| Escape hatch shape | **Line pragma, not directory exclusion** | A blanket `docs/superpowers/**` exclusion is exactly the decay the brief exists to stop. |

## 5. AWS onboarding rewrite

### 5.1 `tools/render_aws_policy.py`

New. Substitutes the three placeholders and writes a rendered policy **outside the
repo tree**, so the concrete artifact is never a scrub-test candidate.

| Placeholder | Source |
|---|---|
| `<AWS_ACCOUNT>` | `sts:GetCallerIdentity` (or `--account` override) |
| `<KMS_KEY_ID>` | `.aws/kms-test-key.arn` — the gitignored file already read at `tools/cloud_perms_probe.py:68` |
| `<S3_BUCKET_PREFIX>` | `--bucket-prefix` flag (required, no default) |

Contract: refuses to write if any `<...>` placeholder survives substitution, and
refuses to write inside the repo root. Both are hard errors, not warnings.

### 5.2 `skypilot-minimal.json` edits

- `KMSLayerW` resource: literal UUID → `<KMS_KEY_ID>`.
- `S3KinoforgeBuckets` resource: `<GCS_KMS_KEYRING>` → `<S3_BUCKET_PREFIX>`. This is
  the F10 cosmetic bug — a GCS-flavoured placeholder naming an S3 bucket prefix —
  and the new name matches what `.aws/README.md:65,80-81,93,115` already uses.

### 5.3 `.env.example` AWS block

Leads with the scoped path:

```bash
aws iam create-user --user-name kinoforge-runner
python tools/render_aws_policy.py \
  --bucket-prefix <your-bucket-prefix> \
  --out /tmp/skypilot-minimal.rendered.json
aws iam put-user-policy --user-name kinoforge-runner \
  --policy-name KinoforgeSkypilotMinimal \
  --policy-document file:///tmp/skypilot-minimal.rendered.json
aws iam create-access-key --user-name kinoforge-runner
```

The FullAccess recipe stays, moved below, relabelled as a bootstrap shortcut
acceptable **only in an AWS account holding nothing else**, with the consequence
stated: those three grants let the key launch any instance type in any region on the
account's bill. `IAMFullAccess` and `ServiceQuotasFullAccess` — attached in practice
per F9 but documented nowhere — are noted as what the project actually ran on, not
recommended.

### 5.4 `.aws/README.md`

Same inversion. Bootstrap step 1 stops saying "attach `AmazonS3FullAccess`" and
points at the render-and-attach sequence. The existing console-paste instructions
(`.aws/README.md:88-116`) are kept as the GUI alternative, below the CLI path.

## 6. GCP: `.gcp/policies/roles.txt`

```
roles/compute.instanceAdmin.v1
roles/iam.serviceAccountUser
roles/storage.admin
```

Grounded in §3.4 plus GCS, which the probe list omits because it probes compute.

**Stated caveat in the file:** `compute.instanceAdmin.v1` does not grant firewall or
VPC mutation, and SkyPilot opens ports. That is the most likely first failure of a
real launch under this role set, and it is why the file carries the UNVALIDATED
banner from §9.

### 6.1 `securityAdmin` — grant, bootstrap, revoke

`roles/iam.securityAdmin` on the runner service account is project-owner under a
quieter name: that identity can grant itself any role in the project, and its key
sits in a `.env` on a laptop. It is required only while binding the roles above, and
never at runtime (§3.5).

`.gcp/README.md:30` currently annotates it as a feature. That annotation is rewritten
into an explicit grant-bootstrap-revoke sequence, with the revoke command inline and
a direct instruction to revoke it now if it is still bound to the existing service
account. `roles/iam.serviceAccountAdmin` and
`roles/serviceusage.serviceUsageAdmin` — also currently bound, also unreferenced by
code — get the same bootstrap-only treatment. `roles/viewer` is dropped from the
recommended set.

`roles.txt` documents the bootstrap grant next to the runtime role list so an
operator reading only that file sees both halves.

## 7. `tests/test_cloud_identifier_scrub.py`

Sweeps **every** tracked file (`git ls-files`). No directory exclusions.

| Pattern | Rule | Exemption |
|---|---|---|
| AWS account | `arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(\d{12})` and `account[_ -]?id[:=]\s*(\d{12})` | `123456789012`, `000000000000` |
| KMS key | `key/([0-9a-f-]{36})` | none — must be `<KMS_KEY_ID>` |
| SA email | `@([a-z0-9-]+)\.iam\.gserviceaccount\.com` | fake project parts: `proj`, `kinoforge-prod-deadbeef`, `<GCP_PROJECT>` |
| GCP project | `kinoforge-prod-(?!deadbeef)[0-9a-z]{8}` | none |
| Bucket | `(?:gs\|s3)://([a-z0-9][a-z0-9._-]{2,62})` | allowlist: `bkt`, `bucket`, `my-bucket`, `layer-w-test`, `probe-discard`, plus any `<PLACEHOLDER>` form |

**Escape hatch:** a `kinoforge: allow-identifier` line pragma, mirroring the existing
`kinoforge: allow-secret` idiom from the credential scanner. Suppresses on that line
only. Used solely in the F10 section of the verification doc, where quoting the
leaked value *is* the finding. Deliberately not a directory exclusion.

**Reverse test** (the discipline the brief asks for — a test that would notice its own
regression): plant each identifier class into a temp tree and assert the sweep fires,
so a regression in the sweep function itself cannot pass silently. Mirrors
`tests/test_source_audit.py:76-110`.

### 7.1 Scrub targets

| Class | Files | Replacement |
|---|---|---|
| GCP project id | 10 | `kinoforge-prod-deadbeef` — convention already established at `tests/stores/test_recording.py:48-50` |
| Bucket names | 20 | `<GCS_BUCKET>` / `<S3_BUCKET>` in docs and example configs; `bkt`-family doubles in tests |
| KMS UUID | 3 (`skypilot-minimal.json`, `PROGRESS.md`, verification doc) | `<KMS_KEY_ID>`; verification doc takes the pragma instead |

`tools/quota_burn_lib.py:266` is the one production-code site — a default parameter
naming the real project's billing dataset. It becomes the `deadbeef` fake, which
fails loudly rather than silently pointing at a real project.

## 8. `tests/test_env_example_drift.py`

**Parse check.** `.env.example` round-trips through
`kinoforge.core.dotenv_loader`, so `RUNPOD_TERMINATE_KEY=${RUNPOD_API_KEY}`
expansion is exercised rather than regex-matched.

**Forward direction.** Every `KEY=` must be referenced in tracked non-doc source, or
carry an explicit marker comment. 11 keys currently fail — `AZURE_SUBSCRIPTION_ID`,
`AZURE_STORAGE_CONNECTION_STRING`, `KINOFORGE_AZURE_CONTAINER`,
`KINOFORGE_AZURE_PREFIX`, and the R2/B2 sets — because those stores are
unimplemented. Each gets `# UNIMPLEMENTED — no kinoforge code reads this yet`.

**Reverse direction.** Runs against a curated operator-facing var list, not all of
`KINOFORGE_*`. Catches the one real gap: `KINOFORGE_S3_BUCKET` is read at
`src/kinoforge/stores/s3/__init__.py:239` and documented nowhere. Internal knobs
(`KINOFORGE_LIVE_TESTS`, `KINOFORGE_DIAG_BUCKET`, `KINOFORGE_PROVISION_B64_*`,
`KINOFORGE_SKIP_USER_REDACT_HOOK`, …) stay out of scope by construction.

## 9. Validation — simulate, no launch

`tools/validate_scoped_policy.py`. Zero compute spend; no EC2 launch, no GCE
instance.

**AWS.** Create a throwaway user, attach *only* the rendered policy, run
`iam:SimulatePrincipalPolicy` over the 15 actions in `_REQUIRED_AWS_ACTIONS`
(`tools/cloud_perms_probe.py:39-55`), delete the user. Reuses the existing two-pass
KMS split at `:66-74` — the simulator rejects a mixed list of `*` and specific ARNs,
so KMS actions need a separate resource-scoped call.

**GCP.** Symmetric and also free: a second service account bound to `roles.txt` only,
then `projects.testIamPermissions` for the permissions those roles are claimed to
supply.

**Honest banner, in both `skypilot-minimal.json` (as a `_comment` key) and
`roles.txt`:** simulate-validated, never exercised against a real launch. Simulation
sees the policy's own logic; it does not see an undocumented API call SkyPilot makes
at launch time. §6's firewall caveat is the concrete known example.

## 10. Billing alerts in `.env.example`

New section placed immediately after the credential-creation blocks and before
artifact storage, so it reads as part of minting a key rather than as an afterthought.

Console-only steps for AWS Budgets and GCP Billing → Budgets & alerts, plus the
per-provider spend caps for RunPod, Modal and Replicate. Framing is explicit about
why it must be here: **nothing in this repo can enforce a monthly ceiling.**
`est_spend` is a wall-clock estimate, the sweeper only reaps what it can see, and
per F12 a cluster killed mid-launch is invisible to every kinoforge command while
still billing. The cloud console is the only place a hard ceiling exists.

`GCP_BILLING_ACCOUNT_ID` already exists at `.env.example:279` for the budget-creation
tool; the new section cross-references it rather than duplicating it.

## 11. Testing

Both new tests are written RED first and genuinely fail on HEAD:

- `test_cloud_identifier_scrub` — ~30 dirty files across 4 identifier classes.
- `test_env_example_drift` — 11 unmarked keys forward, 1 undocumented var reverse.

Existing suites that must stay green: `tests/test_source_audit.py` (the tracked-tree
credential guard), `tests/tools/test_cloud_perms_probe.py`, and the quota-burn tests
whose fixtures carry the project id being renamed.

New unit coverage: `render_aws_policy` placeholder-survival refusal,
in-repo-output refusal, and `.aws/kms-test-key.arn`-missing error;
`validate_scoped_policy` KMS two-pass merge and cleanup-on-failure.

## 12. Out of scope

- Live launch validation of either policy. Deferred by the §4 decision; the
  UNVALIDATED banner is the standing marker that it has not happened.
- The remaining F-findings (F1–F8, F11, F12). Separate work.
- `docs/CLOUD-CREDS.md` restructuring beyond the identifier scrub.
