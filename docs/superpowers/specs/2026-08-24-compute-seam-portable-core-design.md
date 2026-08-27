# Compute seam: a portable core with namespaced escape hatches

**Status:** validated design, 2026-08-24. Written from the build brief of the same date.
**Predecessors that must be in before this starts (both are):** the SkyPilot instance-side
deadline watchdog (Brief 1, shipped 2026-08-15) and provider capability declaration
(Brief 2, shipped 2026-08-17).
**Evidence base:** `docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md`,
findings F4, F5, F6, F11, F12 — all CONFIRMED against `67627cd0`.

---

## 1. The problem, stated as mechanisms

The compute seam was designed against RunPod. Every provider added since widened it instead of
narrowing it, and the widening is invisible at the point where an operator makes a mistake.

**A field set for the wrong provider does nothing, silently.** `ComputeConfig.cloud`
(`core/config.py:778-783`) is documented "Ignored by non-skypilot providers"; `cloud_type`
(`:794-800`) is documented "Ignored by non-runpod providers". `InstanceSpec` carries Modal-only
`image_build_script` / `runtime_provision_script`, RunPod-only `restart_policy`, and a
`Lifecycle.capacity_wait_s` (`core/interfaces.py:100-102`) whose docstring is written purely in
RunPod terms. F5 counted it: 10 of `InstanceSpec`'s 17 fields are ignored by at least one of the
three cloud providers, only `tags` is read by all four, and 3 of `ComputeConfig`'s 9 fields are
provider-specific. No registered validation check cross-references any field against
`cfg.compute.provider`, so `provider: runpod` with `cloud: ["lambda"]` validates clean and the
pin is discarded. The operator learns this from the invoice.

**The selection model belongs to one vendor.** `find_offers` → filter → book one is a marketplace
model, and it is how RunPod works. SkyPilot is a declarative placer: you state constraints, its
optimizer chooses. Handing it a pre-picked SKU cannot work, and did not — on 2026-07-07 a
`compute.cloud=["vast"]` config provisioned a Lambda A100 at $1.99, defeating both the cloud pin
and the price cap. The cloud pin was patched onto the launch (`providers/skypilot/__init__.py:826`
carries the comment); the price cap was not. `max_usd_per_hr` is still consumed in exactly one
place, `core/offers.py:38`, where it filters a *catalog* that the optimizer is free to ignore (F4).

**The reported rate is the catalog's, not the invoice's.** `create_instance` returns
`cost_rate_usd_per_hr=spec.offer.cost_rate_usd_per_hr` (skypilot `:847`, `:895`, `:976`; runpod
`:1110`; modal `:178`). For SkyPilot that is the rate kinoforge *guessed*, not the rate the
optimizer realized, and `orchestrator.py:1631-1640` deliberately declines to refresh it. Ledger
rows, `est_spend`, `kinoforge list`, the cost dashboard and every `lifecycle.budget` computation
inherit the wrong number.

**A cost-relevant knob is reachable only from a test.** `ComputeConfig` has no `region`
(F6). `SkyPilotProvider.__init__` accepts one and applies it at `:746-747`, but the only
config-driven construction path (`_adapters.py:117-129`) injects `_clouds` and nothing else, and
the registry factory takes zero arguments. `_retry_until_up` is dead from config in exactly the
same way. This contradicts a standing project rule — pin the region on every cloud, default
Oregon — that exists because `sky` was once observed picking `asia-southeast1`.

**One provider's PID-1 convention shapes every other provider's adapter.**
`RenderedProvision` documents (`core/interfaces.py:126-128`) that the script ends with
`exec <run_cmd>` so the run command becomes PID 1 — a RunPod single-`dockerArgs` detail promoted
to a cross-provider contract. SkyPilot must undo it before the script can be `Task.setup`, which
is what `_strip_trailing_exec` (`:310-334`) does, by substring-matching `" exec "` on the last
line; a script whose last line legitimately contains that substring loses it silently. Modal then
needed a third representation and got two more spec fields.

