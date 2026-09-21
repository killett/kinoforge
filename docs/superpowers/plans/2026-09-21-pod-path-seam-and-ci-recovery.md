# Pod-Path Seam and CI Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop provider-specific absolute paths from being baked into provider-agnostic server code, which greens CI, fixes Modal's artifact mis-location, and guards against recurrence — then un-rot the weekly live smoke.

**Architecture:** `InstanceSpec.volume_mount` is already the portable seam and Modal already derives `HF_HOME` from it. A new pure helper `core/pod_paths.py` names the shared on-pod layout; each volume-carrying provider calls it once after resolving its own mount and merges with `setdefault`. The server modules then drop their `/workspace` fallbacks for pod-local scratch, so importing them has no filesystem opinion at all.

**Tech Stack:** Python 3.13, pixi, pytest, ruff, mypy. FastAPI servers embedded into provision scripts via gzip+base64.

**Spec:** `docs/superpowers/specs/2026-09-21-pod-path-seam-and-ci-recovery-design.md`

## Global Constraints

- **`HF_HOME` is provider policy, not shared layout.** Modal's is the Volume root
  (`/cache/hf`); RunPod's is a `.hf_cache` subdir. `pod_path_env` takes `hf_home`
  as an explicit keyword argument and never derives it. Deriving it would orphan
  the 144 GiB fetch on the Modal Volume (`minimax_h3_server.py:50-54`).
- **Providers must export `HF_HOME` BEFORE the servers stop defaulting it.**
  Tasks 2 and 3 therefore block Task 4. Inverting that order means a 70 GB Wan
  shard download lands on container disk — the exact failure
  `wan_t2v_server.py:47-49` exists to prevent.
- **Golden regeneration is a reviewed act, never an accepted one.** After any
  regeneration, decode every changed payload and confirm ONLY the expected lines
  moved. This is established project discipline (U19 reviewed 21 moved goldens;
  U33 reviewed 20 + `_golden_provision.json`).
- **Run `pixi run pre-commit run --all-files` BEFORE regenerating goldens**, never
  after. Formatting moves the embedded server bytes; regenerating first means
  regenerating twice.
- **`git add` new files before trusting a green `--all-files` run** — pre-commit
  skips untracked files.
- All functions need type hints and Google-style docstrings.
- Conventional Commits, imperative mood.

**User decisions (already made):**
- Scope: both CI problems, sequenced offline-work-first. (2026-09-21)
- Fix depth: "Provider supplies the writable root" — no provider-specific absolute path survives in server code. (2026-09-21)
- Weekly smoke: fix the cfg, then make it `workflow_dispatch`-only; drop the cron. (2026-09-21)
- Explicitly OUT of scope: notification, branch protection, or any local CI-parity gate for the "nobody reads CI" meta-problem. (2026-09-21)
- The live dispatch run proving the weekly cfg green is **deliberately not a task in this plan.** It is a live-spend call for the operator at execution time. The command is recorded under "Deferred, out of plan" below so it is available without being assumed.

---

## File Structure

| File | Responsibility | Tasks |
|------|----------------|-------|
| `src/kinoforge/core/pod_paths.py` | **Create.** Pure helper naming the shared on-pod dir layout. No I/O, no provider imports. | 1 |
| `tests/core/test_pod_paths.py` | **Create.** Unit tests for the helper. | 1 |
| `src/kinoforge/providers/runpod/__init__.py` | **Modify.** `_assemble_create_env` exports the trio; hoist the duplicated `/workspace` default to a module constant. | 2 |
| `tests/providers/test_runpod_pod_path_env.py` | **Create.** Proves RunPod exports the trio, including `HF_HOME`. | 2 |
| `src/kinoforge/providers/modal/__init__.py` | **Modify.** Replace the lone `HF_HOME` setdefault with the full trio. | 3 |
| `tests/providers/modal/test_hf_home_env.py` | **Modify.** Extend with pod-dir assertions and an `HF_HOME`-stays-at-the-root regression guard. | 3 |
| `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` | **Modify.** Drop `/workspace` fallbacks for pod-local scratch; delete the `HF_HOME` setdefault. | 4 |
| `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py` | **Modify.** Same treatment. | 4 |
| `tests/smoke/local_cpu/conftest.py` | **Modify.** Delete the now-redundant ad-hoc workaround. | 4 |
| `tests/test_pod_path_audit.py` | **Create.** Standing source audit + its falsification test. | 5 |
| `tests/conftest.py` | **Modify.** Autouse fixture pointing the pod dirs at `tmp_path`. | 5 |
| `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml` | **Modify.** Cap raise + secure pin. | 6 |
| `.github/workflows/smoke-wan21-weekly.yml` | **Modify.** Drop the cron, keep dispatch. | 6 |

---

### Task 1: The pod-path helper

**Goal:** A pure function naming the shared on-pod directory layout, with no provider knowledge and no filesystem access.

**Files:**
- Create: `src/kinoforge/core/pod_paths.py`
- Test: `tests/core/test_pod_paths.py`

**Acceptance Criteria:**
- [ ] `pod_path_env("/workspace")` returns artifact and loras dirs rooted on `/workspace`, and no `HF_HOME` key
- [ ] `pod_path_env("/workspace", hf_home="/workspace/.hf_cache")` additionally returns that exact `HF_HOME`
- [ ] `pod_path_env("")` and `pod_path_env(None)` return pod-local scratch dirs and omit `HF_HOME` entirely
- [ ] A trailing slash on the mount does not produce a doubled separator
- [ ] The module imports nothing from `kinoforge.providers` and performs no I/O
- [ ] Neither provider mount literal appears inside `pod_path_env`'s own source (the module docstring may name them — it explains the seam)

**Verify:** `pixi run python -m pytest tests/core/test_pod_paths.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_pod_paths.py`:

```python
"""Behavior: the shared on-pod directory layout, derived from a volume mount.

This helper exists because ``/workspace`` is RunPod's volume mount and Modal
mounts at ``/cache/hf``. A server module that hardcodes either one is wrong on
the other provider and wrong on every CI runner, which is the defect this whole
plan removes. The layout is named once, here, and the providers supply the mount.
"""

from __future__ import annotations

import inspect

from kinoforge.core import pod_paths
from kinoforge.core.pod_paths import pod_path_env


def test_dirs_are_rooted_on_the_supplied_mount() -> None:
    """A mount produces artifact + loras dirs beneath it.

    Catches a helper that ignores its argument and returns a constant — which
    would reintroduce exactly the hardcoded-path defect it exists to remove.
    """
    env = pod_path_env("/mnt/example-volume")

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/example-volume/artifacts"
    assert env["KINOFORGE_LORAS_DIR"] == "/mnt/example-volume/loras"


def test_hf_home_is_absent_unless_the_caller_supplies_one() -> None:
    """The helper never invents an HF cache location.

    Catches deriving HF_HOME from the mount. Modal's HF_HOME is the Volume ROOT
    and that Volume holds a 144 GiB fetch; a derived ``<mount>/.hf_cache`` would
    orphan it and silently re-download 123.8 GiB. The difference between Modal's
    root and RunPod's subdir is real provider policy, so it is passed in.
    """
    assert "HF_HOME" not in pod_path_env("/mnt/example-volume")


def test_supplied_hf_home_is_passed_through_verbatim() -> None:
    """Whatever the provider names is what lands in the env.

    Catches a helper that accepts the argument then rewrites it — the
    pass-through is the entire point of making it an argument.
    """
    env = pod_path_env("/mnt/example-volume", hf_home="/mnt/example-volume")

    assert env["HF_HOME"] == "/mnt/example-volume"


def test_no_volume_falls_back_to_pod_local_scratch() -> None:
    """A volumeless host (SkyPilot, Local) still gets writable dirs.

    Catches emitting a mount-rooted path built from an empty string, which
    yields bare ``/artifacts`` — unwritable on every host, and the same class
    of failure as the /workspace bug.
    """
    for empty in ("", None):
        env = pod_path_env(empty)

        assert env["KINOFORGE_ARTIFACT_DIR"] == pod_paths.SCRATCH_ARTIFACT_DIR
        assert env["KINOFORGE_LORAS_DIR"] == pod_paths.SCRATCH_LORAS_DIR
        assert env["KINOFORGE_ARTIFACT_DIR"].startswith("/tmp/")
        assert "HF_HOME" not in env, (
            "a volumeless host must inherit huggingface_hub's own default"
        )


def test_trailing_slash_does_not_double_the_separator() -> None:
    """``/mnt/vol/`` and ``/mnt/vol`` produce the same paths.

    Catches naive f-string concatenation. A doubled slash is survivable on
    POSIX but makes the golden launch payloads differ for configs that are
    semantically identical, which corrupts the ratchet.
    """
    assert pod_path_env("/mnt/vol/") == pod_path_env("/mnt/vol")


def test_helper_is_pure_and_provider_agnostic() -> None:
    """No provider import, and no provider mount inside the logic.

    Catches the helper growing a dependency on the providers it serves, which
    would make it uncallable from inside one without a circular import.

    The mount check is scoped to the FUNCTION, not the module, and that is
    deliberate: the module docstring names both ``/workspace`` and
    ``/cache/hf`` to explain why this helper exists, and a module-wide
    substring check cannot tell an explanation from a runtime default. The
    function's own source is where a hardcoded mount would actually do harm.
    Task 5's audit does not cover this file — it scans ``os.environ``
    fallbacks, and there are none here — so this assertion is the only guard
    on it.
    """
    module_source = inspect.getsource(pod_paths)
    function_source = inspect.getsource(pod_path_env)

    assert "kinoforge.providers" not in module_source
    assert "/workspace" not in function_source, "RunPod's mount must not appear here"
    assert "/cache/hf" not in function_source, "Modal's mount must not appear here"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run python -m pytest tests/core/test_pod_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kinoforge.core.pod_paths'`

- [ ] **Step 3: Write the implementation**

Create `src/kinoforge/core/pod_paths.py`:

```python
"""Where a pod's writable directories live, derived from its volume mount.

``/workspace`` is RunPod's volume mount; Modal mounts its Volume at
``/cache/hf``; SkyPilot and Local attach no volume at all. A server module is
provider-agnostic and must therefore carry none of those literals as a runtime
default — the provider that knows its own mount exports these vars into the pod
env, and the server reads them.

``HF_HOME`` is deliberately NOT derived here. Modal's is the Volume root itself
and that Volume holds a 144 GiB fetch; RunPod's is a ``.hf_cache`` subdir on its
volume. That difference is real provider policy, so the caller passes it.

See docs/superpowers/specs/2026-09-21-pod-path-seam-and-ci-recovery-design.md.
"""

from __future__ import annotations

ARTIFACT_DIR_VAR = "KINOFORGE_ARTIFACT_DIR"
LORAS_DIR_VAR = "KINOFORGE_LORAS_DIR"
HF_HOME_VAR = "HF_HOME"

# Pod-local writable scratch for hosts with no attached volume. Mirrors the
# existing `_UPLOAD_DIR = Path("/tmp/kf-uploads")` convention in
# wan_t2v_server.py, including its justification: this is a pod's own tmpfs,
# not a shared multi-user /tmp.
SCRATCH_ARTIFACT_DIR = "/tmp/kf-artifacts"  # noqa: S108
SCRATCH_LORAS_DIR = "/tmp/kf-loras"  # noqa: S108


def pod_path_env(
    volume_mount: str | None, *, hf_home: str | None = None
) -> dict[str, str]:
    """Return the pod env vars naming the server's writable directories.

    Args:
        volume_mount: The provider's resolved volume mount path, or ``None`` /
            ``""`` when the host attaches no volume (SkyPilot, Local).
        hf_home: Absolute path for ``HF_HOME``, when the caller wants one set.
            Omitted from the result when ``None``, so ``huggingface_hub``'s own
            ``~/.cache/huggingface`` default applies. Never derived from
            ``volume_mount`` — see the module docstring.

    Returns:
        Mapping of env var name to absolute pod path, suitable for merging
        into a pod env dict with ``setdefault`` so an explicit config-supplied
        value still wins.
    """
    if volume_mount:
        root = volume_mount.rstrip("/")
        env = {
            ARTIFACT_DIR_VAR: f"{root}/artifacts",
            LORAS_DIR_VAR: f"{root}/loras",
        }
    else:
        env = {
            ARTIFACT_DIR_VAR: SCRATCH_ARTIFACT_DIR,
            LORAS_DIR_VAR: SCRATCH_LORAS_DIR,
        }
    if hf_home is not None:
        env[HF_HOME_VAR] = hf_home
    return env
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run python -m pytest tests/core/test_pod_paths.py -v`
Expected: 6 passed

- [ ] **Step 5: Lint, type-check, commit**

```bash
git add src/kinoforge/core/pod_paths.py tests/core/test_pod_paths.py
pixi run pre-commit run --files \
  src/kinoforge/core/pod_paths.py \
  tests/core/test_pod_paths.py
git commit -m "feat(core): name the shared on-pod directory layout once"
```

---

### Task 2: RunPod exports the pod paths

**Goal:** `RunPodProvider._assemble_create_env` exports the artifact, loras and `HF_HOME` vars derived from the mount it already resolves, so the server no longer has to default them.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py:1248-1281` (`_assemble_create_env`), `:1403` (`volumeMountPath`)
- Create: `tests/providers/test_runpod_pod_path_env.py`
- Regenerate: RunPod launch-payload goldens under `tests/providers/golden/launch_payloads/`

**Acceptance Criteria:**
- [ ] `_assemble_create_env` includes `KINOFORGE_ARTIFACT_DIR`, `KINOFORGE_LORAS_DIR` and `HF_HOME` rooted on the resolved mount
- [ ] `HF_HOME` is `<mount>/.hf_cache` — matching what `wan_t2v_server.py:50` set before this plan removes it
- [ ] A spec that already carries any of the three keys in `spec.env` keeps its own value (`setdefault`, not overwrite)
- [ ] The `/workspace` default appears exactly once in the module, as a named constant used by both `_assemble_create_env` and `volumeMountPath`
- [ ] The existing "main API key never reaches the pod" invariant still holds
- [ ] Every moved golden's diff is the three new env keys and nothing else

**Verify:** `pixi run python -m pytest tests/providers/test_runpod_pod_path_env.py tests/providers/ -q` → all PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/test_runpod_pod_path_env.py`:

```python
"""Behavior: RunPod tells the pod where its writable directories are.

Before this seam, the server guessed — it hardcoded ``/workspace`` as a module
constant read at import. That is RunPod's volume mount, so it was right here by
luck and wrong on Modal and on every CI runner. The provider knows its own
mount; it is the only component that can answer this.
"""

from __future__ import annotations

from kinoforge.core.interfaces import InstanceSpec
from kinoforge.providers.runpod import RunPodProvider


def _env_for(spec: InstanceSpec) -> dict[str, str]:
    """Assemble the create-pod env for *spec*.

    Args:
        spec: The instance specification under test.

    Returns:
        The env dict the create mutation would carry.
    """
    provider = RunPodProvider(api_key="kinoforge-prod-deadbeef")
    return provider._assemble_create_env(spec)


def test_exports_the_pod_dirs_rooted_on_the_default_mount() -> None:
    """A spec with no explicit mount gets RunPod's own ``/workspace``.

    Catches the provider staying silent and leaving the server to guess, which
    is the whole defect: a silent provider means the server needs a default,
    and any default it picks is another provider's path somewhere.
    """
    env = _env_for(InstanceSpec(image="python:3.13-slim"))

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/workspace/artifacts"
    assert env["KINOFORGE_LORAS_DIR"] == "/workspace/loras"


def test_exports_hf_home_as_a_subdir_of_the_volume() -> None:
    """``HF_HOME`` lands on the volume, not on container disk.

    This is the load-bearing one. Until this plan, the pinning came from the
    server's own import-time setdefault, which Task 4 deletes. If the provider
    does not carry it, a 70 GB Wan shard download goes to the 250 GB container
    disk instead of the volume — the precise failure the comment at
    wan_t2v_server.py:47-49 was written to prevent.
    """
    env = _env_for(InstanceSpec(image="python:3.13-slim"))

    assert env["HF_HOME"] == "/workspace/.hf_cache"


def test_an_explicit_mount_moves_all_three() -> None:
    """A spec-supplied mount is honoured, not ignored.

    Catches hardcoding ``/workspace`` a second time inside the new code rather
    than reading the mount the spec declares.
    """
    env = _env_for(
        InstanceSpec(image="python:3.13-slim", volume_mount="/mnt/example-volume")
    )

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/example-volume/artifacts"
    assert env["KINOFORGE_LORAS_DIR"] == "/mnt/example-volume/loras"
    assert env["HF_HOME"] == "/mnt/example-volume/.hf_cache"


def test_config_supplied_values_win_over_the_derived_ones() -> None:
    """An explicit env value is not clobbered by the derivation.

    Catches using plain assignment instead of ``setdefault``. A cfg that
    deliberately redirects its artifact dir must keep doing so, or this seam
    becomes a regression for anyone already overriding it.
    """
    env = _env_for(
        InstanceSpec(
            image="python:3.13-slim",
            env={
                "KINOFORGE_ARTIFACT_DIR": "/mnt/somewhere-else",
                "HF_HOME": "/mnt/hf",
            },
        )
    )

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/somewhere-else"
    assert env["HF_HOME"] == "/mnt/hf"
    assert env["KINOFORGE_LORAS_DIR"] == "/workspace/loras", (
        "the keys the caller did NOT set must still be derived"
    )
```

Then extend the EXISTING safety test rather than restating the credential
variable name in a new file. Find it with
`rg -n 'never.*api.key|API_KEY' tests/providers/ | head`, and confirm it still
passes unchanged after Step 4 — `_assemble_create_env` keeps its final
`env.pop(...)` guard, and moving lines around it is exactly the careless
refactor that test exists to catch.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run python -m pytest tests/providers/test_runpod_pod_path_env.py -v`
Expected: FAIL — `KeyError: 'KINOFORGE_ARTIFACT_DIR'`

- [ ] **Step 3: Hoist the mount default to a module constant**

In `src/kinoforge/providers/runpod/__init__.py`, near the other module constants, add:

```python
# RunPod's volume mount. Named once: `_assemble_create_env` derives the pod's
# writable dirs from it and the create mutation sends it as `volumeMountPath`.
# Those two MUST agree — a pod told to write to a path it did not mount fails
# at the first artifact write, minutes into a booked card.
_DEFAULT_VOLUME_MOUNT = "/workspace"
```

Then at `:1403`, replace the inline literal:

```python
                    "volumeMountPath": spec.volume_mount or _DEFAULT_VOLUME_MOUNT,
```

- [ ] **Step 4: Export the trio from `_assemble_create_env`**

Add the import at the top of the module:

```python
from kinoforge.core.pod_paths import pod_path_env
```

In `_assemble_create_env`, immediately after `env: dict[str, str] = dict(spec.env)`:

```python
    # Tell the pod where its writable dirs are. The server used to hardcode
    # these as import-time constants defaulting to /workspace — right here by
    # luck, wrong on Modal, and unwritable on every CI runner. The provider
    # owns its mount, so the provider answers. `setdefault` so a cfg that
    # deliberately redirects a dir keeps winning.
    mount = spec.volume_mount or _DEFAULT_VOLUME_MOUNT
    for key, value in pod_path_env(
        mount, hf_home=f"{mount.rstrip('/')}/.hf_cache"
    ).items():
        env.setdefault(key, value)
```

Leave the existing terminate-key injection, self-terminator embed, and the final
safety `env.pop(...)` exactly where they are. The pop must stay last.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pixi run python -m pytest tests/providers/test_runpod_pod_path_env.py -v`
Expected: 4 passed

- [ ] **Step 6: Format BEFORE regenerating goldens**

```bash
git add src/kinoforge/providers/runpod/__init__.py \
  tests/providers/test_runpod_pod_path_env.py
pixi run pre-commit run --all-files
```

Expected: PASS (ruff may reformat; re-`git add` if it does).

- [ ] **Step 7: Regenerate the launch-payload goldens**

```bash
pixi run python tools/snapshot_launch_payloads.py
```

- [ ] **Step 8: REVIEW the regeneration — do not accept it**

```bash
git diff --stat tests/providers/golden/launch_payloads/
git diff tests/providers/golden/launch_payloads/ | grep '^[+-]' | grep -v '^[+-][+-]' | sort | uniq -c | sort -rn | head -30
```

Expected: the only changed lines are the three new env keys on RunPod payloads.
If ANY other line moved, stop and investigate before committing — a golden diff
you did not predict is a behaviour change you did not intend.

- [ ] **Step 9: Run the provider suite and commit**

```bash
pixi run python -m pytest tests/providers/ -q
git add -A tests/providers/golden/launch_payloads/ \
  src/kinoforge/providers/runpod/__init__.py \
  tests/providers/test_runpod_pod_path_env.py
git commit -m "feat(runpod): tell the pod where its writable directories are"
```

---

### Task 3: Modal exports the pod paths

