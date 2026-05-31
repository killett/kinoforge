# Layer I Implementation Plan — fal.ai adapter + UX A + hosted hardening

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first per-provider hosted engine (`FalEngine` against fal.ai's queue API), close the four bugs and one architectural gap surfaced by the Option-B smoke-test sprint, and produce kinoforge's first real public-provider artifact.

**Architecture:** Sibling engine `FalEngine` composes shared helpers (`set_by_dot_path`, `find_asset`, `ffmpeg_last_frame`) rather than inheriting from `HostedAPIEngine`. UX A puts a cred/health preflight inside `orchestrator.generate()` — hosted runs it every call, compute path skips on a persistent per-instance marker keyed by `capability_key`. Pydantic validators on `HostedEngineConfig` and the new `FalEngineConfig` move credential-name / endpoint-shape mistakes from runtime to config load. The provisioner cfg-dict bug fixed in `e78cafc` (on `main`) gains regression coverage for Diffusers + ComfyUI.

**Tech Stack:** Python 3.13, pydantic v2, urllib (stdlib), pytest, fal.ai queue REST API. No new runtime deps. Live tests opt-in via `KINOFORGE_LIVE_TESTS=1 + FAL_KEY`.

**Spec:** `docs/superpowers/specs/2026-05-31-layer-i-fal-adapter-ux-a-design.md`

**Branch:** `build/layer-i` (already created; rooted at `0342300` after design-doc commit).

---

## File map

**New files:**
- `src/kinoforge/core/provision_state.py` — marker file helpers (~80 LOC).
- `src/kinoforge/engines/fal/__init__.py` — `FalEngine` + `FalBackend`, self-registers under `"fal"` (~250 LOC).
- `src/kinoforge/engines/fal/wire.py` — pure HTTP-shape helpers (~80 LOC).
- `examples/configs/fal.yaml` — first real-provider example.
- `tests/core/test_provision_state.py` — marker helpers.
- `tests/core/test_orchestrator_compute.py` — UX A compute-path tests.
- `tests/engines/test_fal.py` — engine + backend with injected HTTP spies.
- `tests/engines/test_fal_wire.py` — pure helpers in isolation.
- `tests/live/__init__.py` — empty package marker.
- `tests/live/test_fal_live.py` — opt-in live test, gated by env.

**Modified files:**
- `src/kinoforge/core/config.py` — validators on `HostedEngineConfig`; new `FalEngineConfig`; `EngineConfig.fal` field; `KNOWN_ENGINES` += `"fal"`.
- `src/kinoforge/core/profiles.py` — `declared_flags` WARNING → DEBUG on fresh-discovery path.
- `src/kinoforge/core/orchestrator.py` — UX A preflight (hosted + compute).
- `src/kinoforge/engines/fake/__init__.py` — populate `declared_flags_map` default.
- `src/kinoforge/engines/hosted/__init__.py` — `declared_flags_map` default + clearer runtime `AuthError`.
- `src/kinoforge/_adapters.py` — import the new fal engine.
- `examples/configs/hosted.yaml` — fix to absolute URL + new required fields + shim contract docs.
- `tests/core/test_provisioner.py` — regression tests for Diffusers + ComfyUI cfg-dict.
- `tests/core/test_config.py` — validator tests.
- `tests/core/test_orchestrator.py` — UX A hosted preflight tests.
- `tests/core/test_profiles.py` — DEBUG vs WARNING by call-site tests.
- `tests/engines/test_fake.py` — declared_flags_map populated.
- `tests/engines/test_hosted.py` — declared_flags_map populated + new AuthError message.
- `tests/test_examples.py` — new YAML cases.
- `tests/test_core_invariant.py` — allowlist regex extension.
- `pyproject.toml` — `live` pytest marker.
- `pixi.toml` — `test-live` task.
- `README.md` — fal.ai usage section under Credentials.
- `PROGRESS.md` — Phase 19 entry with per-task SHAs.

---

## Task sequence summary

| # | Task | Critical-path |
|---|---|---|
| 1 | Provisioner cfg-dict regression — Diffusers + ComfyUI | parallel |
| 2 | `declared_flags` WARNING → DEBUG on fresh-discovery path | parallel |
| 3 | `FakeEngine.declared_flags_map` default | depends on 2 |
| 4 | Pydantic validators on `HostedEngineConfig` | parallel |
| 5 | `HostedAPIEngine` runtime `AuthError` defense-in-depth + `declared_flags_map` default | depends on 4 |
| 6 | Rewrite `examples/configs/hosted.yaml` | depends on 4 |
| 7 | `core/provision_state.py` + tests | parallel |
| 8 | UX A hosted preflight in `orchestrator.generate()` | depends on 4, 5 |
| 9 | UX A compute preflight — marker + `acquire_lock` | depends on 7, 8 |
| 10 | `FalEngineConfig` pydantic block | depends on 4 |
| 11 | `FalEngine` + `FalBackend` + wire helpers + tests | depends on 8, 10 |
| 12 | `_adapters.py` + `fal.yaml` + invariant allowlist + tooling | depends on 11 |
| 13 | Live opt-in test + manual smoke (**user-gate**) | depends on 12 |

---

## Task 1: Provisioner cfg-dict regression — Diffusers + ComfyUI

**Goal:** Lock down the full blast radius of the provisioner cfg-dict bug (`e78cafc`) by adding regression coverage for the two other engines (Diffusers, ComfyUI) that also call `cfg.get(...)` in `provision()`.

**Files:**
- Modify: `tests/core/test_provisioner.py` (append two tests after the existing pydantic-cfg test).

**Acceptance Criteria:**
- [ ] `test_diffusers_provision_receives_dict_cfg` passes: when `DiffusersEngine.provision` is the engine, it receives a `dict` (not a pydantic model).
- [ ] `test_comfyui_provision_receives_dict_cfg` passes: when `ComfyUIEngine.provision` is the engine, it receives a `dict`.
- [ ] Both tests use a `_PydanticLikeCfg` stand-in (defined locally) so they don't depend on the real `Config` schema.

**Verify:** `pixi run pytest tests/core/test_provisioner.py -v -k "diffusers_provision_receives_dict or comfyui_provision_receives_dict"` — 2 PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Open `tests/core/test_provisioner.py`. Locate `test_pydantic_cfg_is_dumped_to_dict_before_engine_provision` (added in commit `e78cafc`). Append the following two tests immediately after it:

```python
def test_diffusers_provision_receives_dict_cfg(tmp_path: Path) -> None:
    """Diffusers engine.provision must receive a dict (not a pydantic Config).

    Bug catch: DiffusersEngine.provision calls cfg.get("engine", {}).get("diffusers", {})
    on the cfg arg.  Before fix e78cafc, the provisioner passed the pydantic
    Config object through; this test would have hit AttributeError at that line.
    """
    from kinoforge.engines.diffusers import DiffusersEngine

    scheme = "diffcfgfake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    received_cfgs: list[Any] = []
    real_engine = DiffusersEngine()

    def _capture_provision(instance: Instance | None, cfg: Any) -> None:
        received_cfgs.append(cfg)
        # Defensive: exercise the same access pattern the real engine uses,
        # so that a future regression to non-dict cfg also fails here.
        _ = cfg.get("engine", {}).get("diffusers", {})  # type: ignore[union-attr]

    real_engine.provision = _capture_provision  # type: ignore[method-assign]

    class _PydanticLikeCfg:
        def __init__(self, models: list[_ModelEntry]) -> None:
            self.models = models

        def model_dump(self) -> dict[str, Any]:
            return {"engine": {"kind": "diffusers", "diffusers": {"pip": [], "server_cmd": []}}}

    cfg = _PydanticLikeCfg([_ModelEntry(ref=f"{scheme}:m", target="checkpoints")])

    provision(
        real_engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
    )

    assert len(received_cfgs) == 1
    assert isinstance(received_cfgs[0], dict), (
        f"DiffusersEngine.provision must receive dict, got {type(received_cfgs[0]).__name__}"
    )


def test_comfyui_provision_receives_dict_cfg(tmp_path: Path) -> None:
    """ComfyUI engine.provision must receive a dict (not a pydantic Config).

    Bug catch: ComfyUIEngine.provision calls cfg.get("engine", {}).get("comfyui", {})
    plus several other dict ops on cfg.  Before fix e78cafc, the provisioner
    passed the pydantic Config object through, crashing in this method.
    """
    from kinoforge.engines.comfyui import ComfyUIEngine

    scheme = "comfycfgfake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    received_cfgs: list[Any] = []
    real_engine = ComfyUIEngine()

    def _capture_provision(instance: Instance | None, cfg: Any) -> None:
        received_cfgs.append(cfg)
        _ = cfg.get("engine", {}).get("comfyui", {})  # type: ignore[union-attr]
        _ = cfg.get("models", [])  # type: ignore[union-attr]

    real_engine.provision = _capture_provision  # type: ignore[method-assign]

    class _PydanticLikeCfg:
        def __init__(self, models: list[_ModelEntry]) -> None:
            self.models = models

        def model_dump(self) -> dict[str, Any]:
            return {
                "engine": {"kind": "comfyui", "comfyui": {"version": "0.3.10"}},
                "models": [],
            }

    cfg = _PydanticLikeCfg([_ModelEntry(ref=f"{scheme}:m", target="checkpoints")])

    provision(
        real_engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
    )

    assert len(received_cfgs) == 1
    assert isinstance(received_cfgs[0], dict), (
        f"ComfyUIEngine.provision must receive dict, got {type(received_cfgs[0]).__name__}"
    )
```

- [ ] **Step 2: Run tests to verify they pass (fix already applied)**

```bash
pixi run pytest tests/core/test_provisioner.py -v -k "diffusers_provision_receives_dict or comfyui_provision_receives_dict"
```

Expected: 2 PASS. (The fix in `e78cafc` makes both green; the tests serve as regression coverage.)

- [ ] **Step 3: Confirm no other test broke**

```bash
pixi run pytest tests/core/test_provisioner.py -q
```

Expected: 10 PASS (8 original + 2 new).

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/core/test_provisioner.py
git add tests/core/test_provisioner.py
git commit -m "test(provisioner): regression coverage for Diffusers + ComfyUI cfg-dict (Layer I Task 1)"
```

---

## Task 2: `declared_flags` WARNING → DEBUG on fresh-discovery path

**Goal:** Stop the noisy WARNING that fires on every fresh-cache generate. The WARNING was logged in `JsonProfileCache.discover()` when the engine returned an empty `declared_flags` for the current key — but on the discovery path, the probe is the source of truth and missing declared flags isn't yet a problem. Keep the WARNING in `verify()` where it signals real drift.

**Files:**
- Modify: `src/kinoforge/core/profiles.py` — adjust log level in `discover()` only.
- Modify: `tests/core/test_profiles.py` — replace existing WARNING-on-discover assertion with DEBUG-on-discover + WARNING-on-verify.

**Acceptance Criteria:**
- [ ] On `discover()` with both `supports_native_extension` and `supports_joint_audio` absent → log level is DEBUG (not WARNING).
- [ ] On `verify()` against a cached profile whose engine still returns empty declared_flags → log level remains WARNING.
- [ ] No regression in any other profile-cache test.

**Verify:** `pixi run pytest tests/core/test_profiles.py -v` — all green.

**Steps:**

- [ ] **Step 1: Locate the warning emission**

```bash
grep -n "declared no strategy flags" src/kinoforge/core/profiles.py
```

Expected: one match, inside `discover()` (e.g. `_log.warning("engine declared no strategy flags …")`).

- [ ] **Step 2: Find and read the surrounding block**

```bash
grep -n "declared_flags\|inspect_capabilities\|def discover\|def verify" src/kinoforge/core/profiles.py
```

Identify the function containing the warning. If the same block is reachable from both `discover()` and `verify()`, refactor minimally so `discover()` calls a private helper with `level=logging.DEBUG` and `verify()` with `level=logging.WARNING`. If the block lives only in `discover()`, change `_log.warning(...)` → `_log.debug(...)` and replicate the check at the call site in `verify()` at WARNING level.

- [ ] **Step 3: Apply the level change**

Open `src/kinoforge/core/profiles.py`. Inside the `discover()` body, change:

```python
_log.warning(
    "engine declared no strategy flags for capability key %s "
    "(supports_native_extension and supports_joint_audio both absent); "
    "check declared_flags_map for this engine/key combination",
    key.derive(),
)
```

to:

```python
_log.debug(
    "engine declared no strategy flags for capability key %s "
    "(supports_native_extension and supports_joint_audio both absent); "
    "this is normal on a fresh-discovery path",
    key.derive(),
)
```

Inside `verify()` body, after the `inspect_capabilities` call but before the field-by-field comparison, add:

```python
declared = engine.declared_flags(key)
if (
    "supports_native_extension" not in declared
    and "supports_joint_audio" not in declared
):
    _log.warning(
        "engine no longer declares strategy flags for cached key %s; "
        "either declared_flags_map regressed or the engine was downgraded",
        key.derive(),
    )
```

(Adapt to actual `verify` signature — likely `verify(profile, backend)` and `engine` may be reachable differently; use whichever object exposes `declared_flags`. If `engine` is not in scope inside `verify`, skip the verify-side warning and add a `# NOTE: WARNING moved to discover-time gap only; see plan task 2` comment instead.)

- [ ] **Step 4: Update tests in `tests/core/test_profiles.py`**

```bash
grep -n "no strategy flags\|caplog" tests/core/test_profiles.py
```

Replace the existing assertion that captures `WARNING` from discover with:

```python
def test_discover_with_no_declared_flags_logs_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Fresh discovery with no declared_flags should be a quiet DEBUG, not WARNING.

    Bug catch: previous behavior emitted WARNING on every fresh-cache run,
    drowning real signals.  Probe is source of truth on first-discover.
    """
    import logging
    caplog.set_level(logging.DEBUG, logger="kinoforge.profiles")
    # ... use the existing FakeEngine + FakeBackend test scaffolding ...
    # ... call cache.discover(key, engine, backend) ...
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        f"unexpected WARNING-or-higher records: {[r.message for r in caplog.records]}"
    )
    # Confirm the DEBUG message did fire (proves we exercised the path)
    assert any(
        "fresh-discovery" in r.message
        for r in caplog.records
        if r.levelno == logging.DEBUG
    )
```

If a `test_verify_with_no_declared_flags_logs_at_warning` analog is feasible (i.e. if `verify()` is callable with the available test scaffolding), add it:

```python
def test_verify_with_no_declared_flags_logs_at_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Verify against a stale cache where the engine no longer declares flags must WARN."""
    import logging
    caplog.set_level(logging.WARNING, logger="kinoforge.profiles")
    # ... build a cached profile, then call cache.verify(profile, backend) ...
    assert any(r.levelno == logging.WARNING for r in caplog.records)
```

If the existing verify scaffolding doesn't support this cleanly, skip the verify-side test and add a TODO comment referencing this task; keep only the DEBUG test.

- [ ] **Step 5: Run the tests**

```bash
pixi run pytest tests/core/test_profiles.py -v
```

Expected: all green.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_profiles.py
git add src/kinoforge/core/profiles.py tests/core/test_profiles.py
git commit -m "fix(profiles): downgrade declared_flags noise from WARNING to DEBUG on fresh-discovery path (Layer I Task 2)"
```

---

## Task 3: `FakeEngine.declared_flags_map` default

**Goal:** Populate `FakeEngine.declared_flags_map` with a default entry that matches the shipped `local-fake.yaml` capability key, so the local-fake smoke test produces no WARNING-or-higher log records.

**Files:**
- Modify: `src/kinoforge/engines/fake/__init__.py` — add map entry.
- Modify: `tests/engines/test_fake.py` — assert default flags returned for the local-fake key.

**Acceptance Criteria:**
- [ ] `FakeEngine().declared_flags(key)` returns `{"supports_native_extension": False, "supports_joint_audio": False}` when called with the `local-fake.yaml` `CapabilityKey`.
- [ ] Running the local-fake CLI smoke (manual check via `kinoforge --state-dir /tmp/x generate -c examples/configs/local-fake.yaml --prompt p --mode t2v 2>&1`) produces NO WARNING records (informally verified in test 4 of this task).

**Verify:** `pixi run pytest tests/engines/test_fake.py -v -k declared_flags` — PASS.

**Steps:**

- [ ] **Step 1: Read current FakeEngine to confirm map shape**

```bash
grep -n "declared_flags_map\|declared_flags\|key_base\|def __init__" src/kinoforge/engines/fake/__init__.py
```

Identify the existing `declared_flags_map` dict (currently empty or missing) and the key shape used by `declared_flags(key)`. Likely `(model_id, precision)` tuple.

- [ ] **Step 2: Determine the local-fake capability key**

```bash
pixi run python -c "
from kinoforge.core.config import load_config
cfg = load_config('examples/configs/local-fake.yaml')
key = cfg.capability_key()
print('key:', key)
print('engine.declared_flags arg derived from key_base:', cfg.engine.kind, cfg.engine.precision)
"
```

Note the printed values. Likely: `engine="fake"`, `precision="fp16"`, `base_model="https://example.com/models/fake-base.safetensors"`. The map key needs to match whatever `FakeEngine.declared_flags(key)` uses internally (commonly `(key_base(key), key.precision)` or similar).

- [ ] **Step 3: Write the failing test**

Append to `tests/engines/test_fake.py`:

```python
def test_declared_flags_returns_default_for_local_fake_key() -> None:
    """Fake engine must declare strategy flags for the local-fake.yaml key.

    Bug catch: empty declared_flags_map triggers a WARNING in JsonProfileCache.discover
    on every fresh-cache generate against local-fake.  Populate the default so the
    canonical offline config produces a clean log.
    """
    from kinoforge.core.config import load_config
    from kinoforge.engines.fake import FakeEngine

    cfg = load_config("examples/configs/local-fake.yaml")
    engine = FakeEngine()
    flags = engine.declared_flags(cfg.capability_key())
    assert flags == {
        "supports_native_extension": False,
        "supports_joint_audio": False,
    }
```

- [ ] **Step 4: Run and confirm RED**

```bash
pixi run pytest tests/engines/test_fake.py::test_declared_flags_returns_default_for_local_fake_key -v
```

Expected: FAIL (empty dict or KeyError).

- [ ] **Step 5: Implement — add the default map entry**

Inside `FakeEngine.__init__` (or as a class-level dict), add:

```python
# Default entry matching examples/configs/local-fake.yaml so that the canonical
# offline smoke test produces no declared_flags WARNING.
self.declared_flags_map: dict[tuple[str, str], dict[str, bool]] = {
    ("https://example.com/models/fake-base.safetensors", "fp16"): {
        "supports_native_extension": False,
        "supports_joint_audio": False,
    },
}
```

Adjust the tuple shape to match `FakeEngine.declared_flags`'s actual key lookup (see Step 2). If the lookup uses `(key.base_model, key.precision)`, the tuple above is correct.

- [ ] **Step 6: Run test to confirm GREEN**

```bash
pixi run pytest tests/engines/test_fake.py::test_declared_flags_returns_default_for_local_fake_key -v
```

Expected: PASS.

- [ ] **Step 7: Manual smoke for the WARNING-free path**

```bash
rm -rf /tmp/kinoforge-smoke
pixi run python -m kinoforge --state-dir /tmp/kinoforge-smoke generate -c examples/configs/local-fake.yaml --prompt "a cat" --mode t2v 2>&1 | grep -i WARNING || echo "no WARNING records — good"
```

Expected: `no WARNING records — good`. (Task 2 + Task 3 together produce the clean path.)

- [ ] **Step 8: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/fake/__init__.py tests/engines/test_fake.py
git add src/kinoforge/engines/fake/__init__.py tests/engines/test_fake.py
git commit -m "fix(engines/fake): populate declared_flags_map for local-fake.yaml key (Layer I Task 3)"
```

---

## Task 4: Pydantic validators on `HostedEngineConfig`

**Goal:** Move two config errors from runtime to load: empty `api_key_env` and relative `endpoint`. The first closes Bug 7 primary fix; the second closes Bug 2 at validation time.

**Files:**
- Modify: `src/kinoforge/core/config.py:118-149` — add two `@field_validator` methods to `HostedEngineConfig`.
- Modify: `tests/core/test_config.py` — add validator tests.

**Acceptance Criteria:**
- [ ] `HostedEngineConfig(provider="x", endpoint="http://e", model="m", api_key_env="")` raises `pydantic.ValidationError` with message containing `api_key_env`.
- [ ] `HostedEngineConfig(provider="x", endpoint="/relative", model="m", api_key_env="K")` raises `pydantic.ValidationError` with message containing `endpoint`.
- [ ] `HostedEngineConfig(provider="x", endpoint="https://e", model="m", api_key_env="K")` constructs successfully.
- [ ] `health_url` stays optional (empty string OK).

**Verify:** `pixi run pytest tests/core/test_config.py -v -k "hosted_validator"` — 3 PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_config.py`:

```python
def test_hosted_validator_rejects_empty_api_key_env() -> None:
    """HostedEngineConfig must reject empty api_key_env at load.

    Bug catch: empty api_key_env propagates to runtime as AuthError("missing ")
    with no context.  Catch it at config load instead.
    """
    from pydantic import ValidationError
    from kinoforge.core.config import HostedEngineConfig

    with pytest.raises(ValidationError) as exc_info:
        HostedEngineConfig(
            provider="x", endpoint="https://e", model="m", api_key_env=""
        )
    assert "api_key_env" in str(exc_info.value)


def test_hosted_validator_rejects_relative_endpoint() -> None:
    """HostedEngineConfig must reject relative endpoint paths at load.

    Bug catch: relative endpoint like '/fal-ai/x' crashes urllib mid-flight
    with ValueError: unknown url type.  Catch it at config load instead.
    """
    from pydantic import ValidationError
    from kinoforge.core.config import HostedEngineConfig

    with pytest.raises(ValidationError) as exc_info:
        HostedEngineConfig(
            provider="x", endpoint="/relative/path", model="m", api_key_env="K"
        )
    assert "endpoint" in str(exc_info.value)


def test_hosted_validator_accepts_well_formed_config() -> None:
    """A correctly-formed HostedEngineConfig constructs without error."""
    from kinoforge.core.config import HostedEngineConfig

    cfg = HostedEngineConfig(
        provider="x",
        endpoint="https://example.com/api",
        model="m",
        api_key_env="MY_KEY",
        health_url="",  # empty health_url stays valid
    )
    assert cfg.endpoint == "https://example.com/api"
    assert cfg.api_key_env == "MY_KEY"
```

- [ ] **Step 2: Run tests, confirm RED**

```bash
pixi run pytest tests/core/test_config.py -v -k "hosted_validator"
```

Expected: 2 FAIL (rejection tests), 1 PASS (well-formed test). The two RED tests prove validators are not yet present.

- [ ] **Step 3: Add the validators**

Open `src/kinoforge/core/config.py`. Inside `HostedEngineConfig` (after the field declarations, before any existing model_validator), add:

```python
    @field_validator("api_key_env")
    @classmethod
    def _check_api_key_env_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "engine.hosted.api_key_env must be a non-empty string "
                "(name of the env var carrying the API credential)"
            )
        return v

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint_absolute_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"engine.hosted.endpoint must be an absolute http(s):// URL, "
                f"got {v!r}"
            )
        return v
