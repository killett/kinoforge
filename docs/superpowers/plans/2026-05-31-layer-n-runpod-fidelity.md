# Layer N — RunPod cloud-fidelity hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lock the `RunPodProvider` GraphQL response shape against the real RunPod API via committed JSON fixtures captured during a live opt-in end-to-end smoke (ComfyUI + Wan i2v), and refactor the 24 existing offline tests to load those fixtures so future schema drift fails loudly offline.

**Architecture:** Three-layer verification stack. (1) A recording HTTP-seam helper (`tests/providers/conftest_runpod.py`) wraps `_urllib_post_json`/`_urllib_get_json` to dispatch each GraphQL call to a fixed fixture filename via an operation-name table, redacting any field name matching `(?i)(token|key|secret|password)` before write. (2) An opt-in live E2E test (`tests/live/test_runpod_live.py`) gated by `KINOFORGE_LIVE_TESTS=1` + `RUNPOD_API_KEY` + `RUNPOD_TERMINATE_KEY` drives `kinoforge generate` against `examples/configs/runpod-comfyui-wan.yaml`, produces a real MP4, and always destroys the pod in a `finally` block. (3) The existing `tests/providers/test_runpod.py` (24 tests) is refactored to load JSON via `_load_fixture(name)` — production code stays untouched unless the live smoke surfaces a real bug.

**Tech Stack:** Python 3.11+, pytest, pixi tasks (`pixi run test`/`lint`/`format`/`typecheck`), pydantic v2 (existing `Config`), stdlib `json`/`urllib`/`pathlib`, RunPod GraphQL API, ComfyUI 0.3.x, Wan 2.2 i2v.

**Spec convention deviation (note):** the spec §3 references `KINOFORGE_LIVE_RUNPOD=1` as the env-var gate. The established convention in the existing fal live test (`tests/live/test_fal_live.py`) is `KINOFORGE_LIVE_TESTS=1` (global on/off) + per-provider credential env vars. This plan follows the established convention; the spec acceptance criteria are interpreted with that substitution (`KINOFORGE_LIVE_RUNPOD` → `KINOFORGE_LIVE_TESTS` everywhere). Same intent, consistent ergonomics.

**File structure:**

| Path | Role | New / Modified |
|---|---|---|
| `tests/providers/conftest_runpod.py` | `_load_fixture`, `_RecordingHTTPSeam`, `_redact` | New |
| `tests/providers/fixtures/runpod/gpu_types.json` | `find_offers` capture | New (placeholder in Task 2; replaced in Task 4) |
| `tests/providers/fixtures/runpod/list_pods.json` | `list_instances` capture | New (placeholder; replaced in Task 4) |
| `tests/providers/fixtures/runpod/get_pod.json` | `get_instance` capture | New (placeholder; replaced in Task 4) |
| `tests/providers/fixtures/runpod/create_pod.json` | `create_instance` capture | New (placeholder; replaced in Task 4) |
| `tests/providers/fixtures/runpod/terminate_pod.json` | `destroy_instance` capture | New (placeholder; replaced in Task 4) |
| `tests/providers/fixtures/runpod/sample_init_frame.png` | i2v input asset | New (binary, ~32 KB, Task 3) |
| `examples/configs/runpod-comfyui-wan.yaml` | live smoke config | New (Task 3) |
| `tests/live/test_runpod_live.py` | gated live E2E test | New (Task 3) |
| `tests/providers/test_runpod.py` | dicts → `_load_fixture` + 2 new tests | Modified (Tasks 5, 6) |
| `README.md` | "Real providers — RunPod" section | Modified (Task 7) |
| `PROGRESS.md` | Phase 24 entry + close carry-forward #1 | Modified (Task 7) |

**Dependency graph:**

```
Task 1 (conftest_runpod) ──┬─► Task 2 (placeholders + Config round-trip)
                            │
                            └─► Task 3 (yaml + live test skeleton + sample_init_frame)
                                  │
                                  └─► Task 4 (USER-GATE: run live smoke, capture fixtures)
                                        │
                                        ├─► Task 5 (refactor offline tests)
                                        ├─► Task 6 (add real-shape lockdown tests)
                                        │
                                        └─► Task 7 (README + PROGRESS + final gate + merge)
```

---

### Task 1: Recording HTTP seam + `_load_fixture` helper + secret redaction

**Goal:** Build `tests/providers/conftest_runpod.py` with three primitives — `_load_fixture(name)`, `_RecordingHTTPSeam`, `_redact(obj)` — and a redaction unit test that covers AC2.

**Files:**
- Create: `tests/providers/conftest_runpod.py`
- Create: `tests/providers/fixtures/runpod/` (empty directory, `.gitkeep` placeholder)
- Test: `tests/providers/test_runpod_conftest.py`

**Acceptance Criteria:**
- [ ] `_redact()` recursively replaces any value at a key matching `r"(?i)(token|key|secret|password)"` with the literal string `"<REDACTED>"`, leaving other values untouched
- [ ] `_redact()` does not match on key fragments that aren't whole-word matches (e.g. `"checkpoint"` is not redacted; `"apikey"` is)
- [ ] `_load_fixture(name)` reads `tests/providers/fixtures/runpod/<name>` and returns `json.load(f)["response"]` as a `dict[str, Any]`
- [ ] `_load_fixture` raises `FileNotFoundError` with a helpful message if the fixture is missing (mentions the path + suggests running with `KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1`)
- [ ] `_RecordingHTTPSeam` wraps a real `http_get` / `http_post` callable, captures each call's request + redacted response, dispatches to one of five fixture filenames (`gpu_types.json`, `list_pods.json`, `get_pod.json`, `create_pod.json`, `terminate_pod.json`) via a query-fragment table, and writes a `_meta` block per spec §2
- [ ] Unknown queries write to `unknown_<sha8>.json` AND log a `WARNING` via `kinoforge.core.logging`
- [ ] mypy + ruff + pre-commit clean

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v` → 6 tests pass

**Steps:**

- [ ] **Step 1: Write the failing tests** — `tests/providers/test_runpod_conftest.py`

```python
"""Unit tests for tests/providers/conftest_runpod.py (Layer N Task 1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.providers.conftest_runpod import (
    _load_fixture,
    _RecordingHTTPSeam,
    _redact,
)


def test_redact_replaces_secret_field_names() -> None:
    body = {"apiKey": "sk-real-secret", "podId": "abc123"}
    out = _redact(body)
    assert out["apiKey"] == "<REDACTED>"
    assert out["podId"] == "abc123"


def test_redact_is_case_insensitive_and_recursive() -> None:
    body = {
        "data": {
            "Token": "bearer-x",
            "pod": {"password": "pw", "imageName": "foo:bar"},
        },
        "Secret_Tail": "y",
    }
    out = _redact(body)
    assert out["data"]["Token"] == "<REDACTED>"
    assert out["data"]["pod"]["password"] == "<REDACTED>"
    assert out["data"]["pod"]["imageName"] == "foo:bar"
    assert out["Secret_Tail"] == "<REDACTED>"


def test_redact_does_not_match_partial_word_collisions() -> None:
    body = {"checkpoint": "ok", "keypoints": "ok", "passport": "ok"}
    out = _redact(body)
    # "checkpoint", "keypoints", "passport" contain key/key/pass substrings
    # but none is a whole-word match for the protected vocab — pass through.
    assert out == body


def test_load_fixture_reads_response_block(tmp_path: Path, monkeypatch: Any) -> None:
    fixture_dir = tmp_path / "fixtures" / "runpod"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "demo.json").write_text(
        json.dumps(
            {
                "_meta": {"captured_at": "2026-05-31", "operation": "demo"},
                "response": {"data": {"k": "v"}},
            }
        )
    )
    import tests.providers.conftest_runpod as conf

    monkeypatch.setattr(conf, "_FIXTURE_DIR", fixture_dir)
    out = _load_fixture("demo.json")
    assert out == {"data": {"k": "v"}}


def test_load_fixture_missing_raises_with_capture_hint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import tests.providers.conftest_runpod as conf

    monkeypatch.setattr(conf, "_FIXTURE_DIR", tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        _load_fixture("missing.json")
    msg = str(exc.value)
    assert "KINOFORGE_LIVE_TESTS=1" in msg
    assert "KINOFORGE_SAVE_FIXTURES=1" in msg
    assert "missing.json" in msg


def test_recording_seam_dispatches_to_named_files(
    tmp_path: Path, caplog: Any
) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, body))
        # Echo a structured response with a secret to prove redaction fires
        return {
            "data": {
                "podFindAndDeployOnDemand": {
                    "id": "pod-1",
                    "apiKey": "sk-leak",
                }
            }
        }

    def fake_get(url: str) -> dict[str, Any]:
        calls.append((url, None))
        if "gpuTypes" in url:
            return {"data": {"gpuTypes": [{"id": "g1", "memoryInGb": 24}]}}
        return {"data": {"unrecognized_root_field": []}}

    seam = _RecordingHTTPSeam(fake_post, fake_get, out_dir=tmp_path)
    seam.http_post(
        "https://api.runpod.io/graphql",
        {"query": "mutation { podFindAndDeployOnDemand(input: $i) { id } }"},
    )
    seam.http_get("https://api.runpod.io/graphql?query={ gpuTypes { id } }")
    with caplog.at_level(logging.WARNING):
        seam.http_get("https://api.runpod.io/graphql?query={ mystery { x } }")

    seam.flush()

    create = json.loads((tmp_path / "create_pod.json").read_text())
    assert (
        create["response"]["data"]["podFindAndDeployOnDemand"]["apiKey"]
        == "<REDACTED>"
    )
    assert create["_meta"]["operation"] == "create_pod"
    assert (tmp_path / "gpu_types.json").exists()
    # Unknown queries land in unknown_<sha>.json and warn
    unknowns = list(tmp_path.glob("unknown_*.json"))
    assert len(unknowns) == 1
    assert any("unrecognized" in rec.message.lower() for rec in caplog.records)
```

- [ ] **Step 2: Run tests — confirm RED** (`pytest tests/providers/test_runpod_conftest.py -v`) → all 6 fail with `ModuleNotFoundError: tests.providers.conftest_runpod`.

- [ ] **Step 3: Implement `tests/providers/conftest_runpod.py`**

```python
"""RunPod test fixtures and recording HTTP seam (Layer N Task 1).