**Goal:** `ModalProvider.create_instance` exports the same trio, with `HF_HOME` kept byte-identical to today's Volume root so the existing cache is not orphaned.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py:283-288`
- Modify: `tests/providers/modal/test_hf_home_env.py` — **extend it; do NOT create a new test module**
- Regenerate: Modal launch-payload goldens

**Acceptance Criteria:**
- [ ] The `ModalAppRequest.env` carries `KINOFORGE_ARTIFACT_DIR` and `KINOFORGE_LORAS_DIR` rooted on the Modal Volume mount
- [ ] `HF_HOME` is still exactly `/cache/hf` — the Volume ROOT, unchanged from today
- [ ] Config-supplied values still win over derived ones (`setdefault`, not assignment)
- [ ] The four new tests live in `test_hf_home_env.py`; no new test module is created
- [ ] The regenerated Modal goldens show `HF_HOME` unchanged and exactly two added env keys

**Verify:** `pixi run python -m pytest tests/providers/modal/ -q` → all PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

`tests/providers/modal/test_hf_home_env.py` already owns this concern — it
captures a real `ModalAppRequest` through `create_instance` via its
`_provider_capturing()` harness and already asserts `HF_HOME` for the default
mount, a custom mount, and an operator override. **Extend that file. Do not
create a parallel module** — splitting one seam across two test files is what
the plan's own File Structure principle argues against, and its harness gives
you true integration coverage that re-testing Task 1's helper would not.

First broaden its module docstring: it currently describes itself as being
about `HF_HOME` alone, and after this change it covers every directory var
Modal exports. Then append four tests, matching the file's existing
`# Bug caught:` comment style rather than introducing docstrings:

```python
def test_pod_dirs_default_to_the_volume_mount():
    # Bug caught: create_instance seeds HF_HOME but leaves the server to guess
    # its artifact/loras dirs, so a Modal container writes to /workspace/... —
    # RunPod's mount — landing on ephemeral container disk, not the Volume.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}))

    req = captured["req"]
    assert req.env["KINOFORGE_ARTIFACT_DIR"] == "/cache/hf/artifacts"
    assert req.env["KINOFORGE_LORAS_DIR"] == "/cache/hf/loras"


def test_pod_dirs_track_a_custom_volume_mount():
    # Bug caught: deriving the dirs from a hardcoded "/cache/hf" instead of the
    # RESOLVED mount desyncs them from where the Volume actually lives, the
    # same defect test_hf_home_tracks_a_custom_volume_mount pins for HF_HOME.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}, volume_mount="/mnt/weights"))

    req = captured["req"]
    assert req.env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/weights/artifacts"
    assert req.env["KINOFORGE_LORAS_DIR"] == "/mnt/weights/loras"


def test_pod_dirs_respect_operator_override():
    # Bug caught: plain assignment instead of setdefault would clobber a cfg
    # that deliberately redirects its artifact dir — a regression for anyone
    # already overriding it.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={"KINOFORGE_ARTIFACT_DIR": "/custom/art"}))

    req = captured["req"]
    assert req.env["KINOFORGE_ARTIFACT_DIR"] == "/custom/art"
    assert req.env["KINOFORGE_LORAS_DIR"] == "/cache/hf/loras", (
        "the key the caller did NOT set must still be derived"
    )


def test_hf_home_is_the_volume_root_never_a_subdir():
    # Bug caught: "unifying" Modal's layout with RunPod's, which puts HF_HOME at
    # <mount>/.hf_cache. Sub-project B's 144 GiB fetch lives at the Volume ROOT;
    # moving HF_HOME one level deeper orphans it and silently re-downloads
    # 123.8 GiB on the next run. This is the guard on that.
    provider, captured = _provider_capturing()

    provider.create_instance(_spec(env={}))

    req = captured["req"]
    assert req.env["HF_HOME"] == "/cache/hf"
    assert not req.env["HF_HOME"].endswith(".hf_cache")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run python -m pytest tests/providers/modal/test_hf_home_env.py -v`
Expected: the three `test_pod_dirs_*` tests FAIL with `KeyError:
'KINOFORGE_ARTIFACT_DIR'`. The three pre-existing `test_hf_home_*` tests and
`test_hf_home_is_the_volume_root_never_a_subdir` PASS already — the last one is
a regression guard on behaviour that is currently correct and must stay correct,
so it passing before your change is expected, not a problem.

- [ ] **Step 3: Write the implementation**

Add the import at the top of `src/kinoforge/providers/modal/__init__.py`:

```python
from kinoforge.core.pod_paths import pod_path_env
```

Replace lines 283-288 — currently:

```python
        volume_mount = spec.volume_mount or "/cache/hf"
        env = dict(spec.env)
        # Persist the HF cache onto the Modal Volume so a preempted/cold
        # container re-uses downloaded weights instead of re-fetching. The
        # server's own os.environ.setdefault("HF_HOME", ...) respects this.
        env.setdefault("HF_HOME", volume_mount)
```

with:

```python
        volume_mount = spec.volume_mount or "/cache/hf"
        env = dict(spec.env)
        # Persist the HF cache onto the Modal Volume so a preempted/cold
        # container re-uses downloaded weights instead of re-fetching, and tell
        # the server where its writable dirs are rather than letting it guess.
        #
        # HF_HOME is the Volume ROOT, not a subdir: Sub-project B's 144 GiB
        # fetch lives there. Rooting it one level deeper would orphan that
        # cache silently. RunPod uses a .hf_cache subdir on its own volume;
        # the difference is deliberate, which is why pod_path_env takes
        # hf_home rather than deriving it.
        for key, value in pod_path_env(volume_mount, hf_home=volume_mount).items():
            env.setdefault(key, value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run python -m pytest tests/providers/modal/ -q`
Expected: all PASS

- [ ] **Step 5: Format, regenerate, REVIEW**

```bash
git add src/kinoforge/providers/modal/__init__.py \
  tests/providers/modal/test_hf_home_env.py
pixi run pre-commit run --all-files
pixi run python tools/snapshot_launch_payloads.py
git diff tests/providers/golden/launch_payloads/ | grep '^[+-]' | grep -v '^[+-][+-]'
```

Expected: exactly two added env keys per Modal payload. `HF_HOME` MUST NOT appear
as a changed line — if it does, the Volume root moved and the cache is orphaned.
Stop and fix before committing.

- [ ] **Step 6: Commit**

```bash
pixi run python -m pytest tests/providers/ -q
git add -A tests/providers/golden/launch_payloads/ \
  src/kinoforge/providers/modal/__init__.py \
  tests/providers/modal/test_hf_home_env.py
git commit -m "feat(modal): export the pod directory layout off the Volume mount"
```

---

### Task 4: Servers stop guessing — this is what greens CI

**Goal:** Neither server module carries a provider-specific absolute path as a runtime default, so importing them or calling `_startup()` touches no path that exists only on one provider's pod.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:47-50`, `:73-76`
- Modify: `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py:49-56`, `:77-79`
- Modify: `tests/smoke/local_cpu/conftest.py:52-57` (delete the now-redundant workaround)
- Regenerate: ALL diffusers launch-payload goldens + `tests/engines/diffusers/_golden_provision.json`