**Two endpoint shapes from one provider, with no discriminator.** `create_instance` returns
`{"8000": "http://127.0.0.1:<port>"}` off a hardcoded `_VIDEO_SERVER_PORT` (`:343`, `:786-804`)
while `endpoints()` returns `{"ssh": f"ssh://{instance.id}"}` (`:914-923`). Warm-attach
(`cli/_commands.py:2024-2026`) is wrong on both branches: without a ledger `endpoints` it hands
`ssh://…` to an HTTP client, and with one it replays a localhost port belonging to a tunnel
process that died with the previous CLI invocation (F11, F12).

**Remote state is process-local until the launch returns.** `self._tunnels` is in-memory
(`:565-566`), and every durable write is an `on_instance_created` callback after
`create_instance` returns (`orchestrator.py:864-877`, `:1189-1194`). Brief 1 closed this for
SkyPilot with a pre-launch provisional ledger row; RunPod and Modal still have the window (F12).

### The goal in one line

A narrow portable core that every provider honours, plus an explicit namespaced escape hatch that
the owning provider validates and that errors on unknown keys. **A field that is silently ignored
is a misconfiguration the operator discovers from the invoice.**

---

## 2. The portable core

**This block is the END STATE, after S4.** Each stage lands a subset; §11 carries the per-stage
delta. Two fields in particular arrive later than the block suggests: `region` lands in S2 (with
its `consumes()` declarations), and `cpus` / `memory_gb` land only when a stage needs them — no
config surface sets either today, and SkyPilot's `cpus: "1+" / memory: "2+"` for the CPU path is
currently hardcoded in the provider (`providers/skypilot/__init__.py:796-798`). Adding a field
nothing sets would put an undeclared row in the §4 guard for no gain.

```python
@dataclass(frozen=True)
class Placement:
    """What to get. Not which SKU to book."""
    accelerators: tuple[str, ...] = ()      # ordered preference; () = CPU-only
    accelerator_count: int = 1
    min_vram_gb: int | None = None          # "any GPU with >= N GB"
    cpus: int | None = None
    memory_gb: int | None = None            # host RAM
    disk_gb: int = 100                      # boot/container disk
    region: str | None = None
    spot: bool = False
    max_usd_per_hr: float | None = None


@dataclass(frozen=True)
class SetupStep:
    """One provisioning step, with the only property a provider needs to route it."""
    script: str
    bakeable: bool = False   # safe to run at image-build time


@dataclass
class InstanceSpec:
    image: str
    placement: Placement
    setup_steps: tuple[SetupStep, ...]
    run_command: tuple[str, ...]
    ports: tuple[str, ...]
    volume_gb: int = 0
    volume_mount: str = ""
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    # volume_gb/volume_mount are the ATTACHED PERSISTENT volume; the boot disk
    # is placement.disk_gb. Two different things, kept apart on purpose.
    env: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    run_id: str = ""
    backend_options: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
```

Deleted from `InstanceSpec`: `offer`, `spot` (moves into `Placement`), `cloud_type`,
`restart_policy`, `provision_script`, `image_build_script`, `runtime_provision_script`,
`diagnostic_env`.

`diagnostic_env` is not vendor-specific — it is an env overlay with `setdefault` semantics. The
orchestrator merges it into `env` while building the spec (`orchestrator.py:897-899` becomes a
merge rather than a field), preserving "user-supplied `env` always wins" and removing a field.

`Lifecycle` keeps only what every provider can honour. `capacity_wait_s` moves to the RunPod
namespace, because it names a RunPod behaviour (retry a create that missed on capacity) and
because after §5 the retry loop lives inside RunPod anyway.

---

## 3. Namespaced extras

```yaml
compute:
  provider: skypilot
  backend_options:
    runpod:   {cloud_type: secure, restart_policy: never, capacity_wait_s: 300}
    skypilot: {clouds: [lambda], retry_until_up: true}
```

Each provider class owns its schema:

```python
class SkyPilotProvider(ComputeProvider):
    class Options(BaseModel, extra="forbid"):
        clouds: list[str] | None = None
        retry_until_up: bool = False

    @classmethod
    def validate_options(cls, raw: Mapping[str, object]) -> "SkyPilotProvider.Options":
        ...
```

Rules, applied at config load:

