# Compute Seam S1 — Portable Core + backend_options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the portable `Placement` core and provider-namespaced `backend_options` so that no
`ComputeConfig` / `InstanceSpec` field is silently ignored by the selected provider, with a
golden launch-payload snapshot proving no wire payload changed.

**Architecture:** Stage 1 of 5 from
`docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md`. Selection is NOT
inverted here — `find_offers` and `spec.offer` stay exactly as they are until S4. What changes is
the *shape* of the config and spec: vendor-specific fields move into
`compute.backend_options.<provider>` namespaces validated by the owning provider class,
`compute.requirements` becomes `compute.placement`, and every provider declares — via a new
`consumes()` classmethod guarded by a parity test — which portable fields it actually reads.

**Tech Stack:** Python 3.13, pydantic v2 (`extra="forbid"` for namespace validation), pytest,
pixi, ruff, mypy. Providers: runpod (GraphQL), skypilot (SDK), modal (SDK), local.

**Global Constraints:**
- **The golden launch payloads must not change during S1.** Every task after Task 1 re-runs
  `pytest tests/providers/test_launch_payload_goldens.py -v` and it must pass unchanged. S1 is a
  shape change, not a behaviour change. The single intended behaviour change is capacity-wait
  scoping (Task 3), which does not appear in any payload.
- **No credential may reach a committed golden.** The snapshot harness uses a stub
  `CredentialProvider` returning `kinoforge-prod-deadbeef`-style synthetic values, and
  `tools/scan_secrets.py` runs at pre-commit over the staged goldens.
- **Every commit runs `pixi run pre-commit run --all-files` first.** Never `--no-verify`.
- **Conventional Commits**, imperative mood.
- Local timezone everywhere (`datetime.now()`), never UTC.
- `rg` not `grep`, `fd` not `find`.

**User decisions (already made):**
- "Full inversion" of the selection model — but that is S4. S1 leaves `find_offers` and
  `spec.offer` alone. Constraint carried into S1: **no code outside a provider's own module may
  read `spec.offer`** (design §14).
- "Capability-gated fail-closed" rate verification — S4, not here.
- Hard break on `compute.cloud` / `compute.cloud_type`, no alias, 8 example configs migrate in
  the same commit as the load error (design §12).
- `region` is deferred to S2; `cpus` / `memory_gb` deferred until a stage needs them (design §2).

---

## File Structure

**Created:**
- `src/kinoforge/core/spec_builder.py` — the config+rendered-provision → `InstanceSpec` mapping,
  extracted from the closure inside `deploy_session`. One responsibility: build a spec.
- `tools/snapshot_launch_payloads.py` — regenerates the golden launch payloads. Runnable, not a
  test, so a reviewed regeneration is an explicit act.
- `tests/providers/golden/launch_payloads/*.json` — one file per example config with a compute
  block.
- `tests/providers/test_launch_payload_goldens.py` — byte-identity test over the above.
- `tests/providers/test_field_consumption_parity.py` — the §4 regression guard.
- `src/kinoforge/validation/checks/field_support.py` — `UnsupportedFieldCheck` +
  `ForeignNamespaceCheck`.

**Modified:**
- `src/kinoforge/core/interfaces.py` — `Placement`, `FieldSupport`, `ComputeProvider.consumes()`,
  `InstanceSpec` field changes, `Lifecycle.capacity_wait_s` removal.
- `src/kinoforge/core/config.py` — `PlacementConfig`, `ComputeConfig.backend_options`, removal of
  `RequirementsConfig` / `cloud` / `cloud_type`, `Config.placement()`,
  `Config.backend_options_for()`.
- `src/kinoforge/core/orchestrator.py` — call the extracted spec builder; capacity-wait source.
- `src/kinoforge/_adapters.py` — `build_provider_for` reads the skypilot namespace;
  new `build_capacity_wait_for`.
- `src/kinoforge/providers/{runpod,skypilot,modal,local}/__init__.py` — `Options` model,
  `consumes()` declaration, namespace reads.
- `src/kinoforge/cli/_commands.py` — the `kinoforge offers` spec construction.
- `examples/configs/*.yaml` — 8 configs migrate keys; all configs with a `requirements:` block
  rename it to `placement:`.

---

## Task 0: Extract the InstanceSpec builder out of `deploy_session`

**Goal:** Move the `_build_spec` closure (`core/orchestrator.py:890-930`) into a module-level
pure function so the golden snapshot in Task 1 can build a spec from a config without running a
deploy.

**Files:**
- Create: `src/kinoforge/core/spec_builder.py`
- Modify: `src/kinoforge/core/orchestrator.py:890-930` (delete closure, call the new function),
  and the second closure at `:1695-1709` (bare `deploy()` path)
- Test: `tests/core/test_spec_builder.py`

**Acceptance Criteria:**
- [ ] `build_instance_spec` is importable from `kinoforge.core.spec_builder` and takes no
      orchestrator state — only explicit arguments.
- [ ] Every field the old closure set is set identically: tags merge order, `image` fallback,
      `ports` tuple, `provision_script`, the empty-string→`None` coercion on
      `image_build_script` / `runtime_provision_script`, `diagnostic_env` gated on
      `cfg.diagnostic_mode`, `restart_policy` gated on the same flag, `cloud_type` from
      `cfg.compute` with `"any"` when `cfg.compute is None`.
- [ ] `deploy_session` and the bare `deploy()` path both call it; neither retains a local
      `_build_spec`.
- [ ] Full suite green — this is a pure move, and the existing 4000+ tests are the equivalence
      proof.

**Verify:** `pixi run python -m pytest tests/core/test_spec_builder.py tests/core -q` → all pass,
then `pixi run test` → no new failures.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_spec_builder.py
"""Behavior: build_instance_spec maps (cfg, rendered, run context) -> InstanceSpec.

Extracted from the deploy_session closure so a spec can be built without a
deploy. These tests pin the field-for-field mapping the closure had; a
careless extraction that drops e.g. the empty-string -> None coercion on
image_build_script would make Modal re-run the heavy installs at container
start (the 2026-07-09 FlashVSR preemption failure).
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Lifecycle, Offer, RenderedProvision
from kinoforge.core.spec_builder import build_instance_spec

_CFG = "examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml"


def _rendered(**over: object) -> RenderedProvision:
    base = {
        "script": "#!/bin/bash\necho hi\nexec python -m server",
        "run_cmd": ["python", "-m", "server"],
        "image": "img:tag",
        "ports": ["8000/http"],
        "env_required": [],
        "build_script": "",
        "runtime_script": "",
    }
    base.update(over)
    return RenderedProvision(**base)  # type: ignore[arg-type]


def _offer() -> Offer:
    return Offer(
        id="NVIDIA A100 80GB PCIe",
        gpu_type="NVIDIA A100 80GB PCIe",
        vram_gb=80,
        cuda="12.4",
        cost_rate_usd_per_hr=1.64,
    )


def _build(**over: object):
    kwargs: dict[str, object] = {
        "cfg": load_config(_CFG),
        "rendered": _rendered(),
        "offer": _offer(),
        "engine_name": "diffusers",
        "key_hash": "abc123",
        "image": "fallback:img",
        "lifecycle": Lifecycle(),
        "env": {"HF_TOKEN": "kinoforge-prod-deadbeef"},
        "run_id": "run-1",
        "tags": None,
    }
    kwargs.update(over)
    return build_instance_spec(**kwargs)  # type: ignore[arg-type]


def test_rendered_image_wins_over_fallback():
    # Bug caught: spec falls back to the cfg image and boots the wrong
    # container when the engine pinned one (the composed-upscaler images).
    assert _build().image == "img:tag"


def test_empty_rendered_image_falls_back():
    assert _build(rendered=_rendered(image="")).image == "fallback:img"


def test_engine_and_key_tags_are_always_present_and_caller_tags_win():
    # Bug caught: caller tags clobbered by the defaults, or vice versa, which
    # breaks warm-reuse matching (kinoforge_key) and the ephemeral index.
    spec = _build(tags={"kinoforge_key": "override", "extra": "x"})
    assert spec.tags["kinoforge_engine"] == "diffusers"
    assert spec.tags["kinoforge_key"] == "override"
    assert spec.tags["extra"] == "x"


def test_empty_build_script_becomes_none_not_empty_string():
    # Bug caught: "" is falsy but not None; Modal's
    # `spec.runtime_provision_script or spec.provision_script` fallback works
    # either way, but the image-bake branch checks `is not None`.
    spec = _build()
    assert spec.image_build_script is None
    assert spec.runtime_provision_script is None


def test_split_scripts_are_carried_when_the_engine_emits_them():
    spec = _build(rendered=_rendered(build_script="pip install x", runtime_script="exec s"))
    assert spec.image_build_script == "pip install x"
    assert spec.runtime_provision_script == "exec s"


def test_ports_come_from_rendered_as_a_tuple():
    assert _build().ports == ("8000/http",)


def test_diagnostic_mode_off_means_no_diagnostic_env_and_restart_always():
    spec = _build()
    assert spec.diagnostic_env == {}
    assert spec.restart_policy == "always"


def test_diagnostic_mode_on_sets_restart_never_and_populates_diagnostic_env():
    # Bug caught: a diagnostic run whose pod RunPod auto-restarts obliterates
    # the snapshot the trap is uploading (C28 A3).
    cfg = load_config(_CFG)
    cfg.diagnostic_mode = True
    spec = _build(cfg=cfg)
    assert spec.restart_policy == "never"
    assert spec.diagnostic_env != {}


def test_cloud_type_defaults_to_any_when_cfg_has_no_compute_block():
    cfg = load_config(_CFG)
    cfg.compute = None
    assert _build(cfg=cfg).cloud_type == "any"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_spec_builder.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kinoforge.core.spec_builder'`

- [ ] **Step 3: Create the module by moving the closure body verbatim**

```python
# src/kinoforge/core/spec_builder.py
"""Build an InstanceSpec from a config plus an engine's rendered provision.

