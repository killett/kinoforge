# Standalone image generation: `kinoforge image`

**Status:** design approved 2026-10-03, not yet planned.
**Scope:** a terminal image-generation command. One prompt in, one PNG out, no compute.
Reaches the four already-registered image engines (`fake`, `fal`, `luma_agents`,
`replicate`) directly instead of only as the head of a video pipeline.
**Non-scope:** still-image *upscaling* (§13.1), user-supplied *input* images (§13.2),
multi-image matrices (§2.3), params normalisation across providers (§8), and live-firing
the Replicate image engine (§13.3). Each is a separate decision with its own spec.

## 1. The question, and the answer

*kinoforge has four image engines, an image-profile cache, an image sink schema and two
live-proven image providers. Why can it not generate an image?*

Because nothing terminates at one. The only route into an `ImageEngine` is
`cfg.keyframe` -> `KeyframeStage`, which exists to fill *missing image-kind conditioning
roles* for a video mode (`pipeline/keyframe.py:72` reads
`MODE_ROLE_REQUIREMENTS[request.mode]`). The table has no entry that yields an image as
the product:

| mode | image roles required |
|---|---|
| `t2v` | none |
| `t2va` | none |
| `i2v` | `init_image` |
| `flf2v` | `first_frame`, `last_frame` |

`t2i` appears in exactly six places in `src/`, all of them inside engine
`supported_modes` literals (`image_engines/fal:271`, `luma_agents:48`, `replicate:39`,
`fake:94`, plus two backend copies). It is not a `MODE_ROLE_REQUIREMENTS` key, not a CLI
mode, and not routed anywhere. The two `t2i` entries in `successful-generations.md`
(§15, §18) were produced by calling `registry.get_image_engine(...)` from a live test and
a scratch matrix script — not through any shipped command.

**The answer is a new command, a new config block, and a ~60-line orchestration function.
Every other piece already exists and is live-proven.**

## 2. Surface: a new command, not a new mode

```
kinoforge image -c <cfg> [--prompt <text>]
                [--output-dir PATH | --no-output-dir]
                [--run-id ID] [--dry-run]
```

### 2.1 Why not `generate --mode t2i`

Three reasons, in descending order of weight.

1. **`generate` is contractually video.** `Config.engine` and `Config.models` are
   required (`core/config.py:1479-1480`), `models: []` raises unless `upscale_only`
   (`config.py:1589`), and `core/validation.py:48` checks the requested mode against the
   **video** `ModelProfile.supported_modes`. A `t2i` mode would force every video engine
   to declare `t2i` in order to pass a check that has nothing to do with it.
2. **`MODE_ROLE_REQUIREMENTS["t2i"] = {}` would be indistinguishable from `t2v`** to the
   role contract — the table's whole job is describing what *conditioning* a video mode
   needs, and an image needs none because it is not conditioning anything.
3. **Precedent.** `upscale` and `interpolate` are already separate subcommands rather
   than `generate --mode upscale`, for this exact reason. `image` is the fourth member of
   that family.

### 2.2 Flags deliberately absent

No `--mode` (always t2i). None of `--no-reuse`, `--attach-pod`, `--instance-id`,
`--force-attach`: every image engine declares `requires_compute = False`, so there is no
pod to reuse, no ledger row, no lifecycle budget and no heartbeat. `--dry-run` renders
the resolved plan and exits 0 without an HTTP call, matching `upscale`/`interpolate`.

`image` MUST be added to the `_propagate_session_globals` node list (`cli/_main.py:349`).
A subcommand that re-declares a session global rather than inheriting it is the U27/U29
failure shape: `kinoforge --ephemeral grid ...` ran non-ephemerally while reporting
success.

### 2.3 One image per invocation

Deliberate. `batch` exists for video to amortise pod boot across rows; with
`requires_compute = False` on every image engine there is no boot to amortise, so a shell
loop costs exactly what a batched call would:

```bash
for p in examples/configs/prompts/*.txt; do
  pixi run -e live-hosted kinoforge image \
    -c examples/configs/luma-uni1-t2i.yaml \
    --prompt "$(cat "$p")"
done
```

This keeps the command's contract trivial — one prompt, one PNG, one exit code — and
sidesteps the question of what `--count 4` should exit with when draw 3 of 4 fails.
Batch integration for N prompts x M models (the §18 matrix shape) stays available as a
later decision.