1. **Unknown key inside a namespace is an error.** `extra="forbid"` on the provider-owned model,
   surfaced as `ConfigError` naming the provider, the key, and the accepted keys.
2. **Unknown provider name as a namespace key is an error.** A typo'd `runpid:` block must not
   read as "options for a provider I have not selected".
3. **Every namespace is shape-validated, not just the selected one.** Cheap, and it catches the
   typo in the block you are not currently using.
4. **A namespace belonging to a registered-but-unselected provider raises a `doctor` WARN**
   naming it. This is the deliberate middle path: a multi-provider YAML stays legal (the operator
   plainly meant "these are the RunPod settings, for when I run on RunPod"), but nothing is
   discarded in silence. The distinction from today's F5 behaviour is that the namespace makes
   the intent explicit and the WARN makes the non-application visible.

---

## 4. `consumes()` — the regression guard

Brief 2 established the discipline: a provider declares what it can enforce, the default
declaration is empty, and an undeclared provider is refused loudly rather than trusted silently.
This is the same discipline on a different axis — not "which guardrails can you enforce" but
"which core fields do you actually read".

```python
class FieldSupport(StrEnum):
    CONSUMED = "consumed"        # read and applied on the wire
    UNSUPPORTED = "unsupported"  # cannot be honoured; setting it is an error

class ComputeProvider(ABC):
    @classmethod
    def consumes(cls) -> Mapping[str, FieldSupport]:
        """Declare, per portable-core field, whether this provider honours it."""
        return {}
```

Consequences:

- **Load-time validation.** Setting an `UNSUPPORTED` field to a non-default value is an ERROR
  naming the provider and the field, with the `backend_options` path if an equivalent exists
  there. This is the check that F5 found missing from the whole registered inventory.
- **The guard test.** A test enumerates every field of `Placement` plus the placement-relevant
  fields of `InstanceSpec` (`ports`, `volume_gb`, `volume_mount`, `setup_steps`, `run_command`,
  `env`, `tags`, `image`, `run_id`), crossed with every registered provider, and fails when any
  pair is absent from a declaration. Adding a field without deciding what each provider does with it breaks the
  suite. Every `CONSUMED` claim is separately wire-verified against a fake transport, so a
  declaration that lies also fails. **This is the regression guard that keeps the union from
  re-forming**, and it is the single most important artifact in this design.
- Keyed by field name string, deliberately: the guard test asserts key-set equality against the
  dataclass fields, so a rename that misses a declaration fails rather than silently defaulting.

`capabilities()` and `consumes()` stay separate. Merging them would conflate "I can enforce an
idle timeout" with "I read `placement.region`", and Brief 2's severity model (spend-risk rows,
substitute-naming WARNs) does not apply to field consumption.

---

## 5. Inverting the selection model

**This is the big call. The argument, explicitly.**

`find_offers` / `Offer` describes a marketplace: enumerate what is bookable, filter it, book one.
RunPod is a marketplace and this is a faithful model of it. SkyPilot is not: its optimizer takes
constraints and picks. The consequence is asymmetric, and that asymmetry is the whole argument:

> A marketplace provider can implement a declarative interface — it enumerates internally and
> picks. A declarative placer cannot be made to honour an externally chosen SKU; it can only be
> narrowed until the choice is nearly forced, and "nearly" is what produced a Lambda A100 at
> $1.99 under a `vast` pin.

So the portable interface becomes declarative, and marketplace enumeration becomes an
implementation detail of the providers that have a marketplace.

```python
def create_instance(self, spec: InstanceSpec) -> Instance:
    """Select, launch, and verify. Selection is the provider's business."""
```

`find_offers` leaves the `ComputeProvider` ABC.

### What breaks, named

1. **`_create_with_offer_retry` (`orchestrator.py:505-549`) moves inside RunPod.** It iterates a
   catalog on `CapacityError`, which is meaningless where there is no catalog. RunPod keeps the
   behaviour verbatim — first-offer-first, `gpu_preference` order, non-capacity errors propagate
   immediately — as a private method. SkyPilot's equivalent is `retry_until_up`, which is a
   `backend_options` key, not an orchestrator loop.
