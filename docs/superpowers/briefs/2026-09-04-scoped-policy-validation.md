# Brief — retire the UNVALIDATED banner, or replace it with something better than a banner

Handed to the session on 2026-09-04. Reproduced verbatim; the decision and outcome are
recorded in `docs/superpowers/plans/2026-09-04-scoped-policy-validation.md`.

---

Build brief: retire the UNVALIDATED banner, or replace it with something
better than a banner.

Only run this if you want the scoped policies to be real. They are currently
honest but aspirational, and that is a defensible place to stop.

CONTEXT

.aws/policies/README.md and .gcp/policies/roles.txt both carry an UNVALIDATED
banner: the scoped AWS policy is simulate-validated only, and the GCP role
list has never gated a real launch. The round-1 design deferred live
validation deliberately and the banner is the standing marker.

The banner is honest, but it does not change the outcome for the person it is
warning. An unvalidated scoped policy fails on the first unusual operation,
mid-launch, with an opaque AWS or GCP denial — and the path of least
resistance from there is FullAccess, which is the exact failure mode brief 4
existed to prevent. A scoped policy nobody can trust is close to no scoped
policy.

DECIDE FIRST, THEN BUILD

Pick one and record the choice:

  (i)  Validate live. Cheap CPU launch under a principal holding only the
       scoped policy, on each cloud. Expect an iteration loop as denials
       surface one at a time — this is not a single run, and budget it as
       an afternoon rather than an hour.
  (ii) Keep the banner, but add the recovery path: document the exact
       command that shows what was denied (CloudTrail lookup for AWS,
       Policy Troubleshooter or the audit log for GCP), so someone hitting a
       denial can fix the policy instead of abandoning it. Cheaper, and it
       converts the failure from a dead end into a fifteen-minute detour.

(ii) is a legitimate stopping point. It is not as good as (i), but it is
much better than the banner alone, and it is the option to take if you do not
want to spend the afternoon.

IF (i)

- One throwaway principal per cloud, scoped policy only, no other grants.
- Cheapest CPU SKU. Teardown in a finally block.
- Log every denial encountered and the permission that fixed it, in the
  policy README — that log is more valuable than the policy diff, because it
  tells the next person what the policy is load-bearing for.
- If a denial can only be fixed by widening beyond what you consider
  least-privilege, say so and stop rather than widening quietly. Record it.
- The GCP roles.txt already names roles/compute.securityAdmin as a suspected
  gap for firewall permissions. Confirm or refute that first — it is the most
  likely single failure.
- Preflight and RED-scaffold rules apply. Commit the scaffold before spending.

IF (ii)

- Add a "when a launch fails on permissions" section to both policy files.
- Give the exact lookup command per cloud, with the field to read.
- Say plainly that FullAccess is the fallback of last resort and that
  reverting to it should be recorded, so a temporary widening does not become
  the permanent state by default.

Either way, update the banner to reflect what was actually done.
