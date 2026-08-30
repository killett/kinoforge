# Compute Seam S2 — Region as a First-Class Field, and Closing the ComputeConfig Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `region` reachable from YAML on the provider that can honour it, and extend S1's no-silently-ignored-field guard from `compute.placement` to the rest of the `compute` block — which is where the union is still forming.

**Architecture:** Stage 2 of 5 from `docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md`. Two halves that interlock. The first is the design's declared S2 subject: `Placement.region`, wired through `_adapters.build_provider_for` into `SkyPilotProvider(region=…)`, declared `UNSUPPORTED` on the providers that cannot honour it. The second is what S1's whole-branch review found and promoted here: `consumes()` covers `Placement` plus a subset of `InstanceSpec`, and `UnsupportedFieldCheck._PREFIX` is hardcoded to `"compute.placement"` — so `compute.mode`, `compute.tags`, `heartbeat_mode` and `warm_reuse_auto_attach` sit outside both, and two of them are already lying (`mode` is written by 46 configs and read by nothing; `tags` is written by 4 configs and does not exist).

**Tech Stack:** Python 3.13, pydantic v2, pytest, pixi, ruff, mypy. Providers: runpod (GraphQL), skypilot (SDK), modal (SDK), local.

**Global Constraints:**
- **The golden launch payloads move exactly once in this plan, in Task 2, and only for the region pin.** Every other task must leave all 31 byte-identical. Task 2's regeneration is a reviewed act: decode the base64 provision blobs and confirm the *decoded* script is unchanged, then confirm the only JSON delta is `resources.region`. Regenerating a golden to make a failing test pass is forbidden in every task including Task 2.
- **`region` is a cloud-scoped string.** `us-west-2` is AWS; Lambda and Vast use their own region vocabularies. Never pin a region onto a config whose `clouds` list is not AWS — a wrong region is worse than none, because SkyPilot will refuse or silently relocate.
- The project's standing rule is to pin region on every cloud, default Oregon: AWS `us-west-2`, GCP `us-west1`, Azure `westus2`.
- `kinoforge.core.*` must not import `kinoforge.providers.*` at module scope. Registry lookups are function-local, mirroring `core/capabilities.py::_provider_class`.
- Google-style docstrings, full type hints, Conventional Commits in imperative mood, never `--no-verify`, `rg` not `grep`, pixi for everything, local timezone.

**User decisions (already made):**
- S1's ruling that `min_cuda` is portable stands; nothing in S2 revisits it.
- S1's ruling that UNSUPPORTED-field severity is by risk coverage (ERROR only when nothing bounds the same risk, WARN naming the substitute and its real bound otherwise) governs every new declaration in this plan.
- Live smokes are pre-authorised for this project up to the session budget; the S2 smoke is expected to cost ~$0.01 on the same `c6i.large` SKU S1 used.

---

## File Structure

**Modified:**
- `src/kinoforge/core/interfaces.py` — `Placement.region`; the `consumes()` contract widens to cover compute-level fields.
- `src/kinoforge/core/config.py` — `PlacementConfig.region`, `ComputeConfig.tags`, `Config.placement()`, `extra="forbid"` on `ComputeConfig`.
- `src/kinoforge/core/spec_builder.py` — thread `compute.tags` and `compute.mode` into `spec.tags`.
- `src/kinoforge/_adapters.py` — `build_provider_for` passes `region` to `SkyPilotProvider`.
- `src/kinoforge/providers/{runpod,skypilot,modal,local}/__init__.py` — declarations for `region` and the compute-level fields.
- `src/kinoforge/validation/checks/field_support.py` — generalise the dotted-path prefix; add substitutes/risks for the new rows.
- `tests/providers/test_field_consumption_parity.py` — extend the guard to the compute-level surface.
- `tools/snapshot_launch_payloads.py` — nothing structural; it already derives everything from the cfg.
- `examples/configs/skypilot-cpu.yaml` (region pin), the 4 configs writing `compute.tags`, `README.md`, `PROGRESS.md`, `docs/breaking-changes.md`.

**Created:**
- `tests/core/test_region.py`, `tests/core/test_compute_surface.py`, `tests/live/test_compute_seam_s2_region_smoke.py`.

---

## Task 0: `Placement.region` and its declarations

**Goal:** Add the portable `region` field and make every provider declare what it does with it, so the parity guard is green and the field is honest before anything is wired.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`Placement`), `src/kinoforge/core/config.py` (`PlacementConfig`, `Config.placement()`)
- Modify: all four provider modules (`consumes()`)
- Test: `tests/core/test_region.py`

**Acceptance Criteria:**
- [ ] `Placement.region: str | None = None` and `PlacementConfig.region: str | None = None`; `Config.placement()` carries it through.
- [ ] `None` is the default and means "let the provider decide" — no config that omits it changes behaviour.
- [ ] skypilot declares `region` `CONSUMED`; runpod, modal and local declare it `UNSUPPORTED`.
- [ ] `tests/providers/test_field_consumption_parity.py` is green with no edit to its assertions — the guard auto-required the new row and all four providers supplied it.
- [ ] All 31 goldens byte-identical (nothing sets `region` yet).

