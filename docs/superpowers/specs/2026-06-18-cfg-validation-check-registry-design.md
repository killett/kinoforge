# Cfg Validation Check Registry — closing the silent-cfg-trap UX gap

**Status:** APPROVED design — ready for `writing-plans`
**Author:** Claude (Opus 4.7), brainstormed with Dr. Twinklebrane
**Date:** 2026-06-18
**Driving incident:** 2026-06-18 Wan 1.3B CLI warm-reuse smoke first
attempt (commits `f2w4sqghw5udio` + `p3oj1qjmjioae1`) — two pods ran
simultaneously because the cfg lacked `lifecycle.heartbeat_interval_s`
and `_resolve_warm_instance`'s classify chain silently fell to
`HEARTBEAT_UNKNOWN` → cold create. Operator (Dr. Twinklebrane)
observed that if the LLM forgot the field, a human will definitely
forget it, and requested a system "flexible, robust and user-friendly
enough that these kinds of failures can't happen."

## 1. Problem statement

The kinoforge cfg surface is **permissive at parse time** (Pydantic v2
accepts the cfg as syntactically valid) but **unusable at runtime** in
ways the operator cannot see until they have spent money discovering
them. The 1.3B smoke caught one specific case, but the failure shape
is a family:

| Trap | Symptom |
|---|---|
| `lifecycle.heartbeat_interval_s` unset while `warm_reuse_auto_attach: true` | Every CLI invocation cold-creates; pods accumulate. |
| `compute.image` is a placeholder that 404s on the registry (e.g. `skypilot/skypilot-gpu:latest`) | Pod boots, docker pull fails, instance orphaned mid-provision. |
| `models[].ref` points at a moved / deleted weight file | Pod boots, weight download fails, instance orphaned. |
| `engine.comfyui.custom_nodes[].ref` is a stale commit SHA | Pod boots, custom-node install fails, instance orphaned. |
| `lifecycle.idle_timeout` set so small that the pod self-terminates between cmd 1 and cmd 2 | Cmd 2 sees `STALE_LEDGER` → cold create. |
| Provider-side capacity (e.g. RunPod's GPU list is empty for the requested type) | Offer-retry exhausts; CapacityError after several seconds of latency. |

What makes this family **silent** rather than loud:
- The cfg passes Pydantic validation because each field is independently
  well-typed.
- The failure surfaces only after a pod is created and money is spent.
- The CLI does not warn at load time even when the misconfiguration is
  statically obvious.
- Each new trap discovered (we found four this session alone) follows
  the same shape, so the fix cannot be one-off — it must be a system
  that absorbs new traps as we find them.

## 2. Goals

1. **Prevent the failure at load time when statically detectable.**
   Reject cfgs whose internal-consistency invariants are violated.
   Auto-fix the cases where a safe default exists.
2. **Prevent the failure before live spend when detectable via cheap
   network probe.** `kinoforge generate` runs a focused subset of
   network checks before any RunPod API call; opt-out via
   `--skip-preflight`.
3. **Make pre-spend confidence achievable on-demand.** Provide
   `kinoforge doctor <cfg>` that runs every check exhaustively and
   surfaces a structured report.
4. **Keep the rule list open to extension.** Future traps must be
   addable by writing a single new Check class and registering it,
   without editing any existing code. Providers and engines must be
   able to register their own checks alongside their existing
   module-level code.

## 3. Non-goals

- Replace Pydantic's existing per-field validation. The new system
  layers on top of parse-time validation, not under it.
- Auto-fix network-dependent failures. Substituting a weight URL or
  image silently is not safe; reject is the right behavior for
  network-detected misconfigurations.
- Real-time monitoring during generate. Once `deploy()` is called, the
  existing reaper / lifecycle machinery takes over; the validation
  system is upstream of that boundary.
- Cover non-cfg operator mistakes (wrong env vars, missing creds,
  wrong working dir). Those are preflight concerns the existing
  `tools/preflight.py` covers.

## 4. Architecture

One new package `kinoforge.validation` with four pieces:

```
src/kinoforge/validation/
  __init__.py          # public API: validate_for_generate(cfg), validate_for_doctor(cfg)
  protocol.py          # Check Protocol, CheckResult dataclass, enums
  registry.py          # CheckRegistry — registration + filtering
  checks/
    __init__.py        # self-registers all built-in checks
    heartbeat.py       # HeartbeatIntervalRequiredCheck
    image.py           # ImageReachableCheck
    models.py          # ModelRefReachableCheck
    custom_nodes.py    # CustomNodeSHAReachableCheck
    lifecycle.py       # IdleTimeoutVsHeartbeatCheck
    ledger.py          # LedgerStaleRowsCheck
```

Surface-area edits to existing code:

- `src/kinoforge/core/config.py::load_config` — calls
  `validate_for_generate(cfg)` after Pydantic parse. Auto-fixes
  mutate the cfg in-memory; errors raise
  `kinoforge.core.errors.ValidationError`.
- `src/kinoforge/cli/_main.py` — new `kinoforge doctor` subcommand →
  calls `validate_for_doctor(cfg)`, prints a result table, exit code
  = number of errors.
- `src/kinoforge/cli/_commands.py::_cmd_generate` — runs the
  `PREFLIGHT` and `NETWORK`-subset categories before `deploy()`. Opt-out:
  `--skip-preflight`.
- `src/kinoforge/providers/runpod/__init__.py` and
  `src/kinoforge/providers/skypilot/__init__.py` — register
  provider-specific checks at import time alongside the existing
  self-registration block.

The package mirrors the existing `kinoforge.core.registry` pattern for
providers / engines / sources — operators recognize the registration
shape and the import-time side-effect.

## 5. Core types + Protocol

```python
# src/kinoforge/validation/protocol.py
from enum import Enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from kinoforge.core.config import Config

class CheckCategory(Enum):
    STATIC = "static"        # internal cfg consistency; no I/O
    NETWORK = "network"      # HEAD/GET against an external resource
    PREFLIGHT = "preflight"  # external state (ledger, provider capacity)

class Severity(Enum):
    ERROR = "error"          # rejects load (or fails generate pre-flight)
    WARN = "warn"            # logs + included in report, does NOT reject

@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    severity: Severity
    message: str
    auto_fix_applied: bool = False
    fix_suggestion: str | None = None

@runtime_checkable
class Check(Protocol):
    name: str
    category: CheckCategory
    severity: Severity

    def applies_to(self, cfg: Config) -> bool:
        """Cheap guard: does this check apply to THIS cfg's shape?"""
        ...

    def run(self, cfg: Config) -> CheckResult:
        """Execute the check. May do I/O if NETWORK or PREFLIGHT."""
        ...

    def auto_fix(self, cfg: Config) -> Config | None:
        """Return a NEW cfg with the issue auto-fixed, or None.
        Default: None. Override only when a safe default exists.
        Only honoured for STATIC category checks."""
        return None
```

Three deliberate design points:

1. **`applies_to(cfg)` is the cheap predicate**, runs before `run()`.
   A Wan-on-RunPod cfg never loads
   `SkyPilotCloudPinAvailabilityCheck.run()`. Keeps fast paths fast.
2. **`auto_fix` returns a NEW cfg**, not in-place mutation. Flow becomes
   `parse → run_auto_fixes → revalidate`. Avoids the "did I forget to
   re-run validation after mutating?" trap.
3. **`CheckResult.auto_fix_applied`** carries the auto-fix flag so
   operator-facing logs distinguish "this was already valid" from
   "this was auto-corrected" — both must be visible.

`registry.py` is ~50 LOC: register, reject duplicate-name registration,
filter by category + applies_to. Module-level `_REGISTRY` + a
`register(check)` function — same pattern as
`kinoforge.core.registry`.

## 6. Public API + data flow

```python
# src/kinoforge/validation/__init__.py

@dataclass
class ValidationReport:
    cfg: Config                          # final cfg (post-auto-fix)
    results: list[CheckResult]
    auto_fixes: list[CheckResult]
    errors: list[CheckResult]            # passed=False AND severity=ERROR
    warnings: list[CheckResult]          # passed=False AND severity=WARN

    @property
    def ok(self) -> bool:
        return not self.errors

def validate_for_generate(cfg: Config) -> ValidationReport:
    """Static + preflight + network-subset. Runs auto-fixes.
    Raises ValidationError if errors remain after auto-fix."""
    ...

def validate_for_doctor(cfg: Config) -> ValidationReport:
    """All categories, all checks. Does NOT raise — returns report
    for the CLI to format and exit on."""
    ...
```

**Data flow for `kinoforge generate`:**

```
load_config(yaml)
  → cfg = parse_via_pydantic(yaml)
  → report = validate_for_generate(cfg)
      → for check in registry.applicable(cfg, categories={STATIC}):
          result = check.run(cfg)
          if not result.passed and check.auto_fix:
              new_cfg = check.auto_fix(cfg)
              if new_cfg is not None:
                  cfg = new_cfg
                  result = check.run(cfg)  # exactly one retry per check
                  if result.passed:
                      mark auto_fix_applied=True
                  # If the retry still fails, the original error stands
                  # — the check's auto_fix had a bug, not the operator's
                  # cfg. The error is logged with both the original and
                  # post-fix message.
      → for check in registry.applicable(cfg, categories={PREFLIGHT}):
          result = check.run(cfg)
      → for check in registry.applicable(cfg, categories={NETWORK}):
          # SUBSET: only checks whose applies_to flags this cfg's
          # active model/image (the one this generate run uses)
          result = check.run(cfg)
      → log every auto_fix at INFO level
      → log every WARN at WARN level
      → if report.errors: raise ValidationError(report.format())
  → return cfg
```

**Data flow for `kinoforge doctor <cfg>`:**

```
load_config(yaml)  → SAME path; static errors still raise
  → report = validate_for_doctor(cfg)
      # all categories, all checks, no early-raise
  → CLI formats report as a table; exit code = number of errors
```

**Data flow for `kinoforge generate --skip-preflight`:**

```
Same as generate but skip PREFLIGHT + NETWORK. STATIC always runs.
Single WARN line: "preflight skipped; cfg-time-only validation applied".
```

Two design points worth flagging:

1. **Auto-fix runs ONLY for STATIC category.** Network and preflight
   checks describe external state; auto-fixing them would mean
   silent operator-unauthorised changes (e.g. substituting a model
   URL). Reject is correct.
2. **Generate's NETWORK subset filters via `applies_to`.** A cfg may
   reference five models but a given generate run consumes only one.
   Pre-flight checks the actually-used one; doctor checks all.

## 7. Concrete check inventory — v1

Six built-in checks ship in v1 — exactly the cases hit in production
already. Each is its own file under `validation/checks/`.

| Check | Category | Severity | `applies_to` | What it does | Auto-fix? |
|---|---|---|---|---|---|
| `heartbeat_interval_required` | STATIC | ERROR | `cfg.compute.warm_reuse_auto_attach is True` | Asserts `cfg.compute.lifecycle.heartbeat_interval_s is not None`. | YES — sets to `30`. |
| `idle_timeout_vs_heartbeat` | STATIC | ERROR | `heartbeat_interval_s is not None` | Asserts `idle_timeout_s >= 3 * heartbeat_interval_s` (reaper dead-man window). | NO — operator chose both knobs; we won't pick. |
| `image_reachable` | NETWORK | ERROR | `cfg.compute is not None and cfg.compute.image` | HEAD the docker registry for the image tag. Catches `skypilot/skypilot-gpu:latest`-style placeholders. | NO. |
| `model_ref_reachable` | NETWORK | ERROR | At least one `cfg.models[]` has scheme `hf:` or `https://` | For each `models[]` ref: `hf:` → HF Hub API HEAD; `https:` → plain HEAD. **Generate subset:** only models with `kind: base` (the diffusion checkpoint the engine consumes as primary weight slot) are checked. **Doctor:** checks every `models[]` ref regardless of `kind`, including `vae`, `text_encoder`, `lora`. | NO. |
| `custom_node_sha_reachable` | NETWORK | WARN | `cfg.engine.kind == "comfyui" and cfg.engine.comfyui.custom_nodes` | For each `custom_nodes[]` ref: GitHub raw HEAD on commit SHA. WARN-only because archived commits may still be cached on the pod. | NO. |
| `ledger_stale_rows` | PREFLIGHT | WARN | always | Reads ledger; if any rows reference pods the provider no longer has, list them with suggested `kinoforge forget --id <id>` chord. | NO. |

Plus two provider-side checks registered alongside provider modules:

| Check | Lives in | Category | Severity | Notes |
|---|---|---|---|---|
| `runpod_capacity_hint` | `providers/runpod/__init__.py` | PREFLIGHT | WARN | Queries `gpuTypes` for the cfg's `gpu_preference` list. WARN if NONE currently available. |
| `skypilot_cloud_pin_supported` | `providers/skypilot/__init__.py` | STATIC | ERROR | If `cfg.compute.cloud` is set, asserts each entry is in `_SUPPORTED_CLOUDS`. Migrates the existing Pydantic validator to the check registry. |

YAGNI'd out of v1, captured for future work:

- `runpod_pricing_drift_warn` — alerts if `max_usd_per_hr` < current
  published RunPod floor. Operator-visible via offer-retry already.
- `comfyui_graph_node_inputs_match_models` — asserts the graph file's
  `WanVideoModelLoader.model` matches a `cfg.models[]` ref. Real bug
  surface but harder to land cleanly; deferred.

## 8. Error handling

Five distinct error surfaces:

1. **STATIC ERROR survives auto-fix retry:** `validate_for_generate`
   raises `ValidationError(report)`. `_cmd_generate` catches, prints
   the report's formatted error block to stderr, exit 2. No pod
   creation attempted.
2. **NETWORK ERROR during generate pre-flight:** Same as #1.
3. **NETWORK transient I/O failure** (e.g. transient 502 from HF Hub):
   each check wraps I/O in `try/except`; on unexpected error returns
   `CheckResult(passed=True, severity=WARN, message="network probe
   inconclusive: <reason>; not blocking")`. A flaky upstream must not
   block legitimate work.
4. **Auto-fix raises:** logged at WARN; the original failure stands;
   operator sees both lines.
5. **Doctor never raises.** Exits with `len(errors)` so CI scripts can
   gate: `kinoforge doctor my.yaml && kinoforge generate ...`.

Operator-facing error format example:

```
error: cfg validation failed
  ✗ heartbeat_interval_required
    lifecycle.heartbeat_interval_s is required when
    compute.warm_reuse_auto_attach=true
    fix: set compute.lifecycle.heartbeat_interval_s: 30
```

## 9. Testing

Three layers, all pytest-native, **zero live spend in v1**:

| Layer | Coverage | Style | Count |
|---|---|---|---|
| `tests/validation/protocol/` | `CheckResult` / `CheckRegistry` semantics (registration, duplicate-rejection, `applies_to` filter, category filter) | Unit, no I/O | ~5 tests |
| `tests/validation/checks/test_<name>.py` | Each built-in check class. Per `test-design` skill: one test per check naming a concrete bug it catches (RED → GREEN); plus auto-fix test where applicable. Network checks mock I/O at the `urllib` seam. | Unit | ≥16 tests |
| `tests/validation/integration/` | `validate_for_generate` and `validate_for_doctor` end-to-end against representative cfgs: clean, auto-fixable, un-fixable, mixed WARN+ERROR. | Unit (mocked I/O) + 1 live network test under `live/` gate | ~4 tests |

Post-merge live verification (zero pod spend, network only):

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest \
  tests/live/test_doctor_examples_live.py -v
```

Runs `kinoforge doctor` against every cfg under `examples/configs/`
and asserts each is clean (catches accidental regressions where an
example cfg ships broken).

## 10. Backward compatibility

Every cfg that loads green today must load green under the new system.
Auto-fix only sets values when fields are unset; explicit operator
choices are never overridden. Existing example cfgs in
`examples/configs/` will surface a one-time INFO-level
"auto-fixed: ..." line on load, which is correct and operator-visible.

The two example cfgs that gained `heartbeat_interval_s` in this
session's commits (`7b93725` for 1.3B, `a5ee765` for 14B) will have
their auto-fix opt-outs respected — INFO line will NOT fire because
the field is already set.

## 11. Out of scope (explicit)

- Replacement of Pydantic per-field validation.
- Auto-fixing network-dependent failures (silent URL substitution).
- Real-time monitoring during generate.
- Env-var / cred / cwd preflight (covered by `tools/preflight.py`).
- Hot reload on cfg edits during a running session.

## 12. Cross-references

- Driving session: 2026-06-18 Wan 1.3B + 14B CLI warm-reuse smokes,
  commits `7b93725` and `a5ee765` patched the immediate cfg gaps.
- Successful-generations entry #7 (`5095c5f`) documents the smoke that
  exposed the family.
- B5b deferral spec
  (`docs/superpowers/specs/2026-06-18-b5b-deferred-design.md`) names
  the local ledger as authoritative substrate under same-host scope.
  The `LedgerStaleRowsCheck` operates against that ledger.
- Existing `tools/preflight.py` covers cred / pod / git-tree state;
  this system covers cfg-content state. Distinct, complementary.
- Related skill: `test-design` governs the per-check tests in
  `tests/validation/checks/`. Each check ships with at least one test
  naming the concrete bug it catches.

## 13. Future work (deferred but anchored)

- **`runpod_pricing_drift_warn` check.** Alerts on max_usd_per_hr
  drifting below current RunPod floor. Add when offer-retry exhaustion
  becomes observable enough to be worth pre-empting.
- **`comfyui_graph_node_inputs_match_models` check.** Asserts the
  ComfyUI graph file's model-loader nodes reference files in
  `cfg.models[]`. Catches the case I hit today where the 1.3B graph
  initially referenced the 14B model file name. Harder to land
  cleanly because graph JSON shape varies per workflow.
- **`kinoforge generate --check` short-circuit.** Same as
  `kinoforge doctor` but treats output as a generate dry-run — useful
  for CI / pre-spend gating in scripts.
- **Cross-check report HTML export.** `kinoforge doctor --html` for
  team-shared cfg review.