2. **`_create_with_capacity_wait` (`:556-604`) stays, with a different inner call.** The
   re-query-and-retry-until-deadline wrapper is still the right shape; it wraps
   `provider.create_instance(spec)` instead of `create(find_offers())`. `capacity_wait_s` reaches
   it from the RunPod namespace; on a provider that does not declare it, the wrapper is a
   pass-through.
3. **`kinoforge offers` (`cli/_commands.py:289`) becomes capability-gated.** A new
   `Capability.CATALOG_ENUMERATION`, declared by RunPod, Modal and Local. On SkyPilot the command
   reports that the provider does not enumerate and points at `sky show-gpus`. Keeping a fake
   catalog for a provider that does not have one is how the current lie started.
4. **`Offer` survives as a RunPod/Modal-internal type**, not a seam type. `core/offers.py`'s
   `filter_offers` keeps its `min_vram_gb` / `min_cuda` / `gpu_preference` / `max_usd_per_hr`
   logic and is called by the providers that enumerate.
5. **`Instance.cost_rate_usd_per_hr` loses its source** and gets a better one — see §6. This is
   the point where the F4 under-reporting actually dies.
6. **`HardwareRequirements` folds into `Placement`, field by field.** `gpu_preference` becomes
   `accelerators` (an ordered preference list, same semantics). `disk_gb` carries over unchanged.
   `min_vram_gb` survives as a portable field because "any GPU with at least N GB" is a real
   intent that an accelerator list cannot express — but it is `CONSUMED` only where a catalog is
   enumerated (runpod, modal) and `UNSUPPORTED` on skypilot, whose optimizer takes named
   accelerators rather than a VRAM floor. Setting it on a skypilot config is then an error
   telling the operator to name accelerators, instead of a filter that quietly does nothing to
   the launch. `min_cuda` stays portable on `Placement`. (**Corrected 2026-08-26**, during S1 Task 4: this
   section originally routed it to the RunPod namespace on the grounds that neither SkyPilot nor
   Modal exposes a CUDA constraint at selection time. True of those APIs, false of kinoforge —
   `core/offers.py::filter_offers` applies `min_cuda` to whatever catalog any enumerating provider
   returns, and SkyPilot's offers carry `cuda="12.0"`, so a `"12.8"` default outside their reach
   empties the catalog. It dies with the catalog-filter path in S4.) `max_usd_per_hr` stops being
   a catalog filter and becomes the verified cap of §6.
7. **Modal's `create_instance` currently raises without `spec.offer` (`:122-123`).** It gains a
   `Placement`→`gpu=` mapping over its hardcoded catalog (`providers/modal/_catalog.py`), which
   is genuinely declarative already: you name a GPU class and Modal attaches exactly that.

### What this buys

The rate cap, the cloud pin and the region become *constraints stated to the thing that chooses*,
rather than filters applied to a list the chooser never sees.

---

## 6. Rate-cap enforcement, in two parts

**Part 1 — narrow what the optimizer may choose.** `accelerators`, `accelerator_count`, `region`,
`spot`, and the `clouds` namespace key all travel into the launch request. This is necessary and
insufficient: narrowing is not a price ceiling.

**Part 2 — read back the realized instance and verify.**

```python
class ComputeProvider(ABC):
    def realized_rate(self, instance: Instance) -> float | None:
        """The rate this instance will actually bill at, read after launch."""
        return None
```

Per provider:

- **SkyPilot** — `handle.launched_resources.get_cost(3600)` (verified present at the pinned
  `skypilot-0.12.3.post1`: `sky/resources.py:1704`). The optimizer chose; only a readback knows.
- **RunPod** — the created pod's `costPerHr`, which `core/lifecycle.py:402-406` already refreshes
  on this provider alone.
- **Modal** — the requested GPU class *is* the billed GPU class, so the published per-class price
  is authoritative.

Two new capabilities capture that split:

| Capability | Declared by | Meaning |
|---|---|---|
| `RATE_READBACK` | skypilot | The provider chooses; the rate must be read from the launched handle. |
| `RATE_DETERMINISTIC` | runpod, modal | The requested SKU is the billed SKU; the catalog price is the rate. |