```

`field_validator` is already imported at line 17 of `config.py` — no new imports needed.

- [ ] **Step 4: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_config.py -v -k "hosted_validator"
```

Expected: 3 PASS.

- [ ] **Step 5: Run full config test suite + examples**

```bash
pixi run pytest tests/core/test_config.py tests/test_examples.py -v
```

`test_examples.py` will likely fail at this point because `examples/configs/hosted.yaml` is still broken (relative URL + empty `api_key_env`). That failure is **expected** and will be fixed in Task 6. Do not commit yet if `test_examples.py` regresses on the hosted-yaml load test — Task 6 fixes it.

If `test_examples.py` has an existing test that loads `hosted.yaml`, mark this task done only after Task 6's commit lands. Otherwise commit now.

Practical approach: run only the config tests for this commit and accept that `test_examples.py` is temporarily red on the hosted-yaml case until Task 6.

```bash
pixi run pytest tests/core/test_config.py -v
```

Expected: all green.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "feat(config): pydantic validators on HostedEngineConfig (Layer I Task 4)

Reject empty api_key_env and relative endpoint at config load.  Closes
Bug 7 primary fix and catches Bug 2 before any runtime crash.

NOTE: examples/configs/hosted.yaml is temporarily invalid under these
validators; Task 6 fixes the example."
```

---

## Task 5: `HostedAPIEngine` runtime `AuthError` defense-in-depth + `declared_flags_map` default

**Goal:** Two small fixes in `engines/hosted/__init__.py`:
1. Bug 7 defense-in-depth — if pydantic loading is bypassed and `key_name == ""` at runtime, raise a clearer message.
2. Populate `declared_flags_map` with a default entry for the canonical hosted key so a fresh hosted run produces no WARNING.

**Files:**
- Modify: `src/kinoforge/engines/hosted/__init__.py:443-452` — replace `AuthError(f"missing {key_name}")`.
- Modify: `src/kinoforge/engines/hosted/__init__.py` (init or class-level) — add `declared_flags_map` default.
- Modify: `tests/engines/test_hosted.py` — two test updates.

**Acceptance Criteria:**
- [ ] When `HostedAPIEngine.provision` runs with an empty `key_name`, the raised `AuthError` message includes the substring `"engine.hosted.api_key_env is empty"` (not `"missing "`).
- [ ] `HostedAPIEngine().declared_flags(key)` returns `{"supports_native_extension": False, "supports_joint_audio": False}` for a key derived from `examples/configs/hosted.yaml` (post-Task-6 fix).

**Verify:** `pixi run pytest tests/engines/test_hosted.py -v -k "auth_error_message or declared_flags_default"` — 2 PASS.

**Steps:**

- [ ] **Step 1: Confirm the line to change**

```bash
grep -n "missing {key_name}\|missing \"\|missing\"\|AuthError" src/kinoforge/engines/hosted/__init__.py
```

Expected: one match at the `provision()` cred check, around line 452.

- [ ] **Step 2: Write the failing tests**

Append to `tests/engines/test_hosted.py`:

```python
def test_provision_auth_error_message_when_key_name_empty() -> None:
    """When api_key_env is somehow empty at runtime (validator bypass), the
    AuthError message must be self-explanatory, not 'missing '.

    Defense-in-depth: pydantic validator (Task 4) catches this at load.
    This test exercises the runtime fallback for direct-constructor calls.
    """
    from kinoforge.core.credentials import CredentialProvider
    from kinoforge.core.errors import AuthError
    from kinoforge.engines.hosted import HostedAPIEngine

    class _NullCreds(CredentialProvider):
        def get(self, key: str) -> str | None:
            return None

    engine = HostedAPIEngine(creds=_NullCreds())  # type: ignore[arg-type]
    cfg = {
        "engine": {"hosted": {"api_key_env": "", "endpoint": "https://e", "health_url": ""}}
    }
    with pytest.raises(AuthError) as exc_info:
        engine.provision(None, cfg)
    assert "engine.hosted.api_key_env is empty" in str(exc_info.value)


