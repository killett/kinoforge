# Plan — scoped-policy validation (brief `2026-09-04-scoped-policy-validation.md`)

## The decision, and why

**Chosen: hybrid — option (i) for AWS, option (ii) for GCP.** Recorded 2026-09-04, operator
decision, after the credential probe below.

The brief asks for one choice across both clouds. That is not reachable here, and the reason is
measured rather than assumed:

| cloud | probe | result |
|---|---|---|
| AWS | `aws sts get-caller-identity` | `arn:aws:iam::<AWS_ACCOUNT>:user/kinoforge-ci` — live, and holds `IAMFullAccess`, so it can mint the throwaway principal option (i) needs |
| GCP | `gcloud projects list` | fails to mint a token; both the runner service account and the operator user are dead, exactly as `.gcp/policies/roles.txt` already records |

Option (i) on GCP needs an interactive `gcloud auth login` or a fresh service-account key, which
only the operator can supply. Rather than block the AWS half on that, AWS gets the live validation
and GCP gets the recovery path. GCP's banner stays — but it stops being a dead end.

Consequence to state plainly, because it is the brief's own framing: the GCP
`roles/compute.securityAdmin` firewall hypothesis is **neither confirmed nor refuted** by this
work. It remains the single most likely first failure of a real GCP launch, and it is now the
first thing the recovery section tells you to check.

## Tasks

- [x] **T1** — persist the brief; record this decision. *(this file — `352323ad`)*
- [x] **T2** — RED live scaffold, committed **before** any spend (CLAUDE.md durability rule):
      `tests/live/test_scoped_policy_aws_live.py` (`41c38654`).
- [x] **T3** — run it (`d097c320`). **Green on the first attempt; there were no denials to
      iterate on.**
- [x] **T4** — `.aws/policies/README.md`: new banner, call inventory, CloudTrail recovery section
      (`c4225787`).
- [x] **T5** — `.gcp/policies/roles.txt`: option-(ii) recovery section, banner updated (`c4225787`).
- [x] **T6** — `PROGRESS.md`, pre-commit, live-resource verification, commit.

## Outcome

**AWS — validated.** `m6i.large` in `us-west-2`, `rc=0`, zero denials, `i-06610e91b03c0cc75`
observed running by a second principal, teardown and account clean. ~$0.02, 119 s of launch.
CloudTrail: 37 calls, `errorCode` NONE.

The brief expected an iteration loop and budgeted an afternoon for it. There was nothing to
iterate on — which is a result about the 2026-08-23 simulation work, not luck: the action list it
produced was already sufficient for this path. What the run adds over simulation is the **call
inventory** (what the policy is load-bearing for) and, more usefully, the **not-exercised list**:
the IAM write path (`skypilot-v1` pre-existed, so only `GetInstanceProfile` ran) and the three
key-pair actions (sky 0.12.3 used none). A fresh account's first launch will exercise the former.

Nothing had to be widened, so the brief's "if you must widen beyond least-privilege, stop and
record it" clause was never reached.

**One test change the live run forced, recorded because it looks like a weakened assertion and is
not.** The negative control asserted on AWS's verbatim denial strings; sky catches that denial and
re-raises it in its own words (`Failed to retrieve AWS regions … ec2:DescribeRegions`). The fix
widens the control to accept either spelling — via a SEPARATE pattern the positive test does not
use, so sky-worded permission trouble there still fails — and *strengthens* it at the same time
with an EC2 oracle proving the bare principal booked nothing.

**GCP — recovery path, not validation.** Still no credential; the `roles/compute.securityAdmin`
firewall hypothesis is still open. The next session that gets a GCP credential should run
`tools/validate_scoped_policy.py --cloud gcp` first, then the equivalent of the AWS live test.

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
