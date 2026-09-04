# Plan — scoped-policy validation (brief `2026-09-04-scoped-policy-validation.md`)

## The decision, and why

**Chosen: hybrid — option (i) for AWS, option (ii) for GCP.** Recorded 2026-09-04, operator
decision, after the credential probe below.

The brief asks for one choice across both clouds. That is not reachable here, and the reason is
measured rather than assumed:

| cloud | probe | result |
|---|---|---|
| AWS | `aws sts get-caller-identity` | `arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci` — live, and holds `IAMFullAccess`, so it can mint the throwaway principal option (i) needs |
| GCP | `gcloud projects list` | fails to mint a token; both `kinoforge-runner@<GCP_PROJECT>` and the operator user are dead, exactly as `.gcp/policies/roles.txt` already records |

Option (i) on GCP needs an interactive `gcloud auth login` or a fresh service-account key, which
only the operator can supply. Rather than block the AWS half on that, AWS gets the live validation
and GCP gets the recovery path. GCP's banner stays — but it stops being a dead end.

Consequence to state plainly, because it is the brief's own framing: the GCP
`roles/compute.securityAdmin` firewall hypothesis is **neither confirmed nor refuted** by this
work. It remains the single most likely first failure of a real GCP launch, and it is now the
first thing the recovery section tells you to check.

## Tasks

- [ ] **T1** — persist the brief; record this decision. *(this file)*
- [ ] **T2** — RED live scaffold, committed **before** any spend (CLAUDE.md durability rule):
      `tests/live/test_scoped_policy_aws_live.py`.
- [ ] **T3** — run it. Iterate on denials; log each one.
- [ ] **T4** — `.aws/policies/README.md`: new banner, denial log, CloudTrail recovery section.
- [ ] **T5** — `.gcp/policies/roles.txt`: option-(ii) recovery section, banner updated.
- [ ] **T6** — `PROGRESS.md`, pre-commit, live-resource verification, commit.

## T2 — what the scaffold must actually do

The claim under test is narrow and worth stating before any code: **a real `sky launch` of a CPU
instance succeeds under a principal that holds the rendered `skypilot-minimal` policy and nothing
else.** Not "the actions simulate clean" — `.aws/policies/README.md` already proves that, and it
is precisely the claim the brief says is insufficient.

Shape:

1. Render the policy (`tools/render_aws_policy.py`), create it as a **managed** policy — inline is
   impossible at 3,422 characters, already measured.
2. Create a throwaway IAM user with **no other grants**, attach only that policy, mint an access key.
3. Sleep for IAM propagation (eventual consistency; a fresh key is not usable instantly).
4. Launch the cheapest CPU SKU in `us-west-2` under **only** those credentials, in an isolated
   `HOME` so the SkyPilot API server cannot reuse the ambient `kinoforge-ci` credential. Stop any
   running API server first — sky's server is a separate process that outlives the launcher and
   holds whatever environment it was started with.
5. Capture every `AccessDenied` / `UnauthorizedOperation` out of the sky logs.
6. `finally`: sky down, force-terminate by tag, delete access key, detach policy, delete user,
   delete policy. Convergent, not one-shot — the S1 lesson is that a clean read can race a server
   that is still creating the instance.

Cost envelope: the CPU SKU is ~$0.09/hr and the run aborts as soon as the cluster is UP or a
denial is captured. Budget a few cents per attempt, and expect several attempts — that iteration
IS the deliverable, per the brief.

## Non-negotiables carried from CLAUDE.md

- `pixi run preflight` before the first spend, and the scaffold committed RED before it.
- No credential value reaches a file, a log line, or the transcript. The throwaway secret key is
  held in memory and passed to the subprocess environment only.
- Teardown verified **after** the process exits, not from a mid-run log line.