Provides three primitives used by the offline RunPod suite + live smoke:

- :func:`_load_fixture` — load a committed real-API JSON capture for
  replay-style offline tests.
- :class:`_RecordingHTTPSeam` — wrap real http_post / http_get callables, log
  every request, redact secrets, and dispatch responses to a fixed fixture
  filename via a query-fragment table.
- :func:`_redact` — recursively scrub any key whose name (whole-word, case
  insensitive) matches the protected vocab ``token / key / secret / password``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_FIXTURE_DIR: Path = Path(__file__).parent / "fixtures" / "runpod"

_REDACT_KEY_RE: re.Pattern[str] = re.compile(
    r"(?i)(?:^|_|-)(token|key|secret|password)(?:$|_|-)|^(token|key|secret|password)s?$",
)

_OPERATION_TABLE: list[tuple[str, str]] = [
    ("gpuTypes {", "gpu_types.json"),
    ("myself { pods", "list_pods.json"),
    ("podFindAndDeployOnDemand", "create_pod.json"),
    ("podTerminate", "terminate_pod.json"),
    ("pod(input:", "get_pod.json"),
]

_log = logging.getLogger(__name__)


def _redact(obj: Any) -> Any:
    """Recursively replace values at protected key names with ``<REDACTED>``.

    The match is case-insensitive and whole-word (or hyphen/underscore-bounded)
    against the vocab ``token, key, secret, password``.  Substrings such as
    ``checkpoint`` or ``keypoints`` pass through untouched.

    Args:
        obj: Any JSON-serialisable Python value (dict, list, str, int, etc).

    Returns:
        A redacted copy of ``obj``.  Original is not mutated.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _REDACT_KEY_RE.search(k):
                out[k] = "<REDACTED>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _load_fixture(name: str) -> dict[str, Any]:
    """Load the ``response`` payload of a committed real-API capture.

    Args:
        name: File name relative to ``tests/providers/fixtures/runpod/``.

    Returns:
        The contents of the ``response`` block as a ``dict``.

    Raises:
        FileNotFoundError: The fixture does not exist; the message includes a
            copy-pasteable command for regenerating fixtures via the live
            smoke.
    """
    path = _FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"RunPod fixture not found: {path}.  Regenerate with:\n"
            f"  KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 "
            f"pixi run pytest tests/live/test_runpod_live.py -v",
        )
    with path.open() as f:
        data = json.load(f)
    return dict(data["response"])


class _RecordingHTTPSeam:
    """Wrap real http_post / http_get callables for the live smoke.

    Each call is captured (request + redacted response).  At :meth:`flush`,
    one JSON file per logical operation is written to ``out_dir``.

    Args:
        http_post: Real POST callable to wrap.
        http_get: Real GET callable to wrap.
        out_dir: Output directory for written fixtures.
    """

    def __init__(
        self,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        out_dir: Path,
    ) -> None:
        self._post = http_post
        self._get = http_get
        self._out = out_dir
        self._records: list[tuple[str, str, dict[str, Any]]] = []

    def http_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST wrapper — records the response under its operation name."""
        response = self._post(url, body)
        query = str(body.get("query", ""))
        filename = self._dispatch(query)
        self._records.append((filename, query, response))
        return response

    def http_get(self, url: str) -> dict[str, Any]:
        """GET wrapper — records the response under its operation name."""
        response = self._get(url)
        # GET URLs include the query string after `?query=`
        query = url.split("?query=", 1)[1] if "?query=" in url else url
        filename = self._dispatch(query)
        self._records.append((filename, query, response))
        return response

    def flush(self) -> None:
        """Write one JSON file per recorded operation to ``out_dir``.

        Re-running a smoke overwrites prior captures — current shape wins.
        """
        self._out.mkdir(parents=True, exist_ok=True)
        for filename, query, response in self._records:
            payload = {
                "_meta": {
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "operation": filename.removesuffix(".json"),
                    "request_query": query[:200],
                },
                "response": _redact(response),
            }
            (self._out / filename).write_text(
                json.dumps(payload, indent=2, sort_keys=False) + "\n",
            )

    def _dispatch(self, query: str) -> str:
        """Map a GraphQL query string to its fixture filename."""
        for fragment, filename in _OPERATION_TABLE:
            if fragment in query:
                return filename
        sha8 = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
        _log.warning(
            "RecordingHTTPSeam: unrecognized GraphQL query, writing to "
            "unknown_%s.json (query fragment: %s)",
            sha8,
            query[:80],
        )
        return f"unknown_{sha8}.json"
```

- [ ] **Step 4: Create the fixture-dir placeholder** so the path exists for tests:

```bash
mkdir -p tests/providers/fixtures/runpod
touch tests/providers/fixtures/runpod/.gitkeep
```

- [ ] **Step 5: Run tests — confirm GREEN** (`pixi run pytest tests/providers/test_runpod_conftest.py -v`) → all 6 pass.

- [ ] **Step 6: Lint / format / typecheck**

```bash
pixi run lint
pixi run format
pixi run typecheck
pixi run pre-commit run --all-files
```

- [ ] **Step 7: Commit**

```bash
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py tests/providers/fixtures/runpod/.gitkeep
git commit -m "feat(test): Layer N Task 1 — RunPod recording HTTP seam + _load_fixture + redaction"
```

---

### Task 2: Placeholder fixture commits + offline-load smoke

**Goal:** Commit hand-crafted starter fixtures derived from the existing inline dicts in `tests/providers/test_runpod.py`.  These are placeholders ONLY — they will be overwritten by Task 4's live capture and the diff will become the AC4 review surface.  Add a single offline test that round-trips each starter fixture through `_load_fixture` so a missing or malformed file fails CI immediately.

**Files:**
- Create: `tests/providers/fixtures/runpod/gpu_types.json`
- Create: `tests/providers/fixtures/runpod/list_pods.json`
- Create: `tests/providers/fixtures/runpod/get_pod.json`
- Create: `tests/providers/fixtures/runpod/create_pod.json`
- Create: `tests/providers/fixtures/runpod/terminate_pod.json`
- Modify: `tests/providers/test_runpod_conftest.py` (add round-trip smoke)

**Acceptance Criteria:**
- [ ] Five fixture files exist and parse as JSON with `_meta` + `response` top-level keys
- [ ] Each `_meta` block has `captured_at: "PLACEHOLDER"`, `git_sha: "PLACEHOLDER"`, `operation: <name>`, `request_query: <fragment from production code>`
- [ ] `_load_fixture(name)` returns a non-empty dict for all five
- [ ] mypy + ruff + pre-commit clean

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py::test_starter_fixtures_load -v` → 1 test passes

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `tests/providers/test_runpod_conftest.py`:

