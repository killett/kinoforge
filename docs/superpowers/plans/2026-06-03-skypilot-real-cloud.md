# Phase 31 — SkyPilot real-cloud verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `SkyPilotProvider`'s lazy `sky` SDK path into a CPU-only live smoke that captures SDK return shapes as JSON fixtures (Layer-N analog), so PROGRESS:114 carry-forward #2 is closed and any future SDK drift is visible at PR-review time.

**Architecture:** New `live-skypilot` pixi feature env keeps the heavy `skypilot[gcp]` dep out of the default env. A test-local `_RecordingProxy` wraps the real `sky` module and JSON-serialises every call's return value into `tests/providers/fixtures/skypilot/`. The live smoke exercises the full provider chain (`find_offers → create_instance → list_instances → endpoints → destroy_instance`) against a single CPU VM in `us-central1` with `autostop=1` plus a four-tier teardown (`destroy_instance` → direct `sky.down` → `gcloud delete` → manual-deletion error). Default `pixi run test` is untouched; only `pixi run -e live-skypilot test-live-skypilot` invokes the live path.

**Tech Stack:** Python 3.12, pixi (feature envs), pytest, skypilot 0.10+ with `[gcp]` extras, google-cloud SDKs (already pinned), gcloud CLI.

**Spec:** `docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md`.

**Per-spec method coverage:** `sky.gpu_list()`, `sky.launch(task_config, autostop=N)`, `sky.status()`, `sky.down(instance_id)` — four methods, four fixture files.

---

## Task 1: `live-skypilot` pixi feature env + test-live-skypilot task

**Goal:** Add a pixi feature env carrying `skypilot[gcp]` and a wrapper task so the smoke can be invoked without touching the default env.

**Files:**
- Modify: `pixi.toml` (~12 LOC added)

**Acceptance Criteria:**
- [ ] `pixi info -e live-skypilot` lists `skypilot` in the env's PyPI deps
- [ ] `pixi run -e live-skypilot python -c "import sky; print(sky.__version__)"` exits 0
- [ ] `pixi run test` (default env) does NOT install skypilot (verified by `pixi run python -c "import sky"` exiting non-zero in the default env)
- [ ] `pixi run -e live-skypilot test-live-skypilot --collect-only` collects the (skipped) live test

**Verify:** `pixi info -e live-skypilot | rg skypilot && pixi run -e live-skypilot python -c "import sky"` → 0

**Steps:**

- [ ] **Step 1: Add the feature, environment, and task to `pixi.toml`**

Append to the end of `pixi.toml`:

```toml

[feature.live-skypilot.pypi-dependencies]
skypilot = { version = "*", extras = ["gcp"] }

[environments]
live-skypilot = { features = ["live-skypilot"] }
```

Also insert into the existing `[tasks]` block (after the existing `test-live` task at line 91):

```toml
# SkyPilot live smoke (Phase 31). Lives in a separate feature env so the
# default env stays lean — skypilot pulls ~500MB of transitive deps.
# Gated by KINOFORGE_LIVE_TESTS=1 (set here) + GOOGLE_APPLICATION_CREDENTIALS
# pointing at a service-account key with compute.admin + storage.admin.
# Cost: <= $0.05 per run (smallest GCP CPU SKU x ~30 min wall-clock).
test-live-skypilot = { cmd = "python -m pytest tests/live/test_skypilot_live.py -v", env = { KINOFORGE_LIVE_TESTS = "1" } }
```

- [ ] **Step 2: Resolve the new env**

Run: `pixi install -e live-skypilot`
Expected: pixi resolves the env, installs skypilot, writes a fresh `pixi.lock` entry. Wall-clock 1-3 min on a warm cache.

- [ ] **Step 3: Verify install separation**

Run: `pixi run -e live-skypilot python -c "import sky; print(sky.__version__)"`
Expected: prints `0.X.Y` (skypilot version).

Run: `pixi run python -c "import sky" 2>&1 | tail -1`
Expected: `ModuleNotFoundError: No module named 'sky'` — default env unaffected.

- [ ] **Step 4: Verify pre-commit still green**

Run: `pixi run pre-commit run --files pixi.toml pixi.lock`
Expected: all hooks pass.

- [ ] **Step 5: Commit**

```bash
git add pixi.toml pixi.lock
git commit -m "feat(pixi): live-skypilot feature env + test-live-skypilot task (Phase 31 T1)"
```

---

## Task 2: `_RecordingProxy` + `_to_jsonable` helpers + Ring-2 unit tests

**Goal:** Implement the test-local recording proxy and serializer, with full unit-test coverage that runs in the default env (no skypilot needed).

**Files:**
- Create: `tests/live/_skypilot_recorder.py` (~80 LOC) — the proxy + serializer
- Create: `tests/providers/test_skypilot_recording_proxy.py` (~120 LOC) — 6 ACs
- Modify: `tests/live/__init__.py` (no-op; just import for collection)

**Acceptance Criteria:**
- [ ] Proxy delegates every method call to the underlying object (return value unchanged)
- [ ] `_to_jsonable` handles dataclasses via `dataclasses.asdict`
- [ ] `_to_jsonable` handles enums via `.value`
- [ ] `_to_jsonable` handles `pathlib.Path` via `str()`
- [ ] `_to_jsonable` handles `datetime` via `.isoformat()`
- [ ] Volatile-key sentinel replacement: keys in `VOLATILE_KEYS` (`launched_at`, `cluster_name_on_cloud`, `internal_ip`, `external_ip`, `handle`, `head_ip`) replaced with `"<volatile>"` at any nesting depth
- [ ] JSON file written to `fixture_dir / f"{method_name}.json"` with `sort_keys=True, indent=2`
- [ ] Two successive calls produce byte-identical files

**Verify:** `pixi run test tests/providers/test_skypilot_recording_proxy.py -v` → 8 passed

**Steps:**

- [ ] **Step 1: Write the failing tests first**

Create `tests/providers/test_skypilot_recording_proxy.py`:

```python
"""Ring-2 unit tests for the SkyPilot recording proxy + serializer.

Runs in the default pixi env (no skypilot installed) — the proxy is fed a
DummySky stand-in. See docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md
section 9 for AC list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from tests.live._skypilot_recorder import _RecordingProxy, _to_jsonable


@dataclass
class _SampleHandle:
    cluster_name_on_cloud: str
    region: str


class _SampleStatus(str, Enum):
    UP = "UP"
    INIT = "INIT"


class _DummySky:
    """A minimal sky-shaped object for proxy tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def gpu_list(self) -> list[dict[str, Any]]:
        self.calls.append(("gpu_list", (), {}))
        return [{"name": "T4", "vram_gb": 16, "cost_rate_usd_per_hr": 0.35}]

    def launch(self, task_config: dict[str, Any], autostop: float) -> dict[str, Any]:
        self.calls.append(("launch", (task_config,), {"autostop": autostop}))
        return {
            "cluster_name": "cluster-abc",
            "handle": _SampleHandle(cluster_name_on_cloud="gcp-12345", region="us-central1"),
            "launched_at": datetime(2026, 6, 3, 12, 0, 0),
            "status": _SampleStatus.UP,
        }


def test_proxy_delegates_call_and_returns_real_value(tmp_path: Path) -> None:
    """AC1: every method call passes through unchanged."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    result = proxy.gpu_list()

    assert result == [{"name": "T4", "vram_gb": 16, "cost_rate_usd_per_hr": 0.35}]
    assert real.calls == [("gpu_list", (), {})]


def test_proxy_forwards_args_and_kwargs(tmp_path: Path) -> None:
    """AC1 reinforced: positional + keyword args flow through unchanged."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    proxy.launch({"image": "alpine"}, autostop=1.0)

    assert real.calls == [("launch", ({"image": "alpine"},), {"autostop": 1.0})]


def test_proxy_writes_fixture_file(tmp_path: Path) -> None:
    """AC7: fixture file written to <method_name>.json with sort_keys + indent."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    proxy.gpu_list()

    fixture = tmp_path / "gpu_list.json"
    assert fixture.exists()
    payload = json.loads(fixture.read_text())
    assert payload == [{"cost_rate_usd_per_hr": 0.35, "name": "T4", "vram_gb": 16}]


def test_to_jsonable_handles_dataclass() -> None:
    """AC2: dataclass → asdict()."""
    handle = _SampleHandle(cluster_name_on_cloud="x", region="us")
    assert _to_jsonable(handle) == {"cluster_name_on_cloud": "<volatile>", "region": "us"}


def test_to_jsonable_handles_enum() -> None:
    """AC3: enum → .value."""
    assert _to_jsonable(_SampleStatus.UP) == "UP"


def test_to_jsonable_handles_path_and_datetime() -> None:
    """AC4 + AC5: pathlib + datetime serialise to str/isoformat."""
    assert _to_jsonable(Path("/tmp/x")) == "/tmp/x"
    assert _to_jsonable(datetime(2026, 6, 3, 12, 0, 0)) == "2026-06-03T12:00:00"


def test_to_jsonable_strips_volatile_keys_recursively() -> None:
    """AC6: volatile keys replaced at any nesting depth."""
    payload = {
        "outer_id": "abc",
        "launched_at": datetime(2026, 6, 3),
        "nested": {"internal_ip": "10.0.0.1", "region": "us"},
        "list_of_dicts": [{"head_ip": "10.0.0.2", "name": "n"}],
    }
    result = _to_jsonable(payload)
    assert result == {
        "outer_id": "abc",
        "launched_at": "<volatile>",
        "nested": {"internal_ip": "<volatile>", "region": "us"},
        "list_of_dicts": [{"head_ip": "<volatile>", "name": "n"}],
    }


def test_two_successive_calls_produce_byte_identical_files(tmp_path: Path) -> None:
    """AC8: stability — same input → same bytes."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    proxy.launch({"image": "alpine"}, autostop=1.0)
    first = (tmp_path / "launch.json").read_bytes()

    proxy.launch({"image": "alpine"}, autostop=1.0)
    second = (tmp_path / "launch.json").read_bytes()

    assert first == second
```

- [ ] **Step 2: Run tests to verify they FAIL**

Run: `pixi run test tests/providers/test_skypilot_recording_proxy.py -v`
Expected: 8 FAILED with `ModuleNotFoundError: No module named 'tests.live._skypilot_recorder'` — file doesn't exist yet.

- [ ] **Step 3: Implement the recorder module**

Create `tests/live/_skypilot_recorder.py`:

```python
"""SkyPilot SDK return-shape recording proxy.

Test-scope only. Production code never imports this module.

Wraps the real `sky` module so each method call delegates, then JSON-
serializes the return value via :func:`_to_jsonable` (which strips
volatile fields with a sentinel) into ``<fixture_dir>/<method_name>.json``.
PR reviewers diff those files — the diff IS the review surface.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "launched_at",
        "cluster_name_on_cloud",
        "internal_ip",
        "external_ip",
        "handle",
        "head_ip",
    }
)

_VOLATILE_SENTINEL: str = "<volatile>"


def _to_jsonable(obj: Any) -> Any:  # noqa: ANN401
    """Convert an SDK return value to a JSON-serialisable form.

    Handles dataclasses, enums, ``pathlib.Path``, ``datetime``, and arbitrary
    nested dicts/lists. Keys in :data:`VOLATILE_KEYS` are replaced with
    :data:`_VOLATILE_SENTINEL` so PR diffs surface shape changes, not noise.

    Args:
        obj: Any return value from an SDK call.

    Returns:
        A value safe to pass to ``json.dumps`` with ``default=str``.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _to_jsonable(dataclasses.asdict(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {
            k: (_VOLATILE_SENTINEL if k in VOLATILE_KEYS else _to_jsonable(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(item) for item in obj]
    return obj


class _RecordingProxy:
    """Wraps a real SDK object; records each method call's return value.

    On every attribute access, returns a wrapper that calls the underlying
    method, JSON-serialises the result to ``<fixture_dir>/<name>.json``
    (last-call-wins), and returns the original result unchanged.

    The proxy is duck-compatible with anything that exposes attributes via
    ``getattr`` (modules, instances). It does not record non-callable
    attribute accesses.
    """

    def __init__(self, real: Any, fixture_dir: Path) -> None:
        """Construct the proxy.

        Args:
            real: The object whose method calls should be recorded.
            fixture_dir: Directory in which to write ``<name>.json`` files.
                Created if it does not exist.
        """
        self._real = real
        self._fixture_dir = fixture_dir
        fixture_dir.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """Return a wrapper around ``self._real.<name>``."""
        target = getattr(self._real, name)
        if not callable(target):
            return target

        def _wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            result = target(*args, **kwargs)
            payload = _to_jsonable(result)
            fixture_path = self._fixture_dir / f"{name}.json"
            fixture_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n"
            )
            return result

        return _wrapper
```