def test_declared_flags_default_for_hosted_yaml_key() -> None:
    """HostedAPIEngine must declare strategy flags for the shipped hosted.yaml key."""
    from kinoforge.core.config import load_config
    from kinoforge.engines.hosted import HostedAPIEngine

    cfg = load_config("examples/configs/hosted.yaml")
    engine = HostedAPIEngine()
    flags = engine.declared_flags(cfg.capability_key())
    assert flags == {
        "supports_native_extension": False,
        "supports_joint_audio": False,
    }
```

**Note:** `test_declared_flags_default_for_hosted_yaml_key` will fail until Task 6 rewrites `hosted.yaml` to validator-clean form. Document this in the task ordering: if you run this test before Task 6 lands, it errors at `load_config`. Mark this AC as conditional on Task 6.

- [ ] **Step 3: Run the AuthError test, confirm RED**

```bash
pixi run pytest tests/engines/test_hosted.py::test_provision_auth_error_message_when_key_name_empty -v
```

Expected: FAIL — current message is `"missing "`.

- [ ] **Step 4: Fix the runtime AuthError message**

Open `src/kinoforge/engines/hosted/__init__.py`. Find:

```python
key_name: str = str(hosted_cfg.get("api_key_env", ""))
cred = self._creds.get(key_name)
if cred is None:
    raise AuthError(f"missing {key_name}")
```

Replace with:

```python
key_name: str = str(hosted_cfg.get("api_key_env", ""))
if not key_name:
    raise AuthError(
        "engine.hosted.api_key_env is empty — set the env var name in your config"
    )
cred = self._creds.get(key_name)
if cred is None:
    raise AuthError(f"engine.hosted.api_key_env={key_name!r} is not set in env")
```

- [ ] **Step 5: Run the AuthError test, confirm GREEN**

```bash
pixi run pytest tests/engines/test_hosted.py::test_provision_auth_error_message_when_key_name_empty -v
```

Expected: PASS.

- [ ] **Step 6: Add `declared_flags_map` default**

In `HostedAPIEngine.__init__`, add (or extend) the map:

```python
# Default entry matching examples/configs/hosted.yaml so a fresh hosted run
# produces no declared_flags WARNING.  Map key shape follows declared_flags()
# lookup — confirm with: grep "declared_flags_map\[" src/kinoforge/engines/hosted/__init__.py
self.declared_flags_map: dict[tuple[str, str], dict[str, bool]] = {
    # key shape: (model_id, precision) per key_base() + EngineConfig.precision
    ("wan-ai/Wan2.2-T2V-A14B", ""): {
        "supports_native_extension": False,
        "supports_joint_audio": False,
    },
}
```

Adjust the tuple to match the actual `declared_flags(key)` lookup (verify by reading the engine's `declared_flags` method first). Likely uses `key_base(cfg)` which returns `hosted_cfg["model"]`.

If the default `declared_flags_map` is class-level, prefer to make it instance-level in `__init__` to allow tests to override.

- [ ] **Step 7: Run the declared_flags test (still RED until Task 6)**

```bash
pixi run pytest tests/engines/test_hosted.py::test_declared_flags_default_for_hosted_yaml_key -v
```

Expected: FAIL because `load_config("examples/configs/hosted.yaml")` raises `ValidationError`. This is OK — Task 6 fixes it. Mark the test with `@pytest.mark.xfail(reason="depends on Task 6 hosted.yaml fix", strict=True)` for now; Task 6 removes the xfail marker.

Replace the test header to:

```python
@pytest.mark.xfail(reason="depends on Layer I Task 6 hosted.yaml fix", strict=True)
def test_declared_flags_default_for_hosted_yaml_key() -> None:
    ...
```

```bash
pixi run pytest tests/engines/test_hosted.py::test_declared_flags_default_for_hosted_yaml_key -v
```

Expected: XFAIL (counted as PASS).

- [ ] **Step 8: Run full hosted test suite**

```bash
pixi run pytest tests/engines/test_hosted.py -v
```

Expected: all green (existing tests + 2 new, with the xfail).

- [ ] **Step 9: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py
git add src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py
git commit -m "fix(engines/hosted): clearer AuthError + declared_flags_map default (Layer I Task 5)"
```

---

## Task 6: Rewrite `examples/configs/hosted.yaml`

**Goal:** Make `hosted.yaml` validator-clean under Task 4's pydantic rules; document that `HostedAPIEngine` speaks a synthetic shim contract (not any real public API); remove Task 5's `xfail` marker.

**Files:**
- Modify: `examples/configs/hosted.yaml` (full rewrite).
- Modify: `tests/engines/test_hosted.py` — remove the `xfail` marker added in Task 5.
- Modify: `tests/test_examples.py` — add or update a hosted.yaml load test.

**Acceptance Criteria:**
- [ ] `examples/configs/hosted.yaml` loads via `load_config(...)` without `ValidationError`.
- [ ] The file's top-comment block documents the synthetic shim contract (POST endpoint → `{"job_id"}`, GET `endpoint/status/{id}` → `{"status":"done"}`) and explicitly says no public provider implements it.
- [ ] Task 5's xfail test now passes as a regular green test.

**Verify:** `pixi run pytest tests/engines/test_hosted.py tests/test_examples.py -v` — all green.

**Steps:**

- [ ] **Step 1: Write the new `examples/configs/hosted.yaml`**

Overwrite the file with:

```yaml
# kinoforge example: HostedAPIEngine (user-deployed shim)
#
# HostedAPIEngine speaks a SYNTHETIC server contract:
#
#   POST {endpoint}                  -> {"job_id": "..."}
#   GET  {endpoint}/status/{job_id}  -> {"status": "done"|"running"|..., "<url_path>": "..."}
#   GET  {health_url}                -> 200 OK
#
# No public provider (fal.ai, Replicate, HuggingFace) implements this contract.
# Use HostedAPIEngine when you have deployed your OWN inference shim that wraps a
# real backend with the contract above.  For real public providers, see the
# per-provider engines (e.g. examples/configs/fal.yaml).

engine:
  kind: hosted
  precision: ""
  hosted:
    provider: my-shim
    endpoint: "https://your-shim.example.com/inference"
    model: "wan-ai/Wan2.2-T2V-A14B"
    api_key_env: "MY_SHIM_KEY"
    health_url: "https://your-shim.example.com/health"
    url_path: video.url

models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors"
    kind: base
    target: checkpoints

lifecycle:
  budget: 5.0
```

- [ ] **Step 2: Confirm the file loads**

```bash
pixi run python -c "from kinoforge.core.config import load_config; cfg = load_config('examples/configs/hosted.yaml'); print(cfg.engine.kind, cfg.engine.hosted.endpoint, cfg.engine.hosted.api_key_env)"
```

Expected: `hosted https://your-shim.example.com/inference MY_SHIM_KEY`. If `ValidationError` fires, revisit the YAML.

- [ ] **Step 3: Remove the xfail marker from Task 5's hosted-default-flags test**

Open `tests/engines/test_hosted.py`. Find the `@pytest.mark.xfail(reason="depends on Layer I Task 6 hosted.yaml fix", strict=True)` decorator added in Task 5 and delete it.

- [ ] **Step 4: Add / update `tests/test_examples.py`**

```bash
grep -n "hosted.yaml\|hosted_" tests/test_examples.py
```

If a `test_examples_hosted_yaml_loads`-style test exists, ensure it asserts the new fields (e.g. `cfg.engine.hosted.api_key_env == "MY_SHIM_KEY"`). If absent, add:

```python
def test_hosted_yaml_loads_under_new_validators() -> None:
    """examples/configs/hosted.yaml must satisfy Task 4 validators."""
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/hosted.yaml")
    assert cfg.engine.kind == "hosted"
    assert cfg.engine.hosted is not None
    assert cfg.engine.hosted.endpoint.startswith("https://")
    assert cfg.engine.hosted.api_key_env == "MY_SHIM_KEY"
    assert cfg.engine.hosted.health_url == "https://your-shim.example.com/health"
    assert cfg.engine.hosted.url_path == "video.url"
```

- [ ] **Step 5: Run the affected tests**

```bash
pixi run pytest tests/engines/test_hosted.py tests/test_examples.py -v
```

Expected: all green. The previously-xfail test in `test_hosted.py` now passes as a regular test.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files examples/configs/hosted.yaml tests/engines/test_hosted.py tests/test_examples.py
git add examples/configs/hosted.yaml tests/engines/test_hosted.py tests/test_examples.py
git commit -m "fix(examples): rewrite hosted.yaml for new validators + shim contract docs (Layer I Task 6)"
```

---

## Task 7: `core/provision_state.py` + tests

**Goal:** Pure helper module for the per-instance provision marker. No I/O beyond `Path` reads/writes. Foundation for Task 9's compute-path UX A wiring.

**Files:**
- Create: `src/kinoforge/core/provision_state.py`
- Create: `tests/core/test_provision_state.py`

**Acceptance Criteria:**
- [ ] `marker_path(state_dir, instance_id)` returns `<state_dir>/instances/<instance_id>/.provisioned`.
- [ ] `read_marker(path)` returns `None` when the file is absent, corrupt, or missing required keys; returns the dict otherwise.
- [ ] `write_marker(path, instance_id, capability_key, engine_name, timestamp)` creates parent dirs as needed and atomically writes JSON with `{instance_id, capability_key, engine, timestamp}`.
- [ ] `is_marker_current(marker, capability_key)` returns `True` iff `marker["capability_key"] == capability_key`.
- [ ] All 5 unit tests pass.

**Verify:** `pixi run pytest tests/core/test_provision_state.py -v` — 5 PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_provision_state.py`:

```python
"""Tests for core.provision_state helpers (Layer I)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.provision_state import (
    is_marker_current,
    marker_path,
    read_marker,
    write_marker,
)


def test_marker_path_layout(tmp_path: Path) -> None:
    """marker_path returns <state_dir>/instances/<instance_id>/.provisioned."""
    p = marker_path(tmp_path, "i-abc123")
    assert p == tmp_path / "instances" / "i-abc123" / ".provisioned"


def test_read_marker_returns_none_when_absent(tmp_path: Path) -> None:
    """Missing marker file yields None, never raises."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    assert read_marker(p) is None


def test_read_marker_returns_none_when_corrupt(tmp_path: Path) -> None:
    """Corrupt JSON yields None, never raises (self-healing on next provision)."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    p.parent.mkdir(parents=True)
    p.write_text("not json at all {{{")
    assert read_marker(p) is None


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    """write_marker then read_marker yields the exact dict written."""
    p = marker_path(tmp_path, "i-abc")
    write_marker(p, "i-abc", "key-hex-xyz", "comfyui", 1717200000.5)
    record = read_marker(p)
    assert record is not None
    assert record["instance_id"] == "i-abc"
    assert record["capability_key"] == "key-hex-xyz"
    assert record["engine"] == "comfyui"
    assert record["timestamp"] == 1717200000.5


def test_is_marker_current_staleness_rule(tmp_path: Path) -> None:
    """is_marker_current returns True iff the cached key matches current key."""
    marker = {
        "instance_id": "i-abc",
        "capability_key": "abc123",
        "engine": "comfyui",
        "timestamp": 1.0,
    }
    assert is_marker_current(marker, "abc123") is True
    assert is_marker_current(marker, "xyz789") is False


def test_read_marker_returns_none_when_keys_missing(tmp_path: Path) -> None:
    """Marker missing required keys yields None (treated as not-provisioned)."""
    p = tmp_path / "instances" / "i-x" / ".provisioned"
    p.parent.mkdir(parents=True)
    p.write_text('{"instance_id": "i-x"}')  # missing capability_key, engine, timestamp
    assert read_marker(p) is None
```

- [ ] **Step 2: Run tests, confirm RED**

```bash
pixi run pytest tests/core/test_provision_state.py -v
```

Expected: 6 FAIL (`ModuleNotFoundError: kinoforge.core.provision_state`).

- [ ] **Step 3: Implement the module**

Create `src/kinoforge/core/provision_state.py`:

```python
"""Per-instance provision marker helpers (Layer I UX A — compute path).

The marker file at ``<state_dir>/instances/<instance_id>/.provisioned`` records
that ``provisioner.provision()`` completed against a specific instance with a
specific ``capability_key``.  ``orchestrator.generate()`` reads the marker on
every compute-path generate to decide whether to re-run provision.

The marker is self-healing: corrupt, missing, or malformed files are treated
as "not provisioned" — never raise from the reader.  The next provision pass
overwrites with a fresh record.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_REQUIRED_KEYS = ("instance_id", "capability_key", "engine", "timestamp")


def marker_path(state_dir: Path, instance_id: str) -> Path:
    """Return the canonical marker path for an instance.

    Args:
        state_dir: Root of the kinoforge state directory (CLI --state-dir).
        instance_id: Provider-assigned instance ID.

    Returns:
        ``<state_dir>/instances/<instance_id>/.provisioned``.
    """
    return state_dir / "instances" / instance_id / ".provisioned"


def read_marker(path: Path) -> dict[str, Any] | None:
    """Read and parse a provision marker.

    Returns ``None`` on any failure (absent file, corrupt JSON, missing
    required keys) so the caller can treat it as "not provisioned" and
    re-run provision.

    Args:
        path: Marker path (see :func:`marker_path`).

    Returns:
        The parsed marker dict, or ``None`` if invalid for any reason.
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(k in data for k in _REQUIRED_KEYS):
        return None
    return data


def write_marker(
    path: Path,
    instance_id: str,
    capability_key: str,
    engine_name: str,
    timestamp: float,
) -> None:
    """Atomically write a provision marker.

    Creates parent directories as needed.  The write is atomic on POSIX
    (write to temp + rename) so a crashed write never leaves a half-formed
    marker that ``read_marker`` would treat as "not provisioned" — though
    that fallback would self-heal anyway.

    Args:
        path: Marker path (see :func:`marker_path`).
        instance_id: Provider-assigned instance ID.
        capability_key: Current ``cfg.capability_key().derive()`` hex.
        engine_name: ``engine.name`` for diagnostic record.
        timestamp: Unix timestamp (seconds, float).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "instance_id": instance_id,
        "capability_key": capability_key,
        "engine": engine_name,
        "timestamp": timestamp,
    }
    # Atomic rename pattern: write to temp file in same directory, then replace.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".provisioned.tmp.", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the temp file on any error.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def is_marker_current(marker: dict[str, Any], capability_key: str) -> bool:
    """Return True iff the marker's capability_key matches *capability_key*.

    Staleness rule: marker is stale (returns False) when the user edited the
    config (model set, precision, engine kind) so the derived key changed.
    Stale marker forces re-provision on next generate.

    Args:
        marker: A marker dict from :func:`read_marker`.
        capability_key: Current ``cfg.capability_key().derive()`` hex.

    Returns:
        True iff ``marker["capability_key"] == capability_key``.
    """
    return marker.get("capability_key") == capability_key
```

