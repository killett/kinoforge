# Layer Q — Cross-engine cross-provider remote provisioning

## Why this layer

Layer P Task 7 item #3 (workflow API JSON + first green MP4) hit a hard architectural
blocker: `ComfyUIEngine.provision()` is local-only
(`src/kinoforge/engines/comfyui/__init__.py:545`, body starts
`del instance  # not used; comfyui runs on the local machine`). It clones git repos,
runs `pip install`, and launches `python main.py` on the **local** process — never on
the remote RunPod pod that the orchestrator just allocated. Items #1 and #2 of Task 7
never exercised this path because both shipped before any successful live provision had
been attempted. Item #3 hit it on first try; two pods leaked ($0.25 burned, both
auto-detected + destroyed by `list_instances()` audit within minutes).

The same gap exists in `DiffusersEngine.provision()` — it also runs `run_cmd` against
the local process. Hosted is `requires_compute=False` and excluded.

This layer ships a cross-engine cross-provider abstraction for first-boot remote
provisioning. After Layer Q:

- `ComfyUIEngine` + `DiffusersEngine` both provision cleanly on RunPod and SkyPilot.
- `LocalProvider` paths are unchanged for local users.
- Layer P Task 7 item #3 unblocks and resumes against the new layer.
- The same `render_provision` surface generalises to any future cloud provider that
  exposes a native boot-script mechanism (AWS user-data, Modal, etc.).

## Architecture

Three new pieces, layered:

1. **`engine.render_provision(cfg) -> RenderedProvision`** — engine emits a self-
   contained bash script + long-running run cmd + image + ports + required cred env
   var names. Pure function: no I/O.
2. **`InstanceSpec.provision_script` / `run_cmd`** — orchestrator attaches the
   rendered output to the spec before `provider.create_instance`.
3. **Provider-native boot-script injection** — each provider's `create_instance` reads
   the spec fields and uses its native mechanism: RunPod via `dockerArgs` + base64-
   encoded env var; SkyPilot via `Task.setup` / `Task.run`; LocalProvider ignores them
   silently.

After the pod boots, the script clones the engine repo, installs custom nodes,
downloads weights, and launches the engine HTTP server. The orchestrator then calls
`engine.provision(instance, cfg)`, which in the remote branch just polls an engine-
specific ready endpoint until HTTP-200 or `cfg.lifecycle.boot_timeout_s` elapses.

### Component summary

| File | Surface |
|---|---|
| `core/interfaces.py` | `RenderedProvision` dataclass; `InstanceSpec.provision_script: str \| None`; `InstanceSpec.run_cmd: list[str] \| None`; `GenerationEngine.render_provision(cfg) -> RenderedProvision`; `GenerationEngine.wait_for_ready(instance, *, http_get, sleep, get_instance, timeout_s)` |
| `core/errors.py` | `ProvisionFailed`, `ProvisionTimeout` |
| `core/config.py` | `LifecycleConfig.boot_timeout_s: int = 900` |
| `core/orchestrator.py` | `_provision_instance_and_build_backend` extended: render → spec.replace → cred-validate → create_instance → wait_for_ready |
| `engines/comfyui/__init__.py` | `render_provision()`; `wait_for_ready()` polls `GET <comfyui>/system_stats`; `provision()` branches on `instance is None or instance.provider == "local"` |
| `engines/diffusers/__init__.py` | parity: `render_provision()`; `wait_for_ready()` polls `GET <base_url>/health`; `provision()` local-branch guard |
| `providers/runpod/__init__.py` | `_create_pod` reads `spec.provision_script` + `spec.run_cmd`; base64-encodes script into env var `KINOFORGE_PROVISION_SCRIPT`; sets `dockerArgs` to decode + execute |
| `providers/skypilot/__init__.py` | `create_instance` maps `spec.provision_script` → `Task.setup`, `spec.run_cmd` → `Task.run` |
| `providers/local/__init__.py` | ignores `provision_script` / `run_cmd` silently (no behavioural change) |

### Why this shape