**Acceptance Criteria:**
- [ ] `tests/engines/test_wan_t2v_server_torch_build_log.py` passes with NO edit to that file
- [ ] Neither server module contains `/workspace` or `/cache/hf` in an `os.environ.get`/`setdefault` fallback position
- [ ] `ARTIFACT_DIR` and `LORAS_DIR` remain module-level attributes (the 8 test files that monkeypatch them keep working)
- [ ] The ad-hoc redirect in `tests/smoke/local_cpu/conftest.py` is deleted and Tier-1 smoke still passes
- [ ] Every moved golden's decoded diff shows only the expected server-source lines

**Verify:** `pixi run python -m pytest tests/engines/ tests/smoke/local_cpu/ -q` → all PASS

**Steps:**

- [ ] **Step 1: Confirm the target test currently passes for the WRONG reason**

```bash
pixi run python -m pytest tests/engines/test_wan_t2v_server_torch_build_log.py -q
ls -d /workspace/artifacts /workspace/loras
```

Expected: 7 passed, and both directories exist. That is the bug — the suite is
green here only because this container's `/workspace` is writable. On CI the same
test raises `PermissionError: [Errno 13] Permission denied: '/workspace'`.

There is no local RED to produce for this task: the falsifying environment is the
CI runner, which is precisely why Task 5 adds an offline guard. Task 8 is where
the real verification happens.

- [ ] **Step 2: Fix `wan_t2v_server.py` — delete the HF_HOME setdefault**

Replace lines 47-50 — currently:

```python
# Pin HF cache onto the /workspace volume so the 70 GB shard download
# does not exhaust the 50 GB container disk. /workspace is the RunPod
# volume mount (volumeInGb in the cfg's placement.disk_gb).
os.environ.setdefault("HF_HOME", "/workspace/.hf_cache")
```

with:

```python
# HF_HOME is the PROVIDER's to set, not this module's: RunPod exports
# <volume>/.hf_cache and Modal exports the Volume root, both via
# core.pod_paths.pod_path_env. This module used to hardcode RunPod's answer,
# which was right on RunPod, wrong on Modal, and unwritable on any CI runner.
# On a host with no volume, huggingface_hub's own ~/.cache/huggingface applies.
```

Keep the `HF_HUB_DISABLE_XET` setdefault on the line above untouched — that one
is a transport kill-switch, not a path.

- [ ] **Step 3: Fix `wan_t2v_server.py` — the two directory constants**

Replace lines 73-76 — currently:

```python
ARTIFACT_DIR: Path = Path(
    os.environ.get("KINOFORGE_ARTIFACT_DIR", "/workspace/artifacts")
)
LORAS_DIR: Path = Path(os.environ.get("KINOFORGE_LORAS_DIR", "/workspace/loras"))
```

with:

```python
# The provider exports both (core.pod_paths.pod_path_env). The fallbacks are
# pod-local scratch, matching _UPLOAD_DIR below — never another provider's
# volume path, which is what made importing this module fail on CI runners.
# These stay module attributes on purpose: eight test modules monkeypatch them,
# and on a real pod the env is set before this process starts, so resolving at
# import is correct.
ARTIFACT_DIR: Path = Path(
    os.environ.get("KINOFORGE_ARTIFACT_DIR", "/tmp/kf-artifacts")  # noqa: S108
)
LORAS_DIR: Path = Path(
    os.environ.get("KINOFORGE_LORAS_DIR", "/tmp/kf-loras")  # noqa: S108
)
```

- [ ] **Step 4: Fix `minimax_h3_server.py`**

Delete the `HF_HOME` setdefault at `:56` — `os.environ.setdefault("HF_HOME", "/cache/hf")` —
and rewrite the comment block at `:49-54` to name the provider as authoritative,
mirroring Step 2's wording. Keep `HF_HUB_DISABLE_XET` on the line above.

Deleting this one is safe **only because Task 3 makes Modal export `HF_HOME`**,
and this server runs on Modal alone. Task 3's integration assertion is the guard
on that; if Task 3 is not complete, do not do this step.

Then replace `:77-79`:

```python
ARTIFACT_DIR: Path = Path(
    os.environ.get("KINOFORGE_ARTIFACT_DIR", "/tmp/kf-artifacts")  # noqa: S108
)
```

- [ ] **Step 5: Delete the now-redundant workaround**

In `tests/smoke/local_cpu/conftest.py`, delete lines 52-57:

```python
    # Wan server's startup mkdir's ARTIFACT_DIR + LORAS_DIR. Defaults
    # to /workspace/{artifacts,loras} which doesn't exist on CI runners.
    # Point at per-test tmp_path so the subprocess uvicorn can boot.
    env["KINOFORGE_ARTIFACT_DIR"] = str(tmp_path / "artifacts")
    env["KINOFORGE_LORAS_DIR"] = str(tmp_path / "loras")
```

This is the ad-hoc mitigation that named the problem in the first place. Its
comment is now false — the defaults no longer point at `/workspace` — and Task 5's
autouse fixture covers the same ground for the whole suite.

If `tmp_path` becomes an unused fixture parameter after this deletion, keep it:
the subprocess still needs a per-test directory elsewhere in that fixture. Check
before removing it, and let ruff tell you if it is genuinely unused.

- [ ] **Step 6: Run the tests**

```bash
pixi run python -m pytest tests/engines/ tests/smoke/local_cpu/ -q
```

Expected: all PASS, including `test_wan_t2v_server_torch_build_log.py` with that
file unmodified.

- [ ] **Step 7: Format BEFORE regenerating — the embedded bytes move**

```bash
git add -A src/kinoforge/engines/diffusers/servers/ tests/smoke/local_cpu/conftest.py
pixi run pre-commit run --all-files
```

- [ ] **Step 8: Regenerate BOTH golden sets**

The server module source is gzip+base64-embedded into every diffusers provision
script, so a one-character edit moves every diffusers payload.

```bash
pixi run python tools/snapshot_launch_payloads.py
```

```bash
pixi run python -c "
from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine
import json, pathlib
p = pathlib.Path('tests/engines/diffusers/_golden_provision.json')
out = {}
for name in json.loads(p.read_text()):
    out[name] = DiffusersEngine().render_provision(load_config(name).model_dump()).script
p.write_text(json.dumps(out, indent=0))
print('regenerated', {k: len(v) for k, v in out.items()})
"
```

- [ ] **Step 9: REVIEW the regeneration by DECODING it**

A base64 blob diff is unreadable, so compare the decoded scripts. `_golden_provision.json`
stores plaintext, so `git diff` on it is directly readable — read it first:

```bash
git diff tests/engines/diffusers/_golden_provision.json | grep '^[+-]' | grep -v '^[+-][+-]'
```

Expected: only the comment and constant lines edited in Steps 2-4.

Then confirm the launch payloads carry the same change and nothing more:

```bash
pixi run python -c "
import base64, gzip, json, pathlib, subprocess, difflib
changed = subprocess.run(['git','diff','--name-only','tests/providers/golden/launch_payloads/'],
                         capture_output=True, text=True).stdout.split()
print(len(changed), 'goldens moved')
def scripts(payload):
    blobs = []
    for entry in payload.get('input', {}).get('env', []) or []:
        if entry.get('key') == 'KINOFORGE_PROVISION_SCRIPT':
            blobs.append(gzip.decompress(base64.b64decode(entry['value'])).decode())
    for key in ('image_build_script', 'provision_script'):
        val = payload.get('request', {}).get(key)
        if val:
            blobs.append(val)
    return blobs
for f in changed:
    old = json.loads(subprocess.run(['git','show',f'HEAD:{f}'],capture_output=True,text=True).stdout)
    new = json.loads(pathlib.Path(f).read_text())
    for a, b in zip(scripts(old), scripts(new)):
        for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm='', n=0):
            if line.startswith(('+','-')) and not line.startswith(('+++','---')):
                print(f, line)
"
```

Expected: the ONLY plaintext lines that moved are the comment and constant edits
from Steps 2-4, identical across every payload. Anything else is an unintended
behaviour change — stop.

- [ ] **Step 10: Full suite, then commit**

```bash
pixi run python -m pytest -m 'not live' -q
git add -A
git commit -m "fix(servers): stop hardcoding one provider's volume path

The server resolved /workspace into module constants at import and _startup
mkdir'd them, so any test touching startup wrote to a path that exists only
on a RunPod pod: PermissionError on the ubuntu runner, read-only filesystem
on macOS. This container hid it, because /workspace is the repo root here —
the suite passed locally while spilling stub artifacts into the tree.

The providers now export these (core.pod_paths), so the fallbacks become
pod-local scratch and importing this module has no filesystem opinion at all.
The ad-hoc redirect in the Tier-1 smoke conftest, which diagnosed this once
and fixed it in one place, is deleted as redundant."
```

---

### Task 5: The recurrence guard

**Goal:** The next provider-absolute path added as a runtime default fails a test offline, and no test can spill into the repo root again.

**Files:**
- Create: `tests/test_pod_path_audit.py`
- Modify: `tests/conftest.py` (add an autouse fixture alongside the existing `_clear_redaction_registry_between_tests`)

**Acceptance Criteria:**
- [ ] The audit passes over the current `src/kinoforge/` tree
- [ ] A falsification test plants a violation in a temp tree and proves the audit reports it
- [ ] The audit does NOT flag `/workspace` in comments, docstrings, or in `providers/runpod/` where it is the correct owner
- [ ] The autouse fixture redirects both dir vars AND patches the module attributes of any already-imported server module
- [ ] After a full suite run, neither scratch dir has been created

**Verify:** `pixi run python -m pytest tests/test_pod_path_audit.py -v` → all PASS

**Steps:**

- [ ] **Step 1: Write the audit and its falsification tests**

Create `tests/test_pod_path_audit.py`:

```python
"""Lockdown: no module may use a provider's volume path as a runtime default.

``/workspace`` is RunPod's volume mount and ``/cache/hf`` is Modal's. A module
that names either as an ``os.environ.get`` / ``setdefault`` fallback is correct
on one provider, wrong on the others, and unwritable on every CI runner — which
is how CI went red on 2026-09-18 and stayed red for three days.

The dev container cannot reproduce that failure: ``/workspace`` is the repo root
here and is writable, so the offending test passed locally while spilling 281
stub files into the tree. This audit is the compensating control for an
environment difference we cannot reproduce. It is NOT equivalent to it — a path
spelled differently, built by concatenation, or read through a helper rather
than ``os.environ.get`` directly is invisible here. See the residual-risk note
in the spec.

Pairs with the autouse fixture in tests/conftest.py, which stops the spill
regardless of what any individual test does.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_SRC_ROOT: Path = _REPO_ROOT / "src" / "kinoforge"

# Volume mounts that belong to one provider each.
_PROVIDER_VOLUME_ROOTS: tuple[str, ...] = ("/workspace", "/cache/hf")

# The package that legitimately OWNS each root — the provider whose mount it is.
# Anywhere else, the literal is a guess about someone else's filesystem.
_OWNERS: dict[str, str] = {
    "/workspace": "providers/runpod/",
    "/cache/hf": "providers/modal/",
}

# Matches the fallback argument of os.environ.get(...) / os.environ.setdefault(...)
# — i.e. the value used when the env var is ABSENT. The same literal in a
# comment, a docstring, or a wire field the provider legitimately sends is fine.
_FALLBACK = re.compile(
    r"os\.environ\.(?:get|setdefault)\(\s*[^,()]+,\s*[\"'](?P<path>/[^\"']*)[\"']"
)


def _scan(root: Path) -> list[tuple[str, int, str]]:
    """Report provider volume paths used as env fallbacks under *root*.

    Args:
        root: Package directory to walk for ``*.py`` files.

    Returns:
        ``(relative_path, line_number, offending_path)`` per violation,
        excluding files inside the owning provider's own package.
    """
    findings: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            match = _FALLBACK.search(line)
            if match is None:
                continue
            found = match.group("path")
            for volume_root in _PROVIDER_VOLUME_ROOTS:
                if not found.startswith(volume_root):
                    continue
                if _OWNERS[volume_root] in rel:
                    continue  # the provider that owns this mount may name it
                findings.append((rel, lineno, found))
    return findings


def test_no_module_defaults_to_a_provider_volume_path() -> None:
    """Every module must let the provider name the pod's writable dirs.

    Catches the exact 2026-09-18 CI break returning under a new name: a server
    module — or any other — resolving /workspace into a constant at import.
    """
    findings = _scan(_SRC_ROOT)

    assert findings == [], (
        "provider volume path used as a runtime default:\n"
        + "\n".join(f"  {p}:{n} -> {v}" for p, n, v in findings)
        + "\nThe provider exports these via core.pod_paths.pod_path_env; "
        "fall back to pod-local scratch, never to another provider's mount."
    )


def test_audit_fires_on_a_planted_violation(tmp_path: Path) -> None:
    """Reverse-test: without it, the guard above could no-op forever.

    A guard that passes vacuously — wrong root, a regex that matches nothing,
    an rglob over an empty tree — looks exactly like a guard that works. This
    is the single most valuable test in the file, and it mirrors
    test_source_audit.py's planted-credential test for the same reason.
    """
    pkg = tmp_path / "kinoforge" / "engines"
    pkg.mkdir(parents=True)
    (pkg / "rogue.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        'D = Path(os.environ.get("KINOFORGE_ARTIFACT_DIR", "/workspace/artifacts"))\n'
    )

    findings = _scan(tmp_path / "kinoforge")

    assert len(findings) == 1, findings
    rel, lineno, found = findings[0]
    assert rel == "engines/rogue.py"
    assert lineno == 3
    assert found == "/workspace/artifacts"


def test_audit_fires_on_modals_mount_too(tmp_path: Path) -> None:
    """Both provider roots are guarded, not just RunPod's.

    Catches a scanner that hardcodes /workspace — which would let the same
    defect reappear spelled as Modal's mount, on a RunPod server module.
    """
    pkg = tmp_path / "kinoforge" / "engines"
    pkg.mkdir(parents=True)
    (pkg / "rogue.py").write_text(
        'import os\nH = os.environ.setdefault("HF_HOME", "/cache/hf")\n'
    )

    findings = _scan(tmp_path / "kinoforge")

    assert [f[2] for f in findings] == ["/cache/hf"]


def test_audit_ignores_the_owning_provider_and_plain_prose(tmp_path: Path) -> None:
    """The owner may name its own mount; comments are never violations.

    Catches an over-broad audit. If this guard flagged RunPod's own module or
    every explanatory comment mentioning /workspace, it would be disabled
    within a week — and a disabled guard protects nothing.
    """
    root = tmp_path / "kinoforge"
    owner = root / "providers" / "runpod"
    owner.mkdir(parents=True)
    (owner / "__init__.py").write_text(
        'import os\nM = os.environ.get("MOUNT", "/workspace")\n'
    )
    prose = root / "engines"
    prose.mkdir(parents=True)
    (prose / "documented.py").write_text(
        "# /workspace is the RunPod volume mount; we do not hardcode it.\n"
        '"""Docstring mentioning /workspace/artifacts for context."""\n'
    )

    assert _scan(root) == []
```