Extracted from the closure that used to live inside ``deploy_session`` so a
spec can be built without running a deploy — the golden launch-payload
snapshot needs exactly that. Pure: no I/O, no orchestrator state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from kinoforge.core.interfaces import InstanceSpec

if TYPE_CHECKING:
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import Lifecycle, Offer, RenderedProvision


def build_instance_spec(
    *,
    cfg: "Config",
    rendered: "RenderedProvision",
    offer: "Offer | None",
    engine_name: str,
    key_hash: str,
    image: str,
    lifecycle: "Lifecycle",
    env: dict[str, str],
    run_id: str,
    tags: dict[str, str] | None = None,
    diagnostic_env: dict[str, str] | None = None,
) -> InstanceSpec:
    """Map a config + rendered provision onto an InstanceSpec.

    Args:
        cfg: The loaded config.
        rendered: The engine's rendered provision payload.
        offer: The chosen offer, or None on paths that do not select one.
        engine_name: Registry name of the resolved engine (goes into tags).
        key_hash: Capability-key hash (goes into tags; warm-reuse matches it).
        image: Fallback image when ``rendered.image`` is empty.
        lifecycle: Effective lifecycle guardrails.
        env: Credential-resolved environment for the instance.
        run_id: Run identifier.
        tags: Caller tags, merged last so they win over the defaults.
        diagnostic_env: Diagnostic overlay; only used when
            ``cfg.diagnostic_mode`` is set.

    Returns:
        The InstanceSpec to hand to ``provider.create_instance``.
    """
    merged_tags: dict[str, str] = {
        "kinoforge_engine": engine_name,
        "kinoforge_key": key_hash,
    }
    if tags:
        merged_tags.update(tags)
    restart_policy: Literal["always", "never"] = (
        "never" if cfg.diagnostic_mode else "always"
    )
    return InstanceSpec(
        image=rendered.image or image,
        offer=offer,
        ports=tuple(rendered.ports),
        lifecycle=lifecycle,
        tags=merged_tags,
        env=dict(env),
        run_id=run_id,
        provision_script=rendered.script,
        image_build_script=(rendered.build_script or None),
        runtime_provision_script=(rendered.runtime_script or None),
        run_cmd=rendered.run_cmd,
        diagnostic_env=(dict(diagnostic_env) if cfg.diagnostic_mode and diagnostic_env else {}),
        restart_policy=restart_policy,
        cloud_type=(cfg.compute.cloud_type if cfg.compute is not None else "any"),
    )
```

- [ ] **Step 4: Rewire both orchestrator call sites**

In `core/orchestrator.py`, replace the closure at `:890-930` with:

```python
    def _build_spec(offer: Offer) -> InstanceSpec:
        return build_instance_spec(
            cfg=cfg,
            rendered=rendered,
            offer=offer,
            engine_name=resolved_engine.name,
            key_hash=key_hash,
            image=image,
            lifecycle=lifecycle,
            env=rendered_env,
            run_id=run_id,
            tags=tags,
            diagnostic_env=_build_diagnostic_env(run_id) if cfg.diagnostic_mode else None,
        )
```

and the bare-`deploy()` closure at `:1695-1709` with the same call, passing
`rendered=RenderedProvision(script="", run_cmd=[], image=image, ports=[], env_required=[])`,
`env={}`, `run_id=""`. Add `from kinoforge.core.spec_builder import build_instance_spec` to the
imports.

- [ ] **Step 5: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_spec_builder.py -q && pixi run test`
Expected: new tests PASS; full suite shows no new failures.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/spec_builder.py \
        src/kinoforge/core/orchestrator.py \
        tests/core/test_spec_builder.py