- **Engine owns the script.** Engines know their own repo URL, requirements,
  custom-node format, and launch args. The script is engine-specific; provider is
  injection-mechanism-agnostic.
- **Provider owns the injection.** RunPod's `dockerArgs` and SkyPilot's `setup:`
  blocks are idiomatic to each provider. We don't paper over them — we use them.
- **Orchestrator owns wiring + cred validation.** Single place that lifts secrets
  from `CredentialProvider` into `spec.env` after the engine declares which env vars
  it needs.
- **No SSH dep.** First-boot bootstrap is the only thing kinoforge does on the pod.
  Steady-state runtime talks HTTP only. No `paramiko`, no SSH key plumbing, no per-
  command latency.
- **Idempotency guards inside the script.** `[ ! -d ComfyUI ] && git clone ...`,
  `[ ! -f models/.../weight.safetensors ] && curl ...` — warm-pod re-runs are no-ops.
  Combined with Layer P Task 7 item #2's warm-pod-reuse tag discovery, cold-start
  cost is paid once per pod lifetime.

## `RenderedProvision` dataclass

```python
# src/kinoforge/core/interfaces.py
@dataclass(frozen=True, slots=True)
class RenderedProvision:
    """Engine-emitted bootstrap payload for a remote pod / VM.

    Attributes:
        script: Self-contained bash script. Must be idempotent (re-runnable on
            warm pods without side effects). Must NOT contain literal credential
            values; reference them as ``$VAR_NAME`` and rely on the orchestrator
            to inject via ``spec.env``.
        run_cmd: Long-running command executed after the script completes.
            Convention: the script ends with ``exec <run_cmd>`` so the run cmd
            becomes the container's PID 1.
        image: Container image to boot. Defaults to a stock provider image.
        ports: Ports the engine listens on. Provider exposes via its native
            mechanism (RunPod proxy, Sky port forward).
        env_required: Names of cred env vars the script references. Orchestrator
            validates each is reachable via the configured ``CredentialProvider``
            before calling ``provider.create_instance``; lifts onto ``spec.env``.
    """

    script: str
    run_cmd: list[str]
    image: str
    ports: list[str]
    env_required: list[str]
```

## `InstanceSpec` extension

```python
# src/kinoforge/core/interfaces.py
@dataclass(frozen=True, slots=True)
class InstanceSpec:
    # ... existing fields ...
    provision_script: str | None = None
    run_cmd: list[str] | None = None
```

Frozen; constructed via `dataclasses.replace`. LocalProvider silently ignores both.

## `GenerationEngine` ABC additions

```python
# src/kinoforge/core/interfaces.py
class GenerationEngine(Protocol):
    # ... existing surface ...

    def render_provision(self, cfg: dict[str, Any]) -> RenderedProvision:
        """Emit the first-boot bootstrap payload for this engine.

        Engines that do not support remote provisioning (e.g. ``HostedAPIEngine``)
        raise ``NotImplementedError``. The orchestrator only calls this for engines
        with ``requires_compute=True``.

        Args:
            cfg: Runtime configuration dict (same shape passed to ``provision``).

        Returns:
            ``RenderedProvision`` ready to attach to ``InstanceSpec``.
        """

    def wait_for_ready(
        self,
        instance: Instance,
        *,
        http_get: Callable[[str], dict[str, Any]],
        sleep: Callable[[float], None],
        get_instance: Callable[[str], Instance],
        timeout_s: float,
    ) -> None:
        """Poll until the engine reports ready, status flips terminal, or timeout.

        Engine knows its own readiness criterion (ComfyUI: GET /system_stats 200;
        Diffusers: GET /health 200). Polls every ``_READY_POLL_INTERVAL_S`` seconds.
        Between HTTP polls, calls ``get_instance(instance.id)``; if status flips to
        ``"terminated"`` or ``"stopped"`` before ready, raises ``ProvisionFailed``.
        Raises ``ProvisionTimeout`` after ``timeout_s`` elapses.

        Raises:
            ProvisionFailed: Pod boot script crashed (status flipped terminal).
            ProvisionTimeout: Ready check never returned 200 within deadline.
        """
```

## `engine.provision()` rewire