```python
def test_starter_fixtures_load() -> None:
    """Every named starter fixture loads cleanly and is non-empty."""
    for name in (
        "gpu_types.json",
        "list_pods.json",
        "get_pod.json",
        "create_pod.json",
        "terminate_pod.json",
    ):
        payload = _load_fixture(name)
        assert payload, f"{name} loaded empty"
```

- [ ] **Step 2: Run test — confirm RED** (`pytest tests/providers/test_runpod_conftest.py::test_starter_fixtures_load`) → `FileNotFoundError`.

- [ ] **Step 3: Write `tests/providers/fixtures/runpod/gpu_types.json`**

```json
{
  "_meta": {
    "captured_at": "PLACEHOLDER",
    "git_sha": "PLACEHOLDER",
    "operation": "gpu_types",
    "request_query": "{ gpuTypes { id displayName memoryInGb secureCloud communityCloud lowestPrice { minimumBidPrice uninterruptablePrice } } }"
  },
  "response": {
    "data": {
      "gpuTypes": [
        {
          "id": "NVIDIA GeForce RTX 3090",
          "displayName": "RTX 3090",
          "memoryInGb": 24,
          "secureCloud": true,
          "communityCloud": true,
          "lowestPrice": {
            "minimumBidPrice": 0.18,
            "uninterruptablePrice": 0.30
          }
        },
        {
          "id": "NVIDIA RTX A5000",
          "displayName": "RTX A5000",
          "memoryInGb": 24,
          "secureCloud": true,
          "communityCloud": true,
          "lowestPrice": {
            "minimumBidPrice": 0.22,
            "uninterruptablePrice": 0.36
          }
        }
      ]
    }
  }
}
```

- [ ] **Step 4: Write `tests/providers/fixtures/runpod/list_pods.json`**

```json
{
  "_meta": {
    "captured_at": "PLACEHOLDER",
    "git_sha": "PLACEHOLDER",
    "operation": "list_pods",
    "request_query": "{ myself { pods { id desiredStatus imageName } } }"
  },
  "response": {
    "data": {
      "myself": {
        "pods": [
          {
            "id": "abc123",
            "desiredStatus": "RUNNING",
            "imageName": "runpod/pytorch:2.4.0"
          }
        ]
      }
    }
  }
}
```

- [ ] **Step 5: Write `tests/providers/fixtures/runpod/get_pod.json`**

```json
{
  "_meta": {
    "captured_at": "PLACEHOLDER",
    "git_sha": "PLACEHOLDER",
    "operation": "get_pod",
    "request_query": "{ pod(input: { podId: \"abc123\" }) { id desiredStatus imageName } }"
  },
  "response": {
    "data": {
      "pod": {
        "id": "abc123",
        "desiredStatus": "RUNNING",
        "imageName": "runpod/pytorch:2.4.0"
      }
    }
  }
}
```

- [ ] **Step 6: Write `tests/providers/fixtures/runpod/create_pod.json`**

```json
{
  "_meta": {
    "captured_at": "PLACEHOLDER",
    "git_sha": "PLACEHOLDER",
    "operation": "create_pod",
    "request_query": "mutation($input: PodFindAndDeployOnDemandInput!) { podFindAndDeployOnDemand(input: $input) { id desiredStatus imageName } }"
  },
  "response": {
    "data": {
      "podFindAndDeployOnDemand": {
        "id": "abc123",
        "desiredStatus": "STARTING",
        "imageName": "runpod/pytorch:2.4.0"
      }
    }
  }
}
```

- [ ] **Step 7: Write `tests/providers/fixtures/runpod/terminate_pod.json`**

```json
{
  "_meta": {
    "captured_at": "PLACEHOLDER",
    "git_sha": "PLACEHOLDER",
    "operation": "terminate_pod",
    "request_query": "mutation { podTerminate(input: { podId: \"abc123\" }) }"
  },
  "response": {
    "data": {
      "podTerminate": null
    }
  }
}
```

- [ ] **Step 8: Run test — confirm GREEN** (`pixi run pytest tests/providers/test_runpod_conftest.py::test_starter_fixtures_load -v`) → 1 test passes.

- [ ] **Step 9: Lint / format / typecheck + commit**

```bash
pixi run pre-commit run --all-files
git add tests/providers/fixtures/runpod/*.json tests/providers/test_runpod_conftest.py
git commit -m "feat(test): Layer N Task 2 — RunPod starter fixtures (placeholders; replaced by live capture in Task 4)"
```

---

### Task 3: Live smoke YAML + skeleton test file + sample init frame

**Goal:** Build the live-smoke artifacts so Task 4 can run end-to-end: a committed config YAML pinning cost caps + ComfyUI + Wan i2v, a gated pytest module mirroring `tests/live/test_fal_live.py`, and a small init-frame PNG asset.

**Files:**
- Create: `examples/configs/runpod-comfyui-wan.yaml`
- Create: `tests/live/test_runpod_live.py`
- Create: `tests/providers/fixtures/runpod/sample_init_frame.png` (32 KB synthetic image)
- Test: `tests/test_examples.py` (extend to load the new YAML)

**Acceptance Criteria:**
- [ ] YAML loads via `Config.load_config` and reads back: `engine.kind=="comfyui"`, `compute.provider=="runpod"`, `compute.mode=="pod"`, `compute.requirements.min_vram_gb==24`, `compute.requirements.max_cost_rate_usd_per_hr==0.50`, `compute.lifecycle.budget==2.00`, `compute.lifecycle.idle_timeout_s==600`
- [ ] `tests/live/test_runpod_live.py` is importable (no syntax errors) AND when collected without `KINOFORGE_LIVE_TESTS=1` set, all tests show as `skipped` with a clear reason
- [ ] `tests/live/test_runpod_live.py` defines a single test `test_runpod_live_e2e_wan_i2v_smoke` whose body is the full §3 control flow from the spec
- [ ] `sample_init_frame.png` exists, is a valid PNG ≥ 1 KB and ≤ 64 KB, and is human-content-free (synthetic gradient or noise)
- [ ] `tests/test_examples.py` adds a single load test for `runpod-comfyui-wan.yaml`
- [ ] Full offline suite still green; no test count regression

**Verify:**
```bash
pixi run pytest tests/live/test_runpod_live.py -v          # collection only; all skipped
pixi run pytest tests/test_examples.py -v                  # 22 tests, +1 new
pixi run test                                              # full suite green
```

**Steps:**

- [ ] **Step 1: Write `examples/configs/runpod-comfyui-wan.yaml`**