- [ ] **Step 4: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_provision_state.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/provision_state.py tests/core/test_provision_state.py
git add src/kinoforge/core/provision_state.py tests/core/test_provision_state.py
git commit -m "feat(core): provision_state marker helpers (Layer I Task 7)"
```

---

## Task 8: UX A hosted preflight in `orchestrator.generate()`

**Goal:** Inside `orchestrator.generate()`, call `engine.provision(None, cfg_dict)` before any backend work when `engine.requires_compute == False`. Fail fast with `AuthError` or `KinoforgeError("hosted endpoint unreachable: …")` instead of crashing mid-pipeline.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` — add Step 2.5 between engine resolution and profile resolve.
- Modify: `tests/core/test_orchestrator.py` — three new tests.

**Acceptance Criteria:**
- [ ] `generate()` invokes `engine.provision(None, cfg_dict)` exactly once before any `backend.submit` for hosted engines.
- [ ] When cred is missing, the raised `AuthError` propagates from `generate()` without any `backend.submit` call having been made.
- [ ] When health probe fails, `KinoforgeError` propagates without any `backend.submit` call having been made.
- [ ] Compute-path engines (`requires_compute=True`) are unaffected by this step (compute UX A lands in Task 9).

**Verify:** `pixi run pytest tests/core/test_orchestrator.py -v -k "hosted_preflight"` — 3 PASS.

**Steps:**

- [ ] **Step 1: Read the current `generate()` around Step 2**

```bash
grep -n "def generate\|requires_compute\|_cfg_dict\|resolved_engine" src/kinoforge/core/orchestrator.py | head -20
```

Identify the location of the engine-resolution block (around line 325).

- [ ] **Step 2: Write the failing tests**

Append to `tests/core/test_orchestrator.py`:

```python
def test_hosted_preflight_calls_provision_before_backend_submit() -> None:
    """generate() must call engine.provision(None, cfg_dict) exactly once before
    any backend.submit, for hosted engines (requires_compute=False).

    Bug catch: previous behavior bypassed engine.provision entirely from
    generate(), causing cred-missing failures to crash mid-flight inside
    backend.submit.
    """
    from kinoforge.core import orchestrator
    # ... build a fake hosted engine that records the call order of:
    #     provision_called_at: int | None
    #     submit_called_at: int | None
    # ... plus minimal fake backend / store / profile_provider scaffolding.
    # ... call orchestrator.generate(cfg, request, store=store, engine=fake_engine, ...)
    # ... assert fake_engine.provision_called_at < fake_engine.submit_called_at
    # ... assert fake_engine.provision_called_count == 1
    raise NotImplementedError("fill in with existing fakes from test_orchestrator.py")


def test_hosted_preflight_auth_error_blocks_backend_submit() -> None:
    """When engine.provision raises AuthError, no backend.submit happens."""
    raise NotImplementedError("fill in with existing fakes from test_orchestrator.py")


def test_hosted_preflight_health_error_blocks_backend_submit() -> None:
    """When engine.provision raises KinoforgeError (health probe failure),
    no backend.submit happens."""
    raise NotImplementedError("fill in with existing fakes from test_orchestrator.py")
```

**Filling in the fakes:** open `tests/core/test_orchestrator.py` and read the existing fake-engine / fake-backend / fake-store scaffolding (around the top of the file). Reuse those classes. The three tests differ only in what `engine.provision` does:
- Test 1: `provision` records call order; both `provision` and `submit` succeed.
- Test 2: `provision` raises `AuthError("foo")`.
- Test 3: `provision` raises `KinoforgeError("hosted endpoint unreachable: down")`.

Each test asserts: `assert fake_backend.submit_call_count == 0` after the `with pytest.raises(...):` block (for tests 2 + 3); and `assert provision_called_at < submit_called_at` (for test 1).

If the existing test scaffolding requires significant adaptation, refactor minimally — but stick to test-level changes; do not change production scaffolding.

- [ ] **Step 3: Run tests, confirm RED**

```bash
pixi run pytest tests/core/test_orchestrator.py -v -k "hosted_preflight"
```

Expected: 3 FAIL (provision never called → call order assertion fails for test 1; cred error never raised before submit → tests 2 + 3 see submit_call_count == 1).

- [ ] **Step 4: Add the preflight step to `orchestrator.generate()`**

Open `src/kinoforge/core/orchestrator.py`. Locate the block (around line 325-330):

```python
resolved_engine = _resolve_engine(cfg, engine)
resolved_provider: ComputeProvider | None = None
if resolved_engine.requires_compute:
    resolved_provider = _resolve_provider(cfg, provider)
```

Immediately AFTER that block (still before the profile-cache resolve at line ~339), add:

```python
# ------------------------------------------------------------------
# Step 2.5 — UX A hosted preflight (Layer I)
#
# For hosted engines (requires_compute=False), run engine.provision()
# before any backend work so cred-missing / health-failure errors fail
# fast with a clear message instead of crashing mid-pipeline inside
# backend.submit.
# ------------------------------------------------------------------
if not resolved_engine.requires_compute:
    resolved_engine.provision(None, cfg_dict)
```

`cfg_dict` is already computed at line 320 (`cfg_dict = _cfg_dict(cfg)`). No import changes needed.

- [ ] **Step 5: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_orchestrator.py -v -k "hosted_preflight"
```

Expected: 3 PASS.

- [ ] **Step 6: Run full orchestrator test suite**

```bash
pixi run pytest tests/core/test_orchestrator.py -v
```

Expected: all green. Existing tests using compute-path fakes are unaffected; existing tests using hosted-path fakes may now require their fake engine to have a working `provision` method — if any existing test breaks because of this, the fake just needs `def provision(self, instance, cfg): pass` added.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git commit -m "feat(orchestrator): UX A hosted preflight calls engine.provision before backend (Layer I Task 8)"
```

---

## Task 9: UX A compute preflight — marker + `acquire_lock`

**Goal:** For compute-path engines, run `provisioner.provision(...)` exactly once per instance per capability_key. Skip on subsequent generates against the same instance + same key. Stale key (config edited) forces re-provision. RMW safety via `store.acquire_lock("provision:<instance_id>", ...)`.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` — extract a helper `_provision_compute_once(...)`; call it after `create_instance` in both the discover branch (Step 4.5) and the post-cache-hit branch (Step 7.5).
- Create: `tests/core/test_orchestrator_compute.py` — four new tests.

**Acceptance Criteria:**
- [ ] First generate against a fresh instance writes the marker and calls `provisioner.provision` exactly once.
- [ ] Second generate against the same instance + same capability_key reads the marker and DOES NOT call `provisioner.provision`.
- [ ] Second generate against the same instance with a *different* capability_key (config edited) calls `provisioner.provision` again and rewrites the marker.
- [ ] Two concurrent `generate()` calls against the same instance + key serialize via `store.acquire_lock`; `provisioner.provision` is called exactly once across both.

**Verify:** `pixi run pytest tests/core/test_orchestrator_compute.py -v` — 4 PASS.

**Steps:**

- [ ] **Step 1: Read the current discover-branch + post-cache-hit-branch layout**

```bash
grep -n "create_instance\|backend = resolved_engine.backend\|profile_provider.discover" src/kinoforge/core/orchestrator.py
```

Note line numbers. Discover branch builds the backend around line 383; post-cache-hit branch around line 452.

- [ ] **Step 2: Write the failing tests**

Create `tests/core/test_orchestrator_compute.py`:

```python
"""UX A compute-path tests for orchestrator.generate (Layer I Task 9)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.provision_state import marker_path


# ---------------------------------------------------------------------------
# Test scaffolding — reuse compute-path fakes from test_orchestrator.py.
#
# The compute fakes used in test_orchestrator.py expose:
#   - FakeProvider with create_instance / get_instance / find_offers
#   - FakeEngine with requires_compute=True, provision counter, backend factory
#   - FakeStore implementing ArtifactStore + acquire_lock (Layer H)
#
# Import them directly here to avoid duplication.
# ---------------------------------------------------------------------------

from tests.core.test_orchestrator import (  # type: ignore[attr-defined]
    _make_compute_cfg,         # builds a Config with engine.kind="<compute fake>"
    _make_request,             # builds a GenerationRequest
    FakeComputeEngine,         # has provision counter
    FakeComputeProvider,       # mints fresh instances
    FakeStoreWithLock,         # ArtifactStore + acquire_lock (Layer H stub)
    FakeProfileProvider,       # always returns ProfileNotCached then a profile
)


# NOTE: if the helpers above do not yet exist in test_orchestrator.py, either
# build them inline here OR add them to test_orchestrator.py first.  Reuse is
# preferred to keep compute-fake behaviour consistent across tests.


def test_first_generate_writes_marker_and_calls_provisioner(
    tmp_path: Path,
) -> None:
    """First generate against a fresh instance writes the marker and provisions."""
    cfg = _make_compute_cfg()
    engine = FakeComputeEngine()
    provider = FakeComputeProvider()
    store = FakeStoreWithLock(tmp_path)
    profile_provider = FakeProfileProvider()

    orchestrator.generate(
        cfg,
        _make_request(),
        store=store,
        provider=provider,
        engine=engine,
        profile_provider=profile_provider,
        run_id="r1",
        state_dir=tmp_path,  # NEW arg — see Task 9 Step 3
    )

    instance_id = provider.created[-1].id
    marker = tmp_path / "instances" / instance_id / ".provisioned"
    assert marker.exists()
    record = json.loads(marker.read_text())
    assert record["instance_id"] == instance_id
    assert record["capability_key"] == cfg.capability_key().derive()
    assert engine.provision_call_count == 1


def test_second_generate_same_key_skips_provision(tmp_path: Path) -> None:
    """Second generate against the same instance + same key reads the marker
    and DOES NOT call provisioner.provision again."""
    cfg = _make_compute_cfg()
    engine = FakeComputeEngine()
    provider = FakeComputeProvider(reuse_instance=True)  # returns same id twice
    store = FakeStoreWithLock(tmp_path)
    profile_provider = FakeProfileProvider()

    orchestrator.generate(cfg, _make_request(), store=store, provider=provider,
                          engine=engine, profile_provider=profile_provider,
                          run_id="r1", state_dir=tmp_path)
    orchestrator.generate(cfg, _make_request(), store=store, provider=provider,
                          engine=engine, profile_provider=profile_provider,
                          run_id="r2", state_dir=tmp_path)

    assert engine.provision_call_count == 1


def test_second_generate_stale_key_reprovisions(tmp_path: Path) -> None:
    """Stale marker (different capability_key) forces re-provision."""
    cfg_a = _make_compute_cfg(precision="fp16")
    cfg_b = _make_compute_cfg(precision="fp32")  # different precision -> different key
    assert cfg_a.capability_key().derive() != cfg_b.capability_key().derive()

    engine = FakeComputeEngine()
    provider = FakeComputeProvider(reuse_instance=True)
    store = FakeStoreWithLock(tmp_path)
    profile_provider = FakeProfileProvider()

    orchestrator.generate(cfg_a, _make_request(), store=store, provider=provider,
                          engine=engine, profile_provider=profile_provider,
                          run_id="r1", state_dir=tmp_path)
    orchestrator.generate(cfg_b, _make_request(), store=store, provider=provider,
                          engine=engine, profile_provider=profile_provider,
                          run_id="r2", state_dir=tmp_path)

    assert engine.provision_call_count == 2


def test_concurrent_generates_serialize_via_lock(tmp_path: Path) -> None:
    """Two concurrent generates against the same instance + key call provision
    exactly once across both, thanks to store.acquire_lock."""
    cfg = _make_compute_cfg()
    engine = FakeComputeEngine(provision_delay_s=0.1)  # makes the race observable
    provider = FakeComputeProvider(reuse_instance=True)
    store = FakeStoreWithLock(tmp_path)
    profile_provider = FakeProfileProvider()

    errors: list[BaseException] = []

    def _run(run_id: str) -> None:
        try:
            orchestrator.generate(cfg, _make_request(), store=store,
                                  provider=provider, engine=engine,
                                  profile_provider=profile_provider,
                                  run_id=run_id, state_dir=tmp_path)
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_run, args=("r1",))
    t2 = threading.Thread(target=_run, args=("r2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert errors == []
    assert engine.provision_call_count == 1
```

**Caveat:** if `_make_compute_cfg`, `FakeComputeEngine`, `FakeComputeProvider`, `FakeStoreWithLock`, `FakeProfileProvider` don't already exist in `tests/core/test_orchestrator.py`, build them as fixtures at the top of `tests/core/test_orchestrator_compute.py` — see the existing fakes in `tests/core/test_orchestrator.py` for prior-art shapes. Keep them minimal.

- [ ] **Step 3: Add `state_dir` argument to `orchestrator.generate()`**

`generate()` currently has no `state_dir` argument — the marker path needs one. Add the parameter:

```python
def generate(
    cfg: Config,
    request: GenerationRequest,
    *,
    store: ArtifactStore,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    run_id: str = "default",
    state_dir: Path = Path(".kinoforge"),   # NEW
) -> Artifact:
```

Update `cli.py:_cmd_generate` to pass `state_dir=state_dir` when it calls `generate(...)`. The CLI already has a `state_dir: Path` local — just forward it.

```bash
grep -n "generate(" src/kinoforge/cli.py
```

Locate the call site (around line 320) and add the `state_dir=state_dir` keyword.

- [ ] **Step 4: Run tests, confirm RED**

```bash
pixi run pytest tests/core/test_orchestrator_compute.py -v
```

Expected: 4 FAIL.

- [ ] **Step 5: Extract `_provision_compute_once` helper**

Add to `src/kinoforge/core/orchestrator.py` (above `generate()`):

```python
def _provision_compute_once(
    *,
    engine: GenerationEngine,
    cfg: Config,
    instance: Instance,
    creds: CredentialProvider | None,
    store: ArtifactStore,
    state_dir: Path,
    capability_key_hex: str,
) -> None:
    """Run provisioner.provision exactly once per (instance, capability_key).

    Layer H ``store.acquire_lock`` makes this safe under cross-process
    concurrent generates against the same instance.

    Args:
        engine: Resolved generation engine.
        cfg: The loaded kinoforge configuration.
        instance: The ready compute instance.
        creds: Optional credential provider (defaults to EnvCredentialProvider).
        store: Artifact store, used for cross-process lock acquisition.
        state_dir: Root of the kinoforge state directory.
        capability_key_hex: Current ``cfg.capability_key().derive()`` hex.
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.provision_state import (
        is_marker_current,
        marker_path,
        read_marker,
        write_marker,
    )
    from kinoforge.core.provisioner import provision as provisioner_provision
    import time

    effective_creds = creds if creds is not None else EnvCredentialProvider()
    marker = marker_path(state_dir, instance.id)

    with store.acquire_lock(f"provision:{instance.id}", ttl_s=300):
        record = read_marker(marker)
        if record is not None and is_marker_current(record, capability_key_hex):
            _log.debug(
                "provision marker current for instance %s key %s — skipping",
                instance.id,
                capability_key_hex[:12],
            )
            return
        _log.info(
            "running provisioner.provision for instance %s (engine=%s key=%s)",
            instance.id,
            engine.name,
            capability_key_hex[:12],
        )
        provisioner_provision(
            engine=engine,
            cfg=cfg,  # type: ignore[arg-type]
            instance=instance,
            creds=effective_creds,
            download_dir=state_dir / "weights",
        )
        write_marker(
            marker,
            instance.id,
            capability_key_hex,
            engine.name,
            time.time(),
        )