The check runs after `create_instance` returns and **before `engine.provision`**, so a violation
is torn down before the expensive part of a boot:

```
realized > cap                          -> destroy_instance + RateCapExceeded
realized unreadable, RATE_READBACK      -> destroy_instance + RateCapExceeded(realized=<unreadable>)
realized unreadable, RATE_DETERMINISTIC -> unreachable; the catalog is the rate
neither capability declared             -> config-validation ERROR before launch
```

`RateCapExceeded` names both numbers and the identity of what was launched:

```
RateCapExceeded: realized $1.9900/hr exceeds cap $1.0900/hr
  (sku=A100:1, cloud=lambda, region=us-west-2, instance=kinoforge-xyz)
  instance destroyed
```

Naming both is the requirement: a message that says only "over budget" leaves the operator unable
to tell a bad cap from a bad placement. `Instance.cost_rate_usd_per_hr` is populated from the
realized read on every provider, which is what makes the ledger, `est_spend`, `kinoforge list`
and every `lifecycle.budget` computation honest.

Teardown failure is not swallowed: if `destroy_instance` also fails, the raised error carries both
the cap violation and the teardown failure, and the provisional ledger row from §9 stays behind
with the instance id so the reaper can finish the job.

---

## 7. Setup / run split, and the death of `_strip_trailing_exec`

The engine emits `(setup_steps, run_command)`. Each provider maps it:

| Provider | `setup_steps` | `run_command` |
|---|---|---|
| runpod | all steps concatenated into the `dockerArgs` script | appended by the provider as a trailing `exec <run_command>` |
| skypilot | concatenated into `Task.setup` | `Task.run` |
| modal | `bakeable` steps baked into the image at build; the rest run at container start | the web-server command |

The PID-1 convention becomes what it always was — a RunPod deployment detail — composed by the
provider that needs it rather than by the engine, and undone by nobody.
`_strip_trailing_exec` is deleted, along with the substring heuristic that could eat a legitimate
last line.

`SetupStep.bakeable` replaces `image_build_script` / `runtime_provision_script`. It is portable
because it states a property of the step ("safe at image-build time") rather than naming a
provider's pipeline stage. Providers that provision at runtime ignore the flag by construction —
which is a real "ignores it" and therefore declared as such in `consumes()`, not left implicit.

Engines produce the steps; `RenderedProvision.script` / `build_script` / `runtime_script` are
replaced by `setup_steps`. The byte-identity invariant that `build_script + runtime_script ==
script` is preserved as an ordering invariant: concatenating all steps in declaration order
reproduces today's combined script, and the golden payload snapshot (§10) proves it.

---

## 8. Region, and consume-or-refuse

`region` is a first-class `Placement` field, reaching providers through
`_adapters.build_provider_for`.

At S2, `region` is `CONSUMED` on skypilot and `UNSUPPORTED` on runpod and modal. Both could take
one — RunPod's create mutation accepts a data-centre id, Modal's function decorator accepts
`region=` — but neither is wired today and neither has live proof. Declaring `UNSUPPORTED` makes
setting it an error naming the provider, which is honest; quietly accepting it and dropping it on
the floor is the exact failure this brief exists to end. Wiring either is a follow-up with its own
live smoke, and the `consumes()` declaration is the one-line change that lands it.

`_retry_until_up` reaches the provider as a `skypilot` namespace key, closing the second half of
F6.

---

## 9. One endpoint shape, one durable-write ordering

**Endpoints.** Every provider returns a port-keyed map of absolute URLs, with the keys derived
from `spec.ports`:

```python
{"8000": "http://127.0.0.1:53411", "8001": "http://127.0.0.1:53412"}
```

`_VIDEO_SERVER_PORT` and the `{"ssh": …}` shape are both deleted. `kinoforge status`, which today
overwrites a live tunnel endpoint with an ssh string (`cli/_commands.py:1773`), prints the cluster
name instead.