```yaml
# kinoforge example: Layer N live-smoke config — ComfyUI + Wan 2.2 i2v on RunPod.
#
# Usage:
#   1. Export creds:
#        export RUNPOD_API_KEY=...
#        export RUNPOD_TERMINATE_KEY=...   # scoped, terminate-only
#   2. (Optional, fixture refresh) export KINOFORGE_SAVE_FIXTURES=1
#   3. Run the gated live test:
#        KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_runpod_live.py -v
#
# Cost guards (quadruple-locked):
#   - max_cost_rate_usd_per_hr filters expensive GPUs upstream of create_instance
#   - budget tears the pod down via BudgetTracker mid-run if exceeded
#   - idle_timeout_s + selfterm script self-destructs the pod after 10 min idle
#   - tests/live/test_runpod_live.py wraps everything in a finally: destroy block

engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"

models:
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:wan2.2_14b_i2v.safetensors"
    kind: base
    target: checkpoints

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_cost_rate_usd_per_hr: 0.50
    gpu_preference:
      - "NVIDIA GeForce RTX 3090"
      - "NVIDIA RTX A5000"
      - "NVIDIA GeForce RTX 4090"
    disk_gb: 80
  lifecycle:
    idle_timeout: 10m
    job_timeout: 15m
    time_buffer: 5m
    max_lifetime: 30m
    budget: 2.0

spec:
  graph:
    nodes: []
  node_overrides: {}

params:
  fps: 16
  num_frames: 81
  steps: 20
  width: 480
  height: 480
```

- [ ] **Step 2: Generate `tests/providers/fixtures/runpod/sample_init_frame.png`** (synthetic, no human content):

```bash
pixi run python -c "
from pathlib import Path
import struct, zlib

# 256x256 grayscale gradient PNG, ~32 KB after zlib
w, h = 256, 256
raw = bytearray()
for y in range(h):
    raw.append(0)  # filter byte: None
    for x in range(w):
        raw.append((x + y) % 256)

def chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack('>I', len(data))
        + tag + data
        + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)
    )

sig = b'\\x89PNG\\r\\n\\x1a\\n'
ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
idat = chunk(b'IDAT', zlib.compress(bytes(raw), 9))
iend = chunk(b'IEND', b'')

out = Path('tests/providers/fixtures/runpod/sample_init_frame.png')
out.write_bytes(sig + ihdr + idat + iend)
print('wrote', out, out.stat().st_size, 'bytes')
"
```

Confirm file size in [1024, 65536]:

```bash
ls -l tests/providers/fixtures/runpod/sample_init_frame.png
```

- [ ] **Step 3: Write `tests/live/test_runpod_live.py`**

```python
"""Opt-in live tests against the real RunPod GraphQL API (Layer N Task 4).

Gated by three env vars:
- ``KINOFORGE_LIVE_TESTS=1`` (global on/off)
- ``RUNPOD_API_KEY=<real key>``
- ``RUNPOD_TERMINATE_KEY=<scoped terminate-only key>``

Optional:
- ``KINOFORGE_SAVE_FIXTURES=1`` — additionally write captured responses to
  ``tests/providers/fixtures/runpod/*.json``.  Pair this flag with a clean
  staging area; the diff is the AC4 review surface.

Cost: ~$0.10-$1.00 per run depending on GPU pick + generation time.  Skipped
silently in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY "
        "+ RUNPOD_TERMINATE_KEY",
        allow_module_level=True,
    )


_CONFIG = "examples/configs/runpod-comfyui-wan.yaml"
_INIT_FRAME = "tests/providers/fixtures/runpod/sample_init_frame.png"
_MP4_MAGIC = b"\x00\x00\x00 ftypisom"


def _run_cli(
    args: list[str], cwd: Path | None = None, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m kinoforge`` with the given args, capturing output."""
    return subprocess.run(
        [sys.executable, "-m", "kinoforge", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_runpod_live_e2e_wan_i2v_smoke(tmp_path: Path) -> None:
    """End-to-end live smoke: deploy → generate → assert MP4 → destroy.

    Implements the §3 control flow from the design.  The cost-safety
    finally-destroy block is guard #3 of 4; see ``examples/configs/runpod-
    comfyui-wan.yaml`` for guards #1, #2, #4.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # 1. Preconditions — config + init frame present.
    assert Path(_CONFIG).exists()
    assert Path(_INIT_FRAME).exists()

    pod_id: str | None = None
    deploy_started: float = time.monotonic()

    try:
        # 2-3. Deploy (find_offers + create_instance + poll until ready).
        deploy = _run_cli(
            [
                "--state-dir", str(state_dir),
                "deploy",
                "--config", _CONFIG,
            ],
            timeout=600,
        )
        assert deploy.returncode == 0, (
            f"deploy failed (exit {deploy.returncode}):\n"
            f"stdout:\n{deploy.stdout}\nstderr:\n{deploy.stderr}"
        )

        # Extract pod_id from deploy stdout (CLI prints "instance: <id>")
        for line in deploy.stdout.splitlines():
            if line.startswith("instance:"):
                pod_id = line.split(":", 1)[1].strip()
                break
        assert pod_id, f"could not parse pod_id from deploy stdout:\n{deploy.stdout}"

        # 4. Generate (provision + submit + download artifact).
        gen_started = time.monotonic()
        gen = _run_cli(
            [
                "--state-dir", str(state_dir),
                "generate",
                "--config", _CONFIG,
                "--prompt", "A landscape unfurling at dawn",
                "--asset", f"init_image={_INIT_FRAME}",
                "--run-id", "layer-n-smoke",
            ],
            timeout=900,
        )
        gen_duration = time.monotonic() - gen_started
        assert gen.returncode == 0, (
            f"generate failed (exit {gen.returncode}):\n"
            f"stdout:\n{gen.stdout}\nstderr:\n{gen.stderr}"
        )

        # 5. Assertions on the real artifact.
        run_dir = state_dir / "layer-n-smoke"
        mp4s = list(run_dir.rglob("*.mp4"))
        assert mp4s, f"no MP4 produced under {run_dir}"
        mp4 = mp4s[0]
        size = mp4.stat().st_size
        assert 100_000 <= size <= 50_000_000, f"MP4 size {size} out of range"
        head = mp4.read_bytes()[:12]
        assert head.startswith(b"\x00\x00\x00") and b"ftyp" in head, (
            f"MP4 magic bytes mismatch: {head!r}"
        )
        assert gen_duration < 900, f"generate too slow: {gen_duration:.1f}s"

    finally:
        # 6. Destroy — last line of defence before billing leak.
        if pod_id:
            destroy = _run_cli(
                [
                    "--state-dir", str(state_dir),
                    "destroy",
                    "--config", _CONFIG,
                    pod_id,
                ],
                timeout=120,
            )
            if destroy.returncode != 0:
                sys.stderr.write(
                    f"\n*** RUNPOD POD {pod_id} NOT CONFIRMED DESTROYED ***\n"
                    f"Manually terminate via the RunPod console or run:\n"
                    f"  curl -X POST https://api.runpod.io/graphql \\\n"
                    f'    -H "Authorization: Bearer $RUNPOD_API_KEY" \\\n'
                    f"    -d '{{\"query\":\"mutation{{podTerminate("
                    f'input:{{podId:\\"{pod_id}\\"}})}}"}}\'\n'
                )
                raise AssertionError(
                    f"destroy failed (exit {destroy.returncode}):\n"
                    f"stdout:\n{destroy.stdout}\nstderr:\n{destroy.stderr}"
                )

    # 7. Total time check.
    total = time.monotonic() - deploy_started
    assert total < 1800, f"smoke total runtime {total:.1f}s exceeded 30 min"


def _capture_fixtures_during_smoke(out_dir: Path) -> Any:
    """Hook into the orchestrator's HTTP seam to record real responses.

    Called only when ``KINOFORGE_SAVE_FIXTURES=1`` is set.  Wires the
    :class:`_RecordingHTTPSeam` into the ``RunPodProvider`` factory so every
    GraphQL request/response round-trip is captured.

    Note: in Task 4 this is invoked by the live smoke's setup code via
    monkey-patching the provider factory registered under ``"runpod"``.
    """
    raise NotImplementedError("wired up in Task 4 alongside fixture capture")
```

- [ ] **Step 4: Extend `tests/test_examples.py`** — add the new YAML to the load-test parametrisation. Read the current file and add one entry:

```bash
# Find the existing parametrize list and confirm pattern:
rg -n "examples/configs" tests/test_examples.py | head -5
```