**Verify:** `pixi run python -m pytest tests/core/test_region.py tests/providers/test_field_consumption_parity.py tests/providers/test_launch_payload_goldens.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_region.py
"""Behavior: compute.placement.region is portable and provider-declared.

F6 in the cloud-layer verification doc: SkyPilotProvider has accepted a
`region` constructor argument all along, and no config path ever reached it,
so `sky` was free to pick a hemisphere. The standing project rule is that
region is pinned on every cloud (default Oregon) — a rule no YAML could
express until this field existed.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.interfaces import FieldSupport, Placement

_BASE = {
    "engine": {"kind": "diffusers", "precision": "bf16"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo", "target": "checkpoints"}],
}


def _load(compute: dict) -> Config:
    return Config.model_validate({**_BASE, "compute": compute})


def test_region_reaches_the_placement_object():
    cfg = _load({"provider": "skypilot", "image": "i", "placement": {"region": "us-west-2"}})
    assert cfg.placement().region == "us-west-2"


def test_region_defaults_to_none_meaning_provider_decides():
    # Bug caught: a non-None default would pin every existing config to one
    # region, which is a behaviour change disguised as a new field.
    assert Placement().region is None
    assert _load({"provider": "skypilot", "image": "i"}).placement().region is None


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("skypilot", FieldSupport.CONSUMED),
        ("runpod", FieldSupport.UNSUPPORTED),
        ("modal", FieldSupport.UNSUPPORTED),
        ("local", FieldSupport.UNSUPPORTED),
    ],
)
def test_every_provider_declares_region(provider: str, expected: FieldSupport):
    # Bug caught: a provider that neither reads region nor declares it lets an
    # operator pin a region that silently does nothing — the F5 failure mode.
    from kinoforge.core import registry

    cls = registry.provider_class(provider)
    assert cls is not None
    assert cls.consumes()["region"] is expected
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_region.py -q`
Expected: FAIL — `PlacementConfig` rejects the extra key `region` (it carries `extra="forbid"`), and `consumes()` has no `region` row.

- [ ] **Step 3: Implement**

In `core/interfaces.py`, add to `Placement` (keep it next to the other placement axes, before `spot`):

```python
    #: Cloud region to pin, e.g. "us-west-2" (AWS) / "us-west1" (GCP). None
    #: means "let the provider or its optimizer decide". The value space is
    #: the CLOUD's, not kinoforge's: a region string is only meaningful
    #: alongside the cloud it belongs to.
    region: str | None = None
```

Mirror it on `PlacementConfig` in `core/config.py` and thread it through `Config.placement()`.

Then add the row to each provider's `consumes()`. SkyPilot's is `CONSUMED` (it applies `resources["region"]`); the other three are `UNSUPPORTED` with a docstring line saying why — RunPod's create mutation takes a data-centre id kinoforge does not send, Modal's function decorator takes a `region=` kinoforge does not pass, and Local runs on this machine.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_region.py tests/providers/test_field_consumption_parity.py tests/providers/test_launch_payload_goldens.py -q`
Expected: all PASS, no golden modified.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/interfaces.py src/kinoforge/core/config.py \
        src/kinoforge/providers tests/core/test_region.py
git commit -m "feat(config): add the portable placement.region field with provider declarations"
```

---

## Task 1: Wire `region` through the composition root

**Goal:** Make the field reach `SkyPilotProvider`, closing the half of F6 that says the constructor knob is unreachable from any YAML.

**Files:**
- Modify: `src/kinoforge/_adapters.py` (`build_provider_for`)
- Test: `tests/core/test_region.py` (extend), `tests/test_adapters_build_provider_for.py` (extend)

**Acceptance Criteria:**
- [ ] `build_provider_for` passes `cfg.placement().region` to `SkyPilotProvider`; a cfg with no region leaves `_region` `None`.
- [ ] A cfg-set region lands in the launch payload's `resources["region"]`, proven by capture through `tools/snapshot_launch_payloads.py`, not by asserting on the provider attribute.
- [ ] Setting `placement.region` on a runpod or modal config is a load-time finding at the severity S1's risk-coverage rule dictates — see Task 5 for where the substitute text lives; here, just confirm the declaration drives it.
- [ ] All 31 goldens byte-identical (no shipped config sets a region yet).