git commit -m "refactor(core): extract the InstanceSpec builder out of deploy_session"
```

---

## Task 1: Golden launch-payload snapshot (USER GATE)

**Goal:** Capture, for every example config with a compute block, the exact payload each provider
would put on the wire today — so every later S1 task proves equivalence instead of asserting it.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current
> conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or
> by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been
> re-validated independently, with output captured.

**Files:**
- Create: `tools/snapshot_launch_payloads.py`
- Create: `tests/providers/golden/launch_payloads/<config-stem>.json` (one per config)
- Create: `tests/providers/test_launch_payload_goldens.py`

**Acceptance Criteria:**
- [ ] A golden exists for every `examples/configs/*.yaml` that has a `compute:` block; the test
      fails if a config has no golden (so a new config cannot skip the ratchet).
- [ ] The golden captures the PROVIDER payload, not the spec: RunPod's GraphQL
      `variables.input` dict, Modal's `ModalAppRequest` as a dict, SkyPilot's
      `(task_config, launch_kwargs)` pair.
- [ ] Payloads are deterministic: `run_id` pinned to `golden-run`, `time.time` frozen at
      `1756000000.0`, credentials stubbed to `kinoforge-prod-deadbeef`.
- [ ] `pytest tests/providers/test_launch_payload_goldens.py -v` passes on unmodified `main`.
- [ ] Regenerating with `pixi run python tools/snapshot_launch_payloads.py` produces a
      byte-identical tree (idempotent), proven by `git status --short` being clean afterwards.
- [ ] No credential-shaped value appears in any golden — `pixi run python tools/scan_secrets.py`
      over the staged goldens passes.
- [ ] Two goldens (one RunPod, one SkyPilot) read by hand and confirmed non-empty and plausible —
      a golden that captured `{}` passes the test and proves nothing.

**Verify:** `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -v` → all
pass; then `pixi run python tools/snapshot_launch_payloads.py && git status --short` → no diff.

**Steps:**

- [ ] **Step 1: Write the capture harness**

```python
# tools/snapshot_launch_payloads.py
"""Regenerate the golden launch payloads under tests/providers/golden/launch_payloads/.

Run deliberately, never from a test: a regenerated golden is a reviewed act.
Every provider is driven through an injected fake so nothing touches a network.

    pixi run python tools/snapshot_launch_payloads.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path("tests/providers/golden/launch_payloads")
FROZEN_EPOCH = 1756000000.0
GOLDEN_RUN_ID = "golden-run"
STUB_SECRET = "kinoforge-prod-deadbeef"


def capture_payload(config_path: Path) -> dict[str, Any]:
    """Return the wire payload the configured provider would send for *config_path*."""
    from kinoforge.core.config import load_config

    cfg = load_config(str(config_path))
    if cfg.compute is None:
        raise ValueError(f"{config_path} has no compute block")
    provider_kind = cfg.compute.provider
    spec = _spec_for(cfg)
    if provider_kind == "runpod":
        return _capture_runpod(spec)
    if provider_kind == "modal":
        return _capture_modal(spec)
    if provider_kind == "skypilot":
        return _capture_skypilot(spec)
    if provider_kind == "local":
        return {"provider": "local", "spec_image": spec.image}
    raise ValueError(f"unknown provider {provider_kind!r} in {config_path}")


def main() -> int:
    """Write one golden per example config with a compute block."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for cfg_path in sorted(Path("examples/configs").glob("*.yaml")):
        from kinoforge.core.config import load_config

        if load_config(str(cfg_path)).compute is None:
            continue
        payload = capture_payload(cfg_path)
        out = GOLDEN_DIR / f"{cfg_path.stem}.json"
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        written += 1
    print(f"wrote {written} goldens to {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

with the three capture helpers and the shared spec builder:

```python
def _spec_for(cfg: Any) -> Any:
    """Build the InstanceSpec the orchestrator would build, deterministically."""
    from kinoforge.core import registry
    from kinoforge.core.spec_builder import build_instance_spec

    engine = registry.get_engine(cfg.engine.kind)()
    rendered = engine.render_provision(cfg.model_dump())
    offers = _catalog_offer(cfg)
    return build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        offer=offers,
        engine_name=engine.name,
        key_hash="golden-key",
        image=cfg.compute.image,
        lifecycle=cfg.lifecycle(),
        env={name: STUB_SECRET for name in rendered.env_required},
        run_id=GOLDEN_RUN_ID,
        tags=None,
    )


def _catalog_offer(cfg: Any) -> Any:
    """Pick the first offer the provider's own catalog would return, offline.

    RunPod and SkyPilot enumerate over a network; both are given the frozen
    synthetic offer below so the golden pins the SPEC->PAYLOAD mapping, which
    is what S1 changes. Offer selection itself is S4's subject.
    """
    from kinoforge.core.interfaces import Offer

    prefs = cfg.compute.requirements.gpu_preference
    gpu = prefs[0] if prefs else "NVIDIA A100 80GB PCIe"
    return Offer(
        id=gpu, gpu_type=gpu, vram_gb=80, cuda="12.4", cost_rate_usd_per_hr=1.64
    )


def _capture_runpod(spec: Any) -> dict[str, Any]:
    from kinoforge.providers.runpod import RunPodProvider

    captured: list[dict[str, Any]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append(body)
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-golden"}}}

    RunPodProvider(api_key=STUB_SECRET, http_post=_http_post).create_instance(spec)
    return {"provider": "runpod", "input": captured[0]["variables"]["input"]}


def _capture_modal(spec: Any) -> dict[str, Any]:
    import dataclasses

    from kinoforge.providers.modal import ModalProvider

    captured: list[Any] = []

    def _app_factory(req: Any, modal_mod: Any) -> tuple[Any, Any]:
        captured.append(req)
        return object(), object()

    ModalProvider(
        app_factory=_app_factory,
        deployer=lambda app, fn: "https://golden--x.modal.run",
        modal_mod=lambda: object(),
        clock=lambda: FROZEN_EPOCH,
    ).create_instance(spec)
    return {"provider": "modal", "request": dataclasses.asdict(captured[0])}


def _capture_skypilot(spec: Any) -> dict[str, Any]:
    from unittest import mock

    from kinoforge.providers.skypilot import SkyPilotProvider

    captured: dict[str, Any] = {}

    class _FakeSky:
        def launch(self, task: Any, **kwargs: Any) -> Any:
            captured["task"] = task.to_yaml_config()
            captured["launch_kwargs"] = {k: str(v) for k, v in kwargs.items()}
            raise _StopLaunch()

    with mock.patch("kinoforge.providers.skypilot.time.time", return_value=FROZEN_EPOCH):
        try:
            SkyPilotProvider(sky_client=_FakeSky()).create_instance(spec)
        except _StopLaunch:
            pass
    return {"provider": "skypilot", **captured}


class _StopLaunch(Exception):
    """Abort the launch once the payload has been captured."""
```

> Implementation note for the executor: the exact constructor kwargs above
> (`RunPodProvider(api_key=…, http_post=…)`, `ModalProvider(app_factory=…, deployer=…)`,
> `SkyPilotProvider(sky_client=…)`) must be checked against each provider's `__init__` and the
> existing fakes in `tests/providers/test_runpod_create_pod_cloud_type.py`,
> `tests/providers/modal/test_provider.py` and `tests/providers/test_skypilot.py`, and adjusted to
> match. If SkyPilot's fake needs `sky.Task.from_yaml_config`, copy the DummySky shape from
> `tests/providers/test_skypilot.py` rather than inventing one. Do NOT change provider code to
> make capture easier — if a provider has no injection seam for something, capture at the nearest
> seam that exists and record that in the golden's `provider` key.

- [ ] **Step 2: Write the byte-identity test**

```python
# tests/providers/test_launch_payload_goldens.py
"""Behavior: the wire payload for every example config is frozen.

This is the S1..S5 ratchet. The compute-seam rework changes the SHAPE of
ComputeConfig and InstanceSpec; it must not change what any provider actually
sends. A stage that silently drops cloud_type, reorders env, or loses the
provision script fails here rather than on an invoice.

Regenerate deliberately with `pixi run python tools/snapshot_launch_payloads.py`
and review the diff — never regenerate to make this test pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from tools.snapshot_launch_payloads import capture_payload

_GOLDEN_DIR = Path("tests/providers/golden/launch_payloads")


def _compute_configs() -> list[Path]:
    return [
        p
        for p in sorted(Path("examples/configs").glob("*.yaml"))
        if load_config(str(p)).compute is not None
    ]


@pytest.mark.parametrize("cfg_path", _compute_configs(), ids=lambda p: p.stem)
def test_launch_payload_matches_golden(cfg_path: Path) -> None:
    golden_path = _GOLDEN_DIR / f"{cfg_path.stem}.json"
    assert golden_path.exists(), (
        f"no golden for {cfg_path}; run tools/snapshot_launch_payloads.py "
        "and review the new file before committing"
    )
    assert capture_payload(cfg_path) == json.loads(golden_path.read_text())


def test_no_orphan_goldens() -> None:
    # Bug caught: a config is deleted/renamed and its golden lingers, so the
    # ratchet silently stops covering it.
    stems = {p.stem for p in _compute_configs()}
    orphans = {g.stem for g in _GOLDEN_DIR.glob("*.json")} - stems
    assert not orphans, f"goldens with no config: {sorted(orphans)}"


def test_goldens_carry_no_credential_shapes() -> None:
    # Bug caught: a real token reaches a committed fixture. Belt to the
    # pre-commit scanner's braces.
    from kinoforge.core.credential_patterns import CREDENTIAL_PATTERNS

    for g in _GOLDEN_DIR.glob("*.json"):
        text = g.read_text()
        for name, pattern in CREDENTIAL_PATTERNS.items():
            assert not pattern.search(text), f"{g.name} matches {name}"
```

> Note: `CREDENTIAL_PATTERNS` is a real export of
> `src/kinoforge/core/credential_patterns.py`; confirm whether the strict or loose tier is the
> right mapping name at implementation time and use the strict tier (the blocking one).

- [ ] **Step 3: Run — expect failure, then generate**

Run: `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q`
Expected: FAIL — "no golden for examples/configs/…"

Run: `pixi run python tools/snapshot_launch_payloads.py`
Expected: `wrote N goldens to tests/providers/golden/launch_payloads`

- [ ] **Step 4: Re-run and verify idempotence**

Run: `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -v`
Expected: all PASS

Run: `pixi run python tools/snapshot_launch_payloads.py && git status --short`
Expected: no modified goldens (only untracked-then-added ones from the first generation)

- [ ] **Step 5: Read a golden and sanity-check it**

Open two goldens by hand — one RunPod, one SkyPilot — and confirm the payload contains what you
expect (`cloudType`, `dockerArgs`, `env`, `ports` for RunPod; `resources`, `setup`, `run` for
SkyPilot). A golden that captured an empty dict passes the test and proves nothing.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add tools/snapshot_launch_payloads.py \
        tests/providers/test_launch_payload_goldens.py \
        tests/providers/golden/launch_payloads
git commit -m "test(providers): snapshot every example config's launch payload"
```

---

## Task 2: `backend_options` plumbing and per-provider Options models

**Goal:** Add the namespaced escape hatch and its validation, with no provider consuming it yet —
so the mechanism lands and is tested before anything depends on it.

**Files:**
- Modify: `src/kinoforge/core/config.py` (`ComputeConfig.backend_options`,
  `Config.backend_options_for`)
- Modify: `src/kinoforge/core/interfaces.py` (`InstanceSpec.backend_options`)
- Modify: `src/kinoforge/providers/runpod/__init__.py`,
  `src/kinoforge/providers/skypilot/__init__.py`,
  `src/kinoforge/providers/modal/__init__.py`,
  `src/kinoforge/providers/local/__init__.py` (add `Options`)
- Test: `tests/core/test_backend_options.py`

**Acceptance Criteria:**
- [ ] `compute.backend_options` accepts a mapping of provider-name → options mapping.
- [ ] An unknown key inside a namespace raises `ConfigError` naming the provider, the key, and the
      accepted keys.
- [ ] An unknown provider name as a namespace key raises `ConfigError`.
- [ ] Every registered provider class exposes `Options` (a pydantic model with
      `model_config = ConfigDict(extra="forbid")`) and a `validate_options` classmethod.
- [ ] `Config.backend_options_for("runpod")` returns the validated model for the runpod namespace,
      with defaults applied when the namespace is absent.
- [ ] Golden payloads unchanged.

**Verify:** `pixi run python -m pytest tests/core/test_backend_options.py tests/providers/test_launch_payload_goldens.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_backend_options.py
"""Behavior: compute.backend_options is namespaced and validated by its owner.

The whole point of the compute-seam rework: a field set for the wrong provider
must be loud. A typo'd key inside a namespace, or a namespace for a provider
that does not exist, is a misconfiguration the operator should learn about at
load time rather than from an invoice.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError


def _cfg(backend_options: dict) -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers"},
            "spec": {"model": "m", "precision": "bf16"},
            "models": [{"kind": "base", "ref": "hf:org/repo"}],
            "compute": {
                "provider": "runpod",
                "image": "img:tag",
                "backend_options": backend_options,
            },
        }
    )


def test_known_key_in_the_selected_namespace_loads():
    cfg = _cfg({"runpod": {"cloud_type": "secure"}})
    assert cfg.backend_options_for("runpod").cloud_type == "secure"


def test_unknown_key_in_a_namespace_is_rejected_and_names_the_key():
    # Bug caught: `cloudtype: secure` silently does nothing and the pod lands
    # on a community host that deletes it mid-run (2026-07-03).
    with pytest.raises(ConfigError) as exc:
        _cfg({"runpod": {"cloudtype": "secure"}})
    assert "cloudtype" in str(exc.value)
    assert "cloud_type" in str(exc.value)  # names the accepted keys


def test_unknown_provider_namespace_is_rejected():
    # Bug caught: `runpid:` reads as "options for a provider I did not select"
    # and is discarded, which is precisely the F5 failure mode.
    with pytest.raises(ConfigError) as exc:
        _cfg({"runpid": {"cloud_type": "secure"}})
    assert "runpid" in str(exc.value)


def test_a_namespace_for_an_unselected_provider_still_validates_its_shape():
    # Bug caught: the typo hides in the block you are not currently running.
    with pytest.raises(ConfigError):
        _cfg({"skypilot": {"cluods": ["lambda"]}})


def test_absent_namespace_yields_defaults_not_none():
    cfg = _cfg({})
    assert cfg.backend_options_for("runpod").cloud_type == "any"


def test_options_models_forbid_extra_on_every_registered_provider():
    from kinoforge.core import registry

    for name in registry.provider_names():
        cls = registry.provider_class(name)
        assert cls is not None and hasattr(cls, "Options"), name
        assert cls.Options.model_config.get("extra") == "forbid", name
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_backend_options.py -q`
Expected: FAIL — `ComputeConfig` has no `backend_options`

- [ ] **Step 3: Implement**

In each provider module (example shown for RunPod; SkyPilot gets `clouds` + `retry_until_up`;
Modal and Local get empty models that still forbid extras):

```python
class RunPodProvider(ComputeProvider):
    class Options(BaseModel):
        """Options only RunPod honours. Unknown keys are a config error."""

        model_config = ConfigDict(extra="forbid")

        cloud_type: Literal["any", "secure", "community"] = "any"
        restart_policy: Literal["always", "never"] = "always"
        capacity_wait_s: float = 300.0

    @classmethod
    def validate_options(cls, raw: Mapping[str, Any]) -> "RunPodProvider.Options":
        """Parse *raw* into this provider's Options, forbidding unknown keys."""
        return cls.Options.model_validate(dict(raw))
```

In `core/config.py`:

```python
    backend_options: dict[str, dict[str, Any]] = {}

    @field_validator("backend_options")
    @classmethod
    def _validate_backend_options(
        cls, v: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Reject unknown provider namespaces and unknown keys within one.

        Validation is delegated to the OWNING provider class so the accepted
        key set lives next to the code that reads it — the alternative is a
        second list in core that drifts from the first.
        """
        from kinoforge.core import registry
        from kinoforge.core.errors import ConfigError

        for provider_name, raw in v.items():
            cls_ = registry.provider_class(provider_name)
            if cls_ is None:
                raise ConfigError(
                    f"compute.backend_options.{provider_name}: unknown provider; "
                    f"registered providers are {sorted(registry.provider_names())}"
                )
            validate = getattr(cls_, "validate_options", None)
            if validate is None:
                raise ConfigError(
                    f"compute.backend_options.{provider_name}: provider declares "
                    "no Options schema; it accepts no backend options"
                )
            try:
                validate(raw)
            except PydanticValidationError as exc:
                accepted = sorted(cls_.Options.model_fields)
                raise ConfigError(
                    f"compute.backend_options.{provider_name}: {exc.errors()[0]['msg']} "
                    f"(accepted keys: {accepted})"
                ) from exc
        return v
```

and on `Config`:

```python
    def backend_options_for(self, provider_name: str) -> Any:
        """Return the validated Options model for *provider_name*.

        Defaults are applied when the namespace is absent, so callers never
        branch on presence.
        """
        from kinoforge.core import registry

        cls_ = registry.provider_class(provider_name)
        if cls_ is None or not hasattr(cls_, "validate_options"):
            raise ConfigError(f"unknown provider {provider_name!r}")
        raw = {} if self.compute is None else self.compute.backend_options.get(provider_name, {})
        return cls_.validate_options(raw)
```

Add `backend_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)` to
`InstanceSpec`, and have `build_instance_spec` populate it from
`cfg.compute.backend_options` (empty when `cfg.compute is None`).

> Import-ban note: `core/config.py` importing `kinoforge.core.registry` is fine — the ban is on
> `kinoforge.core.*` importing `kinoforge.providers.*`, and the registry holds classes registered
> by the providers themselves at import time. Keep the import function-local, matching
> `capabilities.py`'s `_provider_class`.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_backend_options.py tests/providers/test_launch_payload_goldens.py -q`
Expected: all PASS (goldens unchanged — nothing consumes the namespace yet)

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/config.py src/kinoforge/core/interfaces.py \
        src/kinoforge/core/spec_builder.py src/kinoforge/providers \
        tests/core/test_backend_options.py
git commit -m "feat(config): add provider-namespaced backend_options with owner-side validation"
```

---

## Task 3: Move `cloud`, `cloud_type`, `restart_policy` and `capacity_wait_s` into the namespaces

**Goal:** Delete the four vendor-specific fields from the portable surface, wire their namespace
equivalents into the providers, and migrate the 8 example configs — all in one commit, so no
half-migrated state exists.

**Files:**
- Modify: `src/kinoforge/core/config.py` (delete `ComputeConfig.cloud`, `cloud_type`,
  `_validate_cloud`; add the removal errors)
- Modify: `src/kinoforge/core/interfaces.py` (delete `InstanceSpec.cloud_type`,
  `InstanceSpec.restart_policy`, `Lifecycle.capacity_wait_s`)
- Modify: `src/kinoforge/core/spec_builder.py`, `src/kinoforge/core/orchestrator.py`
- Modify: `src/kinoforge/_adapters.py` (skypilot namespace; new `build_capacity_wait_for`)
- Modify: `src/kinoforge/providers/runpod/__init__.py` (read the runpod namespace off the spec)
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (`SkyPilotCloudPinSupportedCheck` reads
  the namespace)
- Modify: 8 example configs (3 with `cloud:`, 5 with `cloud_type:`)
- Test: `tests/core/test_legacy_compute_fields_removed.py`, plus updates to
  `tests/providers/test_runpod_create_pod_cloud_type.py`,
  `tests/providers/test_runpod_create_pod_restart_policy.py`,
  `tests/providers/skypilot/test_cloud_pin_check.py`

**Acceptance Criteria:**
- [ ] `compute.cloud` in a YAML raises `ConfigError` naming
      `compute.backend_options.skypilot.clouds`.
- [ ] `compute.cloud_type` raises `ConfigError` naming
      `compute.backend_options.runpod.cloud_type`.
- [ ] RunPod's `cloudType` and `restartPolicy` wire fields come from
      `spec.backend_options["runpod"]`.
- [ ] SkyPilot's cloud pin comes from `compute.backend_options.skypilot.clouds`, and
      `retry_until_up` is reachable from the same namespace (closing half of F6).
- [ ] Capacity-wait retry applies to RunPod only, sourced from its namespace. **Intended
      behaviour change:** skypilot and modal no longer wrap create in the capacity-wait loop;
      SkyPilot's equivalent is its own `retry_until_up`. Recorded here so the diff is not
      mistaken for a regression.
- [ ] All 8 configs migrated; `rg '^\s*(cloud|cloud_type):' examples/configs/` returns nothing.
- [ ] Golden payloads unchanged.

**Verify:** `pixi run python -m pytest tests/core/test_legacy_compute_fields_removed.py tests/providers -q && pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q`

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_legacy_compute_fields_removed.py
"""Behavior: the vendor-specific compute keys are gone, and their removal is loud.

A silent drop would be worse than the F5 status quo: the operator's pin would
stop working with no message at all. Each removed key must name its
replacement path.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError

_BASE = {
    "engine": {"kind": "diffusers"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo"}],
}


def _load(compute: dict) -> Config:
    return Config.model_validate({**_BASE, "compute": compute})


def test_legacy_cloud_key_names_its_replacement():
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "skypilot", "image": "i", "cloud": ["lambda"]})
    assert "compute.backend_options.skypilot.clouds" in str(exc.value)


def test_legacy_cloud_type_key_names_its_replacement():
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "runpod", "image": "i", "cloud_type": "secure"})
    assert "compute.backend_options.runpod.cloud_type" in str(exc.value)


def test_capacity_wait_is_runpod_scoped():
    # Bug caught: capacity_wait_s stays on Lifecycle and every provider keeps
    # paying a RunPod-shaped retry loop, which is what made it look portable.
    from kinoforge.core.interfaces import Lifecycle

    assert not hasattr(Lifecycle(), "capacity_wait_s")


def test_runpod_capacity_wait_reaches_the_orchestrator_from_the_namespace():
    from kinoforge._adapters import build_capacity_wait_for

    cfg = _load(
        {
            "provider": "runpod",
            "image": "i",
            "backend_options": {"runpod": {"capacity_wait_s": 42.0}},
        }
    )
    assert build_capacity_wait_for(cfg) == 42.0


def test_non_runpod_provider_gets_no_capacity_wait():
    from kinoforge._adapters import build_capacity_wait_for

    assert build_capacity_wait_for(_load({"provider": "skypilot", "image": "i"})) == 0.0
```

Update the two RunPod wire tests to build the spec with
`backend_options={"runpod": {"cloud_type": "secure"}}` (respectively
`{"restart_policy": "never"}`) instead of the deleted spec fields, keeping their existing
assertions on `cloudType` / `restartPolicy` in the mutation input.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/core/test_legacy_compute_fields_removed.py -q`
Expected: FAIL — `cloud` is still an accepted field, `build_capacity_wait_for` does not exist

- [ ] **Step 3: Implement the removals + the loud errors**

In `ComputeConfig`, delete `cloud`, `cloud_type` and `_validate_cloud`, then add:

```python
    _REMOVED_KEYS: ClassVar[dict[str, str]] = {
        "cloud": "compute.backend_options.skypilot.clouds",
        "cloud_type": "compute.backend_options.runpod.cloud_type",
    }

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_keys(cls, data: Any) -> Any:
        """Refuse the pre-S1 vendor keys, naming where each one moved.

        Deliberately not an alias: keeping both paths alive is how a
        half-migrated seam survives across stages.
        """
        from kinoforge.core.errors import ConfigError

        if isinstance(data, dict):
            for key, replacement in cls._REMOVED_KEYS.items():
                if key in data:
                    raise ConfigError(
                        f"compute.{key} was removed in the compute-seam rework; "
                        f"it now lives at {replacement}"
                    )
        return data
```

In `providers/runpod/__init__.py`'s `_create_pod`, replace `spec.cloud_type` / `spec.restart_policy`
reads with:

```python
        opts = RunPodProvider.validate_options(spec.backend_options.get("runpod", {}))
```

and use `opts.cloud_type` / `opts.restart_policy` at the existing wire sites (the `cloudType`
mapping and the schema-probed `restartPolicy` branch) — behaviour and defaults unchanged.

In `_adapters.py`:

```python
def build_capacity_wait_for(cfg: "Config") -> float:
    """Return the create-retry window, in seconds, for the configured provider.

    Capacity-miss retry is a RunPod behaviour: its `find_offers` lists an offer
    that can vanish before create. SkyPilot's equivalent is `retry_until_up`,
    which its own optimizer honours, and Modal handles scheduling itself. A
    provider that declares no capacity_wait_s gets 0.0 — fail on the first miss.
    """
    if cfg.compute is None or cfg.compute.provider != "runpod":
        return 0.0
    return float(cfg.backend_options_for("runpod").capacity_wait_s)
```

and in `build_provider_for`, replace the `cfg.compute.cloud` branch with a read of
`cfg.backend_options_for("skypilot")`, setting `provider._clouds` and `provider._retry_until_up`.

In `core/orchestrator.py`, replace `capacity_wait_s=lifecycle.capacity_wait_s` with a
`capacity_wait_s` argument threaded in from the caller (the CLI / `deploy_session` signature),
defaulting to `0.0`; the composition root passes `build_capacity_wait_for(cfg)`.

- [ ] **Step 4: Migrate the 8 configs**

```bash
rg -l '^\s*(cloud|cloud_type):' examples/configs/
```

For each, move the key under a `backend_options` block, e.g.:

```yaml
# examples/configs/skypilot-lambda-comfyui.yaml
compute:
  provider: skypilot
  backend_options:
    skypilot:
      clouds: [lambda]
```

```yaml
# examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml
compute:
  provider: runpod
  backend_options:
    runpod:
      cloud_type: secure
```

- [ ] **Step 5: Run everything**

Run: `pixi run python -m pytest tests/core/test_legacy_compute_fields_removed.py tests/providers -q`
Expected: PASS

Run: `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q`
Expected: PASS with no golden regenerated. If a golden diffs, the migration changed the wire —
stop and fix the code, do NOT regenerate.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(compute): move cloud, cloud_type, restart_policy and capacity_wait into provider namespaces"
```

---

## Task 4: `Placement` replaces `requirements`

**Goal:** Give the portable core its resource block, move `spot` onto it, and move `min_cuda` —
which only RunPod can constrain at selection time — into the RunPod namespace.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`Placement`; delete `InstanceSpec.spot`;
  `HardwareRequirements` stays until S4)
- Modify: `src/kinoforge/core/config.py` (`PlacementConfig`; delete `RequirementsConfig`;
  `Config.placement()`; `Config.hardware_requirements()` derives from placement)
- Modify: `src/kinoforge/core/spec_builder.py` (populate `spec.placement`)
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (`use_spot` from
  `spec.placement.spot`)
- Modify: `src/kinoforge/cli/_commands.py` (the `kinoforge offers` spec construction)
- Modify: every `examples/configs/*.yaml` with a `requirements:` block
- Test: `tests/core/test_placement.py`

**Acceptance Criteria:**
- [ ] `Placement` exists with `accelerators`, `accelerator_count`, `min_vram_gb`, `disk_gb`,
      `spot`, `max_usd_per_hr`; defaults match today's `HardwareRequirements` so no payload moves.
- [ ] `compute.placement` is the YAML surface; `compute.requirements` raises `ConfigError` naming
      it.
- [ ] `gpu_preference` is renamed `accelerators` in the config surface, and
      `Config.hardware_requirements()` sources `gpu_preference` from it (S4 deletes that shim).
- [ ] `min_cuda` stays PORTABLE, on `Placement`. **Plan correction, 2026-08-26, operator ruling:**
      the design claimed only RunPod can constrain CUDA at selection time. That is wrong about
      kinoforge — `core/offers.py::filter_offers` applies `min_cuda` client-side to the catalog of
      every enumerating provider, and SkyPilot's catalog stamps every offer `cuda="12.0"`, so a
      RunPod-namespaced default of `"12.8"` empties it and turns `skypilot-gpu` / `-lambda` /
      `-vast` into `CapacityError`. It is a catalog-filter concept, so S4 deletes it with the rest
      of the marketplace path; until then all three enumerating providers declare it CONSUMED.
      No skypilot or modal config may carry a `backend_options.runpod` block.
- [ ] `InstanceSpec.spot` is gone; SkyPilot reads `spec.placement.spot`.
- [ ] Golden payloads unchanged.

**Verify:** `pixi run python -m pytest tests/core/test_placement.py tests/providers/test_launch_payload_goldens.py -q`

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_placement.py
"""Behavior: compute.placement is the portable resource block.

`requirements` described a catalog filter. `placement` describes what to get,
which is the thing every provider can honour. The rename is not cosmetic: two
of the five old keys change owner, so keeping the old name over new semantics
would leave the config surface lying.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError

_BASE = {
    "engine": {"kind": "diffusers"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo"}],
}


def _load(compute: dict) -> Config:
    return Config.model_validate({**_BASE, "compute": compute})


def test_placement_block_populates_the_placement_object():
    cfg = _load(
        {
            "provider": "runpod",
            "image": "i",
            "placement": {
                "accelerators": ["NVIDIA A100 80GB PCIe"],
                "min_vram_gb": 80,
                "disk_gb": 200,
                "spot": True,
                "max_usd_per_hr": 1.75,
            },
        }
    )
    p = cfg.placement()
    assert p.accelerators == ("NVIDIA A100 80GB PCIe",)
    assert (p.min_vram_gb, p.disk_gb, p.spot, p.max_usd_per_hr) == (80, 200, True, 1.75)


def test_placement_defaults_match_the_old_requirements_defaults():
    # Bug caught: a changed default silently re-prices or re-sizes every config
    # that did not set the block. The goldens would catch it too; this names it.
    p = _load({"provider": "runpod", "image": "i"}).placement()
    assert (p.min_vram_gb, p.disk_gb, p.max_usd_per_hr, p.spot) == (48, 100, 2.20, False)
    assert p.accelerators == ()
    assert p.accelerator_count == 1


def test_legacy_requirements_block_names_its_replacement():
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "runpod", "image": "i", "requirements": {"min_vram_gb": 48}})
    assert "compute.placement" in str(exc.value)


def test_min_cuda_under_placement_is_rejected_and_points_at_the_namespace():
    # Bug caught: min_cuda looks portable but only RunPod can filter on it, so
    # under placement it would be an ignored field of exactly the kind S1 exists
    # to delete.
    with pytest.raises(ConfigError) as exc:
        _load({"provider": "runpod", "image": "i", "placement": {"min_cuda": "12.8"}})
    assert "compute.backend_options.runpod.min_cuda" in str(exc.value)


def test_hardware_requirements_shim_sources_gpu_preference_from_accelerators():
    # The shim keeps find_offers working until S4 deletes it.
    cfg = _load({"provider": "runpod", "image": "i", "placement": {"accelerators": ["H100"]}})
    assert cfg.hardware_requirements().gpu_preference == ("H100",)


def test_instance_spec_no_longer_carries_spot():
    from kinoforge.core.interfaces import InstanceSpec

    assert "spot" not in InstanceSpec.__dataclass_fields__
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_placement.py -q`
Expected: FAIL — `Config` has no `placement`

- [ ] **Step 3: Implement**

```python
# src/kinoforge/core/interfaces.py
@dataclass(frozen=True)
class Placement:
    """What to get. Not which SKU to book.

    Defaults deliberately match the pre-S1 HardwareRequirements defaults so a
    config that set no block launches exactly what it launched before.
    """

    accelerators: tuple[str, ...] = ()
    accelerator_count: int = 1
    min_vram_gb: int = 48
    disk_gb: int = 100
    spot: bool = False
    max_usd_per_hr: float = 2.20
```

```python
# src/kinoforge/core/config.py
class PlacementConfig(BaseModel):
    """The portable resource block. See ComputeConfig.placement."""

    model_config = ConfigDict(extra="forbid")

    accelerators: list[str] = []
    accelerator_count: int = 1
    min_vram_gb: int = 48
    disk_gb: int = 100
    spot: bool = False
    max_usd_per_hr: float = 2.20
```

Add `placement: PlacementConfig = PlacementConfig()` to `ComputeConfig`, extend `_REMOVED_KEYS`
with `"requirements": "compute.placement"`, and special-case `min_cuda` inside `PlacementConfig`
so the error names `compute.backend_options.runpod.min_cuda` rather than the generic
`extra="forbid"` message:

```python
    @model_validator(mode="before")
    @classmethod
    def _reject_min_cuda(cls, data: Any) -> Any:
        from kinoforge.core.errors import ConfigError

        if isinstance(data, dict) and "min_cuda" in data:
            raise ConfigError(
                "compute.placement.min_cuda is not portable — only RunPod can "
                "constrain the CUDA version at selection time; it now lives at "
                "compute.backend_options.runpod.min_cuda"
            )
        return data
```

Add `min_cuda: str = "12.8"` to `RunPodProvider.Options`. Rewrite
`Config.hardware_requirements()` to source from `placement()` plus
`backend_options_for("runpod").min_cuda` (guarded: use the default `"12.8"` when the provider is
not runpod). Add `placement: Placement = field(default_factory=Placement)` to `InstanceSpec`,
delete `InstanceSpec.spot`, populate placement in `build_instance_spec`, and change SkyPilot's
`if spec.spot:` to `if spec.placement.spot:`.

- [ ] **Step 4: Migrate the configs**

```bash
rg -l '^\s*requirements:' examples/configs/
```

Rename each block to `placement:`, rename `gpu_preference:` to `accelerators:`, and move any
`min_cuda:` to `backend_options.runpod.min_cuda`.

- [ ] **Step 5: Run**

Run: `pixi run python -m pytest tests/core/test_placement.py -q && pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q`
Expected: PASS, goldens unchanged

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "feat(config): replace compute.requirements with the portable placement block"
```

---

## Task 5: `consumes()` declarations and the parity guard

**Goal:** Make it impossible to add a field that some provider silently ignores.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`FieldSupport`, `ComputeProvider.consumes`)
- Modify: all four provider modules (declare)
- Test: `tests/providers/test_field_consumption_parity.py`

**Acceptance Criteria:**
- [ ] `FieldSupport` is a `StrEnum` with `CONSUMED` and `UNSUPPORTED`.
- [ ] `ComputeProvider.consumes()` defaults to `{}` — an undeclared provider claims nothing.
- [ ] Every registered provider declares exactly the portable field set: the fields of
      `Placement` plus `image`, `ports`, `volume_gb`, `volume_mount`, `env`, `tags`, `run_id`,
      `provision_script`, `run_cmd`, `image_build_script`, `runtime_provision_script`,
      `lifecycle`, `offer`, `backend_options`, `diagnostic_env`.
- [ ] The guard test fails when a portable field is added without a declaration on every provider,
      AND when a field is removed but a stale declaration remains.
- [ ] Every `CONSUMED` declaration has a wire proof in `_WIRE_PROOFS` that observes the field in a
      captured payload; a `CONSUMED` claim with no proof fails the test.
- [ ] `max_usd_per_hr` is declared `UNSUPPORTED` on skypilot — the honest statement of F4 until
      S4 wires the realized-rate verification.
- [ ] The guard is observed failing: temporarily add `foo` to `Placement`, capture the failure
      message, revert, and quote it in the commit body.

**Verify:** `pixi run python -m pytest tests/providers/test_field_consumption_parity.py -v`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/test_field_consumption_parity.py
"""Behavior: every portable field is declared by every provider, and CONSUMED is proven.

This is the regression guard the whole compute-seam rework exists to install.
F5 counted 10 of 17 InstanceSpec fields ignored by at least one provider, with
nothing anywhere that noticed. After this test, adding a field without deciding
what each provider does with it breaks the suite, and claiming to consume a
field you drop on the floor breaks it too.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

import pytest

from kinoforge.core import registry
from kinoforge.core.interfaces import FieldSupport, InstanceSpec, Placement

_SPEC_PORTABLE = {
    "image",
    "ports",
    "volume_gb",
    "volume_mount",
    "env",
    "tags",
    "run_id",
    "provision_script",
    "run_cmd",
    "image_build_script",
    "runtime_provision_script",
    "lifecycle",
    "offer",
    "backend_options",
    "diagnostic_env",
}


def _expected_fields() -> set[str]:
    placement = {f.name for f in dataclasses.fields(Placement)}
    spec = {f.name for f in dataclasses.fields(InstanceSpec)} & _SPEC_PORTABLE
    return placement | spec


@pytest.mark.parametrize("provider_name", sorted(registry.provider_names()))
def test_declaration_covers_exactly_the_portable_field_set(provider_name: str) -> None:
    cls = registry.provider_class(provider_name)
    assert cls is not None
    declared = dict(cls.consumes())
    expected = _expected_fields()
    missing = expected - set(declared)
    stale = set(declared) - expected
    assert not missing, (
        f"{provider_name} does not declare {sorted(missing)}; a field nobody "
        "declares is a field that can be silently ignored"
    )
    assert not stale, f"{provider_name} declares removed fields {sorted(stale)}"
    assert all(isinstance(v, FieldSupport) for v in declared.values())


def test_spec_portable_set_is_not_stale() -> None:
    # Bug caught: a field is deleted from InstanceSpec, _SPEC_PORTABLE keeps
    # naming it, and the intersection above quietly stops requiring it.
    spec_fields = {f.name for f in dataclasses.fields(InstanceSpec)}
    assert _SPEC_PORTABLE <= spec_fields, sorted(_SPEC_PORTABLE - spec_fields)


# field -> callable(payload) -> bool, per provider. A CONSUMED declaration
# without an entry here is an unproven claim.
_WIRE_PROOFS: dict[str, dict[str, Callable[[dict[str, Any]], bool]]] = {
    "runpod": {
        "image": lambda p: p["input"]["imageName"] != "",
        "ports": lambda p: "ports" in p["input"],
        "env": lambda p: "env" in p["input"],
        "provision_script": lambda p: "dockerArgs" in p["input"],
        "volume_gb": lambda p: "volumeInGb" in p["input"],
        "backend_options": lambda p: "cloudType" in p["input"],
        # ... one entry per CONSUMED field; see Step 3
    },
}


@pytest.mark.parametrize("provider_name", sorted(registry.provider_names()))
def test_every_consumed_field_has_a_wire_proof(provider_name: str) -> None:
    cls = registry.provider_class(provider_name)
    assert cls is not None
    consumed = {f for f, s in cls.consumes().items() if s is FieldSupport.CONSUMED}
    proofs = set(_WIRE_PROOFS.get(provider_name, {}))
    unproven = consumed - proofs
    assert not unproven, (
        f"{provider_name} claims to consume {sorted(unproven)} with no wire "
        "proof; add one to _WIRE_PROOFS or declare the field UNSUPPORTED"
    )


def test_wire_proofs_hold_against_a_captured_payload() -> None:
    from tools.snapshot_launch_payloads import capture_payload
    from pathlib import Path

    payload = capture_payload(Path("examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml"))
    for field, proof in _WIRE_PROOFS["runpod"].items():
        assert proof(payload), f"runpod declares {field} CONSUMED but the payload lacks it"
```

> Executor note: `_WIRE_PROOFS` is written out for runpod in Step 1 as the worked example.
> Step 3 requires the same treatment for skypilot and modal against their own captured payloads
> (`resources` / `setup` / `run` / `envs` for skypilot; the `ModalAppRequest` fields for modal),
> and `test_wire_proofs_hold_against_a_captured_payload` parametrized over one representative
> config per provider. Local declares everything except `tags` UNSUPPORTED and therefore needs no
> proofs.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/providers/test_field_consumption_parity.py -q`
Expected: FAIL — `AttributeError`/empty declaration for every provider

- [ ] **Step 3: Implement `FieldSupport`, `consumes()` and the four declarations**

```python
# src/kinoforge/core/interfaces.py
class FieldSupport(StrEnum):
    """Whether a provider honours a portable field.

    CONSUMED    — read and applied on the wire; a parity test proves it.
    UNSUPPORTED — cannot be honoured. Setting it to a non-default is a
                  config-load ERROR, never a silent discard.
    """

    CONSUMED = "consumed"
    UNSUPPORTED = "unsupported"


class ComputeProvider(ABC):
    @classmethod
    def consumes(cls) -> Mapping[str, FieldSupport]:
        """Declare, per portable field, whether this provider reads it.

        Default empty on purpose, matching ``capabilities()``: a provider that
        has not declared claims nothing, and validation refuses it loudly
        instead of trusting it.
        """
        return {}
```

Declarations follow the F5 sweep (verification doc, "Counts" table), corrected for the S1 moves.
SkyPilot, for example:

```python
    @classmethod
    def consumes(cls) -> Mapping[str, FieldSupport]:
        """SkyPilot honours the resource constraints and the setup/run pair.

        ports/volume_*: SkyPilot's tunnel is opened by kinoforge, not declared
        to sky, and no volume is attached — declaring them CONSUMED would be
        the exact lie this table exists to prevent.
        """
        c, u = FieldSupport.CONSUMED, FieldSupport.UNSUPPORTED
        return {
            "accelerators": c, "accelerator_count": c, "min_vram_gb": u,
            "disk_gb": c, "spot": c, "max_usd_per_hr": u,
            "image": c, "env": c, "tags": c, "run_id": c, "lifecycle": c,
            "provision_script": c, "run_cmd": c, "offer": c,
            "backend_options": c,
            "ports": u, "volume_gb": u, "volume_mount": u,
            "image_build_script": u, "runtime_provision_script": u,
            "diagnostic_env": u,
        }
```

> `max_usd_per_hr` is `UNSUPPORTED` on skypilot in S1 and *stays* that way until S4 wires the
> realized-rate verification — which is exactly the honest statement of F4. RunPod and Modal
> declare it `CONSUMED` (their catalog filter applies it).

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/providers/test_field_consumption_parity.py -v`
Expected: all PASS

- [ ] **Step 5: Prove the guard actually guards**

Temporarily add a field `foo: int = 0` to `Placement`, re-run the test, and confirm it FAILS with
"does not declare ['foo']". Then remove the field. Record the observed failure message in the
commit body — a guard nobody has seen fail is a guard nobody has tested.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/interfaces.py src/kinoforge/providers \
        tests/providers/test_field_consumption_parity.py
git commit -m "feat(providers): declare portable-field consumption and guard it with a parity test"
```

---

## Task 6: Refuse an UNSUPPORTED field at load; warn on a foreign namespace

**Goal:** Turn the declarations into operator-facing errors, so setting a field the selected
provider cannot honour fails before launch instead of vanishing.

**Files:**
- Create: `src/kinoforge/validation/checks/field_support.py`
- Modify: `src/kinoforge/validation/checks/__init__.py` (import for self-registration, matching
  the existing checks' pattern)
- Test: `tests/validation/test_field_support_check.py`

**Acceptance Criteria:**
- [ ] `UnsupportedFieldCheck` is `CheckCategory.STATIC`, no auto-fix, and its severity is
      **by risk coverage**, mirroring `validation/checks/capabilities.py::ProviderCapabilityCheck`.
      **Plan amendment, 2026-08-27, operator ruling.** A uniform hard ERROR would refuse 15 shipped
      configs, because Task 5's declarations found two real silent-ignores that predate this plan:
      `disk_gb` is `UNSUPPORTED` on all four providers (RunPod hardcodes `containerDiskInGb: 250`,
      SkyPilot hardcodes `disk_size` 60/30) and 11 configs set it; skypilot `max_usd_per_hr` is
      F4 itself and 4 configs set it. So:
        - **ERROR** when nothing bounds the same risk — e.g. `accelerator_count`, which no provider
          reads and nothing substitutes for.
        - **WARN naming the substitute and its actual bound** otherwise — `disk_gb` names the
          provider's hardcoded value (so "you asked 100, sky gives 60" is visible at doctor time),
          and skypilot `max_usd_per_hr` names the instance-side deadline watchdog, which bounds
          spend via `budget_usd`/rate until S4 wires the realized-rate check.
- [ ] Setting a field the selected provider declares `UNSUPPORTED` to a NON-DEFAULT value produces
      a finding at the severity above, and the message names the provider, the dotted cfg path,
      the value, and — for a WARN — the substitute and the bound it actually enforces.
- [ ] Leaving that field at its default passes — a default the operator never wrote is not a
      misconfiguration.
- [ ] `ForeignNamespaceCheck` is `Severity.WARN` and fires when `backend_options` carries a
      namespace for a registered provider other than the selected one, naming it.
- [ ] An undeclared provider (empty `consumes()`) fails the ERROR check, matching the
      `capabilities()` precedent.
- [ ] All violations aggregate into ONE `CheckResult` carrying the worst severity, as
      `ProviderCapabilityCheck` does — the operator sees every one at once.
- [ ] Both checks appear in `kinoforge doctor` output.

**Verify:** `pixi run python -m pytest tests/validation/test_field_support_check.py -v`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/validation/test_field_support_check.py
"""Behavior: a field the provider cannot honour refuses the load.

F5's sharpest edge was `provider: runpod` + `cloud: [lambda]` validating clean.
The namespace split makes that specific pair impossible; this check covers the
general case — a portable field the selected provider declares UNSUPPORTED.
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.checks.field_support import (
    ForeignNamespaceCheck,
    UnsupportedFieldCheck,
)
from kinoforge.validation.protocol import Severity

_BASE = {
    "engine": {"kind": "diffusers"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo"}],
}


def _cfg(compute: dict) -> Config:
    return Config.model_validate({**_BASE, "compute": compute})


def test_unsupported_field_set_to_a_non_default_is_an_error():
    # skypilot declares min_vram_gb UNSUPPORTED: its optimizer takes named
    # accelerators, not a VRAM floor, so a floor set here would filter nothing.
    cfg = _cfg({"provider": "skypilot", "image": "i", "placement": {"min_vram_gb": 80}})
    result = UnsupportedFieldCheck().run(cfg)
    assert not result.passed
    assert result.severity is Severity.ERROR
    assert "skypilot" in result.message
    assert "compute.placement.min_vram_gb" in result.message
    assert "80" in result.message


def test_unsupported_field_left_at_its_default_passes():
    # Bug caught: refusing every config because a default exists for a field
    # the provider ignores — which would refuse every shipped skypilot config.
    assert UnsupportedFieldCheck().run(_cfg({"provider": "skypilot", "image": "i"})).passed


def test_consumed_field_set_to_a_non_default_passes():
    cfg = _cfg({"provider": "skypilot", "image": "i", "placement": {"disk_gb": 200}})
    assert UnsupportedFieldCheck().run(cfg).passed


def test_provider_with_an_empty_declaration_is_refused():
    from kinoforge.core import registry

    class _Undeclared:
        name = "undeclared"

    registry.register_provider("undeclared", lambda: _Undeclared(), _Undeclared)
    try:
        result = UnsupportedFieldCheck().run(_cfg({"provider": "undeclared", "image": "i"}))
        assert not result.passed
        assert "declares no field support" in result.message
    finally:
        registry._PROVIDER_CLASSES.pop("undeclared", None)
        registry._PROVIDERS.pop("undeclared", None)


def test_foreign_namespace_warns_and_names_it():
    cfg = _cfg(
        {
            "provider": "skypilot",
            "image": "i",
            "backend_options": {"runpod": {"cloud_type": "secure"}},
        }
    )
    result = ForeignNamespaceCheck().run(cfg)
    assert not result.passed
    assert result.severity is Severity.WARN
    assert "runpod" in result.message
```

> Executor note: the registry teardown above pokes at private dicts. Check
> `src/kinoforge/core/registry.py` for the real attribute names and, if a public
> unregister/reset helper exists, use it instead. If neither exists, use `monkeypatch` over the
> registry module rather than mutating global state in a `finally`.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/validation/test_field_support_check.py -q`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement**

```python
# src/kinoforge/validation/checks/field_support.py
"""UnsupportedFieldCheck + ForeignNamespaceCheck — STATIC, no auto-fix.

Turns each provider's consumes() declaration into an operator-facing refusal.
A field the selected provider declares UNSUPPORTED, set to a value the operator
actually wrote, is a misconfiguration — the alternative is finding out from the
invoice (design doc §4, verification finding F5).
"""
```

with `run(cfg)` comparing each `Placement` field against `Placement()`'s default, skipping
defaults, mapping the field name to its dotted path (`compute.placement.<field>`), and
aggregating every violation into ONE `CheckResult` carrying the worst severity — the same
aggregation `ProviderCapabilityCheck` uses, so the operator sees all of them at once. Register
both checks at import time via `kinoforge.validation.registry.register`, matching the existing
checks.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/validation -q && pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q`
Expected: all PASS

- [ ] **Step 5: Confirm `kinoforge doctor` surfaces both**

Run: `pixi run kinoforge doctor --config examples/configs/skypilot-lambda-comfyui.yaml`
Expected: both check names appear in the output with a pass line.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/validation tests/validation/test_field_support_check.py
git commit -m "feat(validation): refuse a field the selected provider cannot honour"
```

---

## Task 7: Fold `diagnostic_env` into `env`

**Goal:** Delete the last spec field that is an overlay rather than a distinct concept, keeping
its `setdefault` semantics.

**Files:**
- Modify: `src/kinoforge/core/spec_builder.py`, `src/kinoforge/core/interfaces.py`
- Modify: `src/kinoforge/providers/runpod/__init__.py` (delete the merge it does today)
- Test: `tests/core/test_spec_builder.py` (extend), `tests/providers/test_runpod_create_pod_diagnostic_env.py` (update)

**Acceptance Criteria:**
- [ ] `InstanceSpec.diagnostic_env` no longer exists.
- [ ] With `diagnostic_mode` on, the diagnostic variables appear in `spec.env`, and a
      user-supplied `env` value for the same key still wins.
- [ ] The RunPod payload for a diagnostic-mode config is byte-identical to before the change
      (proven by adding one diagnostic-mode golden).
- [ ] The parity guard's expected field set drops `diagnostic_env` and stays green.

**Verify:** `pixi run python -m pytest tests/core/test_spec_builder.py tests/providers -q`

**Steps:**

- [ ] **Step 1: Add the failing test**

```python
def test_diagnostic_env_merges_into_env_without_clobbering_user_values():
    # Bug caught: the overlay wins over an operator-set variable, silently
    # changing a run's behaviour (the setdefault direction is load-bearing).
    cfg = load_config(_CFG)
    cfg.diagnostic_mode = True
    spec = _build(
        cfg=cfg,
        env={"KINOFORGE_DIAG_UPLOAD": "operator-value", "OTHER": "x"},
        diagnostic_env={"KINOFORGE_DIAG_UPLOAD": "overlay", "KINOFORGE_DIAG_RUN": "r"},
    )
    assert spec.env["KINOFORGE_DIAG_UPLOAD"] == "operator-value"
    assert spec.env["KINOFORGE_DIAG_RUN"] == "r"
    assert spec.env["OTHER"] == "x"
    assert not hasattr(spec, "diagnostic_env")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_spec_builder.py -k diagnostic -q`
Expected: FAIL — `diagnostic_env` still an attribute

- [ ] **Step 3: Implement**

In `build_instance_spec`, replace the `diagnostic_env=` argument to `InstanceSpec` with a merge
into `env` before construction:

```python
    merged_env = dict(env)
    if cfg.diagnostic_mode and diagnostic_env:
        for key, value in diagnostic_env.items():
            merged_env.setdefault(key, value)
```

Delete `InstanceSpec.diagnostic_env` and the RunPod-side merge, and drop `"diagnostic_env"` from
`_SPEC_PORTABLE` and from all four `consumes()` declarations.

- [ ] **Step 4: Add a diagnostic-mode golden**

Add a small config under `examples/configs/` only if one with `diagnostic_mode: true` does not
already exist (`rg -l 'diagnostic_mode' examples/configs/`); otherwise snapshot the existing one.
Regenerate goldens and confirm the diagnostic payload is unchanged apart from nothing.

- [ ] **Step 5: Run**

Run: `pixi run python -m pytest tests/core tests/providers -q`
Expected: all PASS, goldens unchanged

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(core): fold diagnostic_env into the spec env overlay"
```

---

## Task 8: Live CPU smoke — same config, same behaviour (USER GATE)

**Goal:** Prove on real infrastructure that an S1-migrated config launches what the pre-S1 config
launched.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current
> conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or
> by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been
> re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_compute_seam_s1_smoke.py`
- Create: `tests/live/_s1_smoke_cfg.yaml` (CPU-only skypilot config in the migrated shape)

**Acceptance Criteria:**
- [ ] The RED scaffold (test + config) is committed BEFORE any live spend — project durability
      rule.
- [ ] `pixi run preflight` exits 0 before the run.
- [ ] The smoke launches the cheapest CPU SKU on skypilot in `us-west-2` (`c6i.large`, the
      watchdog smoke's SKU, ~$0.09/hr), reaches a ready cluster, and tears down.
- [ ] The run passes `--no-reuse`; after the orchestrator exits, `pixi run kinoforge list`
      prints BOTH `[instance overview] No running instances.` AND
      `No instances recorded in ledger.`
- [ ] The captured `task_config` from the live run matches the golden for the same config
      (`tests/providers/golden/launch_payloads/`), proving the migrated shape puts the same thing
      on the wire.
- [ ] GPU/CPU utilisation is polled every 60–90 s during the run and surfaced in the evidence;
      a 0% CPU with flat memory during boot means capture `bootstrap.log`, destroy, fail fast.
- [ ] Total spend recorded and under $1.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s1_smoke.py -v -s` → PASS, followed by `pixi run kinoforge list` showing both empty lines.

**Steps:**

- [ ] **Step 1: Write the scaffold and commit it RED**

```python
# tests/live/test_compute_seam_s1_smoke.py
"""S1 live smoke: a migrated config launches what it launched pre-S1.

Cheapest CPU SKU on purpose — S1 changes config/spec SHAPE, and the thing worth
paying to verify is that the shape reaches a real provider intact. GPU adds
cost, not signal.

Gated on KINOFORGE_LIVE_TESTS=1 like every live test here.
"""
```

with a body that: builds the provider via `build_provider_for`, captures the `task_config` it
sends (recording proxy, as `tests/live/test_skypilot_live.py` does), asserts equality against the
golden, polls utilisation on a 60–90 s cadence while waiting for ready, and tears down in a
`finally`.

```bash
git add tests/live/test_compute_seam_s1_smoke.py tests/live/_s1_smoke_cfg.yaml
git commit -m "test(live): add the RED S1 compute-seam smoke scaffold"
```

- [ ] **Step 2: Preflight**

Run: `pixi run preflight`
Expected: exit 0 (creds present, zero active pods, clean tree)

- [ ] **Step 3: Run the smoke**

Run: `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s1_smoke.py -v -s`
Expected: PASS. Watch the utilisation lines; a stalled boot means kill and investigate rather
than waiting out the timeout.

- [ ] **Step 4: Verify teardown independently**

Run: `pixi run kinoforge list`
Expected, both lines present:
```
[instance overview] No running instances.
No instances recorded in ledger.
```
If either shows a cluster: `pixi run -e live-skypilot kinoforge destroy --id <id>`

- [ ] **Step 5: Record the evidence**

Write the captured payload comparison, the utilisation samples, the teardown output and the
measured spend into `tests/live/_s1_smoke_evidence.json`, matching the shape of the existing
`_c26_phase_a_smoke_evidence.json`.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add tests/live/_s1_smoke_evidence.json tests/live/test_compute_seam_s1_smoke.py
git commit -m "test(live): S1 compute-seam smoke green on the cheapest CPU SKU"
```

---

## Task 9: Documentation and PROGRESS

**Goal:** Leave the config surface documented in the shape it now has.

**Files:**
- Modify: `README.md` (config surface section), `examples/configs/*.yaml` (stale comments
  referencing `requirements` / `cloud` / "ComputeConfig has no region field today"),
  `PROGRESS.md`
- Modify: `docs/superpowers/plans/2026-08-24-compute-seam-s1-portable-core.md` (check off tasks)

**Acceptance Criteria:**
- [ ] `rg 'compute\.requirements|compute\.cloud\b|compute\.cloud_type' README.md examples/ docs/`
      returns only historical references inside dated design/research docs.
- [ ] README documents `compute.placement` and `compute.backend_options` with a worked example.
- [ ] `examples/configs/skypilot-gpu.yaml:38-40`'s "ComputeConfig has no region field today"
      comment is updated to say region lands in S2, with the design doc path.
- [ ] PROGRESS RESUME SNAPSHOT records S1 as shipped, names the intended capacity-wait behaviour
      change, and sets the single next action to the S2 plan.

**Verify:** `pixi run python -m pytest -q` → full suite green; `rg` check above returns clean.

**Steps:**

- [ ] **Step 1: Update README's config surface section** with the before/after from design §12.
- [ ] **Step 2: Sweep example-config comments**

```bash
rg -n 'requirements:|gpu_preference|no region field' examples/configs/
```

- [ ] **Step 3: Update PROGRESS.md** — new RESUME SNAPSHOT block naming: S1 shipped, the goldens'
      location and how to regenerate them, the capacity-wait scoping change, and the next action
      (write the S2 plan: region as first class).
- [ ] **Step 4: Run the full suite**

Run: `pixi run test && pixi run typecheck && pixi run lint`
Expected: green, green, green

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "docs(compute): document the placement and backend_options config surface"
```

---

## Self-Review Notes

**Spec coverage (design §§1-14 → tasks):** §2 portable core → Tasks 4, 7 (region/cpus/memory
explicitly deferred per the design's own S2 note). §3 backend_options → Tasks 2, 3. §4 consumes()
guard → Tasks 5, 6. §5 selection inversion → S4, not this plan. §6 rate cap → S4. §7 setup/run
split → S3. §8 region → S2. §9 endpoints + ledger → S5. §10 testing → Tasks 1, 5, 8. §11 staging
→ this plan is S1. §12 migration → Tasks 3, 4, 9. §13 non-goals → respected. §14 risks → the
"nothing outside a provider may read `spec.offer`" constraint is in the header; the golden-fakes
risk is mitigated by Task 5's wire proofs.

**Known ordering subtlety:** Task 2 lands `backend_options` before Task 3 consumes it, and Task 3
lands the namespace moves before Task 4 moves `min_cuda` into the same namespace. That ordering
is deliberate — each task leaves the tree green and the goldens unchanged.