Add a new test (use the existing pattern, copy adjacent test):

```python
def test_runpod_comfyui_wan_yaml_loads() -> None:
    """examples/configs/runpod-comfyui-wan.yaml loads and reports Layer-N cost caps."""
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/runpod-comfyui-wan.yaml"))
    assert cfg.engine.kind == "comfyui"
    assert cfg.compute.provider == "runpod"
    assert cfg.compute.mode == "pod"
    assert cfg.compute.requirements.min_vram_gb == 24
    assert cfg.compute.requirements.max_cost_rate_usd_per_hr == 0.50
    assert cfg.compute.lifecycle.budget == 2.0
    assert cfg.compute.lifecycle.idle_timeout_s == 600  # 10m == 600s
```

- [ ] **Step 5: Run targeted tests**

```bash
pixi run pytest tests/test_examples.py::test_runpod_comfyui_wan_yaml_loads -v   # PASS
pixi run pytest tests/live/test_runpod_live.py -v                                # all SKIPPED
pixi run test                                                                    # full suite green
```

If `cfg.compute.lifecycle.idle_timeout_s` fails because pydantic stores raw `"10m"` rather than parsed seconds, replace `idle_timeout: 10m` in the YAML with `idle_timeout: 600` (and similarly for other fields). The existing `parse_duration` helper in `config.py` covers this — confirm the round-trip empirically.

- [ ] **Step 6: Lint / format / typecheck**

```bash
pixi run pre-commit run --all-files
```

- [ ] **Step 7: Commit**

```bash
git add examples/configs/runpod-comfyui-wan.yaml \
        tests/live/test_runpod_live.py \
        tests/providers/fixtures/runpod/sample_init_frame.png \
        tests/test_examples.py
git commit -m "feat(test): Layer N Task 3 — live smoke YAML + skeleton + sample init frame"
```

---

### Task 4: USER-GATE — run live smoke against real RunPod + capture fixtures

**Goal:** Dr. Twinklebrane runs the live smoke against a real RunPod account with `KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1`, the test produces a real MP4 artifact, all five fixtures are overwritten with real-API responses, the pod is destroyed in `finally`, and the captured fixtures + last_smoke metadata are committed. Any bugs surfaced become in-scope production fixes per AC9.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `tests/providers/fixtures/runpod/gpu_types.json` (placeholder → real capture)
- Modify: `tests/providers/fixtures/runpod/list_pods.json` (placeholder → real capture)
- Modify: `tests/providers/fixtures/runpod/get_pod.json` (placeholder → real capture)
- Modify: `tests/providers/fixtures/runpod/create_pod.json` (placeholder → real capture)
- Modify: `tests/providers/fixtures/runpod/terminate_pod.json` (placeholder → real capture)
- Create: `tests/providers/fixtures/runpod/last_smoke.json` (artifact metadata: path, size, sha256, git SHA, timestamp)
- Modify: `tests/live/test_runpod_live.py` (wire `_RecordingHTTPSeam` into provider factory when `KINOFORGE_SAVE_FIXTURES=1`; complete the `_capture_fixtures_during_smoke` stub from Task 3)

**Acceptance Criteria:**
- [ ] Live smoke command exits 0: `KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 RUNPOD_API_KEY=… RUNPOD_TERMINATE_KEY=… pixi run pytest tests/live/test_runpod_live.py::test_runpod_live_e2e_wan_i2v_smoke -v -s`
- [ ] All five fixtures show non-PLACEHOLDER `_meta.captured_at` + `_meta.git_sha` AND a non-empty `response` block reflecting real RunPod JSON shape
- [ ] `tests/providers/fixtures/runpod/last_smoke.json` records the artifact path, size, sha256, capture timestamp, git SHA
- [ ] Produced MP4 is ≥ 100 KB and ≤ 50 MB with valid `ftyp` magic bytes
- [ ] Pod is confirmed destroyed (no dangling charges; verified manually in the RunPod console post-smoke)
- [ ] Reviewer scan: `rg -i "sk-|RUNPOD_API_KEY|RUNPOD_TERMINATE_KEY|bearer" tests/providers/fixtures/runpod/*.json` returns zero hits (redaction worked)
- [ ] Any production-code bug surfaced during the live run is fixed in a separate commit on the same branch AND documented in PROGRESS Phase 24 "Live-smoke bug catches integrated"

**Verify:**
```bash
# Manual command (Dr. Twinklebrane runs this on a real RunPod account):
KINOFORGE_LIVE_TESTS=1 \
KINOFORGE_SAVE_FIXTURES=1 \
RUNPOD_API_KEY=<real> \
RUNPOD_TERMINATE_KEY=<scoped> \
pixi run pytest tests/live/test_runpod_live.py::test_runpod_live_e2e_wan_i2v_smoke -v -s

# Post-smoke shape inspection:
jq '._meta' tests/providers/fixtures/runpod/*.json
jq '.response | keys' tests/providers/fixtures/runpod/gpu_types.json

# Secret scan:
rg -i "sk-|RUNPOD_API_KEY|RUNPOD_TERMINATE_KEY|bearer" tests/providers/fixtures/runpod/*.json
# Expected: zero hits.
```

**Steps:**

- [ ] **Step 1: Complete the `_capture_fixtures_during_smoke` wiring in `tests/live/test_runpod_live.py`**

Replace the stub at the bottom of the file from Task 3 with a real implementation that swaps the `RunPodProvider` factory:

```python
def _capture_fixtures_during_smoke(out_dir: Path) -> Any:
    """Install a recording HTTP seam under the runpod factory.

    When ``KINOFORGE_SAVE_FIXTURES=1``, called from the live smoke before
    deploy(): re-registers the ``"runpod"`` provider with a factory that
    wraps real http_post/http_get callables in :class:`_RecordingHTTPSeam`.
    The seam's :meth:`flush` is invoked in a teardown finalizer.
    """
    import atexit

    from kinoforge.core import registry
    from kinoforge.providers.runpod import (
        RunPodProvider,
        _urllib_get_json,
        _urllib_post_json,
    )

    from tests.providers.conftest_runpod import _RecordingHTTPSeam

    seam = _RecordingHTTPSeam(_urllib_post_json, _urllib_get_json, out_dir)

    def _factory() -> RunPodProvider:
        return RunPodProvider(http_post=seam.http_post, http_get=seam.http_get)

    registry.register_provider("runpod", _factory)
    atexit.register(seam.flush)
    return seam
```

Add the activation guard at the top of `test_runpod_live_e2e_wan_i2v_smoke`, right after the preconditions:

```python
    if os.getenv("KINOFORGE_SAVE_FIXTURES") == "1":
        _capture_fixtures_during_smoke(
            Path("tests/providers/fixtures/runpod")
        )
```

- [ ] **Step 2: Write `last_smoke.json` from the live test**

Append to the end of the live test body (inside the `try`, after the MP4 assertions, before the `finally`):

```python
        import hashlib, json
        meta = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "git_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "artifact_path": str(mp4),
            "artifact_size_bytes": size,
            "artifact_sha256": hashlib.sha256(mp4.read_bytes()).hexdigest(),
            "gpu_type_used": pod_id and "see fixture create_pod.json",
            "duration_seconds": round(gen_duration, 1),
        }
        Path("tests/providers/fixtures/runpod/last_smoke.json").write_text(
            json.dumps(meta, indent=2) + "\n",
        )
```

- [ ] **Step 3: Confirm offline suite still green**

```bash
pixi run test
```

- [ ] **Step 4: Run the live smoke** (Dr. Twinklebrane, real keys + real money — this is THE gate):

```bash
KINOFORGE_LIVE_TESTS=1 \
KINOFORGE_SAVE_FIXTURES=1 \
RUNPOD_API_KEY=<real> \
RUNPOD_TERMINATE_KEY=<scoped-terminate-only> \
pixi run pytest tests/live/test_runpod_live.py::test_runpod_live_e2e_wan_i2v_smoke -v -s
```