SkyPilot's `endpoints()` becomes tunnel-ensuring: if the tunnel subprocess for this cluster is
absent or dead, re-establish it and return the fresh local port. That is the actual fix for F11's
warm-attach hazard — the ledger branch currently replays a dead port and the fallback hands an
`ssh://` URL to an HTTP client, and *both* are cured by "ask the provider for a live endpoint"
rather than by replaying a recorded one. Ledger `endpoints` remain recorded (Modal's
non-rebuildable `.modal.run` URL still needs them, per `1cb4299`), but they are now a hint that a
provider may override, not a value the engine trusts blind.

**Ledger before create.** Brief 1's pre-launch provisional row generalises from the SkyPilot
provider into one orchestrator-level writer covering all providers: before `create_instance`, a
row keyed by a client-side id carrying `kf_launch_phase=launching`, reconciled to the real
instance id when create returns and removed if create raises. A process death during the
multi-minute launch then leaves a row the reaper can act on, on every provider, instead of a
billing resource no kinoforge command can see.

---

## 10. Testing

Four things must be true, and each has a test that fails when it stops being true.

1. **Unknown key in a namespace raises.** Per provider, with the error naming the key and the
   accepted set.
2. **No field is silently ignored.** The `consumes()` guard of §4: every portable field × every
   registered provider must be declared, and every `CONSUMED` declaration wire-verified against a
   fake transport. This is the test that fails when someone adds a field and forgets a provider.
3. **Round-trip equivalence.** Before any code changes, snapshot the create payload that each of
   the 39 `examples/configs/*.yaml` produces today, rendered through the existing provider fakes,
   into `tests/providers/golden/launch_payloads/`. Every stage then proves the migrated config
   produces an equivalent payload rather than asserting it. Divergences that are *intended* (the
   trailing `exec` composed by RunPod instead of the engine, the realized rate replacing the
   catalog rate) are recorded as explicit golden updates in the stage that causes them, with the
   diff reviewed — never regenerated wholesale.
4. **Rate-cap verification.** A fake returns an over-cap instance; assert `destroy_instance` was
   called and the raised error names both the realized rate and the cap. A second fake returns
   `None` from `realized_rate` while declaring `RATE_READBACK`; assert the same teardown. A third
   declares `RATE_DETERMINISTIC` and is never asked.

Per stage: a live smoke on the cheapest CPU SKU — same config, same behaviour before and after.
`pixi run preflight` first, RED scaffold committed before any live spend, `--no-reuse`, and
`kinoforge list` verified clean after the orchestrator exits.

---

## 11. Staging

Each stage is independently shippable with green tests. No stage lands half-migrated: where both
paths would otherwise be alive at once, the stage gates on config and deletes the old path in the
same stage.