**Verify:** `pixi run python -m pytest tests/core/test_region.py tests/test_adapters_build_provider_for.py tests/providers/test_launch_payload_goldens.py -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_cfg_region_lands_in_the_skypilot_launch_payload(tmp_path):
    # Bug caught: the field exists, validates, and never reaches sky — which
    # is exactly the state F6 documented (constructor knob, no config path).
    import yaml

    from tools.snapshot_launch_payloads import capture_payload

    src = yaml.safe_load(open("examples/configs/skypilot-cpu.yaml"))
    src["compute"].setdefault("placement", {})["region"] = "us-west-2"
    src["compute"].setdefault("backend_options", {})["skypilot"] = {"clouds": ["aws"]}
    cfg_path = tmp_path / "region-pinned.yaml"
    cfg_path.write_text(yaml.safe_dump(src))

    payload = capture_payload(cfg_path)
    assert payload["task"]["resources"]["region"] == "us-west-2"


def test_no_region_in_cfg_leaves_the_payload_unpinned(tmp_path):
    import yaml

    from tools.snapshot_launch_payloads import capture_payload

    src = yaml.safe_load(open("examples/configs/skypilot-cpu.yaml"))
    cfg_path = tmp_path / "unpinned.yaml"
    cfg_path.write_text(yaml.safe_dump(src))

    assert "region" not in capture_payload(cfg_path)["task"]["resources"]
```

> Executor note: `capture_payload`'s return shape is `{"provider": …, "task": …, "launch_kwargs": …}` for skypilot — confirm the exact keys against `tools/snapshot_launch_payloads.py` and an existing golden (`tests/providers/golden/launch_payloads/skypilot-cpu.json`) before writing the assertion, and follow whatever is there.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_region.py -k region_lands -q`
Expected: FAIL — `KeyError: 'region'`, because `build_provider_for` never passes it.

- [ ] **Step 3: Implement**

In `_adapters.py`'s `build_provider_for`, inside the existing skypilot branch (which S1 widened to run for every skypilot cfg), set the region alongside the namespace reads:

```python
        provider._region = cfg.placement().region
```

Keep it a single assignment beside `_clouds` / `_retry_until_up` rather than adding a constructor call — the registry factory takes zero arguments and changing that is S4's business.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_region.py tests/test_adapters_build_provider_for.py tests/providers/test_launch_payload_goldens.py -q`
Expected: all PASS, goldens untouched.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/_adapters.py tests/core/test_region.py tests/test_adapters_build_provider_for.py
git commit -m "fix(adapters): thread placement.region into SkyPilotProvider"
```

---

## Task 2: Pin the region in the shipped SkyPilot config, and regenerate one golden

**Goal:** Make the standing Oregon rule true of the config that ships, and take the plan's only intended wire change under review.

**Files:**
- Modify: `examples/configs/skypilot-cpu.yaml`
- Modify: `tests/providers/golden/launch_payloads/skypilot-cpu.json` (regenerated)

**Acceptance Criteria:**
- [ ] `skypilot-cpu.yaml` pins BOTH `backend_options.skypilot.clouds: ["aws"]` and `placement.region: us-west-2`, with a comment saying a region is only meaningful next to the cloud it belongs to.
- [ ] `skypilot-gpu.yaml`, `skypilot-lambda-*.yaml` and `skypilot-vast-*.yaml` are NOT given a region: Lambda and Vast use their own region vocabularies, and `skypilot-gpu` pins no cloud. Each gets a one-line comment recording that, replacing the stale "ComputeConfig has no region field today" note in `skypilot-gpu.yaml:38-40`.
- [ ] Exactly ONE golden changes, and its only JSON delta is the added `resources.region` (plus `resources.cloud` if the clouds pin is new to that file).
- [ ] The decoded provision script inside that golden is byte-identical before and after — proven, not assumed.

**Verify:** `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q` → 42 passed; and the diff review below.

**Steps:**

- [ ] **Step 1: Capture the pre-change decoded script**

```bash
pixi run python - <<'PY'
import base64, gzip, hashlib, json
g = json.load(open("tests/providers/golden/launch_payloads/skypilot-cpu.json"))
setup = g["task"]["setup"]
print("setup sha256:", hashlib.sha256(setup.encode()).hexdigest())
PY
```

Record the digest — Step 4 compares against it. (The SkyPilot golden carries `setup` as plain text; the RunPod goldens are the gzip+base64 ones. If this config's payload does carry a base64 blob, decode it with `gzip.decompress(base64.b64decode(...))` before hashing.)

- [ ] **Step 2: Edit the config**

```yaml
compute:
  provider: skypilot
  placement:
    # A region string belongs to ONE cloud's vocabulary: us-west-2 is AWS.
    # Pin the cloud alongside it or the optimizer may pick a cloud where the
    # region does not exist. Project rule: pin region everywhere, Oregon by
    # default (AWS us-west-2 / GCP us-west1 / Azure westus2).
    region: us-west-2
  backend_options:
    skypilot:
      clouds: ["aws"]