Expected outcomes (in order, each must hold):

1. Test exits 0.
2. `tests/providers/fixtures/runpod/*.json` shows 5 modified files with real `_meta.captured_at` (current ISO timestamp) + real `_meta.git_sha` (current HEAD).
3. `tests/providers/fixtures/runpod/last_smoke.json` exists with the artifact metadata.
4. An MP4 file ≥ 100 KB exists under the run dir.
5. The RunPod console shows no active pods belonging to your account.
6. `rg -i "sk-|RUNPOD_API_KEY|RUNPOD_TERMINATE_KEY|bearer" tests/providers/fixtures/runpod/*.json` → zero hits.

If any of (1)–(6) fails, this is an in-scope bug per AC9. Diagnose, fix (separate commit on this branch), and re-run. Document each "Live-smoke bug catch" in PROGRESS Phase 24 (Task 7).

- [ ] **Step 5: Inspect captured shape against expectations**

```bash
jq '.response.data.gpuTypes[0] | keys' tests/providers/fixtures/runpod/gpu_types.json
jq '.response.data | keys' tests/providers/fixtures/runpod/get_pod.json
jq '._meta' tests/providers/fixtures/runpod/*.json
```

Expected: real RunPod field names. Note any unexpected shape (extra fields, renamed fields, missing fields the production code reads). Production-code drift becomes an in-scope fix.

- [ ] **Step 6: Verify pod is gone (console check)** — Dr. Twinklebrane: log into the RunPod web console, confirm no active or stopped pods belong to the test account. If a pod is dangling, terminate manually using the `curl` template logged by the test's `finally` block.

- [ ] **Step 7: Commit (captures + last_smoke + any in-scope fixes)**

```bash
pixi run pre-commit run --all-files
git add tests/providers/fixtures/runpod/ tests/live/test_runpod_live.py
git commit -m "feat(test): Layer N Task 4 — live RunPod smoke captures + first real artifact"
# If any production-code fixes were applied during the smoke, commit them as
# separate atomic commits with conventional prefixes (fix: …, fix(runpod): …).
```

```json:metadata
{
  "files": [
    "tests/providers/fixtures/runpod/gpu_types.json",
    "tests/providers/fixtures/runpod/list_pods.json",
    "tests/providers/fixtures/runpod/get_pod.json",
    "tests/providers/fixtures/runpod/create_pod.json",
    "tests/providers/fixtures/runpod/terminate_pod.json",
    "tests/providers/fixtures/runpod/last_smoke.json",
    "tests/live/test_runpod_live.py"
  ],
  "verifyCommand": "KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 pixi run pytest tests/live/test_runpod_live.py::test_runpod_live_e2e_wan_i2v_smoke -v -s",
  "acceptanceCriteria": [
    "Live smoke command exits 0 with real RUNPOD_API_KEY + RUNPOD_TERMINATE_KEY in env",
    "All 5 fixtures show non-PLACEHOLDER _meta.captured_at + _meta.git_sha",
    "tests/providers/fixtures/runpod/last_smoke.json records artifact path, size, sha256, git SHA, timestamp",
    "Produced MP4 is between 100 KB and 50 MB with valid ftyp magic bytes",
    "Pod is confirmed destroyed via the RunPod console (no dangling charges)",
    "rg -i 'sk-|RUNPOD_API_KEY|RUNPOD_TERMINATE_KEY|bearer' on fixtures returns zero hits",
    "Any production-code bug surfaced is fixed in a separate commit and documented in PROGRESS Phase 24"
  ],
  "userGate": true,
  "tags": ["user-gate"],
  "requireEvidenceTokens": [
    ["live-run", "pytest-exit-0", "real-runpod"],
    ["fixtures-captured", "non-placeholder", "real-shape"],
    ["pod-destroyed", "console-verified", "no-dangling"]
  ]
}
```

---

### Task 5: Refactor offline `tests/providers/test_runpod.py` to use real fixtures

**Goal:** Replace inline hand-crafted GraphQL response dicts in `tests/providers/test_runpod.py` with `_load_fixture(name)` calls so every existing test runs against real-API shape. Any value-assertion that depended on a fictional shape is updated to the real shape; if a production code path under test relied on a fictional shape, that is a real bug — fix in a separate commit on the same branch.

**Files:**
- Modify: `tests/providers/test_runpod.py` (all 24 tests; inline dicts → `_load_fixture`)

**Acceptance Criteria:**
- [ ] `rg "lowestPrice|memoryInGb|desiredStatus" tests/providers/test_runpod.py` returns zero hits inside test function bodies (matches only allowed in fixture JSON or comments)
- [ ] All previously-passing tests still pass; if any test now FAILs because the real shape differs from the hand-crafted shape, the fix is committed atomically with a `fix(runpod):` prefix
- [ ] No new tests added in this task (additions belong in Task 6)
- [ ] mypy + ruff + pre-commit clean

**Verify:** `pixi run pytest tests/providers/test_runpod.py -v` → all 24 tests pass (possibly after in-scope fixes); `rg "lowestPrice|memoryInGb|desiredStatus" tests/providers/test_runpod.py` returns zero hits.

**Steps:**

- [ ] **Step 1: Find every inline GraphQL response dict in `test_runpod.py`**

```bash
rg -n "lowestPrice|memoryInGb|desiredStatus|podFindAndDeployOnDemand" tests/providers/test_runpod.py
```

Each hit is a test that needs refactoring. Expect ~10-15 hits across the 24 tests.

- [ ] **Step 2: For each hit, refactor the inline dict to `_load_fixture(name)`**

Pattern (per spec §4):

```python
# Before:
def test_find_offers_filters_by_vram() -> None:
    http_get = lambda url: {"data": {"gpuTypes": [
        {"id": "RTX 3090", "memoryInGb": 24,
         "lowestPrice": {"uninterruptablePrice": 0.30}},
    ]}}
    p = RunPodProvider(http_get=http_get)
    offers = p.find_offers(HardwareRequirements(min_vram_gb=24))
    assert len(offers) == 1
    assert offers[0].vram_gb == 24
```

```python
# After:
from tests.providers.conftest_runpod import _load_fixture

def test_find_offers_filters_by_vram() -> None:
    http_get = lambda url: _load_fixture("gpu_types.json")
    p = RunPodProvider(http_get=http_get)
    offers = p.find_offers(HardwareRequirements(min_vram_gb=24))
    # gpu_types.json contains two GPUs at 24 GB each (RTX 3090 + RTX A5000)
    assert len(offers) == 2
    assert {o.gpu_type for o in offers} == {
        "NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000",
    }
```

Update each assertion to match the real fixture values (the captured JSON IS the truth).

- [ ] **Step 3: Run the refactored suite** — `pixi run pytest tests/providers/test_runpod.py -v`. Triage failures into two buckets:

  a) **Test-mechanical** — assertion needed to be adjusted to the real shape (e.g. `assert offers[0].gpu_type == "RTX 3090"` becomes `… == "NVIDIA GeForce RTX 3090"`). Fix the assertion.

  b) **Real production bug** — the production code reads a field the real API doesn't return (or vice versa). Document the bug, write a failing offline test that reproduces it using `_load_fixture`, fix the production code in a separate commit prefixed `fix(runpod):`, then re-run. Each bug becomes a PROGRESS Phase 24 entry (Task 7).

- [ ] **Step 4: Confirm zero hand-crafted GraphQL shape remains**

```bash
rg "lowestPrice|memoryInGb|desiredStatus|podFindAndDeployOnDemand|gpuTypes" tests/providers/test_runpod.py
```

Expected: zero hits, OR only inside Python string literals that aren't GraphQL response shape (e.g. an assertion on `provider._GPU_TYPES_QUERY` content). Audit each remaining hit.

- [ ] **Step 5: Lint / format / typecheck**

```bash
pixi run pre-commit run --all-files
```

- [ ] **Step 6: Commit (refactor + any in-scope fixes as separate commits)**

```bash
git add tests/providers/test_runpod.py
git commit -m "refactor(test): Layer N Task 5 — RunPod offline tests load fixtures via _load_fixture"
```