**S0 — snapshot (part of S1's first task).** Golden launch payloads for all 39 example configs.
Nothing else. This is the ratchet everything after it is measured against.

**S1 — portable core + `backend_options`.** `Placement`, the reduced `InstanceSpec`,
`backend_options` with per-provider `Options` models, `consumes()` and its guard test.
`compute.cloud` and `compute.cloud_type` become load errors naming the new path; the 8 example
configs that actually set either key (3 skypilot `cloud`, 5 runpod `cloud_type`, counted by an
`rg` on the key at line start — several more mention them only in comments) move in the same
commit. `capacity_wait_s` and `restart_policy` move to the RunPod
namespace. Selection is untouched — `find_offers` still runs — so the payload goldens should be
byte-identical at the end of this stage.

**S2 — region as first class.** `Placement.region` wired through `_adapters.build_provider_for`;
`retry_until_up` reaches SkyPilot; runpod and modal declare `region` `UNSUPPORTED`. Example
configs pin `us-west-2` / `us-west1` per the standing Oregon rule.

**S3 — setup/run split.** `SetupStep`, engines emit steps, each provider maps them,
`_strip_trailing_exec` deleted, `image_build_script` / `runtime_provision_script` deleted. Golden
diff limited to RunPod's now-provider-composed trailing `exec`.

**S4 — declarative selection + rate-cap verification.** `find_offers` off the ABC; the offer
retry loop moves inside RunPod; `Capability.CATALOG_ENUMERATION` gates `kinoforge offers`;
`realized_rate` + `RATE_READBACK` / `RATE_DETERMINISTIC` + teardown-on-violation;
`Instance.cost_rate_usd_per_hr` sources from the realized read.

**S5 — endpoint shape + ledger generalisation.** One port-keyed shape everywhere; SkyPilot's
`endpoints()` becomes tunnel-ensuring; the pre-launch provisional row generalises to all
providers.

---

## 12. Migration

Hard break, no alias. Legacy top-level `compute.cloud` / `compute.cloud_type` raise at load with
the new path in the message:

```
ConfigError: compute.cloud was removed. SkyPilot cloud pinning now lives at
  compute.backend_options.skypilot.clouds
```

The YAML surface, before and after:

```yaml
# before
compute:
  provider: skypilot
  image: ...
  cloud: [lambda]                 # skypilot-only, silently ignored elsewhere
  cloud_type: secure              # runpod-only, silently ignored elsewhere
  requirements:
    min_vram_gb: 48
    min_cuda: "12.8"
    max_usd_per_hr: 1.09
    gpu_preference: [A100-80GB, H100]
    disk_gb: 200

# after
compute:
  provider: skypilot
  image: ...
  placement:
    accelerators: [A100-80GB, H100]   # was gpu_preference
    accelerator_count: 1
    region: us-west-2                 # F6: previously unreachable from YAML
    spot: false
    max_usd_per_hr: 1.09              # now verified after launch, not just filtered
    disk_gb: 200
  backend_options:
    skypilot: {clouds: [lambda], retry_until_up: true}
```

`compute.requirements` is renamed to `compute.placement` rather than kept as an alias: the block
changes meaning (a filter over a catalog becomes a constraint stated to a placer) and two of its
five keys move — `min_cuda` into the RunPod namespace, `gpu_preference` into `accelerators`.
Keeping the old name over new semantics is how a config surface starts lying.

A deprecation shim would keep two paths alive across stages, which is the failure mode the brief
names explicitly. The 8 affected configs are rewritten in the commit that introduces the error,
and the golden payloads prove the rewrite is equivalent.

---

## 13. Non-goals and follow-ups

- **Wiring `region` on RunPod and Modal.** Declared `UNSUPPORTED`, not implemented. Each wants its
  own live smoke.
- **The F3 env-routing gap** (`sky` lives only in the `live-skypilot` env, so a default-env sweep
  marks every skypilot row `UNROUTABLE`) is untouched here, as it was in Brief 2.
- **The withdrawn capability-aware reaper gate** (Brief 2 §8) stays withdrawn. Nothing in this
  design writes `session_start` on warm attach.
- **`EPHEMERAL_CAPABILITIES` and `_RECONCILABLE_PROVIDERS`** still stand apart from the capability
  declaration; this design adds `consumes()` beside them rather than folding them in.
- **`heartbeat_mode`**, rejected outright on skypilot at `_adapters.py:185-189`, is the third
  F5 field. It is a guardrail, not a placement field, so it belongs to Brief 2's axis; the
  `consumes()` guard covers placement and spec fields only. Making the rejection a load-time
  ERROR rather than a dispatch-time one is a one-line follow-up on the capability side.

---

## 14. Risks

- **S4 is the stage that can break a live path invisibly.** RunPod's offer-retry semantics are
  load-bearing during capacity droughts and are currently proven only by the orchestrator's tests.
  The move inside the provider must carry those tests with it, adapted, not rewritten from scratch.
- **The golden snapshot is only as good as the fakes.** A payload field that no fake asserts can
  change without the golden noticing. The `CONSUMED` wire-verification in §4 is the mitigation:
  it forces each declared-consumed field to be observable in a fake transport.
- **Teardown-on-violation destroys a booted instance.** On SkyPilot, `sky.launch` includes setup,
  so a violation discards several minutes of provisioning. That is the intended trade — the
  alternative is billing at a rate the operator capped — but it makes a wrong cap expensive, so
  `RateCapExceeded` must make the distinction between "bad cap" and "bad placement" obvious.
- **Five stages is a long window.** Between S1 and S4 the seam carries both `Placement` and
  `find_offers`. That is deliberate — selection is inverted only once the core is stable — but it
  means S1–S3 must not encode assumptions that S4 invalidates. Concretely: nothing in S1–S3 may
  read `spec.offer` outside a provider's own module.