```

- [ ] **Step 3: Regenerate exactly one golden**

Run: `pixi run python tools/snapshot_launch_payloads.py`
Then: `git status --short tests/providers/golden/launch_payloads/`
Expected: exactly one modified file, `skypilot-cpu.json`. If more than one moved, STOP and report BLOCKED — something other than the region pin changed.

- [ ] **Step 4: Review the diff as a human would**

```bash
git diff tests/providers/golden/launch_payloads/skypilot-cpu.json
```

Confirm the delta is `resources.region` (and `resources.cloud`, if newly pinned) and nothing else. Then re-run Step 1's digest command and confirm the `setup` hash is UNCHANGED. Paste both the diff and the two digests into your report — this is the plan's only sanctioned golden change and it must be legible.

- [ ] **Step 5: Run and commit**

Run: `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q`
Expected: 42 passed

```bash
pixi run pre-commit run --all-files
git add examples/configs tests/providers/golden/launch_payloads/skypilot-cpu.json
git commit -m "feat(configs): pin the skypilot CPU config to aws/us-west-2"
```

---

## Task 3: `compute.tags` becomes a real field

**Goal:** Stop silently dropping a key four shipped configs already write.

**Files:**
- Modify: `src/kinoforge/core/config.py` (`ComputeConfig.tags`), `src/kinoforge/core/spec_builder.py`
- Test: `tests/core/test_compute_surface.py`

**Acceptance Criteria:**
- [ ] `ComputeConfig.tags: dict[str, str] = {}` exists and reaches `spec.tags`.
- [ ] Precedence is explicit and tested: the caller's `tags` argument to `build_instance_spec` (used by the CLI and grid paths) wins over `compute.tags`, which in turn wins over nothing — and the two kinoforge-owned keys (`kinoforge_engine`, `kinoforge_key`) are never overwritten by either.
- [ ] The four configs that write `compute.tags` (`runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml`, `…-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml`, `…-wan-2_1-1_3b-t2v-strength-grid.yaml`, `…-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml`, each `smoke_tier: …`) now deliver that tag to the provider.
- [ ] Goldens: the four affected RunPod goldens will legitimately gain the `smoke_tier` tag **only if those configs have goldens** — check first. If they do, this is a second sanctioned golden change; apply Task 2's review discipline (decode, diff, confirm only the tag moved) and say so in the report. If they do not (they are grid configs, which S1 recorded as outside the non-recursive glob), no golden moves.

**Verify:** `pixi run python -m pytest tests/core/test_compute_surface.py tests/providers/test_launch_payload_goldens.py -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_compute_surface.py
"""Behavior: the compute block's non-placement keys are real or refused.

S1's whole-branch review found compute.tags written by four shipped configs
and read by nothing — pydantic's default extra="ignore" dropped it silently.
That is the exact bug the compute-seam work exists to end, sitting in the
repo's own examples.
"""

from __future__ import annotations

from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import Lifecycle, Offer, RenderedProvision
from kinoforge.core.spec_builder import build_instance_spec

_BASE = {
    "engine": {"kind": "diffusers", "precision": "bf16"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo", "target": "checkpoints"}],
}


def _spec(compute: dict, *, caller_tags: dict[str, str] | None = None):
    cfg = Config.model_validate({**_BASE, "compute": compute})
    rendered = RenderedProvision(
        script="#!/bin/bash\necho hi",
        run_cmd=["python", "-m", "server"],
        image="img:tag",
        ports=["8000/http"],
        env_required=[],
    )
    return build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        offer=Offer(id="g", gpu_type="g", vram_gb=80, cuda="12.4", cost_rate_usd_per_hr=1.0),
        engine_name="diffusers",
        key_hash="abc",
        image="fallback:img",
        lifecycle=Lifecycle(),
        env={},
        run_id="run-1",
        tags=caller_tags,
    )


def test_compute_tags_reach_the_spec():
    spec = _spec({"provider": "runpod", "image": "i", "tags": {"smoke_tier": "tier-3"}})
    assert spec.tags["smoke_tier"] == "tier-3"


def test_caller_tags_win_over_compute_tags():
    # Bug caught: a per-invocation tag (CLI, grid cell) silently loses to the
    # config's static one, so two grid cells become indistinguishable.
    spec = _spec(
        {"provider": "runpod", "image": "i", "tags": {"cell": "from-cfg"}},
        caller_tags={"cell": "from-caller"},
    )
    assert spec.tags["cell"] == "from-caller"


def test_kinoforge_owned_tags_survive_both():
    # Bug caught: a cfg that sets kinoforge_key breaks warm-reuse matching.
    spec = _spec(
        {"provider": "runpod", "image": "i", "tags": {"kinoforge_key": "hijack"}},
        caller_tags={"kinoforge_engine": "hijack"},
    )
    assert spec.tags["kinoforge_key"] == "abc"
    assert spec.tags["kinoforge_engine"] == "diffusers"


def test_the_four_shipped_configs_deliver_their_smoke_tier():
    for path in (
        "examples/configs/runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml",
        "examples/configs/runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml",
        "examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml",
        "examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml",
    ):
        cfg = load_config(path)
        assert cfg.compute is not None
        assert cfg.compute.tags.get("smoke_tier", "").startswith("kinoforge-smoke-tier"), path
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_compute_surface.py -q`
Expected: FAIL — `ComputeConfig` has no `tags` attribute.

- [ ] **Step 3: Implement**

Add to `ComputeConfig`:

```python
    tags: dict[str, str] = {}