---

### Task 6: Real-shape required-keys + status-mapping lockdown tests

**Goal:** Add the two new test functions (`test_find_offers_real_shape_required_keys`, `test_pod_status_mapping_covers_real_statuses`) plus the existing redaction-scrub test referenced by AC2 (already in Task 1, verify still present).

**Files:**
- Modify: `tests/providers/test_runpod.py` (append two new test functions)

**Acceptance Criteria:**
- [ ] `test_find_offers_real_shape_required_keys` reads `gpu_types.json` and asserts every entry in `response.data.gpuTypes` has the keys `id`, `memoryInGb`, AND has `lowestPrice.uninterruptablePrice` OR `lowestPrice.minimumBidPrice`. Fails loudly if a future fixture regen drops a field.
- [ ] `test_pod_status_mapping_covers_real_statuses` reads `list_pods.json` and `get_pod.json`, gathers every observed `desiredStatus` string, asserts each maps to a real entry in `_runpod_status_to_kinoforge`'s mapping table (no fallback to default `"starting"`).
- [ ] Both tests pass against the captured fixtures from Task 4
- [ ] mypy + ruff + pre-commit clean
- [ ] Total test count for `tests/providers/test_runpod.py` is 26 (was 24)

**Verify:** `pixi run pytest tests/providers/test_runpod.py -v --tb=short | tail -20` → 26 tests pass; both new test names visible.

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `tests/providers/test_runpod.py`:

```python
def test_find_offers_real_shape_required_keys() -> None:
    """Lock the GPU-types fixture against production-code's read fields.

    Bug it catches: a future RunPod schema rename (e.g. memoryInGb → vramGb)
    that breaks ``find_offers`` silently if the fixture is regenerated and
    the production code is not updated.
    """
    fixture = _load_fixture("gpu_types.json")
    gpus = fixture["data"]["gpuTypes"]
    assert gpus, "gpu_types fixture has no entries"
    for gpu in gpus:
        assert "id" in gpu, f"missing id in {gpu}"
        assert "memoryInGb" in gpu, f"missing memoryInGb in {gpu}"
        pricing = gpu.get("lowestPrice", {})
        assert (
            "uninterruptablePrice" in pricing
            or "minimumBidPrice" in pricing
        ), f"missing both price keys in {gpu}"


def test_pod_status_mapping_covers_real_statuses() -> None:
    """Lock _runpod_status_to_kinoforge against real desiredStatus values.

    Bug it catches: RunPod adds a new desiredStatus (e.g. PAUSED) and the
    production code's fallback maps it to "starting", silently
    miscategorising real instance state.
    """
    from kinoforge.providers.runpod import _runpod_status_to_kinoforge

    observed: set[str] = set()
    for fixture_name in ("list_pods.json", "get_pod.json"):
        fixture = _load_fixture(fixture_name)
        data = fixture["data"]
        if "myself" in data:
            for pod in data["myself"]["pods"]:
                observed.add(pod["desiredStatus"])
        elif "pod" in data and data["pod"]:
            observed.add(data["pod"]["desiredStatus"])

    assert observed, "no desiredStatus values observed in fixtures"
    known = {"RUNNING", "EXITED", "DEAD"}
    for status in observed:
        assert status.upper() in known, (
            f"new RunPod status {status!r} not in mapping table — "
            f"_runpod_status_to_kinoforge needs an entry"
        )
        kf = _runpod_status_to_kinoforge(status)
        assert kf in {"ready", "stopped", "terminated"}, (
            f"status {status!r} maps to {kf!r} (default-fallback miss)"
        )
```

- [ ] **Step 2: Run tests — confirm GREEN** against the real Task 4 captures:

```bash
pixi run pytest tests/providers/test_runpod.py::test_find_offers_real_shape_required_keys -v
pixi run pytest tests/providers/test_runpod.py::test_pod_status_mapping_covers_real_statuses -v
```

If either fails, it has caught a real shape drift between production code and the captured fixtures — fix the production code OR adjust the test's "known statuses" set (and add the new status to `_runpod_status_to_kinoforge` in the same commit).

- [ ] **Step 3: Confirm total count is 26**

```bash
pixi run pytest tests/providers/test_runpod.py --collect-only -q | tail -3
```

- [ ] **Step 4: Lint / format / typecheck + commit**

```bash
pixi run pre-commit run --all-files
git add tests/providers/test_runpod.py
git commit -m "test(runpod): Layer N Task 6 — real-shape required-keys + status-mapping lockdown"
```

---

### Task 7: README + PROGRESS + final gate + merge

**Goal:** Update `README.md` with a "Real providers — RunPod" section parallel to the existing fal section; add `PROGRESS.md` Phase 24 entry closing carry-forward #1; run the full test suite + lint + typecheck; produce a `--no-ff` merge to `main` with a substantive body.

