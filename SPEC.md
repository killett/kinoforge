# Build kinoforge — a vendor-agnostic video-generation provisioning & orchestration system

## Mission
Build a new repository that provisions and runs a video-generation environment on
remote (or local) compute, driven entirely by declarative config. It must be **scalable,
portable, flexible, and vendor-agnostic** along *three independent axes*, each a swappable plugin
the core never hard-codes: **compute** (RunPod first), **model source** (CivitAI first), and the
**generation engine itself** (ComfyUI first, with Diffusers and hosted model-APIs as sibling
adapters). ComfyUI is just the first engine, not the foundation — nothing in the core or the
pipeline may assume it.

The motivation is structural rather than reactive: providers, engines, and models change quickly in
this space, and the cost-safety and capability-discovery requirements (pods bill continuously; the
per-segment length the splitter needs is a model property, not a constant) deserve more rigor than
ad-hoc scripts can sustain across that churn. Build the abstractions right once so future
model/engine/provider swaps are config changes, not rewrites.

**This is deliberately the foundation layer of a larger system** (not built now, but designed
for). The eventual system will: take one long text prompt; split it into N segment prompts each
describing roughly one model-length's worth of video; generate a clip per segment that flows
coherently from the previous one; concatenate the clips into one long video; and add a synced
audio track. The single hardest constraint this imposes on *today's* design: the per-segment
length is **a discovered capability of whichever model is loaded** (Wan 2.1 ≈ 5 s; a newer model
must shift that number automatically), so model capabilities must be a first-class, queryable
thing — never a constant. Build the seams described in "Built to grow into a pipeline" below so
the upper layers slot in later without redesign; do **not** build those upper layers yet.

## Non-negotiable principles
1. **The core never imports a vendor or an engine.** Core code depends only on abstract
   interfaces. RunPod and CivitAI live in `providers/` and `sources/`; ComfyUI, Diffusers, and any
   hosted model-API live in `engines/`. All are discovered through a registry at runtime. Adding
   Vast.ai, Lambda, a local box, Hugging Face, S3, a plain URL, **or a new generation engine** must
   require **zero changes to core** — just a new adapter module that registers itself.
2. **Config-driven, not code-driven.** What to install and download is declared in a config
   file using vendor-neutral references (e.g. `civitai:1234@5678`, `hf:org/model`,
   `https://…/file.safetensors`). No model IDs, node lists, or provider names are hardcoded in
   logic.
3. **Secrets are never in config or code.** API keys/tokens come from environment variables or
   a pluggable credential provider. Config files must be safe to commit.
4. **Portable control plane.** The CLI you run locally must work on Linux, macOS, and Windows.
   Prefer the Python standard library; every third-party dependency must be justified and kept
   minimal. (The code that runs *on the GPU instance* may assume Linux.)
5. **Idempotent and resumable.** Re-running provisioning must not re-download what already
   exists (checksum/size check) and must recover from partial downloads.

## How this spec works with Superpowers and CLAUDE.md (read first)
This document is the **requirements spec**, not a process or testing manual. The Superpowers
plugin and the project's `CLAUDE.md` own the *how*; this spec owns the *what*. Specifically:

- **Process:** Let Superpowers drive its normal flow — brainstorm/refine the spec, produce the
  implementation plan, then execute it with its review checkpoints and its code-reviewer agent.
  Treat the "Built to grow into a pipeline", "Initial adapters", and "Build order" sections as
  *inputs to that planning phase*, not a rigid script. Surface genuine ambiguities during
  brainstorming rather than guessing.
- **Testing:** Follow `CLAUDE.md` and Superpowers for the red/green TDD loop and **all** pytest
  design conventions (test structure, fixtures, mocking style, naming, granularity). This spec
  does **not** prescribe test style. Where it lists things to "cover" or a "Definition of done",
  treat each as a **behavioral acceptance criterion to be written as a failing test first**, then
  satisfied — they are red-first targets, not a competing test philosophy. If anything here
  appears to dictate test *style*, defer to `CLAUDE.md`.
- **YAGNI reconciliation (important):** Superpowers' YAGNI must **not** prune the abstractions in
  this spec. Resolve the apparent tension with one rule: *the interfaces/seams are a present
  requirement; the layers behind them are correctly deferred.* For each abstraction
  (`GenerationEngine`, `ModelProfile` + strategy flags, `BackendPool`, `Stage`, `ArtifactStore`,
  the registry), build the interface **plus exactly one real path** now, and **do not** build the
  named future layers (prompt-splitter, stitching, audio stage, concurrent scheduler). That
  satisfies YAGNI honestly — the seam is needed now (it has an acceptance test and proves
  vendor/engine-agnosticism), the layer is not. Do not collapse an interface to a single concrete
  class on YAGNI grounds; multi-implementation swappability *is* the requirement, and the shipped
  adapters exist to prove it.
- **Code review:** the reviewer should check, in addition to plan adherence, the core invariant —
  core never imports a concrete provider/source/engine (see principle 1) — and that engine/provider
  swaps are config-only.

## Session durability & crash recovery (operational requirement — read first)
This project will be built across one or more Claude Code sessions, and a session can die
mid-run — for example, an API `400` on a malformed request that poisons the conversation so every
subsequent turn fails until it is cleared. When that happens, it must cost **at most the current
in-progress task**, never the design or any completed work. This is a **durability requirement
layered on top of Superpowers, not a competing process**: it does not change how Superpowers
brainstorms, plans, sequences, or reviews — it only requires that state live on disk and in git
rather than only in the conversation.

- **Git is the source of truth, not the conversation.** If the directory is not already a git
  repo, run `git init` and make an initial commit before anything else. Commit after every
  completed task or passing test, with a clear message. Never end a step with completed work left
  uncommitted.
- **Persist the brainstorm as it forms.** During brainstorming, as each design section is
  validated, append it to `DESIGN.md` (or whatever design doc Superpowers maintains) and commit it.
  The agreed design must never live only in the conversation — a crash mid-brainstorm must not lose
  it. This is the riskiest window: it is many reasoning-heavy turns before any file exists yet.
- **Plan to disk.** Ensure the implementation plan Superpowers produces is written to a file in the
  repo and committed *before* execution begins.
- **Maintain `PROGRESS.md` as the recovery index.** Keep a `PROGRESS.md` at the repo root, updated
  and committed after every task, containing: the paths of the design doc and the plan; the plan's
  task list with each item marked done / in-progress / next; key decisions and gotchas; and the
  single next action. This is the one file a fresh session reads first to re-orient.
- **Keep the resume protocol in `CLAUDE.md`.** `CLAUDE.md` is auto-loaded at the start of every
  session, so the recovery entry point lives there: it must contain a "Session resume protocol"
  directing a fresh session to read `PROGRESS.md` (then the design and plan it references, then
  `git log --oneline -20`) and resume from the first unchecked task **without redoing committed
  work**. Keep that protocol present and current as the build evolves.

Net effect: if a session dies, recovery is simply to open a new session and continue — the
auto-loaded `CLAUDE.md` plus `PROGRESS.md` and git history reconstruct context, and at most one
in-progress task is repeated.

## What the system must do (functional parity, generalized)
There are two execution contexts sharing one codebase and one config schema:

**A. Control plane (`deploy`)** — runs on the user's machine:
- For engines that need their own compute: select and reserve a GPU instance from the configured
  compute provider, matching hardware requirements (GPU type preference list, min VRAM, CUDA
  version, disk). For a hosted-API engine that runs nowhere of yours, **skip compute entirely** —
  the orchestrator must support engines that declare they need no instance.
- Launch it from a container image, expose the required ports, inject the config + credentials.
- Poll until ready; report the engine's service endpoints (e.g. a generation/UI URL).
- Manage lifecycle: list, status, stop, destroy. Support managing **multiple** instances.

**B. Data plane (`provision`)** — engine-specific setup, runs where the engine runs:
- The **selected generation engine owns its own environment setup**, driven by config. The core
  provisioner orchestrates the shared steps (download models in parallel — resumable, with optional
  checksum verification — and run the optional user hook) and delegates engine-specific setup to
  the engine adapter:
  - *ComfyUI engine*: install ComfyUI (pinned), install configured custom nodes (git +
    `requirements.txt`/`install.py`), route model files to the right ComfyUI subdirs, launch with
    configured flags.
  - *Diffusers engine*: install the Python deps, fetch weights to a cache, start a small headless
    inference server exposing the job API.
  - *Hosted-API engine*: little or no provisioning — just validate credentials and the endpoint.
- Persist heavy assets to a mounted volume when available, so restarts are fast.

## Cost-safety & instance lifecycle (hard requirement — build in from the start)
Non-serverless instances bill continuously, so the system MUST prevent runaway cost with defense in
depth. **Separate the invariant from the mechanism.** The *invariant* — bounded instance lifetime,
idle reap, hung-job abort, confirmed teardown, and no orphans — is universal and non-negotiable
across every provider; *how* it's enforced is provider-specific. A provider with **no native
idle-stop** (e.g. RunPod) must implement the self-terminating-instance mechanism below (layers 1–3);
a provider with its **own lifecycle management** (e.g. SkyPilot via autostop) satisfies the same
invariant through its native mechanism instead, and need not reimplement the in-pod path. Either
way, the universal backstops (layers 4–7: sweeper, confirmed teardown, budget, account limit) apply
to all providers. The guiding principle *for the self-terminating path*: every layer except a
self-terminating instance depends on something external (your machine, network, the orchestrator)
staying alive, so where that path is needed it is primary and the rest are redundancy — no single
failure may leave an instance billing. Three timers, each one concern: an **idle window**
(efficiency, warm reuse), a per-call **job_timeout** (hung-job detection), and a **max_lifetime**
ceiling (cost), the last as a *graceful drain* rather than a mid-job kill. Required layers:

1. **Self-terminating instance — the direct-provider mechanism (primary, zero external dependency).**
   For a provider with no native idle-stop, at provision time install, inside
   the pod: (a) a `max_lifetime` graceful-drain timer, (b) **local enforcement of the job's effective
   deadline** — at dispatch the pod is given that job's deadline (see layer 3) and self-terminates if
   the job overruns it, so a dead controller plus a hung job cannot hold liveness open and defeat the
   switch — and (c) a heartbeat dead-man's switch. **Liveness = "an in-flight job still under its
   effective deadline," OR a `heartbeat()` within ~2× the idle window when idle.** This means a
   single long job or a native-extension stream is NOT killed mid-run (it's alive while under its
   deadline), while a truly idle or wedged pod still self-terminates. It survives the orchestrator,
   laptop, or network dying. This is non-negotiable for a provider like RunPod that doesn't self-stop;
   a provider with native autostop (e.g. SkyPilot) meets the same invariant through that instead and
   skips this in-pod path.
   **Self-termination credential — provisioning & scope:** the in-pod kill needs a credential to
   call the provider's delete API, so use a *dedicated, least-privilege* key — never the account's
   main key. It MUST be scoped so its worst-case leak can only *delete* (ideally just this pod, via
   `RUNPOD_POD_ID`; at most pod-terminate), and MUST NOT be able to create instances or spend, so a
   compromised key can never run up a bill — only tear things down. Inject it at `create_instance`
   time as an environment secret via the `CredentialProvider`; never bake it into the image or
   commit it to config, bound its lifetime to the pod, and rotate it.
2. **Idle window (warm reuse).** The orchestrator reaps a pod after `idle_timeout` of no jobs; jobs
   arriving within the window reuse the already-loaded model + LoRAs (no reload). **Default
   `idle_timeout = 2h`.**
3. **`job_timeout` (hung-job detection) + `max_lifetime` graceful drain (cost ceiling).** These were
   one conflated "hard kill"; split them:
   - **`job_timeout` is a PER-CLIP budget, not a flat per-call cap** — because a native-extension job
     is ONE call whose duration scales with the number of segments. The orchestrator computes each
     job's **effective deadline** from it: `effective_deadline ≈ segments × job_timeout + time_buffer`
     (a configurable buffer, default 30m)
     (so a single-clip/stitching job ≈ `job_timeout`; an N-segment native stream ≈ `N × job_timeout`).
     A job exceeding its effective deadline is the hung-job signal: it's aborted and its instance torn
     down (enforced locally in-pod per layer 1, so it holds even if the controller is gone). This is
     what stops `job_timeout` from false-positive-aborting a legitimately long native generation.
     **Default `job_timeout = 30m` per clip.**
   - **`max_lifetime`** is a graceful drain, NOT an unconditional mid-job kill: at `max_lifetime` the
     instance stops accepting NEW jobs and tears down once the in-flight job finishes or hits its
     effective deadline. **Default `max_lifetime = 5h`.** Worst-case wall-clock is therefore bounded
     at `max_lifetime + (longest in-flight job's effective deadline)` — no third absolute timer is
     needed.