### 2.4 Exit codes

Follows the established convention: **2** for a config or precondition error (allowlist
violation, no prompt resolvable, `--ephemeral` refused, unknown image engine), **1** for an
operational failure (submit rejected, poll timeout, artifact fetch failed, cancelled),
**0** on success and on `--dry-run`.

## 3. Config: `ImageConfig` is the base `KeyframeConfig` extends

```yaml
mode: t2i                  # optional; if present MUST be "t2i" (§4)
prompt: "..."              # optional default

image:
  engine: luma_agents      # image-engine registry name
  prompt: "..."            # optional
  spec:
    model: "uni-1"
  params:
    aspect_ratio: "16:9"

output: {dir: output}
store: {kind: local}
```

### 3.1 The inheritance direction

`KeyframeConfig` (`config.py:1222`) is today `engine`, `prompt`, `spec`, `params`,
`roles`, plus `capability_key()`. An image block is that minus `roles`. The honest
factoring is therefore **not** two siblings but one base and one extension — a keyframe
spec *is* an image spec plus per-role overrides:

```python
class ImageConfig(BaseModel):
    engine: str
    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    def capability_key(self) -> CapabilityKey: ...   # moves here from KeyframeConfig

class KeyframeConfig(ImageConfig):
    roles: dict[str, KeyframeRoleOverride] = Field(default_factory=dict)
    # _at_least_one_prompt + _role_names_known stay HERE, not on the base
```

This deletes the second copy of `capability_key()` rather than adding one, and gives
`resolve_image_stack` (§6) a real nominal parameter type instead of a `Protocol` over
duck-typed attributes.

**The prompt validators must stay on the subclass.** `KeyframeConfig._at_least_one_prompt`
(`config.py:1241`) permits `prompt=None` when a role supplies one; `ImageConfig` permits
`prompt=None` because `--prompt` may supply it at runtime. A base-level "prompt required"
validator would break both. Prompt-presence for images is a preflight check (§3.2), not a
load-time one, because load time cannot see argv.

### 3.2 Prompt precedence: CLI wins

```
--prompt  >  cfg.image.prompt  >  cfg.prompt  >  refuse
```

`--prompt` is **optional**, so a config is self-contained and runnable bare. CLI-over-config
is the established direction in this CLI: `upscale --scale` "overrides cfg.upscale.scale"
and `interpolate --fps` "overrides cfg.interpolate.fps" (`cli/_main.py`, both help
strings). All-absent is refused at preflight with a message naming both fixes.

Note the keyframe path keeps its own precedence unchanged: `keyframe.prompt` beating
`cfg.prompt` is correct there, because a keyframe prompt deliberately describes the still
while the top-level prompt describes the motion (see `fal-keyframe-i2v.yaml`: "a cat
walking through a sunlit meadow, soft motion" vs "photorealistic cat in a sunlit meadow,
shot on 35mm film").

## 4. The allowlist, and why it is not a denylist

`Config.engine` becomes `EngineConfig | None = None` and `Config.models` becomes
`list[ModelEntry] = []`. A `model_validator` then branches so that **every existing
config takes the existing path, byte for byte**:

- `image:` absent -> today's rules fire verbatim (engine required, `models` non-empty
  unless `upscale_only`).
- `image:` present -> those requirements lift, and the config may carry **only**
  `mode`, `prompt`, `image`, `store`, `output`.

Everything else is refused with the offending key named: `engine`, `models`, `compute`,
`loras`, `keyframe`, `upscale`, `interpolate`, `splitter`, top-level `spec`, top-level
`params`, `lifecycle`.

### 4.1 Why an allowlist

Two reasons, and the first is a defect class this repo has already filed twice.

**Silently-inert config is a bug here, not a convenience.** U51: `lifecycle.budget` is
inert on RunPod and "reads like a dollar guard that is not one". U56: a hosted-engine
config carrying `loras:` "still generates LoRA-less". Both are in the URGENT ACTION ITEMS
history for exactly the shape where a key is accepted and ignored. An image config
carrying `loras:` would be U56 reproduced on purpose; one carrying `lifecycle:` would be
U51's shape. `mode: t2i` as "documentary only" — which an earlier draft of this design
proposed — is the same mistake, so `mode` is now *validated* (must be `t2i` when present)
rather than decorative. A typo'd `mode: t2v` on an image config is caught at load.