```python
# Both ComfyUIEngine and DiffusersEngine — same shape
def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
    if instance is None or instance.provider == "local":
        # existing local-only body, unchanged
        ...
        return

    # remote branch — script already ran via provider boot path; just wait
    self.wait_for_ready(
        instance,
        http_get=self._http_get,
        sleep=self._sleep,
        get_instance=self._get_instance,  # injected provider seam, see below
        timeout_s=float(cfg.get("lifecycle", {}).get("boot_timeout_s", 900)),
    )
```

`_get_instance` is a new constructor-injected seam on `ComfyUIEngine`/`DiffusersEngine`:
`Callable[[str], Instance]`. Defaults to a no-op stub that raises
`NotImplementedError`; orchestrator passes `provider.get_instance` when wiring the
engine for a remote run. Tests inject a fake.

## `Orchestrator._provision_instance_and_build_backend` extension

```python
# src/kinoforge/core/orchestrator.py
def _provision_instance_and_build_backend(
    self, engine, provider, cfg, cred_provider, *,
    instance: Instance | None,
    tags: dict[str, str] | None,
):
    if instance is None:
        # NEW: render provision payload
        rendered = engine.render_provision(cfg)

        # NEW: validate every declared cred env var is reachable
        env: dict[str, str] = {}
        for var in rendered.env_required:
            value = cred_provider.get(var)
            if value is None:
                raise AuthError(f"missing required env var: {var}")
            env[var] = value

        # build spec (existing offer-retry path) but with provision_script/run_cmd/image/ports
        spec = self._make_spec(
            offer=..., env=env, image=rendered.image, ports=rendered.ports,
            provision_script=rendered.script, run_cmd=rendered.run_cmd,
            tags=merged_tags,
        )
        instance = self._create_with_offer_retry(provider, spec, offers)
        orchestrator_created_instance = True
    else:
        orchestrator_created_instance = False

    # NEW: wire get_instance seam onto engine for the remote-wait path
    engine = engine.with_get_instance(provider.get_instance)
        # OR: pass via cfg["_get_instance"] — TBD during plan-writing

    try:
        engine.provision(instance, cfg)  # local or remote
    except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
        if orchestrator_created_instance:
            provider.destroy_instance(instance.id)
        raise

    return instance, engine.backend(instance, cfg)
```

## RunPod `_create_pod` extension

```python
# src/kinoforge/providers/runpod/__init__.py — inside _create_pod
import base64

# Existing: build env dict, inject RUNPOD_TERMINATE_KEY + KINOFORGE_SELFTERM_SCRIPT,
# strip RUNPOD_API_KEY

if spec.provision_script is not None:
    # base64 dodges shell-escape hell + dockerArgs length limits
    encoded = base64.b64encode(spec.provision_script.encode("utf-8")).decode("ascii")
    env["KINOFORGE_PROVISION_SCRIPT"] = encoded

if spec.run_cmd is not None:
    docker_args = (
        'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
        '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
    )
else:
    docker_args = ""

body = {
    "query": _CREATE_POD_MUTATION,
    "variables": {"input": {
        # ... existing fields ...
        "imageName": spec.image,
        "dockerArgs": docker_args,
        "env": [{"key": k, "value": v} for k, v in env.items()],
    }},
}
```

The script itself ends with `exec <run_cmd>` so the engine HTTP server becomes the
container's main process. The run_cmd is rendered into the script during
`render_provision`; we do NOT also pass it via `dockerArgs` (single source of truth).

## SkyPilot `create_instance` extension

```python
# src/kinoforge/providers/skypilot/__init__.py
def create_instance(self, spec: InstanceSpec) -> Instance:
    sky = self._get_sky()
    task_config = {
        "resources": {"accelerators": spec.offer.gpu_type if spec.offer else None},
        # ... existing fields ...
    }
    if spec.provision_script is not None:
        task_config["setup"] = spec.provision_script
    if spec.run_cmd is not None:
        task_config["run"] = " ".join(shlex.quote(arg) for arg in spec.run_cmd)
    # rest unchanged
```