4. **Independent external sweeper.** A standalone scheduled job (cron / CI / a small serverless
   function), running independently of the orchestrator, lists ALL account instances and destroys
   any that exceed policy or are orphaned/untagged. Backed by instance **tags** + a **persistent
   ledger** of everything launched, so orphans are always findable (even from a fresh CLI or a
   different machine). The sweep must be **provider-aware**: for a provider with its own state (e.g.
   SkyPilot), reconcile through that provider's state (`sky status`) rather than hitting the
   underlying cloud API directly, so it never destroys instances the provider still believes it
   manages (which would corrupt that provider's state).
5. **Hardened, confirmed teardown.** `destroy_instance` polls until the instance is actually gone;
   on failure it retries and alerts loudly. Fire-and-forget teardown is forbidden — a silently
   failed destroy is the cost leak.
6. **Budget ceiling + visibility.** Track estimated cumulative spend per instance/session and kill
   on crossing `budget`. The CLI surfaces running instances, their age, and estimated spend on
   every invocation, and refuses to spawn a duplicate pod for the same job.
7. **Account backstop (documented).** README must tell the user to set RunPod's account spend limit
   and keep a modest balance as a final net — explicitly noted as last-resort, not a real control.

Serverless engines are exempt from 1–3 (they scale to zero by construction); a SkyPilot-managed
instance satisfies 1–3 through SkyPilot's own autostop/lifecycle rather than the in-pod mechanism.
Both still get the universal layers — tagging, the ledger, the sweeper, confirmed teardown, and the
budget ceiling. Defaults live in config and are overridable:
`idle_timeout = 2h`, `job_timeout = 30m` (per clip), `time_buffer = 30m`, `max_lifetime = 5h`, plus a `budget`. **Config validation
MUST reject nonsensical combinations** (e.g. `idle_timeout >= max_lifetime`, or
`job_timeout > max_lifetime`).

## Architecture you must implement
Two planes, talking through these abstractions. Refine the exact signatures, but keep the
separation strict.

```python
# core/interfaces.py  — the only thing core code depends on

class HardwareRequirements:
    """Filter applied by ComputeProvider.find_offers — provider keeps only offers meeting these.
       Defaults are baked in but every field is config-overridable."""
    min_vram_gb: int = 48                  # default 48 GB; rejects undersized cards
    min_cuda: str = "12.8"                  # minimum CUDA driver version (e.g. "12.8")
    max_usd_per_hr: float = 2.20  # ceiling for POD-MODE offers only; ignored for serverless (per-second billing — use `budget` instead)
    gpu_preference: list[str] = []          # ordered preference list (e.g. ["RTX 4090", "RTX 5090"]); when set, providers should try in order among the offers that already pass the filters above
    disk_gb: int = 100                      # minimum container/instance disk

class ComputeProvider(ABC):
    """A place to run GPU workloads. RunPod (pod or serverless), Vast, Lambda, Local, ...
       Instances must be created with cost guardrails and a self-termination mechanism; see the
       Cost-safety section. `destroy_instance` must CONFIRM termination, never fire-and-forget."""
    name: str
    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]: ...   # MUST exclude any offer failing min_vram_gb, min_cuda, or (for pod mode) max_usd_per_hr; preserve gpu_preference order in the returned list
    def create_instance(self, spec: InstanceSpec) -> Instance: ...   # spec carries guardrails: idle_timeout, job_timeout, max_lifetime, budget; provider installs the in-pod dead-man's switch + local job_timeout enforcement + max_lifetime drain at startup
    def get_instance(self, instance_id: str) -> Instance: ...
    def list_instances(self) -> list[Instance]: ...   # must list ALL of this account's instances, so an external sweeper can find orphans
    def stop_instance(self, instance_id: str) -> None: ...
    def destroy_instance(self, instance_id: str) -> None: ...   # poll until actually gone; retry + alert on failure
    def heartbeat(self, instance_id: str) -> None: ...   # liveness ping for the dead-man's switch when idle; an in-flight job under its effective deadline is itself liveness (so long jobs aren't killed mid-run)
    def endpoints(self, instance: Instance) -> dict[str, str]: ...  # {"generate": "https://..."}

class ModelSource(ABC):
    """Resolves a vendor-neutral reference into downloadable artifact(s).
       CivitAI, HuggingFace, HTTP, S3, ..."""
    scheme: str  # e.g. "civitai", "hf", "https"
    def handles(self, ref: str) -> bool: ...
    def resolve(self, ref: str, creds: "CredentialProvider") -> list[Artifact]: ...
    # Artifact = an addressable content handle reused throughout (downloaded model files, input
    #   conditioning assets, generated output clips, ArtifactStore items). For the download case it
    #   carries url/how-to-fetch + filename + expected size/sha256 + auth; other cases carry a store ref.

class CredentialProvider(ABC):
    def get(self, key: str) -> str | None: ...   # env-backed by default

# --- generation layer: the seams the future long-form pipeline plugs into ---

class CapabilityKey:
    """The full identity a ModelProfile depends on — NOT just the base model. Capability is a
       function of all of these, so the profile cache and every capability lookup key on a STABLE
       DERIVED key over them (e.g. a hash). This is why base-model-alone vs. base+SVI-LoRA, or the
       same model under ComfyUI vs. Diffusers, are DISTINCT cache entries instead of colliding."""
    base_model: str
    loras: tuple[str, ...] = ()   # ORDERED LoRA stack (order matters)
    engine: str = ""               # engine name (capability is engine-specific)
    precision: str = ""            # e.g. "fp16" | "gguf-q8" | "" (precision/quantization)
    def derive(self) -> str: ...   # stable, order-sensitive key over all fields

class ModelProfile:
    """Capabilities of a model+LoRAs+engine, read **at plan time from a cache keyed by the full
       `CapabilityKey`** (base model + ordered LoRA stack + engine + precision/quantization) —
       NOT hardcoded constants, and NOT dependent on a running model in the common case. This is
       what lets a pre-generation stage (the prompt splitter) read `max_segment_seconds` before any
       backend exists, and what makes '5 seconds' a variable that shifts when the model changes. The
       cache is never hand-maintained: on a miss it is auto-populated once by introspecting the live
       model through the existing engine machinery (see ModelProfileProvider.discover). Once cached,
       later runs read it with no compute; `verify` re-introspects and fails hard on any drift."""
    name: str
    max_frames: int
    fps: int
    @property
    def max_segment_seconds(self) -> float: ...   # = max_frames / fps
    supported_modes: set[str]      # the SET of modes this model supports, e.g. {"t2v", "i2v", "flf2v"}
    max_resolution: tuple[int, int]
    # --- strategy flags: let the orchestrator choose per-model how to build long video ---
    # These two are NOT probeable from the model; the engine adapter DECLARES them per CapabilityKey
    # (capability is engine+model+LoRA specific). Undeclared -> default False + a warning (see the capability-discovery item under "Built to grow into a pipeline").
    supports_native_extension: bool   # model can extend/continue its own clip (vs. our stitching)
    supports_joint_audio: bool         # model emits synced audio with video (vs. a separate stage)

# Universal, single source of truth for the conditioning-asset ROLES each mode requires — a mode's
# requirements are conventions of the mode, NOT per-model, so they live here ONCE and the model only
# declares which modes (the set above) it supports. Validation reads roles from here, indexed by mode.
MODE_ROLE_REQUIREMENTS: dict[str, set[str]] = {
    "t2v": set(),
    "i2v": {"init_image"},
    "flf2v": {"first_frame", "last_frame"},
    # new modes add one entry here, in one place
}

class ModelProfileProvider(ABC):
    """A **cache** of ModelProfiles keyed by `CapabilityKey`, not a hand-maintained table — so
       there's nothing to forget. `resolve` is plan-time and static: a CapabilityKey in, cached
       ModelProfile out, with NO backend and NO loaded model, so planning stages (the splitter) can
       read it before any compute exists. On a cache miss it raises `ProfileNotCached`; the
       orchestrator then runs `discover` ONCE to populate the entry. **Discovery is single-flight
       (in-process for now)**: concurrent misses for the SAME key coordinate so the model is probed
       exactly once, not once per caller, and all waiters receive the one persisted profile. (If the
       cache later becomes a store shared across machines, cross-process coordination is a follow-up —
       use the store's atomic primitives; see the brainstorming note.) `discover` and `verify` share
       one DRY source of truth — `GenerationBackend.inspect_capabilities()` — so the rules for
       reading a model's real caps live in exactly one place."""
    def resolve(self, key: CapabilityKey) -> ModelProfile: ...   # plan-time cache read; raises ProfileNotCached on miss
    def discover(self, key: CapabilityKey, engine: "GenerationEngine", backend: "GenerationBackend") -> ModelProfile: ...  # single-flight: probe live model (via backend.inspect_capabilities) for readable fields, MERGE engine.declared_flags(key), persist under key, return
    def verify(self, profile: ModelProfile, backend: "GenerationBackend") -> None: ...      # re-probe & compare ONLY the probeable fields (frames/fps/resolution/modes); raise on drift. NOTE: cannot check the declared strategy flags (they aren't probeable) — a wrong declaration is not caught here.

class GenerationEngine(ABC):
    """A swappable generation engine: ComfyUI, Diffusers, a hosted model-API, ...
       Owns its own environment setup and knows whether it needs compute at all.
       The core selects an engine by name from config; it never imports one."""
    name: str                                  # "comfyui" | "diffusers" | "hosted" | ...
    requires_compute: bool                      # False for hosted-API engines
    def provision(self, instance: "Instance | None", cfg: dict) -> None: ...  # engine-specific setup
    def backend(self, instance: "Instance | None", cfg: dict) -> "GenerationBackend": ...
    def profile_for(self, key: CapabilityKey) -> ModelProfile: ...   # plan-time; delegates to the registered ModelProfileProvider.resolve (cache read; orchestrator handles discovery on miss)
    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]: ...  # engine's per-CapabilityKey declaration of the non-probeable strategy flags; empty/partial if unknown (-> safe False + warning). Merged into the profile by discover.
    def validate_spec(self, job: "GenerationJob") -> None: ...  # validate this job's engine-interpreted `spec` against the engine's schema; raise a clear error on missing/ill-typed keys BEFORE dispatch (never pass an unvalidated dict through)

class GenerationBackend(ABC):
    """A live, ready engine you can submit jobs to. ComfyUI (HTTP/ws graph API),
       Diffusers (in-process or a small served API), hosted (remote API) all implement
       this identically. The orchestrator talks ONLY to this + ModelProfile, never to a
       specific engine or compute vendor."""
    def capabilities(self) -> ModelProfile: ...   # the profile in force (the cached/discovered one)
    def inspect_capabilities(self) -> ModelProfile: ...  # DRY minimum-work introspection of the LIVE model (the "discover" mode/flag): read config/metadata, do the least work needed to fill the PROBEABLE fields (frames/fps/resolution/modes); never generates a full clip. The non-probeable strategy flags are layered in by discover from the engine's declared_flags. Used by discover + verify.
    def submit(self, job: "GenerationJob") -> str: ...        # returns job id
    def result(self, job_id: str) -> "Artifact": ...           # output clip/frames
    def endpoints(self) -> dict[str, str]: ...

class ConditioningAsset:
    """A non-text input. The container is general now; handling is added later behind it.
       `kind` is an OPEN enum — image/audio/video today, extensible — so adding e.g.
       sound-to-video later means a new engine that accepts kind=audio, NOT a contract change.
       `role` determines which model SLOT this asset fills; list position is ignored for slot
       assignment, EXCEPT that when a role legitimately repeats (e.g. a future keyframe role) the
       order among assets sharing that role is the temporal sequence. The reference is stored via the
       ArtifactStore. Keep this minimal; no speculative per-kind fields."""
    kind: str               # "image" | "audio" | "video" | ... (open enum)
    role: str               # slot, e.g. "init_image" | "first_frame" | "last_frame" | "drive_audio" | "source_video"
    ref: "Artifact"          # the asset, in the ArtifactStore
    meta: dict = {}          # optional, minimal

class GenerationRequest:
    """The top-level input to the system: ONE text prompt, an EXPLICIT `mode`, and a role-tagged list
       of conditioning assets. The mode is stated, never inferred from asset count/kind, and is
       validated against the model's `supported_modes`; an unsupported mode is rejected.
       **Role-authoritative validation:** the mode's required-role set (from the universal
       `MODE_ROLE_REQUIREMENTS[mode]` table, NOT duplicated per-model) is the contract — each required
       role must be present exactly once with the right `kind` (e.g. flf2v needs exactly one `image`
       `first_frame` and one `image` `last_frame`); a missing OR duplicated required role is a hard
       error (so two `init_image`s fail loudly). Single-asset modes MAY default the role for
       ergonomics (a lone image → `init_image`); multi-asset modes MUST use explicit roles.
       This `mode` + role check governs the user-supplied ENTRY assets only. In a native-extension
       stream the per-segment continuity assets (e.g. the prior clip's last frame) are injected
       downstream and are NOT re-validated against the entry mode — segments 1..N of an i2v-driven
       long video aren't themselves "i2v". Audio/video assets are representable today but only consumed
       by an engine that declares support for that kind; any engine MUST reject a kind/role it can't
       handle with a clear error, never silently.
       The (future) splitter expands this prompt into an ordered list of `Segment`s and DISTRIBUTES
       these request assets into them (e.g. an i2v init image becomes the first segment's asset)."""
    prompt: str
    mode: str                              # MUST be one of the model's supported_modes
    assets: list[ConditioningAsset] = []

class Segment:
    """One clip's worth of the plan (≈ one `max_segment_seconds` of video): a per-clip prompt, its
       own conditioning assets, and optional per-clip overrides (e.g. SVI requires a DISTINCT seed
       per clip). `assets` is the EFFECTIVE per-clip conditioning the engine actually receives — the
       request assets distributed into this segment PLUS any continuity asset the stitcher injects
       (e.g. the previous clip's last frame). The single-clip happy path is the trivial case: one
       request → one segment, the request's assets copied straight in."""
    prompt: str
    assets: list[ConditioningAsset] = []
    params: dict = {}        # per-clip overrides merged over the job's params (e.g. seed)

class GenerationJob:
    """One unit of work submitted to a backend, carrying an ORDERED list of segments. The strategy
       decision point (see capability discovery) chooses the shape from `supports_native_extension`:
         • native-extension engines (e.g. SVI) take the WHOLE segment stream in ONE call and emit a
           single continuous video — the model does clip-to-clip continuity itself;
         • otherwise the orchestrator issues single-segment jobs (one per clip) and owns continuity
           (carrying the prior clip's last frame as a `Segment.asset`) + concatenation.
       So a stitching job has `len(segments) == 1`; a native-extension job has N. `spec` is
       engine-interpreted (graph template / pipeline params / API payload)."""
    spec: dict               # engine-interpreted
    params: dict = {}        # engine-neutral defaults shared across segments (fps, resolution, steps, ...); a Segment's params override these
    segments: list["Segment"]   # ordered; length 1 for a single-clip/stitching job, N for a native-extension stream (replaces the old single prompt + `inputs`/`condition_on`)

class BackendPool(ABC):
    """Dispatches jobs across one or more GenerationBackends. Ship a trivial
       SequentialPool now; the contract must match what a future concurrent/distributed
       pool would expose so it's a drop-in replacement, not a refactor."""
    def add(self, backend: GenerationBackend) -> None: ...
    def submit(self, job: GenerationJob) -> "Future[Artifact]": ...
    def map(self, jobs: list[GenerationJob]) -> list["Artifact"]: ...


```

- A **registry** (`core/registry.py`) maps provider/source names + schemes to implementations.
  Adapters self-register (entry points or an explicit `register()` call). Core resolves by
  name/scheme only — it must never `import providers.runpod`.
- A shared **downloader** (`core/downloader.py`): parallel, resumable, checksum-verifying.
  Pluggable backend — a stdlib threaded ranged-GET implementation by default, optionally using
  `aria2c` if present on the host. Sources produce `Artifact`s; the downloader fetches them;
  sources stay dumb about HTTP plumbing.
- An **orchestrator** (`core/orchestrator.py`) for the deploy flow and a **provisioner**
  (`core/provisioner.py`) for the on-instance flow, both driven by the validated config model.

## Built to grow into a long-form video pipeline (design these seams now; don't build the layers)
The end goal: long prompt → split into N segment prompts → generate a clip per segment that
flows from the previous → concatenate → add synced audio. Today you build provisioning +
single-clip generation, but you **must** put these seams in place so the rest bolts on later
with no redesign:

1. **Model-capability discovery (the most important one).** Implement `ModelProfile` +
   `ModelProfileProvider` as a **self-populating cache**, so a model's `max_frames`/`fps`
   (→ `max_segment_seconds`), supported modes, and resolution limits are read **at plan time from
   the cache, keyed by the full `CapabilityKey`** (base model + ordered LoRA stack + engine +
   precision/quantization — so base-alone vs. base+SVI-LoRA, or the same model under different
   engines, are distinct entries), with **no running model and no provisioned compute** in the common
   path. This is non-negotiable because of ordering: the prompt splitter runs *before*
   generation, so it must read `max_segment_seconds` from `profile_for(key)` with nothing
   spun up. Nothing downstream may hardcode a duration.
   **No hand-maintained table — nothing to forget.** On a cache miss the system auto-builds the
   entry, DRY, by reusing the *existing* engine + compute machinery in a minimum-work introspection
   mode (`GenerationBackend.inspect_capabilities()`): provision the smallest viable backend, load
   the model, read off every **probeable** field doing the least work necessary (read config/metadata; at
   most a trivial 1-step/1-frame pass — never a full clip); the non-probeable strategy flags are
   merged in from the engine's `declared_flags`. Persist the profile under its key, then
   tear that backend down. Discovery is **single-flight** (concurrent misses for the same key probe
   once, not once per caller). Run the probe in the cheapest viable mode the provider offers (a
   scale-to-zero/serverless mode where available; otherwise a normal short-lived instance).
   Subsequent runs hit the cache and need no compute.
   **Guaranteed ordering (make this explicit in code):** (a) `resolve(key)` cache read →
   hit: continue; miss: run `discover` once (introspect → persist) so the entry now exists →
   (b) **validate the `GenerationRequest`** against the now-known profile — `mode` ∈
   `supported_modes`, and the role-authoritative asset check (`MODE_ROLE_REQUIREMENTS[mode]`) — so an
   unsupported mode or malformed asset set fails HERE, before any split or compute → (c) the
   splitter consumes `max_segment_seconds` and chops the prompt → (d) provision the generation
   backend → (e) call `verify(profile, backend)`, which re-runs the same `inspect_capabilities()`
   and compares. **If the live model contradicts the cached profile, fail hard: raise, abort the
   run, and tear down whatever compute is running the model** (destroy the pod / stop the serverless
   worker via the `ComputeProvider`) so a misconfigured instance can't keep billing or silently
   generate against a wrong split.
   The profile also carries two **strategy flags** — `supports_native_extension` and
   `supports_joint_audio` — which are *not* probeable from the model, so the **engine adapter
   declares them per `CapabilityKey`** (`GenerationEngine.declared_flags(key)`); capability here is
   engine+model+LoRA specific (a model may do native extension via a ComfyUI node but not a Diffusers
   pipeline). `discover` merges these declared flags onto the probed fields. If a model isn't in the
   engine's declaration, the flags default to `False` (safe: fall back to our own stitching/audio)
   **and the system logs a clear warning that the model is running under-used and a declaration would
   unlock native extension/audio** — so a missing declaration is loud, never silent, and never
   *wrong*, just conservative. The design must include a single, explicit decision point where the
   orchestrator reads the flags to choose *how* to build long video per model: native extension
   (feed the whole `segments` stream to one backend call, à la SVI) vs. our own stitching (one job
   per segment, carrying the prior clip's last frame as continuity), and joint audio vs. a separate
   audio stage. Implement the decision point and the native-capability path that needs no extra code;
   the fallback paths (manual stitching, separate audio) stay deferred behind it. This is what keeps
   the system model-agnostic as models absorb more of the pipeline.
2. **An engine-neutral generation primitive + swappable engines.** Generation goes through the
   `GenerationEngine` / `GenerationBackend` interfaces, with **ComfyUI, Diffusers, and a hosted
   model-API as sibling adapters** in `engines/`. Each implements submission its own way — ComfyUI
   drives its HTTP/websocket graph API (`/prompt`, `/history`, progress); Diffusers runs a pipeline
   in-process or behind a small served API; the hosted adapter calls a remote API and needs no
   compute of yours. The orchestrator submits an engine-neutral `GenerationJob` and gets back an
   `Artifact`; it must never branch on which engine is active. Headless/programmatic submission is
   the load-bearing path (interactive UIs, where an engine has one, are a bonus).
3. **A `GenerationBackend` abstraction + a `BackendPool` exercised from day one.** The infra layer
   (the selected engine, optionally atop a ComputeProvider instance) hands back a
   `GenerationBackend`. The future orchestrator depends only on `BackendPool` +
   `GenerationBackend` + `ModelProfile`, never on a specific engine or compute vendor. **Define the `BackendPool`
   interface now** (e.g. `submit(job) -> future`, `map(jobs) -> results`, register/remove backends)
   **and ship a trivial `SequentialPool`** that holds one or more backends and runs jobs one at a
   time. This is deliberately not a real scheduler — it just guarantees the pool interface is real
   and tested, so the eventual parallel/distributed scheduler is a drop-in replacement, not a
   refactor. Keep the submit/map contract identical to what a concurrent pool would expose.
4. **A minimal pipeline/stage seam.** Define a `Stage` protocol (typed input → typed output over a
   shared artifact context) and implement *one* concrete stage — "generate a clip from a
   `GenerationRequest`" — by submitting a `GenerationJob` to a `GenerationBackend`. Future stages
   (prompt-splitting, continuity-stitching, audio-sync) become additional `Stage`s. A job carries an
   ordered `segments` list (each a per-clip prompt + its conditioning assets), so a native-extension
   engine consumes the whole stream in one call while the stitching fallback gets single-segment
   jobs — both continuity and multi-asset modes are wireable later without touching the contract.
5. **An `ArtifactStore` abstraction** (local filesystem now; S3/GCS/etc. as future plugins, same
   registry pattern as sources/providers). Stages read/write media artifacts through it, so data
   can later flow across stages and across instances without rework.

Implement the **interfaces and the single-clip happy path** for all five, including the
`SequentialPool`. Do **not** build the prompt splitter, the continuity/stitching logic, the audio
layer, or a *concurrent/distributed* scheduler — the pool you ship runs sequentially and exists
only to lock the interface. Mark each deferred extension point explicitly in code and in whatever
design/plan doc Superpowers produces.

## Initial adapters to ship (proving the abstraction with >1 of each)
- Compute: **`RunPodProvider`** (REST API: create/get/list/stop/destroy pods, endpoints via the
  `https://{id}-{port}.proxy.runpod.net` pattern, CUDA-version filtering); a **`LocalProvider`**
  (runs against the local machine / a local Docker container) so the system is usable and testable
  without any cloud account; and a **`SkyPilotProvider`** that wraps [SkyPilot](https://github.com/skypilot-org/skypilot)
  (Apache 2.0) for instant multi-cloud reach (AWS, GCP, Azure, CoreWeave, Lambda, Vast, Kubernetes,
  on-prem, …) and price-optimized GPU selection across providers. `RunPodProvider` must expose both
  **pod** mode (a persistent rented instance) and **serverless** mode (a scale-to-zero, autoscaling
  endpoint) behind the same `ComputeProvider` interface, selectable by config — so a stage can
  choose its cheapest viable mode without any caller or core changes. The three providers exist to
  prove the abstraction is real (not just two close cousins) and to let the user pick per workload:
  the direct `RunPodProvider` when the spec's precise guardrails matter (in-pod local enforcement
  of the effective deadline, the dedicated least-privilege self-termination credential, the custom
  sweeper); `SkyPilotProvider` when cloud reach or cross-provider price optimization matter and
  SkyPilot's autostop/lifecycle semantics are acceptable. **Document the trade-off explicitly:**
  the `SkyPilotProvider` will not replicate the exact custom timer model — it accepts SkyPilot's
  defaults (or layers ours on top where possible) — and that limitation is the cost of inheriting
  SkyPilot's multi-cloud reach. The `from skypilot import …` lines only ever live in
  `providers/skypilot/`, consistent with principle 1 (core never imports a vendor).
- Model sources: **`CivitAISource`** (`civitai:<modelId>[@<versionId>]`, token via creds),
  **`HuggingFaceSource`** (`hf:<repo>[:<path>]`), and **`HTTPSource`** (`https://…`, optional
  `sha256`). Shipping three proves routing by scheme is real.
- Generation engines (the new axis): **`ComfyUIEngine`** (drives the ComfyUI graph API; owns
  install + custom-node + launch), **`DiffusersEngine`** (Python pipelines, served headlessly),
  and a **`HostedAPIEngine`** (calls a remote model-API; `requires_compute = False`, so it
  exercises the no-instance path). Plus a **`FakeEngine`** for tests (deterministic fake artifacts,
  no GPU/model). Shipping ComfyUI + Diffusers proves engine-specific provisioning; the hosted +
  fake engines prove the no-compute and test paths.
- Node source: git-based custom-node installation (clone + requirements/install handling), used by
  the ComfyUI engine.

## Config schema (vendor-neutral; secrets excluded)
Use a typed, validated schema (e.g. pydantic) loaded from YAML. Example:

```yaml
engine:
  kind: comfyui              # swap to "diffusers" or "hosted" without code changes
  precision: "fp16"          # precision/quantization variant (e.g. "fp16" | "gguf-q8"); part of the CapabilityKey
  comfyui:                   # only read when kind == comfyui
    version: "v0.3.40"       # pinned git ref or release
    launch_args: ["--listen", "--enable-cors-header=*", "--use-sage-attention"]
    custom_nodes:
      - git: https://github.com/kijai/ComfyUI-WanVideoWrapper
      - git: https://github.com/kijai/ComfyUI-KJNodes
    pip: ["opencv-python"]
  # diffusers: { pipeline: "...", torch_compile: true }      # used when kind == diffusers
  # hosted:    { provider: "fal", endpoint: "...", model: "ltx-2" }  # used when kind == hosted
# The orchestrator DERIVES the CapabilityKey from this config: base model + the ordered LoRA stack
# (from `models` below), the engine `kind`, and the engine `precision`. That key is what the profile
# cache and declared_flags are looked up by — so base-vs-base+LoRA, or a precision/engine change, are
# distinct cache entries automatically.
# Each model entry has two distinct fields: `kind` (base|lora|vae) drives the CapabilityKey IDENTITY
# (base vs LoRA, and LoRA-stack order), while `target` drives FILE PLACEMENT (the engine maps it to
# its own layout). They serve different purposes; config validation MUST reject inconsistent pairings.
models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B"
    kind: base                     # base|lora|vae — identity for the CapabilityKey (NOT a conditioning-asset role)
    target: diffusion_models       # file placement; engine maps the target to its own layout
  - ref: "civitai:1234@5678"
    kind: lora                     # LoRA stack; list order here is part of the CapabilityKey
    target: loras
  - ref: "https://example.com/some.vae.safetensors"
    kind: vae                      # excluded from the CapabilityKey (decode-only, doesn't change caps)
    target: vae
    sha256: "abc123..."
compute:                     # OMIT entirely for a hosted engine (requires_compute = False)
  provider: runpod           # swap to "local", "skypilot", "vast", ... without code changes
  image: "your/engine-image:tag"
  requirements:                  # filter passed to ComputeProvider.find_offers (HardwareRequirements)
    gpu_preference: ["RTX 4090", "RTX 5090"]
    min_vram_gb: 48              # default 48
    min_cuda: "12.8"             # default "12.8"
    max_usd_per_hr: 2.20   # default 2.20; pod-mode only (ignored for serverless — use `lifecycle.budget` instead)
    disk_gb: 100
  ports: ["8188/http", "22/tcp"]
  volume: { size_gb: 100, mount: /workspace }
  mode: pod                  # RunPod-specific: "pod" or "serverless". Not all providers have modes (e.g. SkyPilot has none).
  lifecycle:                 # cost-safety guardrails (see Cost-safety section). NOTE: the INVARIANT is universal, but how each guardrail is honored is provider-specific — a direct provider (RunPod) enforces these via the in-pod mechanism; SkyPilot maps idle_timeout onto its autostop and may not honor the others identically (document the mapping in the adapter).
    idle_timeout: 2h         # reap after this long with no jobs (warm-reuse window)
    job_timeout: 30m         # per-CLIP budget; a job's effective deadline ≈ segments × job_timeout (+ time_buffer), so native-extension streams aren't aborted for being legitimately long; overrun -> abort + teardown (enforced in-pod)
    time_buffer: 30m         # buffer added to a job's computed effective deadline; default 30m
    max_lifetime: 5h         # graceful drain: stop taking NEW jobs, tear down after the in-flight job ends (never mid-job; worst-case wall-clock = max_lifetime + job_timeout)
    budget: 25.00            # USD ceiling per instance/session; kill on crossing
hooks:
  post_provision: "./hooks/extra.sh"   # optional escape hatch
```

The `engine.kind` field selects the engine adapter; only its matching sub-block is read.
Switching engines (and, for hosted, dropping `compute:` altogether) is a config edit, not a code
change.

Credentials referenced by these adapters (`CIVITAI_TOKEN`, `HF_TOKEN`, `RUNPOD_API_KEY`, …) are
read from the environment / credential provider, **never** from this file.

## Suggested repo layout (adjust as needed)
```
.
├── pyproject.toml
├── README.md
├── src/kinoforge/
│   ├── core/         interfaces, registry, config, downloader, orchestrator, provisioner,
│   │                 backend pool, model profiles, the long-video strategy decision point
│   ├── providers/    runpod/, local/
│   ├── sources/      civitai/, huggingface/, http/
│   ├── engines/      comfyui/ (+ git node installer), diffusers/, hosted/, fake/
│   ├── pipeline/     Stage protocol + the single "generate clip" stage (seam for more)
│   ├── stores/       artifact store: local/ (s3/, gcs/ later)
│   └── cli.py        deploy | provision | generate | list | status | stop | destroy | reap
├── examples/configs/
├── hooks/
├── tests/
└── .github/workflows/ci.yml
```

## Tech & quality constraints
- Python 3.10+. Full type hints; pass `mypy` (or `pyright`) and `ruff`.
- Dependency policy: stdlib-first. Each added dependency must be justified in the plan.
- Tests with `pytest`, following `CLAUDE.md`/Superpowers for style and the red/green loop (this
  spec doesn't dictate test design). Hard constraint regardless of style: **no test may require
  real cloud credentials, network, GPUs, or model weights** — the `LocalProvider`, a fake
  in-memory source, and the `FakeEngine` exist precisely so everything is testable offline.
  Behaviors that must end up covered (write them red-first): registry routing, config validation,
  scheme dispatch, downloader resume/checksum, provider lifecycle, engine selection, and the
  long-video strategy decision point (both flag branches).
- A `--dry-run` everywhere it makes sense (print the planned actions/payloads, call nothing).
- Structured logging; clear, actionable errors (esp. auth failures and capacity/availability).
- CI (GitHub Actions): lint + type-check + tests on Linux/macOS/Windows for the control plane.

## Engineering practices
- Let Superpowers own planning, execution sequencing, commits, and review checkpoints — don't
  impose a separate process. (The **Session durability & crash recovery** section above is a durability requirement, not a competing development process — follow it in addition.) The interface definitions and dependency justifications this spec
  asks for should live in whatever plan artifact Superpowers produces. The build order below is a
  *suggested decomposition* for that plan, not a fixed sequence.
- Build order suggestion: (1) interfaces + registry + config model + tests; (2) downloader +
  HTTP source + tests; (3) `GenerationEngine` interface + `FakeEngine` + provisioner +
  `LocalProvider`, end-to-end against the fake engine; (4) ComfyUI engine (incl. node
  installer) + RunPod provider; (5) CivitAI + HuggingFace sources; (6) a second real engine (Diffusers) and
  the `HostedAPIEngine` to prove the no-compute path; (7) `ModelProfile`/profile provider + the
  long-video strategy decision point + `BackendPool`/`SequentialPool` + the single "generate clip"
  `Stage` + local `ArtifactStore`; (8) CLI + examples + README + CI.
- The README must include a quickstart, an "Extending: add a new compute provider / model source /
  **generation engine**" guide showing each is a drop-in adapter with no core changes, **and** a
  short "Roadmap" section naming the deferred pipeline layers (prompt-splitting, continuity, audio,
  concurrent/distributed backend scheduler, and image generation / keyframe production — the last as
  a future *upstream* `Stage` that produces images consumed as `ConditioningAsset`s by i2v/flf2v, NOT
  a new core output mode) and the seam each plugs into; **and** a brief "Design references" note
  crediting SkyPilot (Apache 2.0, UC Berkeley Sky Computing Lab) as a major influence on the
  compute-axis abstractions (the `ComputeProvider` interface, lifecycle/autostop, cost-aware GPU
  selection), since one of the shipped providers wraps it.

## Non-goals (guard the scope)
Do not build: a ComfyUI fork or replacement, a web UI, a workflow editor, a Kubernetes operator,
or a billing/cost system. Equally, do **not** build the upper pipeline layers yet — the prompt
splitter, the continuity/stitching logic, the audio-sync layer, or a *concurrent/distributed*
backend scheduler. (A trivial `SequentialPool` that exercises the pool interface **is** in scope;
real parallel fan-out is not.) This release is the provisioning + single-clip-generation layer
**plus the seams** those future layers will attach to (see "Built to grow into a pipeline").
Designing the seams is in scope; implementing the layers is not.

## Open questions to resolve in brainstorming
Superpowers will brainstorm before coding — raise these genuine design tensions there rather than
silently picking one:
- **Capability-resolution ownership:** `ModelProfileProvider` vs `GenerationEngine.profile_for()`
  both yield a `ModelProfile`. The *timing* is already decided (plan-time, static, no backend; see
  the capability-discovery item) — what's left is the wiring: have `profile_for` delegate to the one registered
  `ModelProfileProvider` so there's a single source of truth, not two implementations that can drift.
  Keep the three profile accessors distinct and documented so they aren't conflated: `resolve` /
  `profile_for` = the plan-time cache read; `GenerationBackend.capabilities()` = the in-force profile
  the live backend was configured with; `inspect_capabilities()` = the raw live probe that `discover`
  and `verify` build on. Only the last touches the running model.
- **Sync vs async submission:** `GenerationBackend.submit/result` reads as poll-based, but
  `BackendPool` returns `Future`s. Reconcile the contract so an inherently synchronous backend
  (in-process Diffusers) and an async one (remote/hosted) both satisfy it without special-casing.
- **What `models:` means per engine:** downloads make sense for ComfyUI/Diffusers but not a hosted
  API. Decide whether the block becomes validation-only (or a no-op) for engines that don't fetch
  weights, and where that branch lives.
- **Conditioning-asset roles:** settle a small, shared vocabulary of `role` values
  (`init_image`, `first_frame`, `last_frame`, …). Validation and the request→segment asset flow are
  now decided (request assets are validated against `supported_modes` up front, then distributed
  into `Segment.assets`; the stitcher injects continuity assets there too) — confirm only the role
  names and that an undersupplied request (e.g. flf2v with one image) is a hard error (it should be).
  Keep the `kind` enum open for audio/video without building handling.
- **Segment-stream packaging & per-clip overrides:** confirm the `Segment.params`-over-`GenerationJob.params`
  merge order, and which knobs are inherently per-clip (SVI requires a distinct seed per clip — that
  belongs in `Segment.params`). Settle how the orchestrator packages a segment list into job(s):
  one N-segment job for native-extension engines vs. N single-segment jobs for the stitching path.
- **`GenerationJob.spec` leakage:** how much goes in engine-neutral `params` vs the
  engine-interpreted `spec`? Too much in `spec` and the abstraction leaks; too little and engines
  can't express what they need. Find the line.
- **Introspectable vs. declared — DECIDED, don't reopen:** `inspect_capabilities()` probes the
  readable fields (`max_frames`/`fps`/`resolution`/`supported_modes`); the behavioral strategy flags
  are **declared per model by the engine adapter** (`declared_flags`) and merged in by `discover`.
  Undeclared → `False` + a logged under-use warning (see the capability-discovery item). Implement this; brainstorming need
  only confirm the warning's wording/level and whether any engine wants to *infer* a flag from probed
  modes (e.g. `flf2v` ⇒ extension-capable) as a convenience on top.
- **Proactive serverless runaway bounds:** `budget` catches serverless overspend only reactively
  (after the fact). Decide proactive caps — e.g. max concurrent workers and a per-request timeout —
  so a stuck worker, retry storm, or unbounded autoscaling can't balloon before `budget` notices.
- **`ArtifactStore` retention / GC:** instances get reaped, but intermediate clips/frames/inputs
  accumulate forever and can fill disk. Decide a retention policy (TTL, per-run cleanup hook, or
  explicit `gc`) so stored media doesn't grow unbounded.
- **Discovery cost & cache location:** the one-time probe costs a real (minimal) launch for
  self-hosted engines. Decide where the cache lives (repo file, network volume, artifact store) so
  it survives across runs and machines, and confirm the probe runs in the cheapest mode available
  (scale-to-zero where the provider offers it) to keep it cheap. If the cache becomes a store shared
  across machines, decide how single-flight discovery coordinates cross-process (the store's atomic
  primitives / a lock) — in-process single-flight is enough for the first build.
- **Where the long-video strategy decision point lives** relative to the (deferred) orchestrator —
  keep it a pure, testable function with stubbed fallbacks so no premature pipeline structure
  leaks in now.

## Definition of done
Treat each item below as a behavioral acceptance criterion — write it as a failing test first
(per `CLAUDE.md`/Superpowers), then make it pass:
- `kinoforge deploy --config examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml --dry-run` prints a correct, vendor- and
  engine-neutral plan with no network calls.
- Against `LocalProvider` + `FakeEngine`, an end-to-end provision downloads models from HTTP/HF
  sources and produces a clip artifact — with no GPU or real model weights.
- Swapping `compute.provider` between `runpod` and `local`, swapping `engine.kind` between
  `comfyui` and `diffusers`, or adding a model from a different source scheme, each requires
  **only config edits** — no code changes.
- The `HostedAPIEngine` runs the generate path with **no compute instance provisioned**
  (`requires_compute = False`), proving the no-instance branch.
- **Capability discovery works**: selecting a different model changes the reported
  `max_segment_seconds` (e.g. Wan 2.1 ≈ 5 s) with no code change, and nothing downstream hardcodes
  a duration.
- **Plan-time profile access (cache hit)**: with the profile already cached,
  `profile_for(key)` / `ModelProfileProvider.resolve(key)` returns it with **no compute
  provisioned and no model loaded** — a test calls it with nothing spun up and reads
  `max_segment_seconds`, standing in for the pre-generation splitter.
- **Capability-key distinctness (#1)**: the same base model with vs. without a LoRA (e.g. base Wan
  vs. base Wan + SVI LoRA), and the same `CapabilityKey` base under two different engines, produce
  **distinct cache entries** — a test asserts they don't collide and that the SVI-LoRA variant can
  carry `supports_native_extension = True` while the bare base does not.
- **Self-healing discovery (cache miss)**: for an unknown `CapabilityKey`, `discover` calls the
  backend's `inspect_capabilities()` and merges `engine.declared_flags(key)` (both supplied by a
  `FakeEngine` in tests), persists the profile, and a
  subsequent `resolve` returns it — no hand-written entry, and a test asserts the whole
  splitter→generate path then succeeds for a model that had no profile to begin with.
- **Single-flight discovery (#6)**: two concurrent `resolve()` calls for the same uncached
  `CapabilityKey` trigger exactly ONE `discover()` / `inspect_capabilities()`, and both callers
  receive the same persisted profile (no double-probe, no write race).
- **Fail-hard on contradiction**: a test where `verify(profile, backend)` finds the live model
  disagreeing with the cached profile asserts the run raises/aborts **and** the `ComputeProvider`
  teardown (destroy pod / stop serverless worker) is invoked — no silent continuation.
- **`find_offers` filters correctly**: given a synthetic offer list (a fake provider in tests), it
  excludes offers below `min_vram_gb` or below `min_cuda`; excludes pod-mode offers above
  `max_usd_per_hr` but does NOT exclude serverless offers on that field; and preserves
  `gpu_preference` order among the offers that survive. Defaults (`48 GB / "12.8" / 2.20`) take
  effect when unspecified.
- **Mode + role-authoritative input validation (#4/#5)**: a `GenerationRequest` carries an EXPLICIT
  `mode`; a mode not in the model's `supported_modes` is rejected. Validation reads the required-role
  set from the shared `MODE_ROLE_REQUIREMENTS` table (not per-model), not by count: flf2v requires
  exactly one `image` `first_frame` and one `image`
  `last_frame` — two `init_image`s, a missing role, or a duplicated required role all raise. A lone
  image in a single-asset mode may default to `init_image`; multi-asset modes require explicit roles.
  And an engine that doesn't declare support for a `kind` (e.g. an image-only engine handed an
  `audio` asset) rejects it with a clear error rather than ignoring it.
- **Engine validates its own `spec` (#9)**: an engine handed a `GenerationJob.spec` with missing
  required keys or wrong-typed values raises a clear validation error via `validate_spec` and NEVER
  dispatches to the graph/pipeline/API.
- The long-video **strategy decision point** reads `supports_native_extension` /
  `supports_joint_audio` and selects the native-capability path; a test asserts both flag branches
  route correctly (the deferred fallback paths may be stubbed).
- **Segment stream packaging**: given an ordered list of segments and a `FakeEngine` declared
  native-extension, the orchestrator builds ONE `GenerationJob` carrying all segments and submits a
  single call; given a non-native engine, it builds one single-segment job per clip. A test asserts
  the packaging differs by flag while the segment data (per-clip prompt + assets + params) is
  preserved either way.
- **Engine-declared flags merge correctly**: a test shows a model the engine declares as
  extension/audio-capable ends up with those flags `True` in the cached profile and routes to the
  native path, while an *undeclared* model gets `False` (safe fallback) **and emits the under-use
  warning** — proving capable models aren't under-used and missing declarations are loud, not silent.
- A `GenerationJob` submitted through a `GenerationBackend` returns an `Artifact` via the
  `ArtifactStore` — i.e. the single-clip pipeline stage runs end-to-end.
- A `SequentialPool` runs a list of jobs through the same `submit`/`map` contract a concurrent
  pool would expose, with a test asserting that swapping in a different pool implementation needs
  no caller changes.
- **Cost-safety guardrails (tested against `LocalProvider`, clock mocked):** (a) a pod is reaped
  after `idle_timeout` of no jobs, but a job arriving inside the window reuses the same instance (no
  new create_instance) — proving warm reuse; (b) **graceful drain**: at `max_lifetime` the instance
  stops accepting NEW jobs but the in-flight job is allowed to finish (or hit its effective deadline),
  then it tears down — it is NEVER killed mid-job; (c) **hung-job detection scales with job size**: a
  job exceeding its effective deadline is aborted and torn down, AND a test asserts an N-segment
  native-extension job is given ≈ `N × job_timeout` (not a flat `job_timeout`), so a legitimately long
  native stream is not aborted for its length; (d) **in-flight liveness**: a single job running longer
  than ~2× the idle window does NOT trip the dead-man's switch (an in-flight job under its effective
  deadline is liveness), while a pod idle past the window with no `heartbeat()` DOES self-terminate; (e)
  `destroy_instance` polls to confirm termination and retries+alerts if the instance is still
  present; (f) the sweeper, given a list including an orphaned/over-age instance, destroys it without
  the orchestrator running; (g) when estimated cumulative spend crosses `budget`, the instance is
  killed; (h) config validation rejects nonsensical combinations (`idle_timeout >= max_lifetime`,
  `job_timeout > max_lifetime`); (i) defaults are `idle_timeout = 2h`, `job_timeout = 30m`,
  `max_lifetime = 5h` unless overridden.
- Lint, types, and the full test suite pass in CI on all three OSes.
- README documents quickstart + how to add a new provider / source / engine.

Hand this spec to Superpowers as the starting brief: let it brainstorm/refine and produce its
implementation plan before any code, raising genuine ambiguities with me during that phase. Then
proceed under its red/green TDD loop, treating the Definition of done as the acceptance tests.