**A denylist rots; an allowlist cannot.** `Config` will grow blocks. A denylist admits
every future one silently; an allowlist refuses it until someone decides it belongs.

### 4.2 The cost this imposes

`image:` is mutually exclusive with `engine:`, so no single config serves both
`generate` and `image`. This is a real restriction, accepted deliberately: it buys a
crisp contract in which `kinoforge generate` can never silently ignore an `image:` block.

### 4.3 The audit this forces

Making a required field optional turns every dereference into a potential
`AttributeError`. The surface is **22 `cfg.engine` sites across 12 files**:

| file | sites |
|---|---|
| `core/config.py` | 6 |
| `validation/checks/custom_nodes.py` | 4 |
| `validation/checks/loras.py` | 2 |
| `core/orchestrator.py` | 2 |
| `validation/checks/models.py` | 1 |
| `engines/{hosted,comfyui,bedrock_video}/__init__.py` | 1 each |
| `core/vault.py`, `core/lora_apply.py` | 1 each |
| `cli/_main.py`, `cli/_commands.py` | 1 each |

Seven of those sit in `validation/checks/*`, which is why §12.2 is an open question and
not an assumption. `cli/_main.py:270` is already written defensively
(`cfg.engine.kind if cfg.engine else ""`), which is suggestive but not proof that the
rest are.

## 5. Orchestration: a plain function, no pipeline

New `core/image_run.py`:

```python
def generate_image(
    cfg: Config, *, store: ArtifactStore, run_id: str, sink: OutputSink | None,
    namespace: str | None = None, image_engine: ImageEngine | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
    cancel_token: CancelToken | None = None,
) -> Artifact
```

No `PipelineState`, no `GenerationRequest`, no `deploy_session`, no ledger, no heartbeat,
no reaper. All of it is dead weight against `requires_compute = False`.

Concretely this also avoids replicating the dummy at `orchestrator.py:2792`:

```python
effective_request = GenerationRequest(prompt="", mode="upscale")
```

a cast-hack the surrounding comment already apologises for. `PipelineState` exists to
*chain* video stages; a terminal image has nothing to chain, so it needs no state object
to chain through. Image-then-upscale (§13.1) would be file hand-off between two commands,
exactly like the §30/§32 video chains — it needs nothing from this function.

The `image_engine` / `image_profile_provider` parameters are test-injection seams,
mirroring the ones `orchestrator.generate` and `batch` already expose.

## 6. `resolve_image_stack`: three call sites, two copies deleted

`orchestrator.py:2726-2751` and `batch.py:649-672` are the same ~20 lines — resolve engine
from registry or injection, `provision(None, cfg_dict)`, `backend(None, cfg_dict)`,
`capability_key()`, then `resolve` falling back to `discover` on `ProfileNotCached`.
They differ only in local variable names. `generate_image` needs a third.

```python
# core/image_stack.py
def resolve_image_stack(
    block: ImageConfig, *, store: ArtifactStore,
    image_engine: ImageEngine | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
) -> tuple[ImageEngine, ImageBackend, ImageProfile]
```

`block: ImageConfig` is nominal, not structural, because of §3.1 — `KeyframeConfig` is an
`ImageConfig`, so both existing call sites pass their block unchanged.

**Placement matters.** This does **not** live in `core/image_run.py`: that would make the
video path (`orchestrator`, `batch`) import the standalone-image command's module. A
neutral `core/image_stack.py` keeps the dependency arrows pointing at a shared seam
rather than at a sibling feature.

Net effect on duplication: three call sites, one implementation, two existing copies
removed.

## 7. Cancellation: an ABC widening

`ImageBackend.submit` and `result` take no cancel token
(`core/interfaces.py:852`, `:855`):

```python
@abstractmethod
def submit(self, job: ImageJob) -> str: ...
@abstractmethod
def result(self, job_id: str) -> Artifact: ...
```

So a Luma poll — measured at ~125 s in §15 — ignores the two-press SIGINT handler the CLI
installs. The machinery underneath is already there and unreachable:
`RemoteSubmitPollBackend`, which the `luma_agents` and `replicate` inner backends extend,
checks the token at the top of every iteration and uses `cancel_token.wait` in place of
`time.sleep` (`core/remote_backend.py:247-249`). `FalImageBackend` is hand-rolled
(`for _ in range(self.max_polls)`) and supports none.