## ComfyUI engine `render_provision` body

```python
def render_provision(self, cfg: dict[str, Any]) -> RenderedProvision:
    engine_cfg = cfg.get("engine", {}).get("comfyui", {})
    repo = engine_cfg.get("repo", "https://github.com/comfyanonymous/ComfyUI")
    branch = engine_cfg.get("branch", "master")
    custom_nodes = engine_cfg.get("custom_nodes", [])
    launch_args = engine_cfg.get("launch_args", ["--listen", "0.0.0.0", "--port", "8188"])
    models = cfg.get("models", [])

    lines = ["set -euo pipefail", "cd /workspace"]
    lines.append(f"[ ! -d ComfyUI ] && git clone --depth 1 --branch {branch} {repo} ComfyUI")
    lines.append("cd ComfyUI && pip install -q -r requirements.txt")

    for node in custom_nodes:
        node_url = node["git"]
        node_name = node_url.rstrip("/").split("/")[-1]
        ref = node.get("ref")
        if ref:
            # clone full, then checkout ref
            lines.append(
                f"[ ! -d custom_nodes/{node_name} ] && "
                f"git clone {node_url} custom_nodes/{node_name} && "
                f"cd custom_nodes/{node_name} && git checkout {ref} && cd ../.."
            )
        else:
            lines.append(
                f"[ ! -d custom_nodes/{node_name} ] && "
                f"git clone --depth 1 {node_url} custom_nodes/{node_name}"
            )
        lines.append(
            f"[ -f custom_nodes/{node_name}/requirements.txt ] && "
            f"pip install -q -r custom_nodes/{node_name}/requirements.txt || true"
        )

    env_required: list[str] = []
    for entry in models:
        src = entry["src"]
        target = entry["target"]
        subdir = TARGET_TO_SUBDIR.get(target, f"models/{target}")
        # Resolve via the source registry once at render time to get the URL + headers
        source = registry.get_source_for_ref(src)
        artifact = source.resolve(src)
        filename = entry.get("filename") or artifact.filename
        # Auth header — engine emits ${VAR} reference; orchestrator lifts onto spec.env
        auth_header = ""
        for hk, hv in (artifact.headers or {}).items():
            # Pattern: "Authorization: Bearer $HF_TOKEN"
            #   artifact.headers value carries "$HF_TOKEN" already? or we map env-var name?
            if hk.lower() == "authorization":
                env_var = _extract_env_var(hv)  # parses "Bearer ${HF_TOKEN}" → "HF_TOKEN"
                env_required.append(env_var)
                auth_header = f' -H "Authorization: Bearer ${env_var}"'
        lines.append(f"mkdir -p {subdir}")
        lines.append(
            f"[ ! -f {subdir}/{filename} ] && "
            f"curl -L --fail{auth_header} '{artifact.url}' -o {subdir}/{filename}"
        )

    run_cmd = ["python", "main.py"] + list(launch_args)
    lines.append(f"cd /workspace/ComfyUI && exec {' '.join(shlex.quote(c) for c in run_cmd)}")

    return RenderedProvision(
        script="\n".join(lines),
        run_cmd=run_cmd,
        image=engine_cfg.get("image", "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"),
        ports=[str(_extract_port(launch_args))],
        env_required=sorted(set(env_required)),
    )
```

`_extract_env_var(hv)` is a small helper parsing `"Bearer ${VAR}"` /
`"Bearer $VAR"` patterns. `_extract_port(launch_args)` defaults to 8188 if absent.

## ComfyUI engine `wait_for_ready` body

```python
def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s):
    port = next(iter(self._ports_from_instance(instance)), "8188")
    ready_url = f"{instance.endpoints[port]}/system_stats"
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        try:
            http_get(ready_url)
            return  # ComfyUI's /system_stats returns 200 with JSON when up
        except Exception:
            pass
        current = get_instance(instance.id)
        if current.status in ("terminated", "stopped"):
            raise ProvisionFailed(
                f"pod {instance.id!r} entered terminal status "
                f"{current.status!r} before ready"
            )
        sleep(_READY_POLL_INTERVAL_S)
    raise ProvisionTimeout(
        f"engine ready check timed out after {timeout_s:.0f}s for pod {instance.id!r}"
    )
```