- [ ] **Step 4: Run tests to verify they PASS**

Run: `pixi run test tests/providers/test_skypilot_recording_proxy.py -v`
Expected: 8 PASSED.

- [ ] **Step 5: Confirm typecheck + lint**

Run: `pixi run typecheck tests/live/_skypilot_recorder.py tests/providers/test_skypilot_recording_proxy.py && pixi run lint tests/live/_skypilot_recorder.py tests/providers/test_skypilot_recording_proxy.py`
Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
git add tests/live/_skypilot_recorder.py tests/providers/test_skypilot_recording_proxy.py
git commit -m "feat(tests/live): SkyPilot recording proxy + 8 Ring-2 unit tests (Phase 31 T2)"
```

---

## Task 3: `examples/configs/skypilot.yaml` operator config example

**Goal:** Ship a runnable example YAML for SkyPilot+GCS so operators have a starting template; verify it parses through `Config.load()`.

**Files:**
- Create: `examples/configs/skypilot.yaml` (~80 LOC)
- Modify: `tests/test_examples.py` — extend the existing parametrised parse test

**Acceptance Criteria:**
- [ ] `examples/configs/skypilot.yaml` parses through `Config.load_config(path)` without error
- [ ] Config has `compute.provider == "skypilot"`, `compute.region == "us-central1"`, `lifecycle.idle_timeout_s == 60`
- [ ] The 4 base + 6 segment + commented-store layout mirrors `examples/configs/wan.yaml`

**Verify:** `pixi run test tests/test_examples.py -v -k skypilot` → 1+ passed

**Steps:**

- [ ] **Step 1: Inspect the existing example shape**

Run: `pixi run test tests/test_examples.py -v --collect-only | head -30`
Note the existing parametrised structure so the new entry follows it.

- [ ] **Step 2: Write the failing test (extend the existing parametrise list)**

Edit `tests/test_examples.py` — find the existing list of example paths and add `"examples/configs/skypilot.yaml"`. If the test uses `@pytest.mark.parametrize`, just add the path; the assertion body should already check `load_config(path)` succeeds.

If no parametrised parse exists, add this minimal test:

```python
def test_skypilot_example_parses() -> None:
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/skypilot.yaml"))
    assert cfg.compute.provider == "skypilot"
    assert cfg.compute.region == "us-central1"
    assert cfg.lifecycle.idle_timeout_s == 60
```

Run: `pixi run test tests/test_examples.py -v -k skypilot`
Expected: FAIL with `FileNotFoundError` — yaml not created yet.

- [ ] **Step 3: Create `examples/configs/skypilot.yaml`**

```yaml
# kinoforge config — SkyPilot compute (GCP) + optional GCS artifact store
#
# Mirror of examples/configs/wan.yaml shape; differs only in the compute
# block. See docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md
# for the design rationale.
#
# Live verification: `pixi run -e live-skypilot test-live-skypilot`
# (Phase 31 layer). Cost: <= $0.05 per smoke run (CPU SKU + autostop=1).

models:
  - name: wan-i2v-base
    kind: base
    ref: "hf:Wan-AI/Wan2.2-Animate-14B"

requirements:
  vram_gb: 24
  cuda: "12.1"

lifecycle:
  idle_timeout_s: 60         # maps to SkyPilot autostop=1 (minute)
  max_lifetime_s: 1800
  max_in_flight: 1

compute:
  provider: skypilot
  region: us-central1
  # SkyPilot picks the SKU from cpus/memory; the live smoke uses the
  # smallest available CPU class. Real workloads override with
  # requirements.vram_gb to drive GPU selection.

engine:
  kind: comfyui
  # Engine-on-SkyPilot smoke is deferred (see spec section 7 — scope cuts).
  # This block is a placeholder so the YAML parses; replace with a real
  # ComfyUI engine config when running actual workloads.

# Optional: GCS artifact store. Uncomment + fill in bucket name.
#
# store:
#   kind: gcs
#   bucket: my-org-kinoforge
#   prefix: prod/runs
#
# Authentication: GOOGLE_APPLICATION_CREDENTIALS must point at a service-
# account key JSON (see .env.example for the kinoforge-runner recipe).
```

- [ ] **Step 4: Run test to verify it PASSES**

Run: `pixi run test tests/test_examples.py -v -k skypilot`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/configs/skypilot.yaml tests/test_examples.py
git commit -m "feat(examples): skypilot.yaml config example + parse test (Phase 31 T3)"
```

---

## Task 4: Extend `tools/preflight.py` with SkyPilot cluster check

**Goal:** Add `_check_no_active_sky_clusters()` so `pixi run preflight` fails fast if any leftover SkyPilot cluster is still running before live spend.

**Files:**
- Modify: `tools/preflight.py` (~25 LOC added)
- Modify: `tests/test_preflight.py` (or whichever file holds preflight tests; locate first)