This barely mattered for keyframes: a ~5 s fal call inside a longer run that had other
interruption points. It is the *entire* command here.

**Fix:** widen to `result(self, job_id: str, *, cancel_token: CancelToken | None = None)`.
The default keeps `KeyframeStage` and all four engines source-compatible; `luma_agents`
and `replicate` inherit working cancellation by passing it through to their inner backend;
`fal`'s loop learns `cancel_token.wait`. Then pass the token from `KeyframeStage` too, so
the existing path improves rather than standing still.

`submit` is left alone: it is a single POST, and `RemoteSubmitPollBackend.submit` already
does one `raise_if_set` check before it (`remote_backend.py:205`).

## 8. The `ImageProfile` gets a consumer

Today it has none. `pipeline/keyframe.py:48` reads:

```python
image_profile: ImageProfile  # reserved for future spec validation
```

and nothing ever reads the field. Every engine's `validate_spec` checks only `spec.model`
and a non-empty prompt (`image_engines/fal:274`, `luma_agents:308`, `replicate:233`,
`fake:143`). So `resolve_image_stack` would resolve, cache and discover data with no
consumer — ceremony that costs a `JsonImageProfileCache` write per new
`(engine, model)` pair.

`generate_image` gives it one: **refuse when `"t2i" not in profile.supported_modes`,
before submitting.** This mirrors `core/validation.py:48` on the video side and is uniform
across all four engines.

**`max_resolution` stays ornamental, deliberately.** A generic width/height check is not
expressible: fal takes `image_size`, Luma takes `aspect_ratio`, and `params` is an opaque
pass-through merged straight into the provider body (`image_engines/fal/__init__.py`
`body.update(job.params)`). Normalising that is a params-translation layer this design does
not propose, so the field keeps no coverage it cannot honour. Said plainly here so a later
reader does not assume dimension validation exists.

## 9. Output: store, sink, filename

Mirrors `KeyframeStage` exactly.

**Store:** `store.put_bytes(run_id, "image.png", png_bytes)`. A fixed identifier, not
prompt-derived, so it carries the same pragma as `pipeline/keyframe.py:92`:
`# kinoforge:public-name`.

**Sink:** `kind="image"`, yielding
`{ts}_image_{provider}_{model}_{slug}.png` via `outputs/base.py:136`, e.g.

```
output/20261003-141522_image_luma_agents_uni-1_Photorealistic-cinem.png
```

`provider` is the image-engine registry name; `model` is
`engine.model_identity(cfg_dict)`.

**Collisions are already handled.** `LocalOutputSink.publish` routes through
`_resolve_collision` (`outputs/local.py:108`), so a shell loop landing two images in the
same clock second is safe. `_build_sink(cfg, args)` is likewise already a shared CLI
helper, so `_cmd_image` reuses it.

**The `model_identity` trap gets a test.** §17 recorded that fal's `model_identity` read
only `engine.fal.endpoint` and returned `""` for keyframe sub-configs carrying
`spec.model`, rendering `_fal_unknown_` into two filenames. It was fixed in that same
commit, and `ImageConfig` carries `spec.model` in the identical shape, so the fix covers
this path — but the bug was invisible until a live run, so §11 asserts a non-`"unknown"`
model slug offline.

`run_id` defaults to `image-{ts}`, matching the `run-` / `upscale-` / `interpolate-`
prefixes at `cli/_commands.py:955`, `:1120`, `:1237`. Those three are a 4-line copy of the
same derivation and `image-` would be a fourth; a shared `_resolve_run_id(args, prefix)`
is in scope only because this work already touches the neighbourhood.

## 10. `--ephemeral`: refused, deliberately

### 10.1 Today it would be refused by accident

`_preflight_ephemeral` reads `cfg.engine.kind if cfg.engine else ""`
(`cli/_main.py:270`). With `engine: None` that is `""`, the lookup key becomes
`("", None)`, `EPHEMERAL_CAPABILITIES.get(...)` misses, and the refusal block prints.
Right outcome, wrong mechanism: a key miss rather than a decision, and no test pins it.

### 10.2 The ground truth