```

- [ ] **Step 6: Call the helper in both compute branches**

In `generate()`'s discover branch, after the `create_instance` loop and BEFORE `backend = resolved_engine.backend(instance, cfg_dict)`:

```python
instance = resolved_provider.create_instance(spec)
while instance.status != "ready":
    instance = resolved_provider.get_instance(instance.id)
# Layer I Task 9 — provision once per (instance, capability_key)
_provision_compute_once(
    engine=resolved_engine,
    cfg=cfg,
    instance=instance,
    creds=creds,
    store=store,
    state_dir=state_dir,
    capability_key_hex=key.derive(),
)
backend = resolved_engine.backend(instance, cfg_dict)
```

Apply the same insertion in the post-cache-hit branch (around line 452, after the second `create_instance` loop) — identical block.

- [ ] **Step 7: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_orchestrator_compute.py -v
```

Expected: 4 PASS.

- [ ] **Step 8: Run full orchestrator suite**

```bash
pixi run pytest tests/core/test_orchestrator.py tests/core/test_orchestrator_compute.py -v
```

Expected: all green. If any pre-existing compute-path test broke, it's because `generate()` now runs `_provision_compute_once`; either the test's fake `provisioner.provision` needs to be `pass`-able or its `FakeStoreWithLock` needs `acquire_lock` support. Make the minimal change to keep them green.

- [ ] **Step 9: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py src/kinoforge/cli.py tests/core/test_orchestrator_compute.py
git add src/kinoforge/core/orchestrator.py src/kinoforge/cli.py tests/core/test_orchestrator_compute.py
git commit -m "feat(orchestrator): UX A compute preflight with per-instance marker (Layer I Task 9)"
```

---

## Task 10: `FalEngineConfig` pydantic block

**Goal:** Add `FalEngineConfig` model + register `engine.fal` field + extend cross-field validator. Foundation for Task 11.

**Files:**
- Modify: `src/kinoforge/core/config.py` — add `FalEngineConfig` class; extend `EngineConfig`; update `KNOWN_ENGINES`; extend `Config._validate_cross_fields`.
- Modify: `tests/core/test_config.py` — validator tests.

**Acceptance Criteria:**
- [ ] `FalEngineConfig(endpoint="fal-ai/wan", url_path="video.url")` constructs with defaults (`queue_base="https://queue.fal.run"`, `api_key_env="FAL_KEY"`, `asset_paths={}`, `health_url=""`).
- [ ] `FalEngineConfig(endpoint="", url_path="x")` raises `ValidationError` (endpoint non-empty).
- [ ] `FalEngineConfig(endpoint="x", url_path="")` raises `ValidationError` (url_path non-empty).
- [ ] `FalEngineConfig(endpoint="x", url_path="x", queue_base="not-a-url")` raises `ValidationError`.
- [ ] `FalEngineConfig(endpoint="x", url_path="x", api_key_env="")` raises `ValidationError`.
- [ ] Loading a YAML with `engine.kind: fal` but no `engine.fal` block raises `ValidationError`.
- [ ] `"fal"` is in `KNOWN_ENGINES`.

**Verify:** `pixi run pytest tests/core/test_config.py -v -k "fal"` — 6 PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_config.py`:

```python
def test_fal_engine_config_defaults() -> None:
    """FalEngineConfig fills sensible defaults for queue_base, api_key_env, asset_paths."""
    from kinoforge.core.config import FalEngineConfig
    cfg = FalEngineConfig(endpoint="fal-ai/wan/v2.2/t2v", url_path="video.url")
    assert cfg.queue_base == "https://queue.fal.run"
    assert cfg.api_key_env == "FAL_KEY"
    assert cfg.asset_paths == {}
    assert cfg.health_url == ""


def test_fal_engine_config_rejects_empty_endpoint() -> None:
    from pydantic import ValidationError
    from kinoforge.core.config import FalEngineConfig
    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="", url_path="video.url")
    assert "endpoint" in str(exc.value)


def test_fal_engine_config_rejects_empty_url_path() -> None:
    from pydantic import ValidationError
    from kinoforge.core.config import FalEngineConfig
    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="fal-ai/wan", url_path="")
    assert "url_path" in str(exc.value)


def test_fal_engine_config_rejects_relative_queue_base() -> None:
    from pydantic import ValidationError
    from kinoforge.core.config import FalEngineConfig
    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="x", url_path="y", queue_base="not-a-url")
    assert "queue_base" in str(exc.value)


def test_fal_engine_config_rejects_empty_api_key_env() -> None:
    from pydantic import ValidationError
    from kinoforge.core.config import FalEngineConfig
    with pytest.raises(ValidationError) as exc:
        FalEngineConfig(endpoint="x", url_path="y", api_key_env="")
    assert "api_key_env" in str(exc.value)


def test_fal_kind_without_fal_block_raises() -> None:
    """engine.kind == 'fal' but no engine.fal block must fail at load."""
    from pydantic import ValidationError
    from kinoforge.core.config import load_config

    yaml_text = """
engine:
  kind: fal
  precision: ""
models:
  - ref: "hf:org/m:f"
    kind: base
    target: checkpoints
lifecycle:
  budget: 1.0
"""
    with pytest.raises(ValidationError):
        load_config(yaml_text)
```

- [ ] **Step 2: Run tests, confirm RED**

```bash
pixi run pytest tests/core/test_config.py -v -k "fal"
```

Expected: 6 FAIL (`ImportError: cannot import name 'FalEngineConfig'`).

- [ ] **Step 3: Add `FalEngineConfig` to `src/kinoforge/core/config.py`**

After `DiffusersEngineConfig` (line 174), insert:

```python
class FalEngineConfig(BaseModel):
    """fal.ai engine parameters (queue API).

    Attributes:
        endpoint: fal model path, e.g. ``"fal-ai/wan/v2.2/t2v"``.  Prepended
            by ``queue_base`` at submit time.
        queue_base: Base URL of the fal queue API.  Defaults to the public
            ``https://queue.fal.run``; rarely overridden.
        api_key_env: Env-var name carrying the FAL_KEY.  Defaults to ``"FAL_KEY"``.
        url_path: Dot-path walked over the response body by
            :meth:`FalBackend.result` to extract the artifact URL.
        asset_paths: Mapping from conditioning-asset role to a dot-path in
            the request body where the asset's URL is injected at submit time.
        health_url: Optional URL pinged by :meth:`FalEngine.provision`; empty
            disables the health probe (fal has no documented health endpoint).
    """

    endpoint: str
    queue_base: str = "https://queue.fal.run"
    api_key_env: str = "FAL_KEY"
    url_path: str
    asset_paths: dict[str, str] = Field(default_factory=dict)
    health_url: str = ""

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("engine.fal.endpoint must be a non-empty model path")
        return v

    @field_validator("url_path")
    @classmethod
    def _check_url_path_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "engine.fal.url_path must be a non-empty dot-path "
                "(e.g. 'video.url')"
            )
        return v

    @field_validator("queue_base")
    @classmethod
    def _check_queue_base_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"engine.fal.queue_base must be an absolute http(s):// URL, got {v!r}"
            )
        return v

    @field_validator("api_key_env")
    @classmethod
    def _check_api_key_env_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "engine.fal.api_key_env must be a non-empty env-var name"
            )
        return v
```

- [ ] **Step 4: Extend `EngineConfig`**

In `EngineConfig` (line 177-193), add:

```python
    fal: FalEngineConfig | None = None
```

Update docstring `Attributes:` to mention `fal`.

- [ ] **Step 5: Extend `KNOWN_ENGINES`**

Line 68:

```python
KNOWN_ENGINES = {"comfyui", "diffusers", "hosted", "fake", "fal"}
```

- [ ] **Step 6: Extend `Config._validate_cross_fields`**

Locate the `_validate_cross_fields` model_validator (line 317). After the existing `KNOWN_ENGINES` check, add:

```python
        # engine.kind == "fal" requires the engine.fal block.
        if self.engine.kind == "fal" and self.engine.fal is None:
            raise ValueError(
                "engine.kind == 'fal' requires the engine.fal block"
            )
        # engine.kind == "fal" must not have a compute block (hosted-like).
        if self.engine.kind == "fal" and self.compute is not None:
            raise ValueError(
                "compute: must not be set when engine.kind == 'fal'"
            )
```

- [ ] **Step 7: Run tests to confirm GREEN**

```bash
pixi run pytest tests/core/test_config.py -v -k "fal"
```

Expected: 6 PASS.

- [ ] **Step 8: Run full config + examples sweep**

```bash
pixi run pytest tests/core/test_config.py tests/test_examples.py -v
```

Expected: all green.

- [ ] **Step 9: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "feat(config): FalEngineConfig pydantic block + KNOWN_ENGINES update (Layer I Task 10)"
```

---

## Task 11: `FalEngine` + `FalBackend` + wire helpers + tests

**Goal:** Ship the fal.ai sibling engine. Pure-helper wire layer (status URL builder, result URL extraction, status-string interpretation) lives in `wire.py`; the engine + backend wire injected HTTP seams to those helpers.

**Files:**
- Create: `src/kinoforge/engines/fal/__init__.py`
- Create: `src/kinoforge/engines/fal/wire.py`
- Create: `tests/engines/test_fal.py`
- Create: `tests/engines/test_fal_wire.py`

**Acceptance Criteria:**
- [ ] `FalEngine.provision(None, cfg)`:
  - Missing cred → `AuthError("engine.fal.api_key_env=<name> is not set in env")`.
  - Health probe failure when `health_url` non-empty → `KinoforgeError("fal endpoint unreachable: …")`.
  - Empty `health_url` → no probe attempted.
- [ ] `FalEngine.backend(None, cfg)` returns a `FalBackend` wired with the resolved `api_key`, `endpoint`, `queue_base`, `url_path`, `asset_paths`, and injected seams.
- [ ] `FalBackend.submit(job)`:
  - POSTs to `f"{queue_base}/{endpoint}"` with header `Authorization: Key <api_key>` and `Content-Type: application/json`.
  - Body equals `job.spec` with `asset_paths` injection applied (via `set_by_dot_path` + `find_asset`).
  - Returns `response["request_id"]`.
  - Records `status_url`, `response_url` in an internal `_jobs` map keyed by `request_id`.
- [ ] `FalBackend.result(request_id)`:
  - Polls `status_url` (from `_jobs` map) until status is `COMPLETED`. Sleeps between polls via injected `sleep`.
  - Status `IN_QUEUE` / `IN_PROGRESS` → continue.
  - Status `FAILED` → `KinoforgeError("fal job <id> failed: <logs>")`.
  - Unknown status → `KinoforgeError("fal job <id> unknown status: <status>")`.
  - More than `max_poll` iterations → `TimeoutError`.
  - On `COMPLETED` → GET `response_url`, extract URL via `_walk_dot_path(data, url_path)`, return `Artifact(filename=basename(url), url=url, meta={"request_id": request_id})`.
- [ ] `FalEngine` self-registers under `"fal"` on import.
- [ ] `wire.py` pure helpers tested in isolation (8 tests).
- [ ] `FalEngine` + `FalBackend` tested with injected HTTP spies (12 tests).

**Verify:** `pixi run pytest tests/engines/test_fal.py tests/engines/test_fal_wire.py -v` — 20 PASS.

**Steps:**

- [ ] **Step 1: Write `tests/engines/test_fal_wire.py` (red-first)**

```python
"""Pure HTTP-shape helpers for FalBackend (Layer I Task 11)."""