**Acceptance Criteria:**
- [ ] When `skypilot` is not installed, the new check logs `"skypilot not installed; skipping"` and returns OK (does NOT fail preflight)
- [ ] When `skypilot` is installed AND `sky.status()` returns an empty list, the check passes
- [ ] When `skypilot` is installed AND `sky.status()` returns at least one cluster with status `UP` or `INIT`, the check fails with the cluster names in the error message
- [ ] Existing preflight checks (RunPod pods, creds, clean tree) remain unchanged

**Verify:** `pixi run test tests/test_preflight.py -v -k sky` → all new tests pass + existing tests still green.

**Steps:**

- [ ] **Step 1: Locate existing preflight structure**

Run: `pixi run test tests/ -v -k preflight --collect-only | head -20`
Read `tools/preflight.py:1-50` and the existing RunPod check to mirror the pattern.

- [ ] **Step 2: Write failing tests first**

Add to `tests/test_preflight.py`:

```python
def test_check_no_active_sky_clusters_skipped_when_skypilot_missing(monkeypatch, caplog):
    """When skypilot not installed, the check skips with a clear log line."""
    import builtins
    real_import = builtins.__import__

    def _fail_sky(name, *a, **kw):
        if name == "sky":
            raise ImportError("skypilot not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_sky)
    from tools.preflight import _check_no_active_sky_clusters

    caplog.set_level("INFO")
    assert _check_no_active_sky_clusters() is True
    assert "skypilot not installed; skipping" in caplog.text


def test_check_no_active_sky_clusters_passes_when_empty(monkeypatch):
    """When sky.status() returns empty, the check passes."""
    import sys
    fake_sky = type(sys)("sky")
    fake_sky.status = lambda **kw: []
    monkeypatch.setitem(sys.modules, "sky", fake_sky)

    from tools.preflight import _check_no_active_sky_clusters

    assert _check_no_active_sky_clusters() is True


def test_check_no_active_sky_clusters_fails_when_cluster_up(monkeypatch, capsys):
    """When sky.status() shows an UP cluster, the check fails with the name."""
    import sys
    fake_sky = type(sys)("sky")
    fake_sky.status = lambda **kw: [
        {"name": "leftover-cluster", "status": "UP"},
        {"name": "another", "status": "INIT"},
    ]
    monkeypatch.setitem(sys.modules, "sky", fake_sky)

    from tools.preflight import _check_no_active_sky_clusters

    assert _check_no_active_sky_clusters() is False
    captured = capsys.readouterr()
    assert "leftover-cluster" in captured.err or "leftover-cluster" in captured.out
    assert "another" in captured.err or "another" in captured.out
```

Run: `pixi run test tests/test_preflight.py -v -k sky`
Expected: 3 FAILED with `ImportError: cannot import name '_check_no_active_sky_clusters'`.

- [ ] **Step 3: Implement the check**

Add to `tools/preflight.py`, mirroring the existing `_check_no_active_runpod_pods()`:

```python
def _check_no_active_sky_clusters() -> bool:
    """Verify no SkyPilot clusters are currently UP or INIT.

    SkyPilot is optional infrastructure — this check skips silently if
    the SDK is not installed in the active env (e.g. the default env).
    When the SDK IS installed, any active cluster is treated as leaked
    state from a prior live run and fails preflight loud.

    Returns:
        True if safe to proceed (no clusters OR skypilot not installed);
        False if leaked clusters were found.
    """
    try:
        import sky
    except ImportError:
        _log.info("skypilot not installed; skipping SkyPilot cluster check")
        return True

    clusters = sky.status()
    active = [
        c for c in clusters if c.get("status") in {"UP", "INIT"}
    ]
    if active:
        names = ", ".join(c.get("name", "<unknown>") for c in active)
        print(
            f"FAIL: active SkyPilot clusters present: {names}\n"
            f"      run `sky down <name>` for each before invoking the live smoke",
            file=sys.stderr,
        )
        return False
    return True
```

Wire it into the existing `main()` / check list near the RunPod check.

- [ ] **Step 4: Run tests to verify they PASS**

Run: `pixi run test tests/test_preflight.py -v`
Expected: all preflight tests pass (new + existing).

- [ ] **Step 5: Smoke the actual preflight command**

Run: `pixi run preflight`
Expected: exit 0 (no clusters; skypilot not in default env so SkyPilot check skips with the log line).

Run: `pixi run -e live-skypilot preflight`
Expected: exit 0 (skypilot is installed in this env but `sky.status()` returns empty — no clusters created yet).

- [ ] **Step 6: Commit**

```bash
git add tools/preflight.py tests/test_preflight.py
git commit -m "feat(preflight): SkyPilot cluster check + 3 unit tests (Phase 31 T4)"
```

---

## Task 5: RED live-smoke scaffold — `tests/live/test_skypilot_live.py`

**Goal:** Commit the full live-smoke scaffold (test function, helpers, gates, teardown) BEFORE any live spend, per CLAUDE.md's "Commit RED scaffolds before any live spend" mandate. The test should be **skipped** under default conditions and **runnable but failing** under `live-skypilot` env until GCP is reachable.

**Files:**
- Create: `tests/live/test_skypilot_live.py` (~220 LOC)

**Acceptance Criteria:**
- [ ] Default `pixi run test tests/live/test_skypilot_live.py` reports the test as skipped (no live execution attempted)
- [ ] `pixi run -e live-skypilot test-live-skypilot --collect-only` collects the test (env vars unset → still skipped)
- [ ] Module imports cleanly: `pixi run -e live-skypilot python -c "import tests.live.test_skypilot_live"` exits 0
- [ ] Skip message lists every missing precondition (`KINOFORGE_LIVE_TESTS`, `GOOGLE_APPLICATION_CREDENTIALS`, `sky` import)
- [ ] Teardown helper is unit-testable (no live deps for its branches)

**Verify:** `pixi run test tests/live/test_skypilot_live.py -v` → reports skipped.

**Steps:**

- [ ] **Step 1: Write the scaffold**

Create `tests/live/test_skypilot_live.py`:

```python
"""Opt-in live smoke: SkyPilot CPU lifecycle (Phase 31).

Validates the lazy `sky` SDK path of :class:`SkyPilotProvider` against
real GCP, captures SDK return shapes as JSON fixtures, and tears down
with a four-tier safety net.

Gated by three preconditions (module-level skip if any are missing):
- ``KINOFORGE_LIVE_TESTS=1`` — global live-test gate (project convention).
- ``GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json`` — GCP service account.
- ``import sky`` succeeds — requires `pixi run -e live-skypilot ...`.

Cost: <= $0.05 per run (smallest GCP CPU SKU, ~30 min wall-clock max,
autostop=1).

Fixtures land in ``tests/providers/fixtures/skypilot/*.json`` (last-call-
wins per method). See ``docs/superpowers/specs/2026-06-03-skypilot-real-
cloud-design.md`` for the design.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_REASONS: list[str] = []
if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
    _REASONS.append("KINOFORGE_LIVE_TESTS=1 required")
if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    _REASONS.append("GOOGLE_APPLICATION_CREDENTIALS must be set")
try:
    import sky as _sky_module  # noqa: F401
except ImportError:
    _REASONS.append(
        "skypilot[gcp] not installed in the active env "
        "(use `pixi run -e live-skypilot`)"
    )

if _REASONS:
    pytest.skip(
        "SkyPilot live smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
import sky  # noqa: E402

from kinoforge.core.interfaces import (  # noqa: E402
    HardwareRequirements,
    InstanceSpec,
    Lifecycle,
)
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from tests.live._skypilot_recorder import _RecordingProxy  # noqa: E402

_log = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent.parent / "providers" / "fixtures" / "skypilot"
_POLL_INTERVAL_S: float = 5.0
_READY_TIMEOUT_S: float = 600.0  # 10 min
_DESTROY_TIMEOUT_S: float = 300.0  # 5 min

HW_REQS_CPU = HardwareRequirements(vram_gb=0, cuda="0.0")


def _poll_until_ready(
    provider: SkyPilotProvider,
    instance_id: str,
    timeout_s: float,
) -> None:
    """Poll ``provider.list_instances()`` until the cluster reports ``UP``.

    Raises:
        TimeoutError: cluster did not reach UP within ``timeout_s``.
    """
    start = time.time()
    while time.time() - start < timeout_s:
        instances = provider.list_instances()
        for inst in instances:
            if inst.id == instance_id:
                elapsed = int(time.time() - start)
                _log.info("cluster status=%s elapsed=%ds", inst.status, elapsed)
                if inst.status in {"ready", "running", "UP"}:
                    return
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(
        f"cluster {instance_id!r} did not reach UP within {timeout_s}s"
    )


def _teardown(provider: SkyPilotProvider, cluster_name: str) -> None:
    """Four-tier teardown. Each tier logs + swallows its own exception so
    the next tier always runs. Final tier re-raises if survivors remain."""
    # Tier 1: provider.destroy_instance
    try:
        _log.info("tearing down via provider.destroy_instance")
        provider.destroy_instance(cluster_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("provider.destroy_instance raised: %r", exc)

    # Tier 2: direct sky.down
    try:
        _log.info("tearing down via direct sky.down")
        sky.down(cluster_name, purge=True)
    except Exception as exc:  # noqa: BLE001
        _log.warning("sky.down raised: %r", exc)

    # Tier 3: gcloud nuclear
    try:
        _log.info("tearing down via gcloud nuclear")
        listing = subprocess.run(
            [
                "gcloud", "compute", "instances", "list",
                "--filter", f"labels.skypilot-cluster={cluster_name}",
                "--format", "value(name,zone)",
            ],
            capture_output=True, text=True, check=False, timeout=60,
        )
        for line in listing.stdout.strip().splitlines():
            if not line.strip():
                continue
            name, _, zone = line.partition("\t")
            subprocess.run(
                [
                    "gcloud", "compute", "instances", "delete", name.strip(),
                    "--zone", zone.strip(), "--quiet",
                ],
                check=False, timeout=120,
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("gcloud nuclear teardown raised: %r", exc)

    # Tier 4: final verification — raise if survivors
    survivors = subprocess.run(
        [
            "gcloud", "compute", "instances", "list",
            "--filter", f"labels.skypilot-cluster={cluster_name}",
            "--format", "value(name)",
        ],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if survivors.stdout.strip():
        raise RuntimeError(
            f"CLEANUP FAILED — manual VM deletion required: "
            f"{survivors.stdout.strip()}"
        )
    _log.info("teardown complete cluster=%s", cluster_name)


def test_skypilot_live_e2e_cpu_lifecycle_smoke() -> None:
    """End-to-end live smoke: gpu_list → launch → status → endpoints → down.

    Validates the lazy `sky` SDK path. Fixtures are written by the
    ``_RecordingProxy`` wrapping every ``sky.*`` call; the test asserts
    the high-level provider contract (cluster reaches UP, list contains
    the cluster, endpoints formed, teardown succeeds).
    """
    cluster_name = f"kinoforge-skypilot-smoke-{secrets.token_hex(4)}"
    provider = SkyPilotProvider(sky_client=_RecordingProxy(sky, FIXTURE_DIR))

    try:
        offers = provider.find_offers(HW_REQS_CPU)
        _log.info("find_offers returned %d offers", len(offers))
        assert offers, "expected at least one CPU offer from sky.gpu_list()"

        lifecycle = Lifecycle(idle_timeout_s=60, max_lifetime_s=1800)
        spec = InstanceSpec(
            run_id=cluster_name,
            image="alpine:3",
            env={},
            tags={"layer": "phase-31-smoke"},
            lifecycle=lifecycle,
            offer=offers[0],
            provision_script="",
            run_cmd=("sleep", "60"),
        )
        _log.info(
            "launching cluster=%s region=us-central1 cpus=1+ memory=2+ autostop=1",
            cluster_name,
        )
        inst = provider.create_instance(spec)

        _poll_until_ready(provider, inst.id, timeout_s=_READY_TIMEOUT_S)

        ep = provider.endpoints(inst)
        assert ep["ssh"].startswith("ssh://"), f"bad endpoint: {ep!r}"

        listed = provider.list_instances()
        assert any(i.id == inst.id for i in listed), (
            f"cluster {inst.id!r} missing from list_instances()"
        )
    finally:
        _teardown(provider, cluster_name)
```