`_READY_POLL_INTERVAL_S = 5.0` module constant.

## Errors

```python
# src/kinoforge/core/errors.py

class ProvisionFailed(KinoforgeError):
    """Pod boot script crashed — provider reported terminal status before ready."""


class ProvisionTimeout(KinoforgeError):
    """Ready check never returned success within ``boot_timeout_s``."""
```

Both subclass `KinoforgeError` per existing convention. Caught alongside
`CapabilityMismatch` and `ValidationError` in the orchestrator teardown guard so a
caller-supplied instance is NOT destroyed (Layer P Task 7 item #2 contract preserved).

## Data flow (remote ComfyUI on RunPod, cold start)

1. `orchestrator.deploy_session(cfg, instance=None, tags={...})`
2. `rendered = engine.render_provision(cfg)` — script, run_cmd, image, ports,
   env_required computed from cfg.
3. Orchestrator iterates `rendered.env_required`; pulls each from
   `cred_provider.get(var)`; raises `AuthError` if any is None; assembles
   `env` dict.
4. `spec = InstanceSpec(..., image=rendered.image, ports=rendered.ports, env=env,
   provision_script=rendered.script, run_cmd=rendered.run_cmd, tags=...)`.
5. `provider.create_instance(spec)` — RunPod encodes `provision_script` into
   `KINOFORGE_PROVISION_SCRIPT` (base64 env var) and sets `dockerArgs` to decode
   + exec. Returns `instance` with `status="starting"`.
6. Pod boots, decodes script, runs git clone + pip install + weight download +
   `exec python main.py --listen 0.0.0.0 --port 8188`. Steps 5-6 ≈ 10-25 min cold.
7. `engine.provision(instance, cfg)` — branches into remote; calls
   `engine.wait_for_ready` polling `https://{id}-8188.proxy.runpod.net/system_stats`
   every 5s until 200 (or timeout / status terminal).
8. `engine.backend(instance, cfg)` — returns `ComfyUIBackend` with
   `base_url=instance.endpoints["8188"]`.

Warm-pod path (item #2 tag discovery): step 1 supplies `instance=`; orchestrator
skips steps 2-6 entirely; step 7 still runs (cheap if pod healthy: one 200 = return);
step 8 unchanged.

## Acceptance criteria

| # | AC | How verified |
|---|---|---|
| 1 | `RenderedProvision` dataclass exists; `InstanceSpec.provision_script` + `run_cmd` fields exist; both default `None`; both frozen | unit |
| 2 | `GenerationEngine.render_provision` + `wait_for_ready` exist as Protocol methods | mypy |
| 3 | `ComfyUIEngine.render_provision` returns a script that, when run in a clean pod, would clone ComfyUI, clone N custom nodes (with optional `ref` checkout), download N weights with HF/CivitAI auth headers, and `exec python main.py …` | unit (snapshot per cfg variant: 0 nodes / 1 node / 1+ref / N nodes; 0 models / HF model / CivitAI model / HF+CivitAI mix) |
| 4 | `ComfyUIEngine.wait_for_ready` returns when `http_get(<ready_url>)` succeeds; raises `ProvisionFailed` when `get_instance` returns status `"terminated"` / `"stopped"` before 200; raises `ProvisionTimeout` after `timeout_s` elapses | unit (3 tests with injected spies) |
| 5 | `DiffusersEngine.render_provision` + `wait_for_ready` ship with parity (pip install + python -m server + `GET /health` ready check) | unit |
| 6 | `ComfyUIEngine.provision` + `DiffusersEngine.provision` branch on `instance is None or instance.provider == "local"`: local body unchanged; remote body calls `wait_for_ready` | unit (regression of existing local tests + 2 new remote-branch tests per engine) |
| 7 | `RunPodProvider._create_pod` reads `spec.provision_script` and `spec.run_cmd`; base64-encodes script into `KINOFORGE_PROVISION_SCRIPT` env var; sets `dockerArgs` to the decode-and-exec one-liner; passes `spec.image` as `imageName` | unit (1 snapshot test + 1 redactor-verification test asserting the script doesn't surface plaintext creds) |
| 8 | `SkyPilotProvider.create_instance` maps `spec.provision_script` → `Task.setup`, `spec.run_cmd` → `Task.run` (shell-quoted) | unit (offline fake sky client) |
| 9 | `LocalProvider.create_instance` silently ignores `spec.provision_script` and `spec.run_cmd` | unit (1 regression test) |
| 10 | `LifecycleConfig.boot_timeout_s` field exists; defaults to 900; round-trips through YAML | unit |
| 11 | Orchestrator wires `render_provision` → cred-validate → `spec.replace(provision_script=…, run_cmd=…, image=…, ports=…)` → `create_instance` → `wait_for_ready` → `backend` | unit (spy provider captures spec; asserts cred-validation runs before `create_instance`; asserts `wait_for_ready` invoked once per provision) |
| 12 | Orchestrator raises `AuthError` when any `env_required` var is missing from `CredentialProvider`; pod is NOT created (no `create_instance` call) | unit |
| 13 | Orchestrator destroys orchestrator-created instance on `ProvisionFailed` / `ProvisionTimeout`; does NOT destroy caller-supplied instance (item #2 contract preserved) | unit (2 tests) |
| 14 | Fixture audit (bug-fix #1) continues to pass; no plaintext cred in any committed JSON | existing `tests/providers/test_fixtures_audit.py` |
| 15 | Layer P Task 7 item #3 live smoke (`tests/live/test_comfyui_wan_live.py`) runs on a real RunPod pod end-to-end: cold start → MP4 → cleanup. Warm second iteration: pod reused, no re-download | live (manual; budget ≤ $1.50; logged on PROGRESS) |

## Non-goals (explicitly out of scope)

- **Ad-hoc remote shell** (Approach A from brainstorm) — `paramiko` / `sky exec` for
  arbitrary post-provision commands. Useful for debug; not needed for steady-state.
  Future layer if demand emerges.
- **kinoforge-published base images** + per-engine `skip_engine_clone` toggle (Option
  3 from brainstorm). Toggle is a single-field future extension; no architectural
  lock-in by deferring.
- **Pod boot-log tailing** for debugging. RunPod exposes `podLogs` via GraphQL but
  surfacing it through kinoforge is a separate debug-UX layer.
- **`extract_last_frame`** for remote case. Already URL-based per Layer E
  (`tasks 4/5/6`); works for remote out of the box.
- **New providers** (AWS direct, Modal, Replicate, etc.). Same `render_provision`
  shape applies; not in this layer.
- **Layer P Task 7 item #3 itself.** Layer Q unblocks it; item #3 resumes in its own
  sub-plan after Layer Q closes.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Rendered script too large for RunPod env var | low | high (pod fails to boot) | RunPod env vars accept ≥4KB; base64 of typical script (≤2KB plaintext) is ≤3KB. Reserve cap check in render: warn if script > 8KB. |
| `dockerArgs` length limit on RunPod | low | high (truncated boot cmd) | One-liner is < 200 chars regardless of script size. Script content is in env var. |
| Weight download fails on cold start (HF rate-limit, network blip) | medium | medium (boot timeout) | `set -euo pipefail` + `curl --fail` raises; pod exits non-zero; orchestrator sees status flip → `ProvisionFailed` (fast fail vs. timeout). Retry is caller's responsibility (item #1 offer-retry pattern). |
| Engine ready check returns 200 before models are loaded → backend job dispatch fails immediately | medium | medium (job fails before useful work) | ComfyUI's `/system_stats` returns 200 once the HTTP server is up; model loading is lazy on first job. Acceptable: first job pays the model-load cost. If unacceptable later, switch ready check to a known-warm sentinel (e.g. POST a 1-frame test prompt). |
| Cold-start time exceeds default `boot_timeout_s=900` for very large models | low | low | `cfg.lifecycle.boot_timeout_s` is configurable. Layer P Task 7 Wan i2v Q4 fits comfortably; future big-model workflows tune up. |
| Provider boot-script env-var injection leaks plaintext creds into GraphQL request body | medium | high (cred leak in fixture) | Bug-fix #1 redactor (`_redact_kv_shape` + `_redact_credential_patterns`) already covers `env[*].value` patterns. Layer Q adds no new leak shapes. New test: render_provision output asserted to contain `$HF_TOKEN` (referent, not value). |
| Engine `_get_instance` seam not wired correctly → `wait_for_ready` can't check status | low | medium (timeout instead of fast-fail) | Default seam raises `NotImplementedError`; orchestrator MUST inject `provider.get_instance` before calling `engine.provision`. Unit test locks this down. |
| `LocalProvider` accidentally honours `provision_script` (e.g. someone wires it in by mistake) | low | low | Explicit test: `LocalProvider.create_instance(spec_with_provision_script).status == ready` and no subprocess invoked. |

## Open knobs (resolved during plan-writing or first live run)

- **How does engine get `provider.get_instance` seam?** Two candidates:
  (a) `cfg["_get_instance"] = provider.get_instance` (cfg-borne, no engine API change),
  (b) New `engine.with_get_instance(get_instance) -> GenerationEngine` (returns a copy
  with the seam wired). Plan picks one; offline tests don't care.
- **Diffusers engine: server stub location.** Currently `DiffusersEngine.provision`
  runs `pip install + server_cmd` locally. For remote we need a self-contained
  bash script that installs deps + launches a diffusers HTTP server. Two options:
  bundle a small `kinoforge-diffusers-server` script alongside the engine module
  (copied to pod via the script as a heredoc), or rely on an existing community
  server. Plan picks one; the simpler one is heredoc-bundled.
- **Should `render_provision` accept a `CredentialProvider` and embed env-var values
  directly?** No — locked. Reference-only via `$VAR`; orchestrator lifts. Keeps
  values out of `render_provision`'s output (which feeds fixture captures).

## Decisions log (from brainstorming, 2026-06-01)

| # | Decision | Rationale |
|---|---|---|
| D1 | Goal frame: **design-it for long-term architecture** | Layer Q unblocks Layer P item #3 AND becomes the foundational remote-provision layer for every future cloud engine/provider. |
| D2 | Generalisation scope: **all current cloud engines × all current providers** | ComfyUI + Diffusers × RunPod + SkyPilot from day 1. Local + Hosted unchanged. |
| D3 | Approach: **B — `engine.render_provision()` + `InstanceSpec.provision_script`** | No SSH dep; uses each provider's idiomatic boot path; first-boot is one round-trip; ad-hoc shell can layer on later. |
| D4 | Bootstrap surface: **full bootstrap; script owns engine clone + nodes + weights + launch** | Zero base-image management; stock provider images work; warm-pod reuse (item #2) negates cold-start cost concern. Add `skip_engine_clone` toggle later if real base images materialise. |
| D5 | Ready check: **engine owns; `wait_for_ready` polls engine-specific endpoint with `boot_timeout_s` from cfg.lifecycle; checks provider status between polls** | Engine knows its own ready criterion; centralises remote-startup error semantics. |
| D6 | Cred plumbing: **`render_provision` declares `env_required: list[str]`; orchestrator validates + lifts via `spec.env`; script references `$VAR`** | Bug-fix #1 redactor covers `env[*].value`. Values never enter `RenderedProvision` output. |
| D7 | Local-vs-remote branch in `engine.provision`: **`instance is None or instance.provider == "local"`** | Zero behavioural change for local users; cleanest test of "are we remote." |
| D8 | New errors: **`ProvisionFailed` + `ProvisionTimeout` under `KinoforgeError`** | Orchestrator catches alongside `CapabilityMismatch` / `ValidationError`; item #2 caller-supplied-instance teardown guard preserved. |

## File / diff inventory

**New code:**

| File | LOC est. | Purpose |
|---|---|---|
| (new) `tests/engines/test_comfyui_render_provision.py` | ~250 | snapshot + parametrised render tests |
| (new) `tests/engines/test_comfyui_wait_for_ready.py` | ~80 | 3 polling/timeout tests |
| (new) `tests/engines/test_diffusers_render_provision.py` | ~100 | parity |
| (new) `tests/engines/test_diffusers_wait_for_ready.py` | ~80 | parity |
| (new) `tests/providers/test_runpod_provision_script.py` | ~120 | base64 encoding + dockerArgs assembly |
| (new) `tests/providers/test_skypilot_provision_script.py` | ~80 | setup/run mapping |
| (new) `tests/providers/test_local_ignores_provision_script.py` | ~40 | regression |
| (new) `tests/core/test_orchestrator_render_provision.py` | ~200 | wiring + cred-validate + teardown branches |

**Modified code:**

| File | Δ LOC est. | Notes |
|---|---|---|
| `src/kinoforge/core/interfaces.py` | +60 | `RenderedProvision` + 2 spec fields + 2 ABC methods |
| `src/kinoforge/core/errors.py` | +10 | 2 new errors |
| `src/kinoforge/core/config.py` | +5 | `boot_timeout_s` field |
| `src/kinoforge/core/orchestrator.py` | +60 | `_provision_instance_and_build_backend` extension |
| `src/kinoforge/engines/comfyui/__init__.py` | +130 | `render_provision` + `wait_for_ready` + `provision` branch + ports helper |
| `src/kinoforge/engines/diffusers/__init__.py` | +120 | parity |
| `src/kinoforge/providers/runpod/__init__.py` | +30 | dockerArgs assembly + base64 env-var injection |
| `src/kinoforge/providers/skypilot/__init__.py` | +25 | setup/run mapping |
| `tests/conftest.py` (or per-test) | ±10 | shared spy provider for orchestrator tests |
| `PROGRESS.md` | +35 | Layer Q closure block |
| `README.md` | +20 | "Remote provisioning" subsection |

## Test count expectation

Pre-Layer-Q offline: 888 (per PROGRESS:238).
Layer Q adds approximately 50–70 new offline tests.
Post-Layer-Q expected: 938–958.
4 pre-existing RED scaffold tests (Task 7 item #3) remain RED until item #3 resumes.

## Branch + merge

- Work branches off `build/layer-p` so Layer Q can stand on Layer P's already-shipped
  warm-reuse + redactor work without rebase pain.
- Or — cleaner — Layer Q branches off `main` since none of Layer P's HEAD-state code
  is needed by Layer Q itself (the redactor work is in `tests/providers/conftest_runpod.py`
  which Layer Q doesn't modify in mechanism, only in coverage). **Plan-writing decides.**
- Merge to `main` via `--no-ff` once all ACs green + reviewers approve.
- Layer P Task 7 item #3 sub-plan re-opens against Layer Q's HEAD; its blocking
  status updates in PROGRESS.

## Spec self-review notes (inline, before commit)

- All cfg keys referenced (`engine.comfyui.repo`, `engine.comfyui.branch`,
  `engine.comfyui.custom_nodes`, `engine.comfyui.launch_args`,
  `engine.comfyui.image`, `models[*].src`, `models[*].target`,
  `models[*].filename`, `lifecycle.boot_timeout_s`) are already pydantic-modelled
  in `core/config.py` EXCEPT `engine.comfyui.repo` / `branch` / `image` and
  `lifecycle.boot_timeout_s`. Plan must add these to `ComfyUIEngineConfig` and
  `LifecycleConfig` respectively.
- `TARGET_TO_SUBDIR` map is currently in `engines/comfyui/__init__.py`; `render_provision`
  reuses it. No move needed.
- `registry.get_source_for_ref` is the canonical lookup; `render_provision` calls it
  at render time to resolve weight URLs + headers without doing any I/O.
- The "engine ready before models loaded" caveat is in the risk register and is
  acceptable for Wan i2v (model load takes ~30s, first job pays it). If a future
  workflow has unacceptable first-job latency, the mitigation is a sentinel POST.
- The "how does engine get `provider.get_instance`?" knob is intentionally left
  open; plan picks one and implements it.