- [ ] **Step 2: Run the tests, then prove the guard is not vacuous**

Run: `pixi run python -m pytest tests/test_pod_path_audit.py -v`
Expected: 4 passed (Task 4 already removed the real violations).

Then confirm the standing guard actually bites. Temporarily revert one server
constant to `/workspace/artifacts`:

```bash
pixi run python -m pytest tests/test_pod_path_audit.py::test_no_module_defaults_to_a_provider_volume_path -q
```

Expected: FAIL, naming that file and line. **Restore the file afterwards** and
re-run to confirm green. The three planted-violation tests cover this
automatically; this manual check confirms the `_SRC_ROOT` wiring is right, which
they cannot.

- [ ] **Step 3: Add the autouse fixture**

In `tests/conftest.py`, alongside the existing autouse redaction fixture:

```python
_POD_PATH_SERVER_MODULES: tuple[str, ...] = (
    "kinoforge.engines.diffusers.servers.wan_t2v_server",
    "kinoforge.engines.diffusers.servers.minimax_h3_server",
)


@pytest.fixture(autouse=True)
def _pod_dirs_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every server's pod directories at this test's tmp_path.

    Two mechanisms, because the constants resolve at IMPORT:

    * the env vars cover modules reloaded mid-test (the torch-build-log fixture
      calls importlib.reload) and subprocess servers (Tier-1 smoke);
    * the attribute patches cover modules already imported, where the constant
      is long since bound and changing the env would do nothing.

    Only already-imported modules are patched — this must not force-import a
    server into every unrelated test. Tests that patch these themselves still
    win, since their own monkeypatch runs after this fixture's.

    Args:
        tmp_path: pytest's per-test temporary directory.
        monkeypatch: pytest's patcher; undoes both mechanisms at teardown.
    """
    monkeypatch.setenv("KINOFORGE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("KINOFORGE_LORAS_DIR", str(tmp_path / "loras"))
    for name in _POD_PATH_SERVER_MODULES:
        module = sys.modules.get(name)
        if module is None:
            continue
        monkeypatch.setattr(
            module, "ARTIFACT_DIR", tmp_path / "artifacts", raising=False
        )
        monkeypatch.setattr(module, "LORAS_DIR", tmp_path / "loras", raising=False)
```

Add `import sys` to the imports at the top of `tests/conftest.py` if absent.

**Do NOT set `HF_HOME` here.** It is read by `huggingface_hub` at import across
the whole suite and appears in golden payloads; redirecting it per-test buys
nothing (no test writes an HF cache) and risks moving goldens.

- [ ] **Step 4: Prove the spill is closed**

```bash
rm -rf /tmp/kf-artifacts /tmp/kf-loras
pixi run python -m pytest -m 'not live' -q
ls -d /tmp/kf-artifacts /tmp/kf-loras 2>&1
```

Expected: full suite PASS, and both scratch dirs absent — every test wrote into
its own `tmp_path` instead. Their absence IS the assertion: if the fixture were
not working, the Task 4 fallbacks would have created them.

- [ ] **Step 5: Commit**

```bash
git add tests/test_pod_path_audit.py tests/conftest.py
pixi run pre-commit run --files tests/test_pod_path_audit.py tests/conftest.py
git commit -m "test: guard against provider volume paths returning as defaults"
```

---

### Task 6: Un-rot the weekly smoke

**Goal:** The Tier-3 config can actually pass, and the job stops booking pods on a cron nobody reads.

**Files:**
- Modify: `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml:49` and its `backend_options` block
- Modify: `.github/workflows/smoke-wan21-weekly.yml:3-5`

**Acceptance Criteria:**
- [ ] `max_usd_per_hr` is `0.60`, matching the operator decision of 2026-09-12 (`2c446eef`) on the sibling grid cfg
- [ ] `backend_options.runpod.cloud_type` is `secure`
- [ ] The cfg still loads and passes `kinoforge doctor`
- [ ] The workflow has no `schedule:` trigger and retains `workflow_dispatch:`
- [ ] The cfg's launch-payload golden reflects the new cap and `cloudType: SECURE`

**Verify:** `pixi run kinoforge doctor -c examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml` → exit 0

**Steps:**

- [ ] **Step 1: Raise the cap**

In `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml`,
change line 49 from `max_usd_per_hr: 0.40` to:

```yaml
    # Raised 0.40 -> 0.60 on 2026-09-21, matching the operator decision of
    # 2026-09-12 (2c446eef) on the sibling 1.3B grid cfg and taken for the
    # identical reason: at 0.40 no current RunPod offer can satisfy this cfg,
    # so every scheduled run failed in ~47 s with RateCapExceeded.
    max_usd_per_hr: 0.60
```

- [ ] **Step 2: Pin the secure pool**

Check for an existing block first — if one is there, extend it rather than adding a second:

```bash
rg -n 'backend_options' examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml
```

Add (or extend) under `compute:` at 2-space indent — sibling of `placement:`,
`lifecycle:` and `warm_reuse_auto_attach:`, matching
`runpod-diffusers-rife-60fps-interpolate.yaml:79-81`:

```yaml
  backend_options:
    runpod:
      # This cfg's max_lifetime is 60m and boot_timeout 30m — far past the
      # ~10 minute window after which RunPod reclaims community-pool pods
      # (U42; CLAUDE.md "RunPod community-pool deletions 2026-07-03"). The
      # 2026-09-21 scheduled run died exactly this way: ProvisionFailed,
      # "pod 'nn7goq4pu1fmnh' boot stalled", 19 minutes of billed nothing.
      cloud_type: secure
```

- [ ] **Step 3: Validate the config loads**

```bash
pixi run kinoforge doctor -c examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml
```

Expected: exit 0. (`doctor` runs the cfg validation registry — there is no
`validate` subcommand.)

- [ ] **Step 4: Drop the cron**

In `.github/workflows/smoke-wan21-weekly.yml`, replace:

```yaml
on:
  schedule:
    - cron: '0 12 * * 1'   # Monday 04:00 PT = 12:00 UTC
  workflow_dispatch:
```

with:

```yaml
# Dispatch-only since 2026-09-21. This job books real RunPod pods; running it
# unattended on a cron gave it none of the live-spend discipline CLAUDE.md
# requires (no authorisation in conversation, no utilisation polling, no frame
# QA), and it had been red for ten consecutive weeks without anyone reading the
# result. Live spend is authorised by user statement, so it runs on request.
# The independent leak-sweep cron remains the standing money guard.
on:
  workflow_dispatch:
```

- [ ] **Step 5: Regenerate this config's golden and REVIEW**

```bash
pixi run pre-commit run --all-files
pixi run python tools/snapshot_launch_payloads.py
git diff tests/providers/golden/launch_payloads/ | grep '^[+-]' | grep -v '^[+-][+-]'
```

Expected: only this cfg's payload moves, showing `cloudType` → `SECURE` and
possibly a different selected SKU consistent with the raised cap. A changed SKU
here is expected, not alarming — the golden pins which SKU a config selects, and
raising the cap is exactly a selection change. Confirm the new SKU's rate is at
or under 0.60.

- [ ] **Step 6: Commit**

```bash
pixi run python -m pytest tests/providers/ tests/cli/ -q
git add -A examples/configs/ .github/workflows/smoke-wan21-weekly.yml \
  tests/providers/golden/launch_payloads/
git commit -m "fix(ci): un-rot the Tier-3 smoke config and take it off the cron

The cfg carried max_usd_per_hr 0.40 — the cap raised to 0.60 elsewhere on
2026-09-12 because no RunPod offer could satisfy it — and no secure pin,
so pods past the ~10 minute community reclamation window boot-stalled. Ten
consecutive weekly failures, two distinct causes, both already fixed on
sibling configs and never propagated here.

Dropping the cron follows: the job books real pods with none of the
live-spend discipline the project requires, and nothing read its result."
```

---

### Task 7: Clear the spilled tree

**Goal:** Remove the artifacts of the bug from the working tree.

**Files:** No repo files. Removes the untracked `/workspace/artifacts/` and `/workspace/loras/`.

**Acceptance Criteria:**
- [ ] Contents verified as stub output BEFORE deletion, not assumed
- [ ] `/workspace/artifacts` and `/workspace/loras` are gone
- [ ] `git status` is clean afterwards (both were gitignored; nothing tracked moves)
- [ ] `output/` is untouched

**Verify:** `ls -d /workspace/artifacts /workspace/loras 2>&1` → "No such file or directory" for both; `git status --porcelain` → empty

**Steps:**

- [ ] **Step 1: Verify before deleting**

```bash
find /workspace/artifacts -type f -printf '%s\n' | sort -u
ls -la /workspace/loras
git check-ignore -v /workspace/artifacts /workspace/loras
```

Expected: a single distinct file size (`1647` — the stub mp4), a stub safetensors
in loras, and both paths confirmed gitignored.

**If more than one distinct size appears, STOP.** Something real may be in there
and it needs a human look before anything is removed.

- [ ] **Step 2: Confirm nothing tracked lives there**

```bash
git ls-files --error-unmatch /workspace/artifacts 2>&1 | head -2
```

Expected: `did not match any file(s) known to git`.

- [ ] **Step 3: Remove**

```bash
rm -rf /workspace/artifacts /workspace/loras
ls -d /workspace/artifacts /workspace/loras 2>&1
git status --porcelain
```

Expected: both absent; `git status` empty.

**`output/` is NOT touched** — it is the real CLI sink (`core/config.py:1369`)
and holds the FlashVSR fixture clip CLAUDE.md names for upscaler validation.

- [ ] **Step 4: No commit needed**

Nothing tracked changed. Proceed to Task 8.

---

### Task 8: Confirm CI is actually green

**Goal:** The claim "CI is fixed" rests on a green run on the runners, not on a local suite that cannot reproduce the failure.

**Files:** `PROGRESS.md` (snapshot update).

**Acceptance Criteria:**
- [ ] The branch is pushed
- [ ] The `CI` workflow completes `success` on BOTH `ubuntu-latest` and `macos-latest`
- [ ] `test_wan_t2v_server_torch_build_log.py` passes on the runners with that file unmodified
- [ ] `PROGRESS.md` records the outcome

**Verify:** `gh run list --workflow=ci.yml --limit 1` → `completed success`

**Steps:**

- [ ] **Step 1: Confirm the tree is clean and the full suite is green locally**

```bash
git status --porcelain
pixi run python -m pytest -m 'not live' -q 2>&1 | tail -5
```

Expected: empty status, suite PASS.

- [ ] **Step 2: Push**

```bash
git push
```

- [ ] **Step 3: Watch the run to completion**

```bash
gh run watch "$(gh run list --workflow=ci.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

- [ ] **Step 4: Confirm both matrix legs succeeded**

```bash
gh run view "$(gh run list --workflow=ci.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Expected: `Test (ubuntu-latest)` and `Test (macos-latest)` both ✓.

If either fails, get the failing test with:

```bash
gh api repos/killett/kinoforge/actions/jobs/<job-id>/logs | grep -E 'FAILED|short test summary'
```

and treat it as a new defect. **Do not claim the plan complete on a red run.**

- [ ] **Step 5: Update PROGRESS.md**

Add a RESUME SNAPSHOT entry recording: the pod-path seam shipped; CI green on
both runners with the run id as evidence; the Modal artifact mis-location fixed
as a side effect; the weekly smoke now dispatch-only with its cfg un-rotted and
NOT yet live-proven. Then:

```bash
git add PROGRESS.md
git commit -m "docs: record the pod-path seam and the CI recovery"
git push
```

---

## Deferred, out of plan

**Proving the weekly smoke config green costs money, so it is the operator's
call — not a task here.** The evidence for the two edits is strong (a logged
`RateCapExceeded` at exactly the old cap, and a logged boot-stall on an unpinned
pool) but neither is measured. If you want it proven:

```bash
pixi run preflight
```

```bash
gh workflow run smoke-wan21-weekly.yml
```

Poll utilisation during the run per CLAUDE.md — GPU 0% for three consecutive
probes means a dead pod, not patience — and verify teardown after the run exits
with `pixi run kinoforge list`, expecting both `No running instances.` AND
`No instances recorded in ledger.`

## Risks

| Risk | Mitigation |
|------|-----------|
| The `HF_HOME` hand-off drops between Tasks 2-3 and Task 4, sending a 70 GB download to container disk | Tasks 2 and 3 block Task 4; both carry an explicit `HF_HOME` assertion; Task 4 Step 4 refuses to delete the H3 setdefault unless Task 3 is done |
| Golden regeneration hides an unintended change inside a base64 blob | Every regeneration step decodes to plaintext before committing; `_golden_provision.json` is plaintext and is read first |
| The autouse fixture masks a real bug by redirecting paths production should have set | The Task 5 audit tests the SOURCE, not the runtime, so it still fires with the fixture active |
| Raising the cap selects a pricier SKU | `lifecycle.budget: 0.50` and `max_lifetime: 60m` still bound the run; the golden pins which SKU is selected, so a surprise is visible offline before any spend |
| The audit's narrow regex gives false confidence | Stated as residual risk in the module docstring and the spec: concatenated paths, helper-wrapped reads, and differently-spelled mounts are invisible to it |