- [ ] **Step 2: Verify the module skips under default env**

Run: `pixi run test tests/live/test_skypilot_live.py -v`
Expected: `SKIPPED [1] tests/live/test_skypilot_live.py: SkyPilot live smoke skipped: ...`.

- [ ] **Step 3: Verify the module imports under live env**

Run: `pixi run -e live-skypilot python -c "import tests.live.test_skypilot_live as m; print('imports OK; skip reason:', getattr(m, 'pytest', None))"`
Expected: exits 0; module-level skip path triggers because `KINOFORGE_LIVE_TESTS` unset.

- [ ] **Step 4: Verify collection under the env+gate**

Run: `pixi run -e live-skypilot test-live-skypilot --collect-only`
Expected: 1 test collected; reported as skipped because `GOOGLE_APPLICATION_CREDENTIALS` is set but the test does not actually run yet (this is just `--collect-only`).

- [ ] **Step 5: Pre-commit + lint**

Run: `pixi run pre-commit run --files tests/live/test_skypilot_live.py`
Expected: all hooks pass.

- [ ] **Step 6: Commit the RED scaffold (BEFORE any live spend)**

```bash
git add tests/live/test_skypilot_live.py
git commit -m "feat(tests/live): RED scaffold for SkyPilot live smoke (Phase 31 T5)

Committed BEFORE live spend per CLAUDE.md \"Commit RED scaffolds before
any live spend\" mandate. Module skips under default conditions; the
actual live invocation happens in T6."
```

---

## Task 6: First live invocation — produce fixtures

**Goal:** Execute the smoke against real GCP. This is the only task that spends money.

**Files:**
- Create (via test run): `tests/providers/fixtures/skypilot/gpu_list.json`
- Create (via test run): `tests/providers/fixtures/skypilot/launch.json`
- Create (via test run): `tests/providers/fixtures/skypilot/status.json`
- Create (via test run): `tests/providers/fixtures/skypilot/down.json`

**Acceptance Criteria:**
- [ ] `pixi run -e live-skypilot preflight` exits 0 (zero clusters, clean tree, creds reachable)
- [ ] Git working tree is clean (RED scaffold from T5 committed; this is a CLAUDE.md hard rule)
- [ ] `pixi run -e live-skypilot test-live-skypilot -v` exits 0
- [ ] Wall-clock < 10 min
- [ ] All 4 fixture files exist under `tests/providers/fixtures/skypilot/` after the run
- [ ] `gcloud compute instances list --project=$(gcloud config get-value project) --format=json` returns `[]` within 2 min of test exit

**Verify:** `gcloud compute instances list --format=json | python -c "import json,sys; assert json.load(sys.stdin) == [], 'survivors!'"` → 0 (empty list)

**Steps:**

- [ ] **Step 1: Pre-live-spend ceremony (CLAUDE.md mandate)**

Run all four in order; each must exit 0 before proceeding:

```bash
git status                                                # must be clean
pixi run -e live-skypilot pre-commit run --all-files     # must pass
pixi run -e live-skypilot test                            # 1083 passed (Rings 1 + 2)
pixi run -e live-skypilot preflight                       # must exit 0
```

If any fails: fix the underlying issue and re-run from Step 1. Do NOT proceed to Step 2 until all four are green.

- [ ] **Step 2: Sanity-check the GCP target**

Run: `gcloud config get-value project && gcloud auth list --filter=status:ACTIVE --format="value(account)"`
Expected: project ID matches the one in `.env` (`<GCP_PROJECT>` or whatever is current); active account is the SA email.

Run: `gcloud compute instances list --format=json`
Expected: `[]` — no VMs in the project before the smoke runs.

- [ ] **Step 3: Invoke the live smoke**

```bash
pixi run -e live-skypilot test-live-skypilot -v --capture=no 2>&1 | tee /tmp/skypilot-smoke.log
```

Expected: 1 PASSED in 5-10 min. Real-time log shows:
- `find_offers returned N offers`
- `launching cluster=kinoforge-skypilot-smoke-<8hex> region=us-central1 cpus=1+ memory=2+ autostop=1`
- `cluster status=<X> elapsed=<Ns>` (multiple lines as it provisions)
- `tearing down via provider.destroy_instance`
- `teardown complete cluster=...`

If any tier of teardown raises, the log will show `WARNING ... raised: ...` lines but the test still proceeds through subsequent tiers.

- [ ] **Step 4: Verify zero survivors**

```bash
gcloud compute instances list --format=json
```

Expected: `[]`. If non-empty, manually run:
```bash
for INST in $(gcloud compute instances list --format="value(name,zone)" | tr '\t' ':'); do
  NAME=${INST%:*}; ZONE=${INST#*:}
  gcloud compute instances delete "$NAME" --zone "$ZONE" --quiet
done
```
Then re-run `gcloud compute instances list --format=json` until empty.

- [ ] **Step 5: Inspect the captured fixtures**

```bash
ls -la tests/providers/fixtures/skypilot/
for f in tests/providers/fixtures/skypilot/*.json; do
  echo "=== $f ==="
  head -20 "$f"
  echo "..."
done
```

Expected: 4 files (`gpu_list.json`, `launch.json`, `status.json`, `down.json`), each non-empty, each parses as valid JSON. Volatile-stripped fields show `"<volatile>"` where expected.

- [ ] **Step 6: Re-run preflight (clean state check)**

```bash
pixi run -e live-skypilot preflight
```

Expected: exit 0 — no SkyPilot clusters, no leaked RunPod pods.