```

and in `build_instance_spec`, fold it in BELOW the kinoforge-owned defaults and ABOVE the caller's tags, so the precedence reads top-to-bottom in one place:

```python
    merged_tags: dict[str, str] = {
        "kinoforge_engine": engine_name,
        "kinoforge_key": key_hash,
    }
    if cfg.compute is not None and cfg.compute.tags:
        merged_tags.update(cfg.compute.tags)
    if tags:
        merged_tags.update(tags)
    # kinoforge-owned keys are re-asserted last: a cfg or caller that sets
    # kinoforge_key would break warm-reuse matching, which keys off it.
    merged_tags["kinoforge_engine"] = engine_name
    merged_tags["kinoforge_key"] = key_hash
```

- [ ] **Step 4: Run, check goldens, commit**

Run: `pixi run python -m pytest tests/core/test_compute_surface.py tests/providers -q`
Then `git status --short tests/providers/golden/launch_payloads/` — follow the AC's branch depending on whether any golden moved.

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "feat(config): make compute.tags a real field instead of a silently dropped key"
```

---

## Task 4: `compute.mode` reaches the provider that reads it

**Goal:** Close the lie S1's review proved: 46 configs write `mode`, nothing reads it, and RunPod branches on a spec tag nobody sets.

**Files:**
- Modify: `src/kinoforge/core/spec_builder.py`
- Modify: all four provider modules (`consumes()` gains the compute-level rows — see Task 5 for the guard that requires them)
- Test: `tests/core/test_compute_surface.py` (extend)

**Acceptance Criteria:**
- [ ] `build_instance_spec` writes `spec.tags["mode"]` from `cfg.compute.mode`, so `RunPodProvider.create_instance`'s existing `spec.tags.get("mode", "pod")` branch (`providers/runpod/__init__.py:569`) actually sees the operator's value.
- [ ] A config with `mode: pod` (all 45 of them) produces a byte-identical payload — proven by the goldens staying green.
- [ ] A config with `mode: serverless` reaches `_create_serverless` — proven by a test that captures which branch ran, not by reading the code.
- [ ] `mode` is declared: `CONSUMED` on runpod, `UNSUPPORTED` on skypilot, modal and local, each with a docstring line saying what it means there.

**Verify:** `pixi run python -m pytest tests/core/test_compute_surface.py tests/providers -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_pod_mode_is_the_default_and_reaches_the_spec_tag():
    spec = _spec({"provider": "runpod", "image": "i"})
    assert spec.tags["mode"] == "pod"


def test_serverless_mode_reaches_the_serverless_branch():
    # Bug caught (found by the S1 whole-branch review): compute.mode was
    # written by 46 configs and read by nothing, so `mode: serverless` took
    # the pod branch and produced a byte-identical payload.
    from unittest import mock

    from kinoforge.providers.runpod import RunPodProvider

    spec = _spec({"provider": "runpod", "image": "i", "mode": "serverless"})
    assert spec.tags["mode"] == "serverless"

    provider = RunPodProvider(api_key="kinoforge-prod-deadbeef", http_post=lambda *a, **k: {})
    with (
        mock.patch.object(RunPodProvider, "_create_serverless") as serverless,
        mock.patch.object(RunPodProvider, "_create_pod") as pod,
    ):
        serverless.return_value = mock.MagicMock(id="sl-1")
        provider.create_instance(spec)
    assert serverless.called and not pod.called
```

> Executor note: check `RunPodProvider.__init__`'s real signature and the existing fakes in `tests/providers/test_runpod_create_pod_cloud_type.py` before writing the constructor call; adapt rather than inventing kwargs.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_compute_surface.py -k mode -q`
Expected: FAIL — `KeyError: 'mode'` on `spec.tags`.

- [ ] **Step 3: Implement**

In `build_instance_spec`, after the tag merge:

```python
    # RunPod branches on this tag (providers/runpod/__init__.py:569). Before
    # S2 nothing wrote it, so `compute.mode: serverless` silently took the pod
    # branch — the S1 whole-branch review proved the payload was identical
    # either way.
    if cfg.compute is not None:
        merged_tags.setdefault("mode", cfg.compute.mode)
```