from __future__ import annotations

import pytest

from kinoforge.engines.fal.wire import (
    build_status_url,
    build_response_url,
    extract_result_url,
    interpret_status,
    FalStatus,
)


def test_build_status_url_uses_response_when_present() -> None:
    """build_status_url prefers the server-supplied status_url over construction."""
    url = build_status_url(
        submit_response={"request_id": "r1", "status_url": "https://q.fal/x/status"},
        queue_base="https://q.fal", endpoint="endpoint", request_id="r1",
    )
    assert url == "https://q.fal/x/status"


def test_build_status_url_falls_back_to_construction() -> None:
    """When submit_response omits status_url, build one from queue_base + endpoint."""
    url = build_status_url(
        submit_response={"request_id": "r1"},
        queue_base="https://queue.fal.run", endpoint="fal-ai/wan", request_id="r1",
    )
    assert url == "https://queue.fal.run/fal-ai/wan/requests/r1/status"


def test_build_response_url_uses_response_when_present() -> None:
    url = build_response_url(
        submit_response={"request_id": "r1", "response_url": "https://q.fal/x"},
        queue_base="https://q.fal", endpoint="endpoint", request_id="r1",
    )
    assert url == "https://q.fal/x"


def test_build_response_url_falls_back_to_construction() -> None:
    url = build_response_url(
        submit_response={"request_id": "r1"},
        queue_base="https://queue.fal.run", endpoint="fal-ai/wan", request_id="r1",
    )
    assert url == "https://queue.fal.run/fal-ai/wan/requests/r1"


def test_extract_result_url_walks_dot_path() -> None:
    """Extract a nested URL via dot-path walk."""
    data = {"video": {"url": "https://media.fal/v.mp4", "size": 1234}}
    assert extract_result_url(data, "video.url") == "https://media.fal/v.mp4"


def test_extract_result_url_raises_on_missing_path() -> None:
    from kinoforge.core.errors import KinoforgeError
    data = {"video": {}}
    with pytest.raises(KinoforgeError) as exc:
        extract_result_url(data, "video.url")
    assert "url_path" in str(exc.value)


def test_interpret_status_recognizes_canonical_states() -> None:
    """COMPLETED, IN_QUEUE, IN_PROGRESS, FAILED all recognized."""
    assert interpret_status("COMPLETED") is FalStatus.COMPLETED
    assert interpret_status("IN_QUEUE") is FalStatus.PENDING
    assert interpret_status("IN_PROGRESS") is FalStatus.PENDING
    assert interpret_status("FAILED") is FalStatus.FAILED


def test_interpret_status_unknown_returns_unknown_marker() -> None:
    """An unknown status string returns FalStatus.UNKNOWN, not an exception."""
    assert interpret_status("SOMETHING_ELSE") is FalStatus.UNKNOWN
```

- [ ] **Step 2: Run wire tests, confirm RED**

```bash
pixi run pytest tests/engines/test_fal_wire.py -v
```

Expected: 8 FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `src/kinoforge/engines/fal/wire.py`**

```python
"""Pure HTTP-shape helpers for the fal.ai queue adapter.

Kept I/O-free so the wire shape is testable in isolation without HTTP spies.
"""

from __future__ import annotations

import enum
from typing import Any

from kinoforge.core.errors import KinoforgeError


class FalStatus(enum.Enum):
    """Canonical fal queue status classes.

    Maps the fal-side strings ("IN_QUEUE", "IN_PROGRESS", "COMPLETED", "FAILED")
    to a 4-way classification the poll loop branches on.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


_PENDING_STATES = frozenset({"IN_QUEUE", "IN_PROGRESS"})
_COMPLETED_STATES = frozenset({"COMPLETED"})
_FAILED_STATES = frozenset({"FAILED"})


def interpret_status(status_str: str) -> FalStatus:
    """Classify a fal status string into one of the 4 :class:`FalStatus` members."""
    if status_str in _COMPLETED_STATES:
        return FalStatus.COMPLETED
    if status_str in _PENDING_STATES:
        return FalStatus.PENDING
    if status_str in _FAILED_STATES:
        return FalStatus.FAILED
    return FalStatus.UNKNOWN


def build_status_url(
    *,
    submit_response: dict[str, Any],
    queue_base: str,
    endpoint: str,
    request_id: str,
) -> str:
    """Return the URL to poll for status.

    Prefers ``submit_response["status_url"]`` when present (the server's
    canonical URL); falls back to the constructed URL otherwise.
    """
    server_url = submit_response.get("status_url")
    if isinstance(server_url, str) and server_url:
        return server_url
    return f"{queue_base.rstrip('/')}/{endpoint}/requests/{request_id}/status"


def build_response_url(
    *,
    submit_response: dict[str, Any],
    queue_base: str,
    endpoint: str,
    request_id: str,
) -> str:
    """Return the URL to GET for the final result."""
    server_url = submit_response.get("response_url")
    if isinstance(server_url, str) and server_url:
        return server_url
    return f"{queue_base.rstrip('/')}/{endpoint}/requests/{request_id}"


def extract_result_url(data: dict[str, Any], url_path: str) -> str:
    """Walk a dot-path through *data* and return the URL string at that path.

    Raises :class:`KinoforgeError` when the path is missing or doesn't
    terminate at a string.
    """
    current: Any = data
    for part in url_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KinoforgeError(
                f"fal response missing url_path {url_path!r} at component {part!r}"
            )
        current = current[part]
    if not isinstance(current, str):
        raise KinoforgeError(
            f"fal response url_path {url_path!r} did not terminate at a string "
            f"(got {type(current).__name__})"
        )
    return current
```

- [ ] **Step 4: Run wire tests to confirm GREEN**

```bash
pixi run pytest tests/engines/test_fal_wire.py -v
```

Expected: 8 PASS.

- [ ] **Step 5: Write `tests/engines/test_fal.py` (red-first)**

```python
"""FalEngine + FalBackend unit tests with injected HTTP spies (Layer I Task 11)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    CredentialProvider,
    GenerationJob,
    Segment,
)


class _StaticCreds(CredentialProvider):
    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._m = mapping

    def get(self, key: str) -> str | None:
        return self._m.get(key)


def _make_job(spec: dict[str, Any] | None = None) -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt="a cat", params={}, assets=[])],
        params={}, spec=spec or {"prompt": "a cat"},
    )


# ---------------------------------------------------------------------------
# Engine provision
# ---------------------------------------------------------------------------


def test_provision_missing_cred_raises_auth_error() -> None:
    from kinoforge.engines.fal import FalEngine
    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": None}))
    cfg = {"engine": {"fal": {"api_key_env": "FAL_KEY", "health_url": ""}}}
    with pytest.raises(AuthError) as exc:
        eng.provision(None, cfg)
    assert "FAL_KEY" in str(exc.value)


def test_provision_skips_health_when_empty() -> None:
    """Empty health_url means no probe — provision succeeds."""
    from kinoforge.engines.fal import FalEngine
    pings: list[str] = []

    def _spy_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        pings.append(url)
        return {}

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}), http_get=_spy_get)
    eng.provision(None, {"engine": {"fal": {"api_key_env": "FAL_KEY", "health_url": ""}}})
    assert pings == []


def test_provision_pings_health_when_set() -> None:
    from kinoforge.engines.fal import FalEngine
    pings: list[str] = []

    def _spy_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        pings.append(url)
        return {}

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}), http_get=_spy_get)
    eng.provision(None, {"engine": {"fal": {"api_key_env": "FAL_KEY", "health_url": "https://q.fal/health"}}})
    assert pings == ["https://q.fal/health"]


def test_provision_health_failure_raises() -> None:
    from kinoforge.engines.fal import FalEngine

    def _bad_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        raise OSError("connection refused")

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}), http_get=_bad_get)
    with pytest.raises(KinoforgeError) as exc:
        eng.provision(None, {"engine": {"fal": {"api_key_env": "FAL_KEY", "health_url": "https://q.fal/health"}}})
    assert "fal endpoint unreachable" in str(exc.value)


def test_provision_rejects_non_none_instance() -> None:
    from kinoforge.engines.fal import FalEngine
    from kinoforge.core.interfaces import Instance
    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}))
    with pytest.raises(KinoforgeError):
        eng.provision(
            Instance(id="i-1", provider="x", status="ready", created_at=0.0),
            {"engine": {"fal": {"api_key_env": "FAL_KEY"}}},
        )


# ---------------------------------------------------------------------------
# Backend submit + result
# ---------------------------------------------------------------------------


def _make_backend(
    *,
    submit_response: dict[str, Any] | None = None,
    status_responses: list[dict[str, Any]] | None = None,
    result_response: dict[str, Any] | None = None,
    asset_paths: dict[str, str] | None = None,
) -> Any:
    """Build a FalBackend with HTTP spies replaying the provided sequence."""
    from kinoforge.engines.fal import FalBackend
    from kinoforge.core.profiles import ModelProfile

    sr = submit_response or {"request_id": "r1"}
    statuses = list(status_responses or [{"status": "COMPLETED"}])
    rr = result_response or {"video": {"url": "https://media.fal/x.mp4"}}

    posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    gets: list[tuple[str, dict[str, str]]] = []

    def _spy_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        posts.append((url, body, headers))
        return sr

    def _spy_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        gets.append((url, headers or {}))
        # Status URLs return queued/in_progress/completed; non-status URL returns result.
        if "/status" in url or "status" in url.rsplit("/", 1)[-1]:
            return statuses.pop(0) if statuses else {"status": "COMPLETED"}
        return rr

    profile = ModelProfile(
        max_frames=16, fps=24, max_resolution=(512, 512),
        supported_modes=("t2v",), required_spec_keys=("prompt",),
    )

    backend = FalBackend(
        endpoint="fal-ai/wan/v2.2/t2v",
        queue_base="https://queue.fal.run",
        api_key="abc",
        url_path="video.url",
        asset_paths=asset_paths or {},
        profile=profile,
        http_post=_spy_post,
        http_get=_spy_get,
        sleep=lambda _s: None,
        max_poll=50,
    )
    backend._spy_posts = posts  # type: ignore[attr-defined]
    backend._spy_gets = gets  # type: ignore[attr-defined]
    return backend


def test_submit_posts_to_queue_base_endpoint_with_auth() -> None:
    backend = _make_backend()
    request_id = backend.submit(_make_job())
    assert request_id == "r1"
    url, body, headers = backend._spy_posts[0]
    assert url == "https://queue.fal.run/fal-ai/wan/v2.2/t2v"
    assert headers["Authorization"] == "Key abc"
    assert headers["Content-Type"] == "application/json"
    assert body == {"prompt": "a cat"}


def test_submit_injects_asset_urls_at_configured_paths() -> None:
    from kinoforge.core.interfaces import Asset, AssetRef
    backend = _make_backend(asset_paths={"init_image": "image_url"})
    asset = Asset(
        role="init_image", kind="image",
        ref=AssetRef(scheme="https", uri="https://i.example/x.png"),
    )
    job = GenerationJob(
        segments=[Segment(prompt="x", params={}, assets=[asset])],
        params={}, spec={"prompt": "x"},
    )
    backend.submit(job)
    _, body, _ = backend._spy_posts[0]
    assert body["image_url"] == "https://i.example/x.png"