- [ ] **Step 7: Do NOT commit fixtures yet**

Fixture commit happens in T7 after the stability re-run confirms byte-identical output. Leave the 4 files untracked.

```bash
git status   # 4 untracked .json files under tests/providers/fixtures/skypilot/
```

---

## Task 7: Stability re-run + commit fixtures

**Goal:** Confirm the fixtures are byte-identical across two successive runs (proving the volatile-stripping works), then commit them in their own commit so the PR diff isolates the captured-shape review surface.

**Files:**
- Modify (via test run): same 4 fixtures from T6 — must produce identical bytes
- Commit: `tests/providers/fixtures/skypilot/*.json`

**Acceptance Criteria:**
- [ ] Second smoke run exits 0
- [ ] All 4 fixture files are byte-identical to the T6 run (`diff -r` shows no changes)
- [ ] Fixtures are committed in their own commit, separate from the RED scaffold

**Verify:** After re-run, `git diff --stat tests/providers/fixtures/skypilot/` → empty.

**Steps:**

- [ ] **Step 1: Snapshot the T6 fixtures**

```bash
mkdir -p /tmp/skypilot-fixtures-t6
cp tests/providers/fixtures/skypilot/*.json /tmp/skypilot-fixtures-t6/
```

- [ ] **Step 2: Re-run the smoke**

```bash
pixi run -e live-skypilot test-live-skypilot -v
```

Expected: 1 PASSED.

- [ ] **Step 3: Diff fixtures**

```bash
diff -r /tmp/skypilot-fixtures-t6/ tests/providers/fixtures/skypilot/
```

Expected: no output (files identical).

If there IS a diff, look at the keys that changed:
- A new key that varies run-to-run → add to `VOLATILE_KEYS` in `tests/live/_skypilot_recorder.py` and re-run from T6 Step 3.
- A shape change → SkyPilot SDK is non-deterministic in a way the volatile sentinel can't normalise; document in PROGRESS.md and accept the diff if cosmetic.

- [ ] **Step 4: Verify zero VM survivors**

```bash
gcloud compute instances list --format=json
```
Expected: `[]`.

- [ ] **Step 5: Commit the fixtures (their own commit, isolating the review surface)**

```bash
git add tests/providers/fixtures/skypilot/*.json
git commit -m "test(fixtures): SkyPilot SDK return-shape fixtures (Phase 31 T7)

Captured via tests/live/test_skypilot_live.py against real GCP. Diff
THESE files when reviewing SkyPilot SDK upgrades — they ARE the review
surface for SDK contract drift.

4 fixtures: gpu_list, launch, status, down. Volatile fields (timestamps,
IPs, cluster_name_on_cloud, handle) replaced with \"<volatile>\" so PR
diffs surface shape changes, not run-to-run noise."
```

- [ ] **Step 6: If the live run surfaced a `SkyPilotProvider` shape disagreement…**

…add a sub-task here. Each shape-disagreement fix is its own:
1. Failing offline regression test in `tests/providers/test_skypilot.py` that captures the real shape
2. Provider patch
3. Re-run the live smoke to confirm fixtures regenerate identically
4. Commit the fix + regression test together: `fix(providers/skypilot): <what>. (Phase 31 T7 follow-up)`

If zero disagreements: skip this step and move to T8.

---

## Task 8: PROGRESS.md Phase 31 entry + strike PROGRESS:114 #2

**Goal:** Document the layer in PROGRESS.md mirroring the Phase 30 shape, strike the closed carry-forward, and add the new "CPU-only" known-limitation marker for whoever picks up the GPU-smoke layer.

**Files:**
- Modify: `PROGRESS.md` (~80 LOC added in the new Phase 31 entry; ~2 LOC modified in the known-limitations section)

**Acceptance Criteria:**
- [ ] New `### Phase 31 — SkyPilot real-cloud verification (PROGRESS:114 #2)` entry exists with: per-task SHAs (T1-T7), key design decisions, live-smoke confirmation block (wall-clock seconds, cluster name pattern, estimated cost, fixture sha256 chain), test count delta (1071 → 1083 pre-live + 1 post-live skip)
- [ ] PROGRESS:114 known-limitations list — the bullet *"`SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK"* is struck through with `~~...~~ — **CLOSED** by Phase 31.`
- [ ] A new known-limitation appears: *"SkyPilot live smoke is CPU lifecycle only; GPU + engine smokes remain deferred (see spec section 7)."*
- [ ] The "RESUME — START HERE" block is refreshed: new HEAD SHA, new test count, next-layer candidates list updated

**Verify:** `rg -n 'Phase 31' PROGRESS.md | head -5 && rg -n 'CLOSED.*Phase 31' PROGRESS.md`

**Steps:**

- [ ] **Step 1: Compute SHAs and the test-count delta**

```bash
git log --oneline -20                      # capture T1-T7 commit SHAs
pixi run test 2>&1 | tail -1                # capture default-env count
pixi run -e live-skypilot test 2>&1 | tail -1   # capture live-env count
ls -la tests/providers/fixtures/skypilot/   # capture fixture filenames
sha256sum tests/providers/fixtures/skypilot/*.json   # fixture sha256 chain
```

Record outputs in a scratchpad. These go verbatim into the Phase 31 entry.

- [ ] **Step 2: Append the Phase 31 entry**