`setdefault` so a caller that passes an explicit `mode` tag (the RunPod provider's own internal call at `providers/runpod/__init__.py:1462` does) still wins.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_compute_surface.py tests/providers -q`
Expected: all PASS, all 31 goldens byte-identical — every shipped config is `mode: pod`, which was already the branch taken.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/spec_builder.py src/kinoforge/providers tests/core/test_compute_surface.py
git commit -m "fix(spec-builder): deliver compute.mode to the provider that branches on it"
```

---

## Task 5: Extend the guard and the check to the whole `compute` block

**Goal:** Remove the structural reason `mode` and `tags` could rot: the guard covers `Placement` and part of `InstanceSpec`, and stops at `compute.placement.*`.

**Files:**
- Modify: `tests/providers/test_field_consumption_parity.py`
- Modify: `src/kinoforge/validation/checks/field_support.py`
- Modify: all four provider modules (declarations for the compute-level fields)
- Test: `tests/validation/test_field_support_check.py` (extend)

**Acceptance Criteria:**
- [ ] The parity guard requires a declaration for every `ComputeConfig` field that describes WHAT TO LAUNCH — `image`, `mode`, `tags`, `heartbeat_mode`, `warm_reuse_auto_attach` — derived from `ComputeConfig.model_fields` minus an explicit, commented exclusion set (`provider`, `placement`, `lifecycle`, `backend_options`, which are structural or already covered).
- [ ] The exclusion set is asserted against `ComputeConfig.model_fields` in both directions, so a new compute-level field cannot appear without either a declaration or a deliberate exclusion.
- [ ] `UnsupportedFieldCheck` reports compute-level fields at their correct dotted path (`compute.mode`, not `compute.placement.mode`) — the hardcoded `_PREFIX` becomes per-field.
- [ ] `heartbeat_mode` is declared honestly: RunPod `CONSUMED` (graphql-tag substrate), skypilot/modal/local `UNSUPPORTED`, matching `_adapters.build_heartbeat_endpoint_for`'s real dispatch.
- [ ] Severity follows S1's risk-coverage rule; no shipped config gains an ERROR. Prove it with the existing all-configs sweep, extended to the new rows.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/providers/test_field_consumption_parity.py tests/validation -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_compute_level_fields_are_declared_by_every_provider():
    # Bug caught: consumes() covered Placement and part of InstanceSpec, so
    # compute.mode rotted for months — written by 46 configs, read by nobody.
    from kinoforge.core import registry
    from kinoforge.core.config import ComputeConfig

    structural = {"provider", "placement", "lifecycle", "backend_options"}
    expected = set(ComputeConfig.model_fields) - structural
    assert expected, "ComputeConfig lost every declarable field — check the exclusion set"

    for name in sorted(registry.provider_names()):
        cls = registry.provider_class(name)
        assert cls is not None
        declared = set(cls.consumes())
        missing = expected - declared
        assert not missing, f"{name} does not declare {sorted(missing)}"


def test_the_structural_exclusion_set_is_not_stale():
    # Bug caught: a field is renamed, the exclusion set keeps the old name,
    # and the subtraction above silently stops requiring the new one.
    from kinoforge.core.config import ComputeConfig

    structural = {"provider", "placement", "lifecycle", "backend_options"}
    assert structural <= set(ComputeConfig.model_fields)


def test_unsupported_compute_level_field_reports_its_real_path():
    from kinoforge.core.config import Config
    from kinoforge.validation.checks.field_support import UnsupportedFieldCheck

    cfg = Config.model_validate({**_BASE, "compute": {
        "provider": "skypilot", "image": "i", "mode": "serverless",
    }})
    result = UnsupportedFieldCheck().run(cfg)
    assert "compute.mode" in result.message
    assert "compute.placement.mode" not in result.message
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/providers/test_field_consumption_parity.py -k compute_level -q`
Expected: FAIL — every provider is missing `image`, `mode`, `tags`, `heartbeat_mode`, `warm_reuse_auto_attach`.

- [ ] **Step 3: Implement**

Give each provider the five new rows, deriving each from what its code actually does — read `create_instance` and `_adapters.build_heartbeat_endpoint_for` rather than copying this list:
`image` is CONSUMED everywhere except local; `mode` CONSUMED on runpod only; `tags` CONSUMED wherever the provider puts tags on the wire; `heartbeat_mode` CONSUMED on runpod only; `warm_reuse_auto_attach` is orchestrator-side on every provider — declare it UNSUPPORTED across the board with a docstring line saying the orchestrator honours it, not the provider, and add a substitute row so it reports as a WARN naming the orchestrator rather than an ERROR.

In `field_support.py`, replace the module-level `_PREFIX` with a per-field path map:

```python
_PATHS: dict[str, str] = {
    # compute-level fields keep their own dotted path; everything else is a
    # placement axis. A wrong path in a finding sends the operator to a key
    # that does not exist.
    "image": "compute.image",
    "mode": "compute.mode",
    "tags": "compute.tags",
    "heartbeat_mode": "compute.heartbeat_mode",
    "warm_reuse_auto_attach": "compute.warm_reuse_auto_attach",
}


def _dotted_path(field: str) -> str:
    """Return the cfg path an operator would have written for *field*."""
    return _PATHS.get(field, f"compute.placement.{field}")
```

and compare compute-level values against `ComputeConfig()`'s defaults exactly as the placement rows compare against `PlacementConfig()`'s — a field left at its default is not a misconfiguration.

- [ ] **Step 4: Run the all-configs sweep**

Run: `pixi run python -m pytest tests/validation -q`
Expected: PASS, with the existing shipped-config sweep confirming no config produces an ERROR. If one does, the declaration or the substitute is wrong — fix that, do not relax the sweep.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/validation src/kinoforge/providers tests
git commit -m "feat(validation): extend field-support declarations to the whole compute block"
```

---

## Task 6: `extra="forbid"` on `ComputeConfig`

**Goal:** Make a misspelled compute key impossible now that the two real keys it was hiding are fields.

**Files:**
- Modify: `src/kinoforge/core/config.py`
- Test: `tests/core/test_compute_surface.py` (extend)

**Acceptance Criteria:**
- [ ] `ComputeConfig` carries `model_config = ConfigDict(extra="forbid")`.
- [ ] `compute: {placemnt: {...}}` raises rather than silently applying every default.
- [ ] Every shipped config still loads — run `load_config` over all of `examples/configs/**` recursively, not just the top level, and name any that fail.
- [ ] The removed-key errors from S1 still fire with their migration messages rather than being swallowed by the new extra-key error — `compute.cloud` must still say `compute.backend_options.skypilot.clouds`.

**Verify:** `pixi run python -m pytest tests/core -q` and the sweep in Step 3.

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_misspelled_compute_key_is_refused():
    # Bug caught: `placemnt:` silently applies every default, including a
    # region and disk the operator never chose.
    import pytest
    from pydantic import ValidationError

    with pytest.raises((ValidationError, Exception)) as exc:
        Config.model_validate({**_BASE, "compute": {
            "provider": "runpod", "image": "i", "placemnt": {"disk_gb": 200},
        }})
    assert "placemnt" in str(exc.value)


def test_removed_key_error_still_names_its_replacement():
    # Bug caught: extra="forbid" fires first and the operator gets
    # "Extra inputs are not permitted" instead of the migration path.
    from kinoforge.core.errors import ConfigError

    import pytest

    with pytest.raises(ConfigError) as exc:
        Config.model_validate({**_BASE, "compute": {
            "provider": "skypilot", "image": "i", "cloud": ["lambda"],
        }})
    assert "compute.backend_options.skypilot.clouds" in str(exc.value)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_compute_surface.py -k misspelled -q`
Expected: FAIL — the misspelled key is silently ignored, so no exception is raised.

- [ ] **Step 3: Implement, then sweep every config**

Add `model_config = ConfigDict(extra="forbid")` to `ComputeConfig`. The `mode="before"` removed-key validator already runs before pydantic's extra-key handling, so the migration messages survive — the second test proves it rather than assuming it.

```bash
pixi run python - <<'PY'
from pathlib import Path
from kinoforge.core.config import load_config
bad = []
for p in sorted(Path("examples/configs").rglob("*.y*ml")):
    try:
        load_config(str(p))
    except Exception as exc:
        bad.append((p.name, type(exc).__name__, str(exc)[:120]))
print(f"{len(bad)} configs fail to load")
for b in bad:
    print(" ", b)
PY
```

Grid fragments and manifests are expected to fail for missing `engine`/`models` — that is pre-existing and unrelated. Any failure mentioning an extra compute key is YOURS: fix the config or the field, and name it in your report.

- [ ] **Step 4: Run and commit**

Run: `pixi run python -m pytest tests/core tests/validation tests/providers -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/config.py tests/core/test_compute_surface.py
git commit -m "feat(config): forbid unknown keys in the compute block"
```

---

## Task 7: Live smoke — the region pin comes from YAML (USER GATE)

**Goal:** Prove on real infrastructure that a region written in a config reaches the cloud, which is the whole point of S2.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_compute_seam_s2_region_smoke.py`
- Create: `tests/live/_s2_smoke_evidence.json`

**Acceptance Criteria:**
- [ ] The RED scaffold is COMMITTED BEFORE any live spend — project durability rule.
- [ ] `pixi run preflight` exits 0 before the run, and its exit code is recorded in the evidence.
- [ ] The smoke launches `examples/configs/skypilot-cpu.yaml` **through `build_provider_for`, with NO constructor region pin** — that is the difference from S1's smoke, which pinned the region itself and could therefore only prove the pin reached `resources`.
- [ ] The launched instance's availability zone starts with `us-west-2`, read from EC2 after launch, and the assertion fails on an empty result.
- [ ] Teardown is convergent and verified AFTER the process exits: both `kinoforge list` lines, `sky status`, and EC2 state. Reuse S1's teardown helper rather than writing a second one — import it from `tests/live/test_compute_seam_s1_smoke.py` or lift it into a shared module and say which you did.
- [ ] Utilisation polled every 60–90 s and recorded; spend recorded and under $0.05.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s2_region_smoke.py -v -s` → PASS, followed by `pixi run kinoforge list` showing both empty lines.

**Steps:**

- [ ] **Step 1: Write the scaffold and commit it RED**

Model it on `tests/live/test_compute_seam_s1_smoke.py`, which is green and carries the hard-won teardown convergence (an unreadable EC2 query counts as a survivor, the ledger row is forgotten only after a clean determination). The one structural difference: build the provider via `kinoforge._adapters.build_provider_for(cfg)` and pass NO region — the cfg must supply it.

```bash
git add tests/live/test_compute_seam_s2_region_smoke.py
git commit -m "test(live): add the RED S2 region smoke scaffold"
```

- [ ] **Step 2: Preflight**

Run: `pixi run preflight` → expect `preflight: PASS — safe to spend`, exit 0. Record the exit code for the evidence; note the ordering trap S1 hit — the smoke rewrites its evidence file, so commit or stash it before invoking preflight.

- [ ] **Step 3: Run the smoke, polling throughout**

Run: `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s2_region_smoke.py -v -s`

Poll utilisation every 60–90 s and log each poll. Utilisation is the health signal, never elapsed spend. A cluster sitting at 0% CPU with flat memory while a boot is supposedly in progress means the boot stalled: capture the log, tear down, fail fast.

- [ ] **Step 4: Verify teardown after exit**

```bash
pixi run kinoforge list
pixi run -e live-skypilot sky status
```
Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` AND `No existing clusters.` If anything survives, destroy it explicitly and report that rather than reporting green.

- [ ] **Step 5: Write evidence and commit**

Follow `tests/live/_s1_smoke_evidence.json`'s shape: the AZ actually launched, the utilisation samples with timestamps, the preflight exit code, the teardown verification output, and the measured spend. Local timezone. No credential value anywhere.

```bash
pixi run pre-commit run --all-files
git add tests/live/_s2_smoke_evidence.json tests/live/test_compute_seam_s2_region_smoke.py
git commit -m "test(live): S2 region pin verified live in us-west-2"
```

---

## Task 8: Documentation and PROGRESS

**Goal:** Leave the config surface and the project record accurate.

**Files:**
- Modify: `README.md`, `SPEC.md`, `docs/breaking-changes.md`, `PROGRESS.md`
- Modify: `examples/configs/skypilot-gpu.yaml` (the stale region comment, if Task 2 did not already reach it)

**Acceptance Criteria:**
- [ ] README documents `placement.region`, including that a region string belongs to one cloud's vocabulary and should be pinned alongside `backend_options.skypilot.clouds`.
- [ ] README and `SPEC.md` document `compute.tags` and the fact that `compute.mode` now reaches RunPod.
- [ ] `docs/breaking-changes.md` gains an S2 entry: `ComputeConfig` now forbids unknown keys, and `compute.mode: serverless` now takes the serverless branch it always claimed to.
- [ ] `rg -n 'no region field|ComputeConfig has no region'` returns nothing outside dated `docs/superpowers/**`.
- [ ] PROGRESS's RESUME SNAPSHOT records S2 shipped, the one intended behaviour change (`mode: serverless` now routes), which golden(s) moved and why, the live smoke result, and the next action (write the S3 plan: setup/run split, `_strip_trailing_exec` deleted).
- [ ] The S1 follow-ups this stage closed are marked closed in PROGRESS; the ones it did not (RunPod/Modal region wiring, the 11 ungated `tests/live` modules, the unreadable provision blob) stay listed.

**Verify:** `pixi run test && pixi run typecheck && pixi run lint` → all green; the `rg` check above returns clean.

**Steps:**

- [ ] **Step 1: Sweep for stale references**

```bash
rg -n 'no region field|ComputeConfig has no region|compute\.mode|compute\.tags' \
   README.md SPEC.md DESIGN.md docs/ examples/ --glob '!docs/superpowers/**'
```

- [ ] **Step 2: Update README + SPEC** with the region guidance and the two newly-real keys.
- [ ] **Step 3: Add the S2 entry to `docs/breaking-changes.md`**, following the format of the S1 entry immediately above it.
- [ ] **Step 4: Update PROGRESS.md** — new RESUME SNAPSHOT block, previous one demoted rather than deleted.
- [ ] **Step 5: Full suite, then commit**

Run: `pixi run test && pixi run typecheck && pixi run lint`

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "docs(compute-seam): document region, compute.tags and the S2 surface"
```

---

## Self-Review Notes

**Design coverage:** §8 (region as first class) → Tasks 0, 1, 2, 7. §4's guard → Task 5, extended to the surface S1's review proved was uncovered. §11's S2 line ("region as first class") is the spine; Tasks 3–6 are the promoted follow-ups from the S1 whole-branch review, which the design doc did not anticipate because the gap was only visible once `consumes()` existed.

**Deliberately NOT in this plan:** wiring `region` on RunPod (`dataCenterId`) or Modal (`region=`) — both are new wire surface needing their own live proof, and both stay `UNSUPPORTED`-and-declared, which is honest. The setup/run split and `_strip_trailing_exec` are S3. The realized-rate check and `find_offers` inversion are S4.

**The ordering that matters:** Task 3 (`tags`) and Task 4 (`mode`) must land before Task 6 (`extra="forbid"`), or four shipped configs stop loading. Task 5 must land after both, or the guard demands declarations for fields that are still fictional.