**Files:**
- Modify: `README.md` (append "Real providers — RunPod" section)
- Modify: `PROGRESS.md` (Phase 24 entry; update PROGRESS:114 carry-forward #1 to CLOSED; update "Single next action")

**Acceptance Criteria:**
- [ ] README has a "Real providers — RunPod" subsection that documents (a) the env-var gate (`KINOFORGE_LIVE_TESTS=1` + `RUNPOD_API_KEY` + `RUNPOD_TERMINATE_KEY`), (b) the `KINOFORGE_SAVE_FIXTURES=1` flag, (c) the four cost guards, (d) a copy-pasteable command
- [ ] PROGRESS.md has a "Phase 24 — Layer N (RunPod cloud-fidelity)" entry with per-task SHAs filled in
- [ ] PROGRESS.md "Real-cloud verification gaps" section updates the RunPod bullet from open to CLOSED (Layer N)
- [ ] PROGRESS.md "Single next action" updates to reflect Layer N shipped + names Layer O candidates (SkyPilot SDK smoke, S3/GCS medium-fidelity)
- [ ] `pixi run test` → all tests pass (offline-only; live test skips); test-count updated in PROGRESS
- [ ] `pixi run lint && pixi run typecheck && pixi run pre-commit run --all-files` all green
- [ ] Merge to `main` is `--no-ff` with a body referencing per-task commits + AC state + first-artifact metadata + carry-forward closure

**Verify:**
```bash
pixi run test                                # green
pixi run pre-commit run --all-files          # green
rg "Real providers — RunPod" README.md       # 1 hit
rg "Phase 24" PROGRESS.md                    # ≥ 1 hit
git log --oneline main..build/layer-n        # 7+ commits
```

**Steps:**

- [ ] **Step 1: Append to `README.md`** under (or next to) the existing "Real providers — fal.ai" section:

```markdown
### Real providers — RunPod

kinoforge ships an opt-in live smoke against the real RunPod GraphQL API for
ComfyUI + Wan 2.2 i2v on a pod-mode GPU. It is skipped by default and never
runs in CI.

```bash
export RUNPOD_API_KEY=...
export RUNPOD_TERMINATE_KEY=...     # scoped, terminate-only

KINOFORGE_LIVE_TESTS=1 \
pixi run pytest tests/live/test_runpod_live.py -v
```

To refresh the committed GraphQL response fixtures (e.g. after a RunPod
schema upgrade), add the capture flag:

```bash
KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 \
pixi run pytest tests/live/test_runpod_live.py -v
```

The smoke is bounded by four independent cost guards:

1. `examples/configs/runpod-comfyui-wan.yaml` pins `max_cost_rate_usd_per_hr=0.50`
2. The same YAML pins `budget=2.00` (BudgetTracker tears the pod down mid-run)
3. The live test always calls `destroy_instance` in a `finally:` block
4. The in-pod selfterm script self-destructs the pod after 10 minutes idle

Cost per run is typically $0.10–$1.00. If the smoke fails before reaching
its `finally:` block (segfault, SIGKILL), guard #4 still tears the pod down
within the idle window.
```

- [ ] **Step 2: Append `PROGRESS.md` Phase 24 entry** — insert at the bottom of "Post-MVP" section, parallel to Phase 23:

```markdown
### Phase 24 — Layer N (RunPod cloud-fidelity hardening)

- [x] Task 1: Recording HTTP seam + `_load_fixture` + redaction — commit `<SHA1>`
- [x] Task 2: Placeholder fixture commits + offline-load smoke — commit `<SHA2>`
- [x] Task 3: Live smoke YAML + skeleton test + sample init frame — commit `<SHA3>`
- [x] Task 4: USER-GATE live smoke + real fixture capture — commit `<SHA4>`
- [x] Task 5: Refactor `test_runpod.py` to load fixtures — commit `<SHA5>`
- [x] Task 6: Real-shape required-keys + status-mapping lockdown — commit `<SHA6>`
- [x] Task 7: README + PROGRESS + final gate — commit `<SHA7>`
- [x] Merge to main via `--no-ff` — merge commit `<MERGESHA>` (closes PROGRESS:114 carry-forward #1)

**First real artifact (RunPod):** `<artifact_path>` — `<artifact_size>` bytes,
MP4 (`ftyp isom`), produced by ComfyUI + Wan2.2 i2v on a `<gpu_type>` pod via
`examples/configs/runpod-comfyui-wan.yaml` (capability_key `<sha>`, git SHA
`<sha>` at smoke time).

**Live-smoke bug catches integrated into Task 4 / Task 5:**
- (list each in-scope production fix surfaced during Tasks 4 + 5, with the
  same format as Phase 19's catches list. If no bugs were caught, write:
  "None — production code matched real GraphQL shape on first pass.")

**Key design decisions:**
- Spec convention deviation: `KINOFORGE_LIVE_RUNPOD` (spec) → `KINOFORGE_LIVE_TESTS`
  (existing fal convention). Same intent, consistent ergonomics.
- Layer N is verification-only by default (AC9): no production-code change
  unless live smoke surfaces a real bug.
- Fixture capture is dual-flag: `KINOFORGE_LIVE_TESTS=1` runs live; adding
  `KINOFORGE_SAVE_FIXTURES=1` writes captures. Two-flag opt-in keeps
  fixture regen explicit.

**Out of scope (Layer O candidates):**
- Serverless mode read-paths + live smoke (Q3 = pod-only this layer)
- SkyPilot SDK smoke test (PROGRESS:113 carry-forward #2)
- S3/GCS medium-fidelity tests (PROGRESS:113 carry-forward #3)

**Test count:** 755 passed + 1 skipped pre-Layer-N → `<NEW>` passed + 2 skipped
post-Layer-N (+`<DELTA>` net; live test adds 1 new skipped).
```

- [ ] **Step 3: Update PROGRESS.md "Real-cloud verification gaps" bullet** (around PROGRESS:113-116):

```markdown
**Real-cloud verification gaps (offline-tested only):**
- ~~`RunPodProvider.find_offers` REST shape is a stub~~ — **CLOSED** by Phase 24 (Layer N).
- `SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK.
- `S3ArtifactStore` + `GCSArtifactStore` never hit real cloud — fake clients don't simulate multipart edge cases, transient retries, SSE/KMS, signed URLs.
```

- [ ] **Step 4: Update "Single next action"** (around PROGRESS:151):

```markdown
## Single next action
**Layer N complete on `build/layer-n`.** RunPod cloud-fidelity shipped. Real-API
GraphQL response shape now locked against committed fixtures captured during a
live ComfyUI + Wan i2v smoke; 24 existing offline tests refactored + 2 new
shape-lockdown tests added; PROGRESS:113 carry-forward #1 closed. Next layer
candidate: **Layer O** — remaining cloud-fidelity (SkyPilot SDK smoke test
[carry-forward #2] + S3/GCS medium-fidelity tests [carry-forward #3]).
```

- [ ] **Step 5: Full gate**

```bash
pixi run test
pixi run lint
pixi run typecheck
pixi run pre-commit run --all-files
```

All four must be green. Test-count delta noted (PROGRESS bullet above).

- [ ] **Step 6: Commit + merge**

```bash
git add README.md PROGRESS.md
git commit -m "docs: Layer N — README + PROGRESS Phase 24 entry"

git checkout main
git merge --no-ff build/layer-n -m "$(cat <<'EOF'
Merge branch 'build/layer-n': RunPod cloud-fidelity hardening (Layer N)

Closes PROGRESS:113 carry-forward #1. Locks RunPodProvider GraphQL response
shape against the real RunPod API via committed fixtures captured during a
live ComfyUI + Wan 2.2 i2v end-to-end smoke. Refactors 24 existing offline
tests to load JSON via _load_fixture; adds 2 shape-lockdown tests. Live
smoke gated by KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY + RUNPOD_TERMINATE_KEY;
fixture refresh gated additively by KINOFORGE_SAVE_FIXTURES=1. Quadruple-
locked cost safety: max_cost_rate_usd_per_hr + budget + finally-destroy +
selfterm.

Per-task commits: <SHA1>, <SHA2>, <SHA3>, <SHA4>, <SHA5>, <SHA6>, <SHA7>.

First real RunPod artifact: <see PROGRESS Phase 24>.

Layer O candidates: SkyPilot SDK smoke (carry-forward #2), S3/GCS medium-
fidelity tests (carry-forward #3).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"

git push origin main          # only after Dr. Twinklebrane confirms
```

- [ ] **Step 7: Backfill the merge SHA into PROGRESS.md** Phase 24 entry, then commit + push:

```bash
git log --oneline -1                                    # capture merge SHA
# Edit PROGRESS.md, replace <MERGESHA> with the real SHA
git add PROGRESS.md
git commit -m "docs(progress): backfill Phase 24 merge commit SHA"
```

---

## Self-Review

**Spec coverage check** (every spec §X mapped to a task):

| Spec section | Task(s) |
|---|---|
| §1 Architecture / file layout | All tasks; structure declared in plan header |
| §2 Capture protocol + fixture format | Task 1 (`_RecordingHTTPSeam` + dispatch table), Task 4 (captures populate) |
| §3 Live smoke control flow | Task 3 (skeleton), Task 4 (live run) |
| §4 Offline fixture-replay refactor | Task 5 |
| §5 AC1 (fixtures committed) | Task 4 |
| §5 AC2 (secret redaction enforced) | Task 1 |
| §5 AC3 (offline tests use fixtures) | Task 5 |
| §5 AC4 (real-shape required-keys + status mapping) | Task 6 |
| §5 AC5 (live smoke produces real artifact) | Task 4 |
| §5 AC6 (cost safety quadruple-locked) | Task 3 (YAML), Task 4 (finally-destroy) |
| §5 AC7 (CI green offline-only) | Task 7 (final gate) |
| §5 AC8 (README + PROGRESS updated) | Task 7 |
| §5 AC9 (verification-only by default) | Tasks 4, 5 (in-scope fixes documented) |
| §5 Non-goals | Explicitly out of plan |
| §6 File / diff inventory | File-structure table at plan top |
| §6 Risk register | Mitigations distributed across tasks (cost guards in Task 3, redaction in Task 1, etc.) |

No spec sections without coverage. No tasks without a spec mapping.

**Placeholder scan:** No "TBD", "TODO", "implement later", "add appropriate error handling", "similar to Task N" patterns in any task. Each step has either an exact command or a complete code block.

**Type consistency:** `_load_fixture(name)` signature is identical in Tasks 1, 2, 5, 6. `_RecordingHTTPSeam.__init__(http_post, http_get, out_dir)` signature is identical in Tasks 1, 4. The five fixture file names are identical across Tasks 1 (`_OPERATION_TABLE`), 2 (placeholder commits), 4 (real captures), 5 (test refactor), 6 (lockdown tests). No drift.

---

## Heads-up (user-gate)

Heads up — I tagged 1 task as user-gate (Task #4). The plan runs end-to-end as-is. If you'd like automatic close-time enforcement, the JSON snippets are in `README.md` — paste them into `.claude/settings.local.json`. Happy to walk you through it; just say the word.