Insert above the current "## Post-MVP" header (or wherever Phase 30's entry sits), mirroring its shape:

```markdown
### Phase 31 — SkyPilot real-cloud verification (PROGRESS:114 #2)

Closes the dormant `SkyPilotProvider._get_sky()` lazy path against real
GCP. CPU-only bare lifecycle smoke (Layer-N analog); captures four SDK
return-shape fixtures (`gpu_list`, `launch`, `status`, `down`) as the PR
review surface for future SDK upgrades.

- Spec: `docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md`
- Plan: `docs/superpowers/plans/2026-06-03-skypilot-real-cloud.md`
- T1 (pixi feature env): `<SHA>`
- T2 (recording proxy + 8 Ring-2 tests): `<SHA>`
- T3 (examples/configs/skypilot.yaml + parse test): `<SHA>`
- T4 (preflight extension + 3 tests): `<SHA>`
- T5 (RED live-smoke scaffold, pre-spend): `<SHA>`
- T6 (first live invocation; fixtures uncommitted): `<SHA — empty if no code commit>`
- T7 (fixtures committed in own commit): `<SHA>`

**Key design decisions:**
- Bare CPU lifecycle only (Q1=A): GPU smoke deferred to a future layer
  when GPU quota is approved AND a specific GPU bug class needs chasing.
- Fixture capture via decorator-based seam in test code (Q6=A): zero
  production-code touch; sibling pattern to Layer N's HTTP recorder.
- Four-tier teardown (Q3=B): `try/finally` + `autostop=1` server-side
  safety + extended preflight + `gcloud` nuclear fallback. Worst case
  (process killed mid-launch, sky state corrupt, gcloud broken): VM
  self-terminates ~1 min after orchestrator dies.
- Pixi feature env `live-skypilot` (Q4=A): default `pixi run test` stays
  lean; `skypilot[gcp]` (~500MB transitive) only in the live env.
- Full method coverage (Q5=A): `gpu_list → launch → status → down`. Layer
  N caught most RunPod bugs in `find_offers` — likely the same here.

**Live-smoke confirmation (T7 stability re-run):**

```
KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot test-live-skypilot -v
============================== 1 passed in <N>s ===============================
```

Wall-clock: <N> seconds.  Cluster name: `kinoforge-skypilot-smoke-<8hex>`.
Estimated cost: $<X> (<smallest CPU SKU> × <wall-clock-hr>).

Fixture sha256 chain (byte-identical across two successive runs):
- `gpu_list.json`: `<sha256>`
- `launch.json`: `<sha256>`
- `status.json`: `<sha256>`
- `down.json`: `<sha256>`

**Test count:** ~1071 (post-Phase-30) → ~1083 (post-Phase-31 T1-T5,
pre-live-smoke).  Delta: +12 net new (T2 +8, T3 +1, T4 +3).  T6/T7 adds
+1 more (live smoke) when `KINOFORGE_LIVE_TESTS=1 + live-skypilot env`;
default-skip count goes from 5 to 6.

**Out of scope (carry-forward):**
- GPU lifecycle smoke (CPU was sufficient to validate SDK shape).
- Engine-on-SkyPilot smoke (ComfyUI/Wan via SkyPilot setup, ~$2-5/run).
- Multi-cloud verification (AWS, Azure, Lambda Labs).
- Retroactive backfill of offline tests from fixtures.
- Cross-process recording (kinoforge CLI subprocess invoked by pytest).

Closes PROGRESS:114 carry-forward #2.
```

- [ ] **Step 3: Strike the closed limitation**

Find the line:
```markdown
- `SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK.
```

Replace with:
```markdown
- ~~`SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK.~~ — **CLOSED** by Phase 31 (CPU lifecycle smoke against real GCP; 4 SDK fixtures captured).
```

- [ ] **Step 4: Add the new known-limitation**

Insert under "Real-cloud verification gaps":
```markdown
- SkyPilot live smoke is CPU lifecycle only; GPU + engine smokes remain deferred (see `docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md` section 7 for scope cuts).
```

- [ ] **Step 5: Refresh the RESUME block**

Update HEAD SHA, test counts, and remove "real-cloud verification of SkyPilot" from the next-layer-candidates list (it's now closed).

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files PROGRESS.md
git add PROGRESS.md
git commit -m "docs(progress): Phase 31 entry + close PROGRESS:114 #2 (Phase 31 T8)"
```

- [ ] **Step 7: Backfill the Phase 31 entry's own SHA into PROGRESS.md**

The T8 commit just made is itself a SHA referenced in the entry. After committing, run:
```bash
git log --oneline -3
# Substitute the T8 SHA into the T7 entry above (T8 has no SHA of its own
# — the SHA chain ends at T7 + this PROGRESS-only commit)
```

If the entry omits the T8 SHA reference, leave it — T8 is a docs-only commit and conventionally doesn't self-reference. The Phase 30 + Phase 24 entries follow the same convention.

---

## Self-review

1. **Spec coverage:** every section maps to at least one task:
   - §1 (Locked decisions) → reflected across T1 (Q4), T2 (Q2/Q6), T5 (Q3), T6/T7 (Q5)
   - §2 (Architecture / files) → T1-T7 each names files explicitly
   - §3 (Components) → T2 (3.1), T5 (3.2), T3 (3.3), T1 (3.4), T4 (3.5), T7 follow-up (3.6)
   - §4 (Data flow) → T5 scaffold + T6/T7 capture realise this
   - §5 (Error handling) → encoded in T5 teardown helper + skip gates
   - §6 (Acceptance criteria) → T6 ACs lift §6 hard gates 1-7 verbatim
   - §7 (Scope cuts) → not a task; documented in T8 PROGRESS entry
   - §8 (Post-layer follow-ups) → T8 Steps 3-4
   - §9 (Testing strategy) → Ring 1 untouched (no task), Ring 2 = T2, Ring 3 = T5/T6/T7
   - §10 (References) → not implemented (informational)
2. **Placeholder scan:** ran — no "TBD" / "implement later" / vague handwaves. Two intentional placeholders in T8 step 2 are explicit `<SHA>` template fields the engineer fills in from `git log` at execution time, which is the correct pattern (Phase 30 entry uses the same).
3. **Type / name consistency:** `_RecordingProxy`, `_to_jsonable`, `VOLATILE_KEYS`, `_check_no_active_sky_clusters`, `_teardown`, `_poll_until_ready` all consistent across T2 / T4 / T5 / T6 / T7. Provider method names match the actual source (`gpu_list`, `launch`, `status`, `down`).

No user-gate tasks were tagged (spec verbs are routine TDD; no Scope/Proof signals beyond the natural test cycle).

---