def test_result_polls_until_completed_then_fetches_url() -> None:
    backend = _make_backend(
        status_responses=[{"status": "IN_QUEUE"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED"}],
        result_response={"video": {"url": "https://media.fal/v.mp4"}},
    )
    backend.submit(_make_job())
    art = backend.result("r1")
    assert isinstance(art, Artifact)
    assert art.url == "https://media.fal/v.mp4"
    assert art.meta["request_id"] == "r1"
    # Three status polls + one result GET = 4 GET calls
    status_calls = [u for u, _ in backend._spy_gets if "/status" in u]
    assert len(status_calls) == 3


def test_result_raises_on_failed_status() -> None:
    backend = _make_backend(status_responses=[{"status": "FAILED", "logs": [{"message": "boom"}]}])
    backend.submit(_make_job())
    with pytest.raises(KinoforgeError) as exc:
        backend.result("r1")
    assert "failed" in str(exc.value).lower()


def test_result_raises_on_unknown_status() -> None:
    backend = _make_backend(status_responses=[{"status": "EXPLODED"}])
    backend.submit(_make_job())
    with pytest.raises(KinoforgeError) as exc:
        backend.result("r1")
    assert "unknown status" in str(exc.value).lower()


def test_result_raises_timeout_when_max_poll_exceeded() -> None:
    backend = _make_backend(status_responses=[{"status": "IN_PROGRESS"}] * 100)
    backend.submit(_make_job())
    with pytest.raises(TimeoutError):
        backend.result("r1")


def test_submit_uses_server_supplied_status_url_for_polling() -> None:
    """If the submit response includes status_url, polling uses it verbatim."""
    backend = _make_backend(
        submit_response={
            "request_id": "r1",
            "status_url": "https://custom.fal/path/status",
            "response_url": "https://custom.fal/path/result",
        },
        status_responses=[{"status": "COMPLETED"}],
        result_response={"video": {"url": "https://media.fal/v.mp4"}},
    )
    backend.submit(_make_job())
    backend.result("r1")
    status_urls = [u for u, _ in backend._spy_gets if "/status" in u]
    assert "https://custom.fal/path/status" in status_urls


def test_engine_self_registers_under_fal() -> None:
    """Importing engines.fal must register the engine factory under 'fal'."""
    import kinoforge.engines.fal  # noqa: F401  (import side effect)
    from kinoforge.core import registry
    factory = registry.get_engine("fal")
    eng = factory()
    assert eng.name == "fal"


def test_backend_endpoints_returns_full_queue_url() -> None:
    backend = _make_backend()
    eps = backend.endpoints()
    assert eps == {"queue": "https://queue.fal.run/fal-ai/wan/v2.2/t2v"}


def test_validate_spec_requires_prompt() -> None:
    from kinoforge.engines.fal import FalEngine
    eng = FalEngine()
    job = GenerationJob(segments=[], params={}, spec={})
    with pytest.raises(KinoforgeError):
        eng.validate_spec(job)
```

- [ ] **Step 6: Run engine tests, confirm RED**

```bash
pixi run pytest tests/engines/test_fal.py -v
```

Expected: 12 FAIL (`ModuleNotFoundError: kinoforge.engines.fal`).

- [ ] **Step 7: Implement `src/kinoforge/engines/fal/__init__.py`**

```python
"""fal.ai sibling engine (queue API).

A standalone :class:`~kinoforge.core.interfaces.GenerationEngine` targeting the
fal.ai queue API.  Composes shared helpers (``set_by_dot_path``, ``find_asset``,
``ffmpeg_last_frame``) from the core library; does NOT inherit from
:class:`~kinoforge.engines.hosted.HostedAPIEngine` (their wire shapes differ).

Self-registers under ``"fal"`` on import.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from os.path import basename
from typing import Any, Callable
from urllib.parse import urlparse

from kinoforge.core import registry
from kinoforge.core.assets import find_asset, set_by_dot_path
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.engines.fal.wire import (
    FalStatus,
    build_response_url,
    build_status_url,
    extract_result_url,
    interpret_status,
)


_MAX_POLL_DEFAULT = 600


def _urllib_post_json(
    url: str, body: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — caller controls URL
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _urllib_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310
        url, method="GET", headers=headers or {},
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


class FalBackend(GenerationBackend):
    """Backend wired to a fal.ai queue endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        queue_base: str,
        api_key: str,
        url_path: str,
        asset_paths: dict[str, str],
        profile: ModelProfile,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
        http_get: Callable[[str, dict[str, str] | None], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_poll: int = _MAX_POLL_DEFAULT,
    ) -> None:
        self._endpoint = endpoint
        self._queue_base = queue_base
        self._api_key = api_key
        self._url_path = url_path
        self._asset_paths = dict(asset_paths)
        self._profile = profile
        self._http_post = http_post or _urllib_post_json
        self._http_get = http_get or _urllib_get_json
        import time as _time
        self._sleep = sleep or _time.sleep
        self._max_poll = max_poll
        # request_id -> {"status_url": ..., "response_url": ...}
        self._jobs: dict[str, dict[str, str]] = {}

    @property
    def profile(self) -> ModelProfile:
        return self._profile

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._api_key}"}

    def submit(self, job: GenerationJob) -> str:
        body = dict(job.spec)
        for role, dot_path in self._asset_paths.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            set_by_dot_path(body, dot_path, asset.ref.uri)
        full_url = f"{self._queue_base.rstrip('/')}/{self._endpoint}"
        headers = {
            "Content-Type": "application/json",
            **self._auth_headers(),
        }
        response = self._http_post(full_url, body, headers)
        request_id = str(response["request_id"])
        status_url = build_status_url(
            submit_response=response, queue_base=self._queue_base,
            endpoint=self._endpoint, request_id=request_id,
        )
        response_url = build_response_url(
            submit_response=response, queue_base=self._queue_base,
            endpoint=self._endpoint, request_id=request_id,
        )
        self._jobs[request_id] = {
            "status_url": status_url,
            "response_url": response_url,
        }
        return request_id

    def result(self, job_id: str) -> Artifact:
        urls = self._jobs.get(job_id)
        if urls is None:
            raise KinoforgeError(
                f"fal job {job_id!r} not found — was submit() called?"
            )
        status_url = urls["status_url"]
        response_url = urls["response_url"]

        for _ in range(self._max_poll):
            data = self._http_get(status_url, self._auth_headers())
            status_str = str(data.get("status", ""))
            cls = interpret_status(status_str)
            if cls is FalStatus.COMPLETED:
                result_data = self._http_get(response_url, self._auth_headers())
                url = extract_result_url(result_data, self._url_path)
                filename = basename(urlparse(url).path) or url
                return Artifact(
                    filename=filename,
                    url=url,
                    meta={"request_id": job_id},
                )
            if cls is FalStatus.FAILED:
                raise KinoforgeError(
                    f"fal job {job_id!r} failed: {data.get('logs', [])}"
                )
            if cls is FalStatus.UNKNOWN:
                raise KinoforgeError(
                    f"fal job {job_id!r} unknown status: {status_str!r}"
                )
            # PENDING: sleep + loop
            self._sleep(1.0)
        raise TimeoutError(
            f"fal job {job_id!r} did not complete within {self._max_poll} polls"
        )

    def endpoints(self) -> dict[str, str]:
        return {"queue": f"{self._queue_base.rstrip('/')}/{self._endpoint}"}


class FalEngine(GenerationEngine):
    """Engine targeting fal.ai's queue API."""

    name = "fal"
    requires_compute = False
    requires_local_weights = False

    def __init__(
        self,
        creds: CredentialProvider | None = None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
        http_get: Callable[[str, dict[str, str] | None], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._creds = creds or EnvCredentialProvider()
        self._http_post = http_post or _urllib_post_json
        self._http_get = http_get or _urllib_get_json
        import time as _time
        self._sleep = sleep or _time.sleep
        self.declared_flags_map: dict[tuple[str, str], dict[str, bool]] = {}

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        if instance is not None:
            raise KinoforgeError(
                "FalEngine.provision: instance must be None "
                "(fal engine has no compute to configure)"
            )
        fal_cfg: dict[str, Any] = cfg.get("engine", {}).get("fal", {})
        key_name = str(fal_cfg.get("api_key_env", "") or "FAL_KEY")
        cred = self._creds.get(key_name)
        if cred is None:
            raise AuthError(f"engine.fal.api_key_env={key_name!r} is not set in env")
        health_url = str(fal_cfg.get("health_url", "") or "")
        if health_url:
            try:
                self._http_get(health_url, None)
            except Exception as exc:
                raise KinoforgeError(
                    f"fal endpoint unreachable: {exc}"
                ) from exc

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> FalBackend:
        fal_cfg: dict[str, Any] = cfg.get("engine", {}).get("fal", {})
        endpoint = str(fal_cfg.get("endpoint", ""))
        queue_base = str(fal_cfg.get("queue_base", "https://queue.fal.run"))
        api_key_env = str(fal_cfg.get("api_key_env", "FAL_KEY"))
        url_path = str(fal_cfg.get("url_path", ""))
        asset_paths = dict(fal_cfg.get("asset_paths", {}) or {})
        api_key = self._creds.get(api_key_env) or ""

        profile = self.profile_for(
            CapabilityKey(
                base_model=endpoint, loras=(), engine="fal",
                precision=str(cfg.get("engine", {}).get("precision", "")),
            )
        )

        return FalBackend(
            endpoint=endpoint, queue_base=queue_base, api_key=api_key,
            url_path=url_path, asset_paths=asset_paths,
            profile=profile,
            http_post=self._http_post, http_get=self._http_get,
            sleep=self._sleep,
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        # Deferred to ModelProfileProvider — engine returns a stub.
        return ModelProfile(
            max_frames=120, fps=24, max_resolution=(1024, 1024),
            supported_modes=("t2v", "i2v"), required_spec_keys=("prompt",),
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        return dict(self.declared_flags_map.get((key.base_model, key.precision), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        if "prompt" not in job.spec or not job.spec["prompt"]:
            raise KinoforgeError(
                "fal engine requires a non-empty 'prompt' in job.spec"
            )


# ---------------------------------------------------------------------------
# Self-registration on import
# ---------------------------------------------------------------------------

registry.register_engine("fal", FalEngine)
```

- [ ] **Step 8: Run engine tests, confirm GREEN**

```bash
pixi run pytest tests/engines/test_fal.py tests/engines/test_fal_wire.py -v
```

Expected: 20 PASS.

- [ ] **Step 9: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/fal/__init__.py src/kinoforge/engines/fal/wire.py tests/engines/test_fal.py tests/engines/test_fal_wire.py
git add src/kinoforge/engines/fal/__init__.py src/kinoforge/engines/fal/wire.py tests/engines/test_fal.py tests/engines/test_fal_wire.py
git commit -m "feat(engines/fal): FalEngine + FalBackend with queue API wire (Layer I Task 11)"
```

---

## Task 12: `_adapters.py` + `fal.yaml` + invariant allowlist + tooling

**Goal:** Wire the fal engine into the CLI's adapter hub; ship the user-facing example config; extend the core-invariant allowlist; add the `live` pytest marker and `test-live` pixi task. Stop short of running the live test — that's Task 13.

**Files:**
- Modify: `src/kinoforge/_adapters.py` — import fal engine.
- Create: `examples/configs/fal.yaml`
- Modify: `tests/test_core_invariant.py` — extend allowlist for `engines.fal`.
- Modify: `tests/test_examples.py` — fal.yaml load test.
- Modify: `pyproject.toml` — `live` pytest marker.
- Modify: `pixi.toml` — `test-live` task.

**Acceptance Criteria:**
- [ ] `kinoforge` CLI imports without error and `kinoforge generate --help` shows usage.
- [ ] `kinoforge deploy --config examples/configs/fal.yaml --dry-run` exits 0 with "hosted engine 'fal'" log line.
- [ ] `examples/configs/fal.yaml` loads via `load_config(...)` without `ValidationError`.
- [ ] `test_core_invariant.py` allowlist accepts `kinoforge.engines.fal` confined to `engines/fal/`.
- [ ] `pyproject.toml` defines the `live` pytest marker.
- [ ] `pixi run test-live` exists (even if it skips silently without `FAL_KEY`).

**Verify:** `pixi run pytest tests/test_examples.py tests/test_core_invariant.py -v` — all green; `pixi run python -m kinoforge deploy --config examples/configs/fal.yaml --dry-run` exits 0.

**Steps:**

- [ ] **Step 1: Add fal engine import to `_adapters.py`**

```bash
grep -n "import kinoforge.engines\|engines.diffusers\|engines.hosted" src/kinoforge/_adapters.py
```

Append the fal import next to the other engine imports:

```python
import kinoforge.engines.fal as _fal_engine  # noqa: F401  (side-effect: registers "fal")
```

- [ ] **Step 2: Create `examples/configs/fal.yaml`**

```yaml
# kinoforge example: FalEngine (fal.ai queue API)
#
# Uses fal.ai's queue API for asynchronous video generation.
# Set FAL_KEY in your .env file (or pass via --env-file).
#
# HostedAPIEngine is NOT used here — fal.ai's wire shape differs from
# HostedAPIEngine's synthetic shim contract.  See examples/configs/hosted.yaml
# for the shim path.

engine:
  kind: fal
  precision: ""
  fal:
    endpoint: "fal-ai/wan/v2.2/t2v"
    queue_base: "https://queue.fal.run"
    api_key_env: "FAL_KEY"
    url_path: video.url

models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors"
    kind: base
    target: checkpoints

lifecycle:
  budget: 5.0
```

- [ ] **Step 3: Extend `tests/test_core_invariant.py` allowlist**

```bash
grep -n "engines.hosted\|engines.diffusers\|engines.comfyui\|allowlist\|providers" tests/test_core_invariant.py
```

Find the engine-allowlist regex (currently lists `comfyui`, `diffusers`, `hosted`, `fake`). Add `"fal"`:

```python
# Find pattern roughly like:
ENGINE_ALLOWLIST = {"comfyui", "diffusers", "hosted", "fake", "fal"}
# or a regex equivalent — add fal to whichever shape is used.
```

If the allowlist is a literal set/list, append `"fal"`. If it's a regex (`r"kinoforge\.engines\.(comfyui|diffusers|hosted|fake)\..*"`), add the alternation.

- [ ] **Step 4: Add fal.yaml load test**

Append to `tests/test_examples.py`:

```python
def test_fal_yaml_loads_under_new_validators() -> None:
    """examples/configs/fal.yaml must satisfy Task 10 validators."""
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/fal.yaml")
    assert cfg.engine.kind == "fal"
    assert cfg.engine.fal is not None
    assert cfg.engine.fal.endpoint == "fal-ai/wan/v2.2/t2v"
    assert cfg.engine.fal.queue_base == "https://queue.fal.run"
    assert cfg.engine.fal.api_key_env == "FAL_KEY"
    assert cfg.engine.fal.url_path == "video.url"
```

- [ ] **Step 5: Add `live` pytest marker**

```bash
grep -n "ini_options\|markers" pyproject.toml
```

Inside `[tool.pytest.ini_options]`, add or extend `markers`:

```toml
markers = [
    "live: opt-in tests that hit real APIs (require KINOFORGE_LIVE_TESTS=1 + provider creds)",
]
```

- [ ] **Step 6: Add `test-live` pixi task**

```bash
grep -n "^\[tasks\]\|^test\b" pixi.toml
```

In `[tasks]` add:

```toml
test-live = "KINOFORGE_LIVE_TESTS=1 pytest tests/live/ -v -m live"
```

- [ ] **Step 7: Smoke-test CLI**

```bash
pixi run python -m kinoforge deploy --config examples/configs/fal.yaml --dry-run
```

Expected: exit 0; log line containing `hosted engine 'fal' — skipping compute provisioning`.

- [ ] **Step 8: Run the relevant test slice**

```bash
pixi run pytest tests/test_examples.py tests/test_core_invariant.py -v
```

Expected: all green.

- [ ] **Step 9: Full sweep**

```bash
pixi run pytest -q
```

Expected: ~600+ green (existing + Tasks 1-11 additions).

- [ ] **Step 10: Pre-commit + commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/_adapters.py examples/configs/fal.yaml tests/test_core_invariant.py tests/test_examples.py pyproject.toml pixi.toml
git commit -m "feat(cli): wire FalEngine into adapters + ship fal.yaml example (Layer I Task 12)"
```

---

## Task 13: Live opt-in test + manual smoke (USER-GATE)

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Produce kinoforge's first real public-provider artifact by running `kinoforge generate -c examples/configs/fal.yaml` against the live fal.ai queue API. Add the opt-in live test file so the smoke is reproducible.

**Files:**
- Create: `tests/live/__init__.py` (empty)
- Create: `tests/live/test_fal_live.py`
- Modify: `README.md` — add "Real providers — fal.ai" section under Credentials.
- Modify: `PROGRESS.md` — Phase 19 entry with per-task SHAs + "first real artifact" milestone.

**Acceptance Criteria:**
- [ ] `tests/live/test_fal_live.py` exists and skips cleanly when `KINOFORGE_LIVE_TESTS` is not set to `"1"` OR `FAL_KEY` is not in the environment.
- [ ] With `KINOFORGE_LIVE_TESTS=1 + FAL_KEY=<real>` in the environment, `pixi run test-live` exits 0.
- [ ] The manual smoke command `pixi run python -m kinoforge --env-file .env generate -c examples/configs/fal.yaml --prompt "a cat sitting on a fence" --mode t2v --run-id smoke-i-1` exits 0 and produces a file with MP4 magic bytes (`ftyp` at byte offset 4).
- [ ] The artifact filename + capability_key hex + git SHA are recorded in PROGRESS Phase 19 under "First real artifact".
- [ ] README has a runnable "fal.ai quickstart" snippet (set FAL_KEY, run the deploy + generate commands).

**Verify:** With `KINOFORGE_LIVE_TESTS=1 FAL_KEY=<real>` in env (set in `.env`), run `pixi run test-live` — both live tests PASS; then run the manual smoke and confirm an MP4 lands on disk.

**Steps:**

- [ ] **Step 1: Create `tests/live/__init__.py`**

Empty file (just an empty string, ensures the directory is a package).

- [ ] **Step 2: Create `tests/live/test_fal_live.py`**

```python
"""Opt-in live tests against the real fal.ai queue API (Layer I Task 13).

Gated by two env vars:
- KINOFORGE_LIVE_TESTS=1
- FAL_KEY=<real fal.ai key>

Cost: ~$0.05–$0.20 per run, depending on the model.  Skipped silently in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if not (os.getenv("KINOFORGE_LIVE_TESTS") == "1" and os.getenv("FAL_KEY")):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + FAL_KEY",
        allow_module_level=True,
    )


_CONFIG = "examples/configs/fal.yaml"


def _run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `python -m kinoforge` with the given args, capturing output."""
    return subprocess.run(
        [sys.executable, "-m", "kinoforge", *args],
        cwd=cwd, capture_output=True, text=True, check=False, timeout=600,
    )


def test_fal_provision_real(tmp_path: Path) -> None:
    """`kinoforge provision -c fal.yaml` succeeds against real fal.ai."""
    result = _run_cli(
        ["--state-dir", str(tmp_path), "provision", "--config", _CONFIG]
    )
    assert result.returncode == 0, (
        f"provision failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_fal_generate_short_t2v_real(tmp_path: Path) -> None:
    """`kinoforge generate -c fal.yaml` produces a real MP4 artifact."""
    result = _run_cli([
        "--state-dir", str(tmp_path),
        "generate", "--config", _CONFIG,
        "--prompt", "a cat sitting on a fence",
        "--mode", "t2v",
        "--run-id", "live-smoke",
    ])
    assert result.returncode == 0, (
        f"generate failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # FalBackend.result returns an Artifact with url=... ; the generate-clip
    # stage downloads to the store.  Find the persisted file under tmp_path.
    candidates = list(tmp_path.rglob("*.mp4"))
    assert candidates, f"no .mp4 found under {tmp_path}; cli output:\n{result.stdout}"
    f = candidates[0]
    raw = f.read_bytes()
    # ISO BMFF "ftyp" box at offset 4 — robust MP4 magic-bytes check.
    assert raw[4:8] == b"ftyp", (
        f"file {f} is not an MP4 (bytes 4-8 = {raw[4:8]!r})"
    )
```

- [ ] **Step 3: Add README "Real providers — fal.ai" section**

```bash
grep -n "Credentials\|.env\|FAL_KEY" README.md
```

Append a new subsection after the existing Credentials section:

````markdown
## Real providers — fal.ai

kinoforge ships with a fal.ai sibling engine (`FalEngine`) for video generation
via fal's queue API.

**Setup:**

1. Put your fal.ai key in `.env` at the repo root:
   ```
   FAL_KEY=fal-XXXXXXXX
   ```
2. Pick a model — `examples/configs/fal.yaml` defaults to Wan2.2 T2V.
3. Run:
   ```bash
   pixi run python -m kinoforge --env-file .env generate \
     -c examples/configs/fal.yaml \
     --prompt "a cat sitting on a fence" --mode t2v
   ```
4. Artifact lands under `.kinoforge/run/<run-id>/`.

To run the live test suite (`pixi run test-live`), set `KINOFORGE_LIVE_TESTS=1`
alongside `FAL_KEY` in your environment.
````

- [ ] **Step 4: Run the live tests (requires .env with FAL_KEY)**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_fal_live.py -v
```

Expected: 2 PASS. If `FAL_KEY` isn't set, the module-level skip catches and the result is `2 skipped` (also OK — confirm via `-v` output the skip reason).

- [ ] **Step 5: Run the manual smoke for the first real artifact**

```bash
rm -rf /tmp/kinoforge-fal-smoke
pixi run python -m kinoforge --state-dir /tmp/kinoforge-fal-smoke --env-file .env generate \
  -c examples/configs/fal.yaml \
  --prompt "a cat sitting on a fence" --mode t2v --run-id smoke-i-1 2>&1
```

Expected: exit 0; final line containing `generated: uri=...`.

Verify the artifact:

```bash
find /tmp/kinoforge-fal-smoke -name "*.mp4" -exec head -c 12 {} \; | xxd
```

Expected: bytes 4-8 of the file are `66 74 79 70` (`ftyp`) — confirms valid MP4.

Record the artifact path:

```bash
ls -la /tmp/kinoforge-fal-smoke/run/smoke-i-1/
```

- [ ] **Step 6: Capture the capability_key + git SHA for PROGRESS**

```bash
pixi run python -c "from kinoforge.core.config import load_config; print(load_config('examples/configs/fal.yaml').capability_key().derive())"
git rev-parse HEAD
```

Note both values.

- [ ] **Step 7: Update PROGRESS.md Phase 19 entry**

Open `PROGRESS.md`. Find the existing PROGRESS structure (Phase 17, 18 etc.) and add a Phase 19 section under Post-MVP, mirroring the existing format:

```markdown
### Phase 19 — Layer I (fal.ai adapter + UX A + hosted hardening, GitHub issue #N)

- [x] Hot-fix: provisioner cfg-dict — commit `e78cafc` on `main`
- [x] Task 1: Diffusers + ComfyUI provisioner-cfg regression — commit `<SHA>`
- [x] Task 2: declared_flags WARNING → DEBUG — commit `<SHA>`
- [x] Task 3: FakeEngine declared_flags_map default — commit `<SHA>`
- [x] Task 4: HostedEngineConfig validators — commit `<SHA>`
- [x] Task 5: HostedAPIEngine AuthError + declared_flags_map default — commit `<SHA>`
- [x] Task 6: Rewrite hosted.yaml + shim contract docs — commit `<SHA>`
- [x] Task 7: core/provision_state.py — commit `<SHA>`
- [x] Task 8: UX A hosted preflight — commit `<SHA>`
- [x] Task 9: UX A compute preflight + marker — commit `<SHA>`
- [x] Task 10: FalEngineConfig pydantic block — commit `<SHA>`
- [x] Task 11: FalEngine + FalBackend + wire — commit `<SHA>`
- [x] Task 12: _adapters + fal.yaml + invariant + tooling — commit `<SHA>`
- [x] Task 13: Live opt-in test + manual smoke — commit `<SHA>`

**First real artifact:** `<filename>` (capability_key `<hex>`, git SHA `<full SHA>`).
```

Update the "Single next action" section at the top of PROGRESS.md to reflect Layer I completion.

- [ ] **Step 8: Pre-commit + commit (test file + README + PROGRESS)**

```bash
pixi run pre-commit run --files tests/live/__init__.py tests/live/test_fal_live.py README.md PROGRESS.md
git add tests/live/__init__.py tests/live/test_fal_live.py README.md PROGRESS.md
git commit -m "feat(live): opt-in fal.ai live test + README + PROGRESS Phase 19 (Layer I Task 13)

First real public-provider artifact produced: <filename>
capability_key: <hex>"
```

- [ ] **Step 9: Merge to main via --no-ff**

```bash
git checkout main
git merge --no-ff build/layer-i -m "$(cat <<'EOF'
Merge branch 'build/layer-i': fal.ai adapter + UX A + hosted hardening (Layer I)

- FalEngine: first real public-provider adapter (queue API)
- UX A: orchestrator.generate() preflights engine.provision() (cred + health)
- HostedAPIEngine: pydantic validators, declared_flags noise removed, AuthError clarity
- examples/configs/hosted.yaml rewritten; examples/configs/fal.yaml added

Tests: 596 -> ~644 offline + 2 opt-in live tests gated by
KINOFORGE_LIVE_TESTS=1 + FAL_KEY.

First real artifact produced: <filename>, capability_key <hex>.

Closes #<N>.
EOF
)"
```

- [ ] **Step 10: Backfill the merge commit SHA in PROGRESS**

After the merge succeeds:

```bash
git log --oneline -1
```

Note the merge SHA. Update PROGRESS Phase 19 to reference it. Commit the doc-update on `main`:

```bash
git add PROGRESS.md
git commit -m "docs(progress): backfill Layer I merge commit SHA (<merge SHA>)"
```

---

## Pre-merge gate checklist

Before merging `build/layer-i` to `main` (Task 13 Step 9):

1. `pixi run pre-commit run --all-files` clean.
2. `pixi run pytest -q` shows ~644 passed (with no XFAIL leftover).
3. `pixi run typecheck` clean.
4. Manual fal.ai smoke produced a real MP4; details captured in PROGRESS.
5. README has the fal.ai quickstart.
6. Optionally: two-stage code review (spec compliance + code quality) per established pattern in earlier layers.

---

## Self-review

This plan has been written against the spec at `docs/superpowers/specs/2026-05-31-layer-i-fal-adapter-ux-a-design.md`. Final self-check:

1. **Spec coverage:** Every section of the spec maps to at least one task:
   - §3.1 FalEngine → Task 11
   - §3.2 UX A hosted → Task 8; UX A compute + marker → Task 9; helpers → Task 7
   - §3.3 Pydantic validators → Task 4 (hosted) + Task 10 (fal)
   - §3.4 Cosmetics → Task 2 (level downgrade) + Task 3 (fake defaults) + Task 5 (hosted defaults + AuthError)
   - §6.1 Offline tests → distributed across Tasks 1–11
   - §6.2 Live test → Task 13
   - §6.3 Tooling (markers, pixi task) → Task 12
   - §6.4 Manual smoke → Task 13
   - §7 Task sequencing → reflected in Tasks 1–13 with dependencies above
   - §8 Pre-merge gates → "Pre-merge gate checklist" above
   - §9 Merge template → Task 13 Step 9
2. **Placeholder scan:** All steps contain runnable commands or exact code blocks. Two acceptable placeholders remain — `<filename>`, `<hex>`, `<SHA>`, `<merge SHA>` — substituted at execution time from real values (these are intentional, not skipped work).
3. **Type consistency:** `FalBackend.submit` returns `str` (the `request_id`); internal `_jobs: dict[str, dict[str, str]]` map preserves the per-job URL pair. `FalEngine` field names match between config (`endpoint`, `queue_base`, `api_key_env`, `url_path`, `asset_paths`, `health_url`) and backend constructor (`endpoint`, `queue_base`, `api_key`, `url_path`, `asset_paths`, ...). `_provision_compute_once` is referenced by the same name in Task 9 throughout.
4. **Spec gaps:** None identified.