| image engine | can scrub provider-side records? | evidence |
|---|---|---|
| `luma_agents` | **no** | §15: "No DELETE endpoint on the agents API; records purge via the dashboard (`manual_cleanup_url`)" |
| `fal` | no | `("fal", None): False` in `EPHEMERAL_CAPABILITIES`; no delete path |
| `replicate` | in principle | `("replicate", None): True` for video, but the *image* engine implements no scrub hook |
| `fake` | trivially | in-process; no provider-side state exists |

No image engine implements record deletion, and two of three cannot.

### 10.3 The decision

Refuse explicitly, via a **separate table** keyed on the image-engine name:

```python
# core/ephemeral.py
IMAGE_EPHEMERAL_CAPABILITIES: dict[str, bool] = {
    "fake": True,
    "fal": False,
    "luma_agents": False,
    "replicate": False,   # flip when a scrub hook exists AND is live-proven
}
```

with `_preflight_ephemeral` branching when `cfg.image is not None`.

**Separate, not new rows in `EPHEMERAL_CAPABILITIES` (`core/ephemeral.py:95`), because the
two registries are independent namespaces.** `registry.py:244` states that image-engine
names may legitimately collide with video-engine names — `fake` already does. Keying both
capability questions off one bare-name table would be a latent bug the moment `fal`-video
and `fal`-image diverge on scrub support.

### 10.4 Consequence for the generation log

Because `--ephemeral` is always refused, no `kinoforge image` run can be ephemeral, so
every one is loggable. CLAUDE.md lists "new kinoforge command" as a qualifying capability
axis, so the first live run **requires** a new `successful-generations.md` section — not a
"See also" under §15, despite sharing the `(luma_agents, uni-1, t2i)` tuple.

## 11. Testing

Offline work rides `FakeImageEngine`, already registered with 9 existing tests.

### 11.1 The assertions that carry weight

Listed explicitly because these are the places a weak test would pass while the bug ships.

| behaviour | assertion | bug it catches |
|---|---|---|
| `KeyframeConfig(ImageConfig)` re-parent | all 8 existing `test_keyframe_config.py` tests green untouched, plus `capability_key()` returns an identical `CapabilityKey` for a fixture before and after | the refactor silently changes live-proven keyframe behaviour or strands every cached image profile |
| mode gate fires before spend | fake backend's `submit` was **never called** | an engine that submits first and validates after — the weak version (assert-raises) passes |
| cancellation | fake backend counts polls; assert the count stops early when the token trips mid-poll | a loop that runs to completion and raises `Cancelled` at the end passes assert-raises |
| `--dry-run` | injected HTTP seam recorded **zero** calls | a dry run that resolves a profile by live-probing |
| allowlist | one case per forbidden key, each asserting the reason string names **that** key | a validator that refuses everything for the wrong reason |
| `model_identity` | published filename's model slug is not `"unknown"` | §17's `_fal_unknown_` class, invisible offline until asserted |
| ephemeral refusal | table-driven over all four engines; exit 2 + the block | §10.1 drifting back from decision to accident |
| session globals | `image` present in `_propagate_session_globals`' node list | U27/U29: a subcommand running non-ephemerally while reporting success |

Prompt precedence gets four cases (CLI wins, block wins over top-level, top-level alone,
all-absent refused). `resolve_image_stack` gets four (triple returned; unknown engine ->
`UnknownAdapter` before any backend construction; `ProfileNotCached` -> discover;
injection overrides registry). `generate_image` gets the happy path plus `sink=None`
(store-only, the `--no-output-dir` shape).

Per the `test-design` skill each test states its behaviour-under-test and a concrete bug
that would make it fail; the table above is the subset where assertion *strength*, not
coverage, is the risk.

### 11.2 Live fire

Exactly one run: `luma_agents`, prompt read verbatim from
`examples/configs/prompts/field-realistic.txt` (no paraphrase, no per-test override),
~$0.01-0.05 against the remaining Luma platform credit.

Visual QA is simpler than CLAUDE.md's video rule — read the PNG directly, no frame
extraction — but it is **not optional**: the verdict is recorded in the
`successful-generations.md` entry following §18's per-prompt verdict table. §18 is also
the reason this matters: its `uni-1` dawn-flight draw carried frame-wide out-of-focus
blobs that objective metrics (dims, latency, byte size) rated identical to a clean image.

## 12. Example configs and docs

Two configs, both validated on load by `tests/test_examples.py`:

- `examples/configs/luma-uni1-t2i.yaml` — quality; the live-fired one.
- `examples/configs/fal-flux-schnell-t2i.yaml` — ~5 s, ~$0.005; the iteration config.

No Replicate config: that engine is report-gap §13.3 and has never been live-fired, so
shipping an example implying otherwise would be dishonest.

Docs: `docs/configuration.md` (the `image:` block and the allowlist), `docs/engines.md`
(image engines as a terminal path, not only a keyframe head), `README.md` quick-usage.
`PROGRESS.md` gets the design-doc path, plan path, checklist and next action per the
durability rules.

### 12.1 Model choice: ships `uni-1`, with a documented trigger to flip

§18 ruled `uni-1-max` the safer default on quality (fewer destructive artifacts, better
subject clarity, +15% latency) and then deliberately left the keyframe config on `uni-1`
**only** because max-tier per-image pricing is not visible on the wire and needs a
dashboard check. That deferral is still open.

**Decision: the example config ships `uni-1`**, preserving §18's status quo, with a
comment pointing at the deferral. Flipping it is a one-line change once the dashboard
confirms the delta.

### 12.2 Open question the plan must resolve, not assume

**Does `kinoforge doctor` survive a config with `engine: null` and `compute: null`?**
Seven of the 22 dereference sites in §4.3 are in `validation/checks/*`, and
`validation/checks/capabilities.py` reasons about compute presence in ways this design has
not read closely. `tests/live/test_doctor_examples_live.py` walks the example configs, so
the new image configs land in its path immediately on landing.

The plan must verify this with a test before the configs ship, not reason about it. If the
checks do not no-op cleanly, the fix belongs in this work, because §12 ships the configs
that trip it.

## 13. Non-scope, and why each is separate

**13.1 Still-image upscaling.** All three registered upscalers are video-only.
`spandrel` is the near miss: it loads genuine image super-resolution models
(RealESRGAN et al.) but `upscalers/spandrel/_runtime.py:1` describes itself as a
"frame-loop video upscale wrapper" — `iio.imread(video_path, plugin="FFMPEG")`, batch
frames, re-encode to `<stem>.upscaled.mp4`. The model capability is already on the pod and
live-proven (§12, §34); only the I/O ends are video-shaped. Separate spec: it touches the
pod-side runtime and the `upscale` command, neither of which this design opens.

**13.2 User-supplied input images.** `cli/_commands.py:942` builds
`GenerationRequest(prompt=args.prompt, mode=args.mode)` — no assets, ever. There is no
`--init-image` flag and no config key for conditioning assets; §4 of the generation log
flagged this in June 2026 and it is still true. This blocks i2i and "animate the photo I
already have". Largest of the three, because it needs a genuine asset-supply seam rather
than a flag.

**13.3 Live-firing the Replicate image engine.** Coded, 7 offline tests, no example
config, never run against the API. ~$0.01 to close, and it pairs naturally with building
the scrub hook that would let §10.3 flip its table entry to `True`.

**13.4 Params normalisation.** See §8. Until `image_size` / `aspect_ratio` /
`width`+`height` have a common representation, `max_resolution` cannot be enforced and
cross-provider params cannot be validated.

## 14. Module-by-module summary

| file | change |
|---|---|
| `core/config.py` | `ImageConfig` (new base); `KeyframeConfig(ImageConfig)`; `Config.image`; `engine`/`models` optional; allowlist validator |
| `core/image_stack.py` | **new** — `resolve_image_stack()`; deletes the copies at `orchestrator.py:2726` and `batch.py:649` |
| `core/image_run.py` | **new** — `generate_image()` |
| `core/interfaces.py` | `ImageBackend.result(..., cancel_token=None)` — ABC widening |
| `core/ephemeral.py` | `IMAGE_EPHEMERAL_CAPABILITIES` |
| `image_engines/fal/__init__.py` | poll loop honours `cancel_token` |
| `pipeline/keyframe.py` | passes `cancel_token` to `result()` |
| `cli/_main.py` | `image` subcommand; `_propagate_session_globals` node list; `_preflight_ephemeral` image branch |
| `cli/_commands.py` | `_cmd_image()`; optional `_resolve_run_id` extraction |
| `examples/configs/` | two t2i configs |
| docs | `configuration.md`, `engines.md`, `README.md`, `PROGRESS.md` |

Three deletions, one ABC widening, two new modules, one new concern per touched file.
