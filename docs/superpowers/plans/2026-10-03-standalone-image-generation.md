# Standalone Image Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kinoforge image` — a terminal image-generation command that reaches the four already-registered image engines directly, so one prompt in produces one PNG out with no compute.

**Architecture:** A new `image:` config block whose type (`ImageConfig`) becomes the base that the existing `KeyframeConfig` extends. A plain `generate_image()` function — no `PipelineState`, no `deploy_session` — because every image engine declares `requires_compute = False`. The ~20-line "resolve engine + backend + profile" block duplicated in `orchestrator.py` and `batch.py` is extracted to a shared `core/image_stack.py` and consumed by all three call sites, so duplication falls rather than rises.

**Tech Stack:** Python 3.13, pydantic v2 (`model_validator`), argparse, pytest, pixi. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-03-standalone-image-generation-design.md`

## Global Constraints

- **Run everything through pixi.** `pixi run pytest …`, `pixi run pre-commit run --all-files`. There is no system pytest or pre-commit binary.
- **Existing behaviour is frozen.** Every config without an `image:` block MUST take the identical validation path it takes today. Task 1 and Task 2 both prove this by leaving existing test files **unmodified** and green — if a pre-existing test needs editing to pass, stop: that is a behaviour change, not a refactor.
- **The allowlist validator MUST be `mode="before"`.** `Config.store` and `Config.output` have `default_factory`, so after validation they are always present and an operator-written key is indistinguishable from an applied default. Only the raw input dict can tell them apart.
- **Local timezone everywhere.** `datetime.now()`, never `utcnow()`. Filenames, run ids, log entries.
- **Credentials: never into a file, test, fixture or commit message.** Each image engine reads its own API credential from the process environment via `EnvCredentialProvider`; no task in this plan needs to look at a credential's value. If you must report on one, report length and shape only.
- **Naming collision to avoid:** `src/kinoforge/validation/checks/image.py` already exists and is about `cfg.compute.image` — the *container* image. Do not add image-generation checks to it, and do not name any new module `image.py` inside `validation/checks/`.
- **Mode token is `t2i`** everywhere — config `mode:`, `ImageProfile.supported_modes`, the gate in Task 6.
- **Exit codes:** 2 = config/precondition error, 1 = operational failure, 0 = success and `--dry-run`.

**User decisions (already made):**
- Surface is a **new `kinoforge image` subcommand**, not `generate --mode t2i`.
- Config carries a **new `image:` block**; `engine:` / `models:` / `compute:` are absent from an image config.
- **Exactly one image per invocation.** No `--count`, no batch integration.
- Spec reviewed and approved 2026-10-03 with "no changes".
- Example config ships **`uni-1`**, preserving §18's deferral on `uni-1-max` pricing.

---

## File Structure

**New files**

| file | responsibility |
|---|---|
| `src/kinoforge/core/image_stack.py` | Resolve an `ImageConfig` into `(engine, backend, profile)`. Shared by `orchestrator`, `batch`, `image_run`. Neutral module so the video path never imports the image command. |
| `src/kinoforge/core/image_run.py` | `generate_image()` — the terminal image orchestration. One concern: produce one artifact and publish it. |
| `tests/core/test_image_config.py` | `ImageConfig` / `Config.image` / the allowlist. |
| `tests/core/test_image_stack.py` | `resolve_image_stack` in isolation. |
| `tests/core/test_image_run.py` | `generate_image` behaviour, prompt precedence, the t2i gate. |
| `tests/cli/test_cmd_image.py` | The subcommand: dry-run, exit codes, ephemeral refusal. |
| `tests/image_engines/test_cancellation.py` | The `ImageBackend.result` cancel-token contract (Task 5). |
| `tests/validation/test_checks_survive_engineless_cfg.py` | The doctor-path regression (Task 3). |
| `examples/configs/luma-uni1-t2i.yaml` | Quality config; the live-fired one. |
| `examples/configs/fal-flux-schnell-t2i.yaml` | Fast/cheap iteration config. |

**Modified files**

| file | change |
|---|---|
| `src/kinoforge/core/config.py` | `ImageConfig` base; `KeyframeConfig(ImageConfig)`; `Config.image`; `engine`/`models` optional; allowlist validator; `_validate_cross_fields` early return; `capability_key()` guard. |
| `src/kinoforge/core/interfaces.py` | `ImageBackend.result(..., cancel_token=None)`. |
| `src/kinoforge/core/orchestrator.py` | Call `resolve_image_stack`; delete the local copy. |
| `src/kinoforge/core/batch.py` | Call `resolve_image_stack`; delete the local copy. |
| `src/kinoforge/core/ephemeral.py` | `IMAGE_EPHEMERAL_CAPABILITIES`. |
| `src/kinoforge/pipeline/keyframe.py` | Pass `cancel_token` into `result()`. |
| `src/kinoforge/image_engines/{fal,luma_agents,replicate,fake}/__init__.py` | Accept and honour/forward `cancel_token`. |
| `src/kinoforge/validation/checks/models.py` | Guard `cfg.engine is None` in `applies_to`. |
| `src/kinoforge/validation/checks/loras.py` | Guard `cfg.engine is None` in `applies_to`. |
| `src/kinoforge/cli/_main.py` | `image` subparser; `_DISPATCH`; `_INTERRUPTIBLE_CMDS`; `_preflight_ephemeral` image branch. |
| `src/kinoforge/cli/_commands.py` | `_cmd_image`; `_resolve_run_id` extraction. |
| `docs/configuration.md`, `docs/engines.md`, `README.md`, `PROGRESS.md` | Documentation. |
| `successful-generations.md` | New section after the live fire (Task 10). |

---

## Task 1: `ImageConfig` base, `KeyframeConfig` extends it

**Goal:** Re-parent `KeyframeConfig` onto a new `ImageConfig` base holding `engine`/`prompt`/`spec`/`params`/`capability_key()`, with zero behaviour change for existing keyframe configs.

**Files:**
- Modify: `src/kinoforge/core/config.py:1222-1278` (the `KeyframeConfig` class)
- Test: `tests/core/test_image_config.py` (create)
- Test (must stay unmodified and green): `tests/core/test_keyframe_config.py`

**Acceptance Criteria:**
- [ ] `ImageConfig` exists with fields `engine: str`, `prompt: str | None`, `spec: dict`, `params: dict`, `extra="forbid"`, and `capability_key()`
- [ ] `KeyframeConfig` subclasses `ImageConfig` and adds only `roles` plus its two existing validators
- [ ] `capability_key()` is defined exactly once per class in the file (ImageConfig + Config = 2 total)
- [ ] All 8 existing tests in `tests/core/test_keyframe_config.py` pass with that file **unmodified**
- [ ] `ImageConfig(engine="x")` is valid with no prompt (`--prompt` may supply it at runtime)
- [ ] `KeyframeConfig(engine="x")` still raises — the prompt validator stayed on the subclass

**Verify:** `pixi run pytest tests/core/test_keyframe_config.py tests/core/test_image_config.py -v` → all pass, zero edits to `test_keyframe_config.py`

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_image_config.py`:

```python
"""ImageConfig: the base KeyframeConfig extends (Layer R terminal-image work)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.config import ImageConfig, KeyframeConfig


def test_image_config_accepts_no_prompt() -> None:
    """ImageConfig permits prompt=None because --prompt may supply it at runtime.

    Bug this catches: a prompt-required validator placed on the BASE instead of
    KeyframeConfig, which would make every bare `image:` block unloadable.
    """
    cfg = ImageConfig(engine="luma_agents", spec={"model": "uni-1"})
    assert cfg.prompt is None
    assert cfg.engine == "luma_agents"


def test_keyframe_config_still_requires_a_prompt() -> None:
    """KeyframeConfig keeps its own prompt validator after re-parenting.

    Bug this catches: moving _at_least_one_prompt up to the base (which would
    break ImageConfig) or dropping it (which would un-guard keyframe configs).
    """
    with pytest.raises(PydanticValidationError, match="requires either top-level"):
        KeyframeConfig(engine="fal", spec={"model": "fal-ai/flux/schnell"})


def test_keyframe_config_is_an_image_config() -> None:
    """The subclass relationship is what lets resolve_image_stack take one type.

    Bug this catches: shipping ImageConfig as a SIBLING, which would force
    resolve_image_stack onto a structural Protocol and silently accept any
    object with the right attribute names.
    """
    kf = KeyframeConfig(engine="fal", prompt="a cat", spec={"model": "m"})
    assert isinstance(kf, ImageConfig)


def test_capability_key_identical_across_both_types() -> None:
    """capability_key() lives once on the base and derives the same key.

    Bug this catches: a second copy of capability_key() on the subclass that
    drifts — which would strand every cached image profile, because the cache
    filename IS the derived key.
    """
    spec = {"model": "uni-1", "precision": "fp16"}
    img = ImageConfig(engine="luma_agents", spec=spec)
    kf = KeyframeConfig(engine="luma_agents", prompt="x", spec=spec)
    assert img.capability_key() == kf.capability_key()
    assert img.capability_key().base_model == "uni-1"
    assert img.capability_key().precision == "fp16"
    assert img.capability_key().engine == "luma_agents"
    assert img.capability_key().loras == ()


def test_image_config_forbids_unknown_keys() -> None:
    """extra="forbid" is inherited, so a typo'd key is refused not ignored.

    Bug this catches: losing model_config on the base during the extraction,
    turning `promt:` into a silently-ignored key — the inert-config class this
    whole design treats as a defect (U51/U56).
    """
    with pytest.raises(PydanticValidationError):
        ImageConfig(engine="fal", promt="typo")  # type: ignore[call-arg]


def test_capability_key_not_duplicated_in_source() -> None:
    """Structural guard: the extraction must DELETE the old copy, not shadow it.

    Bug this catches: leaving KeyframeConfig.capability_key in place so the
    base's version is never used and the duplication the design set out to
    remove silently survives.
    """
    from pathlib import Path

    import kinoforge.core.config as config_mod

    source = Path(config_mod.__file__).read_text(encoding="utf-8")
    assert source.count("def capability_key(self) -> CapabilityKey:") == 2, (
        "expected exactly 2 capability_key definitions in config.py "
        "(ImageConfig and Config); found a third — the KeyframeConfig copy "
        "was not deleted"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_image_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'ImageConfig' from 'kinoforge.core.config'`

- [ ] **Step 3: Write the implementation**

In `src/kinoforge/core/config.py`, replace the `KeyframeConfig` class body (currently at lines 1222-1278) with the two classes below. `KeyframeRoleOverride` stays exactly where it is (above these), unchanged.

```python
class ImageConfig(BaseModel):
    """Image-generation block: an image engine plus the spec it submits.

    Base of :class:`KeyframeConfig` — a keyframe spec IS an image spec plus
    per-role overrides. Carried standalone by `kinoforge image` (``cfg.image``)
    and as the head of a video pipeline by ``cfg.keyframe``.

    ``prompt`` is optional here, deliberately: `kinoforge image --prompt` may
    supply it at runtime, and load time cannot see argv. Prompt-presence is a
    preflight check in :func:`kinoforge.core.image_run.generate_image`.

    Required: ``engine`` (image-engine registry name).
    """

    engine: str
    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    def capability_key(self) -> CapabilityKey:
        """Derive a CapabilityKey for image-engine cache lookup.

        Returns:
            A CapabilityKey with base_model and precision from ``spec``,
            loras empty, and engine from ``self.engine``.
        """
        return CapabilityKey(
            base_model=str(self.spec.get("model", "")),
            loras=(),
            engine=self.engine,
            precision=str(self.spec.get("precision", "")),
        )


class KeyframeConfig(ImageConfig):
    """Keyframe-generation block for image-engine pipeline head.

    Presence opts the orchestrator into constructing a KeyframeStage at the
    head of the pipeline. Extends :class:`ImageConfig` with per-role overrides.

    Required: ``engine`` (image-engine registry name), inherited.
    Required by validator: either ``prompt`` (top-level default) OR
    ``roles.<name>.prompt`` for at least one role.
    """

    roles: dict[str, KeyframeRoleOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _at_least_one_prompt(self) -> KeyframeConfig:
        """Require at least one non-empty prompt (top-level or per-role)."""
        has_top = bool(self.prompt and self.prompt.strip())
        has_role = any((r.prompt and r.prompt.strip()) for r in self.roles.values())
        if not has_top and not has_role:
            raise ValueError(
                "keyframe block requires either top-level `prompt` "
                "or at least one `roles.<role>.prompt`"
            )
        return self

    @model_validator(mode="after")
    def _role_names_known(self) -> KeyframeConfig:
        """Reject role names not defined in MODE_ROLE_REQUIREMENTS."""
        from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS

        known = {role for roles in MODE_ROLE_REQUIREMENTS.values() for role in roles}
        unknown = set(self.roles) - known
        if unknown:
            raise ValueError(
                f"keyframe.roles contains unknown role(s): {sorted(unknown)}; "
                f"known: {sorted(known)}"
            )
        return self
```

`KeyframeConfig` no longer declares `engine` / `prompt` / `spec` / `params` / `model_config` / `capability_key` — all inherited.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_image_config.py tests/core/test_keyframe_config.py -v`
Expected: PASS — all 6 new tests and all 8 existing keyframe tests.

If any test in `tests/core/test_keyframe_config.py` fails, **do not edit that file.** The refactor changed behaviour; fix the implementation instead.

- [ ] **Step 5: Confirm the wider suite is unaffected**

Run: `pixi run pytest tests/core tests/pipeline -q`
Expected: PASS, same count as before the change.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/config.py tests/core/test_image_config.py
git commit -m "refactor(config): ImageConfig is the base KeyframeConfig extends

A keyframe spec IS an image spec plus per-role overrides, so the honest
factoring is one base and one extension, not two siblings. Deletes the
duplicate capability_key() and gives the coming resolve_image_stack a nominal
parameter type instead of a structural Protocol.

The prompt validators stay on KeyframeConfig: it permits prompt=None when a
role supplies one, and ImageConfig permits it because --prompt may supply it at
runtime. A base-level prompt requirement would break both.

tests/core/test_keyframe_config.py is UNMODIFIED and green — that is the
non-regression proof for a refactor of live-proven code."
```

---

## Task 2: `Config.image`, optional `engine`/`models`, and the allowlist

**Goal:** Let a config carry `image:` instead of `engine:`/`models:`, and refuse every key that would be inert on such a config.

**Files:**
- Modify: `src/kinoforge/core/config.py:1479-1490` (field declarations), `_validate_cross_fields` (~`:1590`), `capability_key` (~`:1626`)
- Test: `tests/core/test_image_config.py` (extend)

**Acceptance Criteria:**
- [ ] `Config.engine` is `EngineConfig | None = None`; `Config.models` defaults to `[]`; `Config.image` is `ImageConfig | None = None`
- [ ] A config with `image:` and none of `engine:`/`models:` loads
- [ ] Each of these keys alongside `image:` is refused with the key named in the message: `engine`, `models`, `compute`, `loras`, `keyframe`, `upscale`, `interpolate`, `splitter`, `spec`, `params`, `lifecycle`
- [ ] `mode: t2i` accepted; `mode` absent accepted; `mode: t2v` (or any other value) refused when `image:` is present
- [ ] A config WITHOUT `image:` still requires `engine:` and still raises the existing "at least one entry with kind: base" error — existing tests unmodified
- [ ] `Config.capability_key()` on an image config raises `ConfigError` with a clear message, not `AttributeError`

**Verify:** `pixi run pytest tests/core/test_image_config.py tests/core/test_config.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_image_config.py`:

```python
IMAGE_CFG_MINIMAL = {
    "image": {"engine": "fake", "prompt": "a cat", "spec": {"model": "m"}},
}

# Every key that would be INERT on an image config. Refusing them is the
# U51/U56 lesson: a key that is accepted and ignored is a defect here.
FORBIDDEN_WITH_IMAGE = [
    ("engine", {"kind": "fake", "precision": ""}),
    ("models", [{"kind": "base", "ref": "hf:x/y", "target": "checkpoints"}]),
    ("compute", {"provider": "local"}),
    ("loras", [{"ref": "hf:a/b"}]),
    ("keyframe", {"engine": "fake", "prompt": "x"}),
    ("upscale", {"engine": "spandrel"}),
    ("interpolate", {"engine": "rife"}),
    ("splitter", {"kind": "heuristic"}),
    ("spec", {"model": "x"}),
    ("params", {"width": 512}),
    ("lifecycle", {"budget": 1.0}),
]


def test_image_config_loads_without_engine_or_models() -> None:
    """An image config carries no video engine and no models list.

    Bug this catches: leaving Config.engine/models required, which makes every
    image config unloadable and forces operators to write a fake video engine
    block just to satisfy a validator.
    """
    from kinoforge.core.config import Config

    cfg = Config.model_validate(IMAGE_CFG_MINIMAL)
    assert cfg.image is not None
    assert cfg.image.engine == "fake"
    assert cfg.engine is None
    assert cfg.models == []


@pytest.mark.parametrize(("key", "value"), FORBIDDEN_WITH_IMAGE)
def test_forbidden_key_alongside_image_is_refused_by_name(
    key: str, value: object
) -> None:
    """Each inert key is refused AND the message names that key.

    Bug this catches: a validator that refuses everything for the wrong reason
    (so the operator cannot tell which key was the problem), and the inert-key
    class itself — an image cfg carrying `loras:` would reproduce U56 on
    purpose, one carrying `lifecycle:` would reproduce U51's shape.
    """
    from kinoforge.core.config import Config

    data = {**IMAGE_CFG_MINIMAL, key: value}
    with pytest.raises(PydanticValidationError, match=key):
        Config.model_validate(data)


def test_mode_t2i_is_accepted_and_other_modes_are_not() -> None:
    """`mode` on an image config is VALIDATED, not documentary.

    Bug this catches: accepting `mode: t2v` on an image config and silently
    ignoring it — the same inert-config defect as a forbidden key, which an
    earlier draft of this design shipped as "documentary only".
    """
    from kinoforge.core.config import Config

    assert Config.model_validate({**IMAGE_CFG_MINIMAL, "mode": "t2i"}).mode == "t2i"
    assert Config.model_validate(IMAGE_CFG_MINIMAL).mode is None
    with pytest.raises(PydanticValidationError, match="t2i"):
        Config.model_validate({**IMAGE_CFG_MINIMAL, "mode": "t2v"})


def test_store_and_output_are_permitted_alongside_image() -> None:
    """The allowlist admits exactly the five keys an image run uses.

    Bug this catches: an allowlist built on the VALIDATED model rather than the
    raw input, which cannot distinguish an operator-written `output:` from the
    default_factory one and so either refuses every config or admits every key.
    """
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        {**IMAGE_CFG_MINIMAL, "store": {"kind": "local"}, "output": {"dir": "out"}}
    )
    assert cfg.output.dir == "out"


def test_video_config_still_requires_engine_and_a_base_model() -> None:
    """Configs without `image:` take the existing path byte for byte.

    Bug this catches: making engine/models optional for EVERY config, which
    would let a typo'd video cfg load and fail much later on a booted pod.
    """
    from kinoforge.core.config import Config

    with pytest.raises(PydanticValidationError):
        Config.model_validate({"models": []})  # no engine at all
    with pytest.raises(PydanticValidationError, match="kind: base"):
        Config.model_validate(
            {"engine": {"kind": "fake", "precision": ""}, "models": []}
        )


def test_capability_key_on_image_config_raises_configerror() -> None:
    """An image config has no video identity; say so instead of AttributeError.

    Bug this catches: `self.engine.diffusers` on engine=None raising a bare
    AttributeError from deep inside capability_key, which reads as a kinoforge
    crash rather than "you called the wrong method for this config".
    """
    from kinoforge.core.config import Config
    from kinoforge.core.errors import ConfigError

    cfg = Config.model_validate(IMAGE_CFG_MINIMAL)
    with pytest.raises(ConfigError, match="image"):
        cfg.capability_key()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_image_config.py -v -k "loads_without or forbidden or mode_t2i or store_and_output or capability_key_on_image"`
Expected: FAIL — `engine` is a required field, so `Config.model_validate(IMAGE_CFG_MINIMAL)` raises on the missing `engine`.

- [ ] **Step 3: Write the implementation**

3a. Change the field declarations (currently lines 1479-1490):

```python
    mode: str | None = None
    prompt: str | None = None
    engine: EngineConfig | None = None
    models: list[ModelEntry] = []
```

and add `image` next to `keyframe`:

```python
    keyframe: KeyframeConfig | None = None
    image: ImageConfig | None = None
```

Update the `Config` docstring's `engine` / `models` attribute lines to say they are required unless an `image:` block is present, and add an `image:` line saying its presence opts into the terminal-image path (`kinoforge image`).

3b. Add the module-level allowlist constant just above `class Config(BaseModel):`:

```python
# An image config (`image:` present) is a terminal-image run: no pod, no video
# engine, no model fetch. Every other top-level key would be INERT on it, and an
# accepted-but-ignored key is the defect class behind U51 (`lifecycle.budget`
# reading like a dollar guard that is not one) and U56 (a hosted cfg carrying
# `loras:` generating LoRA-less). So this is an ALLOWLIST, not a denylist: a
# denylist would silently admit every block added to Config after today.
_IMAGE_CFG_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"mode", "prompt", "image", "store", "output"}
)
```

3c. Add the allowlist validator to `Config`, immediately after `_promote_legacy_kind_lora_to_loras_block`:

```python
    @model_validator(mode="before")
    @classmethod
    def _image_cfg_allowlist(cls, data: Any) -> Any:  # noqa: ANN401
        """Refuse keys that would be inert on an image config.

        MUST be ``mode="before"``: ``store`` and ``output`` carry
        ``default_factory``, so on a validated model an operator-written key is
        indistinguishable from an applied default. Only the raw input dict can
        tell them apart.

        ``mode`` is validated here rather than left documentary — a typo'd
        ``mode: t2v`` on an image config is caught at load.
        """
        if not isinstance(data, dict) or data.get("image") is None:
            return data
        forbidden = sorted(set(data) - _IMAGE_CFG_ALLOWED_KEYS)
        if forbidden:
            raise ValueError(
                f"config with an `image:` block must not also carry: "
                f"{', '.join(forbidden)}. An image run has no compute, no video "
                f"engine and no model fetch, so those keys would be silently "
                f"inert. Permitted alongside `image:`: "
                f"{', '.join(sorted(_IMAGE_CFG_ALLOWED_KEYS))}."
            )
        mode = data.get("mode")
        if mode is not None and mode != "t2i":
            raise ValueError(
                f"config with an `image:` block must have mode: t2i "
                f"(or omit mode entirely); got {mode!r}"
            )
        return data
```

3d. Add the early return at the very top of `_validate_cross_fields`, BEFORE the existing `if self.engine.kind not in KNOWN_ENGINES:` line:

```python
    @model_validator(mode="after")
    def _validate_cross_fields(self) -> Self:
        """Validate cross-field constraints after all fields are populated."""
        # An image config carries no engine, no models, no compute and no
        # lifecycle (the allowlist refuses all four), so there is nothing here
        # to cross-validate. Returning early also keeps every dereference below
        # free of `engine is None` guards.
        if self.image is not None:
            return self
        # Neither block present: a real validation error with a message, NOT an
        # assert. `Config.model_validate({"models": []})` must tell the operator
        # what is missing, and this also narrows `engine` for mypy below.
        if self.engine is None:
            raise ValueError(
                "config must contain either an `engine:` block (video "
                "generation) or an `image:` block (terminal image generation)"
            )

        # Validate engine kind is known
        if self.engine.kind not in KNOWN_ENGINES:
```

The `raise` doubles as the mypy narrowing for the ~12 `self.engine.*` reads below it; without it mypy reports `Item "None" of "EngineConfig | None" has no attribute "kind"`. Do NOT use a bare `assert` here — a config with neither block is operator error, not an internal invariant, and deserves a message.

3e. Guard `Config.capability_key()` — add at the top of its body, before the `base_refs` work:

```python
        if self.image is not None:
            raise ConfigError(
                "capability_key() is a video-identity derivation and has no "
                "meaning for an `image:` config (no compute, no warm-reuse "
                "matcher). Use cfg.image.capability_key() for the image-profile "
                "cache key."
            )
        assert self.engine is not None  # noqa: S101 — image branch raised above
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_image_config.py -v`
Expected: PASS — all tests, including the 11 parametrized forbidden-key cases.

- [ ] **Step 5: Prove existing configs are untouched**

Run: `pixi run pytest tests/core/test_config.py tests/test_examples.py -q`
Expected: PASS with the same counts as before. `tests/core/test_config.py` must be **unmodified**.

Then the full suite, since optional `engine` touches 22 dereference sites:

Run: `pixi run pytest -q`
Expected: PASS. If a failure appears under `tests/validation/`, that is Task 3's territory — record it and continue; do not fix it here.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/config.py tests/core/test_image_config.py
git commit -m "feat(config): add the image: block, with an allowlist not a denylist

Config.engine and Config.models become optional so an image config can carry
neither. A config WITHOUT image: takes the existing validation path byte for
byte — tests/core/test_config.py is unmodified and green.

The allowlist is mode=before by necessity: store and output have
default_factory, so on a validated model an operator-written key cannot be told
from an applied default.

An allowlist rather than a denylist because a denylist admits every block added
to Config after today, and because accepted-but-ignored config is the defect
class behind U51 and U56. mode: is validated for the same reason — treating it
as documentary would be the same mistake."
```

---

## Task 3: Keep `kinoforge doctor` working on an engine-less config

**Goal:** Fix the two `applies_to` predicates that dereference `cfg.engine` without a guard, so the validation registry survives a config with `engine: None`.

This is spec §12.2, resolved by audit: exactly two unguarded sites, both in `applies_to` (which `CheckRegistry.applicable` calls for **every** config), and `validation/checks/custom_nodes.py:52` already carries the correct guard pattern.

**Files:**
- Modify: `src/kinoforge/validation/checks/models.py:83` (inside `ModelRefReachableCheck.applies_to`)
- Modify: `src/kinoforge/validation/checks/loras.py:58` (inside `LoraServerSupportCheck.applies_to`)
- Test: `tests/validation/test_checks_survive_engineless_cfg.py` (create)

**Acceptance Criteria:**
- [ ] `CheckRegistry.applicable(image_cfg)` returns without raising
- [ ] The test asserts the registry actually had checks to filter — a sweep that matches nothing would otherwise pass vacuously
- [ ] Both fixed predicates return `False` for an image config, and still return their original answer for a video config
- [ ] No pre-existing validation test is modified

**Verify:** `pixi run pytest tests/validation/ -v` → all pass

**Steps:**

- [ ] **Step 1: Confirm the two class names and the registry entry point**

The test below references class names and a `default_registry` helper. Confirm them first — adjust the test to match what you find, do not guess:

```bash
rg -n '^class .*Check' src/kinoforge/validation/checks/models.py src/kinoforge/validation/checks/loras.py
rg -n 'def default_registry|^class CheckRegistry' src/kinoforge/validation/*.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/validation/test_checks_survive_engineless_cfg.py`:

```python
"""Every check's applies_to must survive a cfg with engine=None (image configs).

CheckRegistry.applicable calls applies_to on EVERY registered check, so a single
unguarded cfg.engine.kind dereference crashes `kinoforge doctor` and the
generate-path preflight for any image config.
"""

from __future__ import annotations

import importlib

import pytest

from kinoforge.core.config import Config

IMAGE_CFG = {
    "image": {"engine": "fake", "prompt": "a cat", "spec": {"model": "m"}},
}

VIDEO_CFG = {
    "engine": {"kind": "fake", "precision": ""},
    "models": [{"kind": "base", "ref": "hf:a/b", "target": "checkpoints"}],
}

# (module, class) pairs confirmed in Step 1.
FIXED_CHECKS = [
    ("kinoforge.validation.checks.models", "ModelRefReachableCheck"),
    ("kinoforge.validation.checks.loras", "LoraServerSupportCheck"),
]


def _registry() -> object:
    """Return a CheckRegistry with every production check registered."""
    import kinoforge._adapters  # noqa: F401  — self-registration side effect
    from kinoforge.validation import default_registry

    return default_registry()


def test_applicable_does_not_raise_on_an_image_cfg() -> None:
    """applies_to is called for every check, guarded or not.

    Bug this catches: validation/checks/models.py:83 and loras.py:58 reading
    cfg.engine.kind with no None guard, which makes `kinoforge doctor` on any
    image config die with AttributeError before a single check runs.
    """
    registry = _registry()
    cfg = Config.model_validate(IMAGE_CFG)
    applicable = registry.applicable(cfg)  # type: ignore[attr-defined]
    assert isinstance(applicable, list)


def test_the_registry_actually_had_checks_to_filter() -> None:
    """Guard the guard: a registry of zero checks passes the test above vacuously.

    Bug this catches: an import regression that leaves default_registry() empty,
    which would make the test above green while proving nothing at all.
    """
    registry = _registry()
    video_applicable = registry.applicable(  # type: ignore[attr-defined]
        Config.model_validate(VIDEO_CFG)
    )
    assert len(video_applicable) >= 3, (
        f"expected the production registry to hold several checks applicable to "
        f"a plain video cfg; got {len(video_applicable)} — the registry is "
        f"probably empty, which would make the engine=None test vacuous"
    )


@pytest.mark.parametrize(("module_name", "class_name"), FIXED_CHECKS)
def test_fixed_predicates_return_false_not_raise(
    module_name: str, class_name: str
) -> None:
    """Each fixed applies_to answers False for an image cfg rather than raising.

    Bug this catches: "fixing" the crash by wrapping applies_to in a bare
    try/except, which would also swallow a genuine misconfiguration.
    """
    module = importlib.import_module(module_name)
    check = getattr(module, class_name)()
    assert check.applies_to(Config.model_validate(IMAGE_CFG)) is False
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pixi run pytest tests/validation/test_checks_survive_engineless_cfg.py -v`
Expected: FAIL — `AttributeError: 'NoneType' object has no attribute 'kind'` raised from `checks/models.py` or `checks/loras.py`.

- [ ] **Step 4: Write the implementation**

In `src/kinoforge/validation/checks/models.py`, inside `applies_to`, change:

```python
        if cfg.engine.kind in self._NON_FETCHING_ENGINES:
            return False
```

to:

```python
        # An image config (`image:` block) has no video engine at all and
        # fetches nothing. Same guard shape as checks/custom_nodes.py.
        if cfg.engine is None:
            return False
        if cfg.engine.kind in self._NON_FETCHING_ENGINES:
            return False
```

In `src/kinoforge/validation/checks/loras.py`, inside `applies_to`, change:

```python
        if cfg.engine.kind != "diffusers":
```

to:

```python
        # An image config has no video engine; LoRAs are refused by the config
        # allowlist before this point, so there is never a stack to check.
        if cfg.engine is None:
            return False
        if cfg.engine.kind != "diffusers":
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pixi run pytest tests/validation/ -v`
Expected: PASS — the new file plus every pre-existing validation test, unmodified.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/validation/checks/models.py src/kinoforge/validation/checks/loras.py \
        tests/validation/test_checks_survive_engineless_cfg.py
git commit -m "fix(validation): two applies_to predicates crash on engine=None

CheckRegistry.applicable calls applies_to for EVERY registered check, so the
unguarded cfg.engine.kind reads in checks/models.py and checks/loras.py would
kill kinoforge doctor on any image config before a single check ran.

Both take the guard shape checks/custom_nodes.py already uses, which is
evidence an optional engine was partly anticipated. Spec 12.2 flagged this as a
question the plan had to answer rather than assume; the audit found exactly two
sites.

The test guards the guard: it asserts the production registry actually holds
checks applicable to a video cfg, because an empty registry would make the
engine=None assertion pass vacuously."
```

---
## Task 4: Extract `resolve_image_stack` and migrate both existing call sites

**Goal:** One implementation of "resolve an image block into (engine, backend, profile)", consumed by `orchestrator`, `batch` and (in Task 6) `image_run` — deleting the two existing copies rather than adding a third.

**Files:**
- Create: `src/kinoforge/core/image_stack.py`
- Modify: `src/kinoforge/core/orchestrator.py:2726-2751` (delete the local block, call the helper)
- Modify: `src/kinoforge/core/batch.py:649-672` (same)
- Test: `tests/core/test_image_stack.py` (create)
- Test (must stay unmodified and green): `tests/core/test_orchestrator.py`, `tests/core/test_batch_generate.py`, `tests/pipeline/test_keyframe_stage.py`

**Acceptance Criteria:**
- [ ] `resolve_image_stack(block, *, store, image_engine=None, image_profile_provider=None)` returns `(ImageEngine, ImageBackend, ImageProfile)`
- [ ] Its parameter type is `ImageConfig` (nominal) — not a `Protocol`, not `Any`
- [ ] An unregistered engine name raises `UnknownAdapter` **before** `provision` or `backend` is called
- [ ] `ProfileNotCached` falls through to `discover`; a cached profile does not call `discover`
- [ ] An injected `image_engine` overrides the registry lookup
- [ ] `orchestrator.py` and `batch.py` each contain **zero** remaining `get_image_engine(` calls
- [ ] All existing orchestrator / batch / keyframe-stage tests pass unmodified

**Verify:** `pixi run pytest tests/core/test_image_stack.py tests/core/test_orchestrator.py tests/core/test_batch_generate.py tests/pipeline/test_keyframe_stage.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_image_stack.py`:

```python
"""resolve_image_stack: the one place an image block becomes a live stack."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.config import ImageConfig
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import CapabilityKey, ImageProfile
from kinoforge.core.errors import ProfileNotCached

BLOCK = ImageConfig(engine="fake", prompt="a cat", spec={"model": "m"})

PROFILE = ImageProfile(
    name="fake-image", max_resolution=(1024, 1024), supported_modes={"t2i"}
)


@dataclass
class _RecordingProvider:
    """ImageProfileProvider double that records which path was taken."""

    cached: ImageProfile | None = None
    resolve_calls: list[CapabilityKey] = field(default_factory=list)
    discover_calls: list[CapabilityKey] = field(default_factory=list)

    def resolve(self, key: CapabilityKey) -> ImageProfile:
        self.resolve_calls.append(key)
        if self.cached is None:
            raise ProfileNotCached(str(key))
        return self.cached

    def discover(self, key: CapabilityKey, engine: Any, backend: Any) -> ImageProfile:
        self.discover_calls.append(key)
        return PROFILE

    def verify(self, *a: Any, **k: Any) -> None:  # pragma: no cover - unused here
        return None


def test_returns_the_triple_for_a_registered_engine(tmp_path: Any) -> None:
    """The happy path: registry name in, live stack out.

    Bug this catches: an extraction that returns the engine but drops the
    backend or profile, which would push the missing construction back out to
    all three call sites — exactly the duplication this removes.
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    import kinoforge._adapters  # noqa: F401  — registers the fake image engine

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=PROFILE)
    engine, backend, profile = resolve_image_stack(
        BLOCK, store=store, image_profile_provider=provider
    )
    assert engine.name == "fake"
    assert backend is not None
    assert profile == PROFILE


def test_unknown_engine_raises_before_any_provisioning(tmp_path: Any) -> None:
    """Registry lookup happens FIRST, so a typo costs nothing.

    Bug this catches: resolving the profile (a cache write, potentially a live
    probe) before discovering the engine name is bogus — the whole reason the
    original orchestrator comment says "unknown engine names fail fast here
    before any compute spend".
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=PROFILE)
    bad = ImageConfig(engine="no-such-image-engine", prompt="x", spec={"model": "m"})
    with pytest.raises(UnknownAdapter, match="no-such-image-engine"):
        resolve_image_stack(bad, store=store, image_profile_provider=provider)
    assert provider.resolve_calls == [], "profile was resolved despite a bad engine"


def test_cached_profile_does_not_trigger_discover(tmp_path: Any) -> None:
    """A warm cache must not re-probe.

    Bug this catches: calling discover unconditionally, which for a live engine
    means an extra provider round trip on every single run.
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    import kinoforge._adapters  # noqa: F401

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=PROFILE)
    resolve_image_stack(BLOCK, store=store, image_profile_provider=provider)
    assert len(provider.resolve_calls) == 1
    assert provider.discover_calls == []


def test_profile_not_cached_falls_through_to_discover(tmp_path: Any) -> None:
    """A cold cache discovers exactly once.

    Bug this catches: letting ProfileNotCached escape to the caller, which would
    make the first run of every new (engine, model) pair fail.
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.stores.local import LocalArtifactStore

    import kinoforge._adapters  # noqa: F401

    store = LocalArtifactStore(root=tmp_path)
    provider = _RecordingProvider(cached=None)
    _, _, profile = resolve_image_stack(
        BLOCK, store=store, image_profile_provider=provider
    )
    assert profile == PROFILE
    assert len(provider.discover_calls) == 1


def test_injected_engine_overrides_the_registry(tmp_path: Any) -> None:
    """Test-injection seam, matching orchestrator.generate / batch.

    Bug this catches: ignoring the injected engine, which would make every
    offline test of the image path reach the real registry.
    """
    from kinoforge.core.image_stack import resolve_image_stack
    from kinoforge.image_engines.fake import FakeImageEngine
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    sentinel = FakeImageEngine()
    engine, _, _ = resolve_image_stack(
        BLOCK,
        store=store,
        image_engine=sentinel,
        image_profile_provider=_RecordingProvider(cached=PROFILE),
    )
    assert engine is sentinel


def test_both_call_sites_use_the_helper() -> None:
    """Structural guard: the extraction must DELETE both copies.

    Bug this catches: adding image_stack.py as a third implementation while the
    orchestrator and batch copies stay — the duplication the design set out to
    remove would then have gone UP, not down, and nothing else in this suite
    would notice.
    """
    from pathlib import Path

    import kinoforge.core.batch as batch_mod
    import kinoforge.core.orchestrator as orch_mod

    for mod in (orch_mod, batch_mod):
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "get_image_engine(" not in source, (
            f"{mod.__name__} still resolves an image engine directly; it must "
            f"call resolve_image_stack"
        )
        assert "resolve_image_stack" in source, (
            f"{mod.__name__} does not call resolve_image_stack"
        )
```

Confirm the store class name before running (`LocalArtifactStore` vs another name) and the `ProfileNotCached` import path:

```bash
rg -n '^class .*ArtifactStore' src/kinoforge/stores/local.py
rg -n 'class ProfileNotCached' src/kinoforge/core/*.py
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_image_stack.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kinoforge.core.image_stack'`

- [ ] **Step 3: Write the implementation**

Create `src/kinoforge/core/image_stack.py`:

```python
"""Resolve an image block into a live (engine, backend, profile) triple.

Shared by three call sites: ``orchestrator.generate`` and ``batch`` (which pass
``cfg.keyframe``) and ``image_run.generate_image`` (which passes ``cfg.image``).
Both config types are :class:`~kinoforge.core.config.ImageConfig` — a keyframe
spec IS an image spec plus per-role overrides — so the parameter type is nominal
rather than a structural Protocol.

Lives in its own neutral module rather than in ``core/image_run.py`` so the
video path (orchestrator, batch) never imports the standalone-image command's
module. The dependency arrows point at a shared seam, not at a sibling feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.config import ImageConfig
    from kinoforge.core.interfaces import (
        ImageBackend,
        ImageEngine,
        ImageProfile,
        ImageProfileProvider,
    )
    from kinoforge.stores.base import ArtifactStore


def resolve_image_stack(
    block: "ImageConfig",
    *,
    store: "ArtifactStore",
    image_engine: "ImageEngine | None" = None,
    image_profile_provider: "ImageProfileProvider | None" = None,
) -> tuple["ImageEngine", "ImageBackend", "ImageProfile"]:
    """Resolve *block* into a provisioned engine, a live backend and a profile.

    The registry lookup happens FIRST so an unregistered engine name fails
    before any provisioning, profile-cache write or live probe — an image run
    costs money the moment the backend is reached.

    Args:
        block: The image block to resolve (``cfg.image`` or ``cfg.keyframe``).
        store: Artifact store backing the default profile cache.
        image_engine: Pre-constructed engine (test injection). When ``None``,
            resolved from the image-engine registry via ``block.engine``.
        image_profile_provider: Profile cache (test injection). When ``None``,
            a :class:`~kinoforge.core.profiles.JsonImageProfileCache` over
            *store*.

    Returns:
        ``(engine, backend, profile)``.

    Raises:
        UnknownAdapter: ``block.engine`` is not a registered image engine.
    """
    from kinoforge.core import registry
    from kinoforge.core.errors import ProfileNotCached
    from kinoforge.core.profiles import JsonImageProfileCache

    engine = (
        image_engine
        if image_engine is not None
        else registry.get_image_engine(block.engine)()
    )
    cfg_dict = block.model_dump()
    engine.provision(None, cfg_dict)
    backend = engine.backend(None, cfg_dict)

    key = block.capability_key()
    provider: ImageProfileProvider = (
        image_profile_provider
        if image_profile_provider is not None
        else JsonImageProfileCache(store)  # type: ignore[assignment]
    )
    try:
        profile = provider.resolve(key)
    except ProfileNotCached:
        profile = provider.discover(key, engine, backend)
    return engine, backend, profile
```

Then replace the block in `src/kinoforge/core/orchestrator.py` (currently lines 2726-2751) with:

```python
    # Pre-resolve image engine + backend + profile if keyframe block present.
    # Resolved BEFORE deploy_session so unknown names fail fast without
    # incurring any compute spend. Shared with batch + image_run.
    # ------------------------------------------------------------------
    image_backend: ImageBackend | None = None
    image_prof = None
    resolved_image_engine: ImageEngine | None = None
    if cfg.keyframe is not None:
        resolved_image_engine, image_backend, image_prof = resolve_image_stack(
            cfg.keyframe,
            store=store,
            image_engine=image_engine,
            image_profile_provider=image_profile_provider,
        )
```

and the block in `src/kinoforge/core/batch.py` (currently lines 649-672) with:

```python
    # Pre-resolve image engine + backend + profile ONCE per batch if
    # cfg.keyframe is set. Amortises construction cost; unknown engine
    # names fail fast here before any compute spend.
    # ------------------------------------------------------------------
    _image_backend: ImageBackend | None = None
    _image_profile: ImageProfile | None = None
    _resolved_image_engine: ImageEngine | None = None
    if cfg.keyframe is not None:
        (
            _resolved_image_engine,
            _image_backend,
            _image_profile,
        ) = resolve_image_stack(
            cfg.keyframe,
            store=store,
            image_engine=image_engine,
            image_profile_provider=image_profile_provider,
        )
```

Add `from kinoforge.core.image_stack import resolve_image_stack` to both modules' imports. Then remove any import that is now unused in each file — likely `JsonImageProfileCache` and `ProfileNotCached` in one or both. Ruff will tell you:

```bash
pixi run ruff check src/kinoforge/core/orchestrator.py src/kinoforge/core/batch.py
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_image_stack.py -v`
Expected: PASS — all 6 tests including the structural guard.

- [ ] **Step 5: Prove the two migrated call sites still behave identically**

Run: `pixi run pytest tests/core/test_orchestrator.py tests/core/test_batch_generate.py tests/pipeline/test_keyframe_stage.py tests/test_layer_r_backcompat.py -v`
Expected: PASS, all files **unmodified**.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/image_stack.py src/kinoforge/core/orchestrator.py \
        src/kinoforge/core/batch.py tests/core/test_image_stack.py
git commit -m "refactor(core): extract resolve_image_stack, delete both copies

orchestrator.py:2726 and batch.py:649 held the same ~20 lines differing only in
local variable names. generate_image needed a third, so the duplication is
removed instead: three call sites, one implementation.

It lives in its own neutral module, NOT in core/image_run.py, so the video path
never imports the standalone-image command's module.

The structural test asserts neither migrated module still calls
get_image_engine directly — without it, adding a third implementation while
leaving both copies in place would pass every other test in the suite."
```

---

## Task 5: Make `ImageBackend.result` cancellable

**Goal:** Widen the `ImageBackend` ABC so a long image poll honours the CLI's SIGINT token, and wire it through all four engines plus `KeyframeStage`.

Spec §7: `luma_agents` and `replicate` already extend `RemoteSubmitPollBackend`, which checks the token every iteration and uses `token.wait` in place of `time.sleep` (`core/remote_backend.py:247-249`) — the capability exists and is unreachable because the ABC has no parameter for it. A Luma poll measured ~125 s in §15.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:855` (`ImageBackend.result`)
- Modify: `src/kinoforge/image_engines/luma_agents/__init__.py:252`
- Modify: `src/kinoforge/image_engines/replicate/__init__.py:171`
- Modify: `src/kinoforge/image_engines/fal/__init__.py:139-182` (hand-rolled loop)
- Modify: `src/kinoforge/image_engines/fake/__init__.py:60`
- Modify: `src/kinoforge/pipeline/keyframe.py` (add a `cancel_token` field; pass it to `result`)
- Modify: `src/kinoforge/core/orchestrator.py`, `src/kinoforge/core/batch.py` (pass the token at each `KeyframeStage(...)` construction) — NOTE: Task 4 also rewrites these two files, so Tasks 4 and 5 must not run concurrently
- Test: `tests/image_engines/test_cancellation.py` (create)

**Acceptance Criteria:**
- [ ] `ImageBackend.result(self, job_id, *, cancel_token=None) -> Artifact` on the ABC
- [ ] `luma_agents` and `replicate` forward the token to their inner backend
- [ ] `fal`'s loop calls `raise_if_set()` at the top of every iteration and waits on the token instead of sleeping **only when a token was supplied** — with no token it still uses the injected `self.sleep`, so existing tests keeping ticks instant are unaffected
- [ ] A token tripped mid-poll stops the loop early — proven by poll **count**, not only by exception type
- [ ] `KeyframeStage` accepts an optional `cancel_token` and passes it through
- [ ] All existing image-engine and keyframe-stage tests pass unmodified

**Verify:** `pixi run pytest tests/image_engines/ tests/pipeline/test_keyframe_stage.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/image_engines/test_cancellation.py`:

```python
"""ImageBackend.result must honour a CancelToken.

Without this the CLI's two-press SIGINT handler is inert for the whole of a
~125 s Luma poll (successful-generations.md 15), even though
RemoteSubmitPollBackend underneath already supports cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled
from kinoforge.core.interfaces import Artifact, ImageBackend, ImageJob


@dataclass
class _CountingBackend(ImageBackend):
    """ImageBackend whose poll count is observable.

    Mimics fal's hand-rolled loop: N iterations, token checked at the top of
    each, injected sleep between.
    """

    max_polls: int = 10
    polls: list[int] = field(default_factory=list)

    def capabilities(self) -> Any:  # pragma: no cover - not under test
        raise NotImplementedError

    def inspect_capabilities(self) -> Any:  # pragma: no cover - not under test
        raise NotImplementedError

    def submit(self, job: ImageJob) -> str:
        return "job-1"

    def result(
        self, job_id: str, *, cancel_token: CancelToken | None = None
    ) -> Artifact:
        from kinoforge.core.cancel import _NULL_TOKEN

        token = cancel_token if cancel_token is not None else _NULL_TOKEN
        for i in range(self.max_polls):
            token.raise_if_set()
            self.polls.append(i)
            if i == 2:  # a sibling "sets" the token mid-poll
                token.set()
        return Artifact(filename="x.png")

    def endpoints(self) -> dict[str, str]:
        return {}


def test_abc_signature_accepts_a_cancel_token() -> None:
    """The ABC must declare the keyword, or no caller can pass one.

    Bug this catches: fixing cancellation inside the engines only, leaving the
    ABC narrow so KeyframeStage and generate_image still cannot pass a token —
    which is the defect exactly as it ships today.
    """
    import inspect

    sig = inspect.signature(ImageBackend.result)
    assert "cancel_token" in sig.parameters
    assert sig.parameters["cancel_token"].default is None


def test_token_tripped_mid_poll_stops_the_loop_early() -> None:
    """Assert the POLL COUNT, not just that Cancelled was raised.

    Bug this catches: a loop that runs all max_polls iterations and only checks
    the token at the end. `pytest.raises(Cancelled)` alone would pass against
    that, which is why the count is the assertion.
    """
    backend = _CountingBackend(max_polls=10)
    token = CancelToken()
    with pytest.raises(Cancelled):
        backend.result("job-1", cancel_token=token)
    assert backend.polls == [0, 1, 2], (
        f"expected the loop to stop on the iteration after the token was set; "
        f"got {len(backend.polls)} polls — the token is being checked too late"
    )


def test_no_token_means_no_behaviour_change() -> None:
    """The default keeps every existing caller source- and behaviour-compatible.

    Bug this catches: making cancel_token required, or defaulting it to a live
    token, either of which breaks KeyframeStage and all four engines' tests.
    """
    backend = _CountingBackend(max_polls=4)
    artifact = backend.result("job-1")
    assert artifact.filename == "x.png"
    assert len(backend.polls) == 4


@pytest.mark.parametrize(
    "module_name",
    [
        "kinoforge.image_engines.fal",
        "kinoforge.image_engines.luma_agents",
        "kinoforge.image_engines.replicate",
        "kinoforge.image_engines.fake",
    ],
)
def test_every_shipped_backend_accepts_the_keyword(module_name: str) -> None:
    """All four engines must take the keyword, or one silently cannot cancel.

    Bug this catches: wiring luma/replicate (easy, they subclass the capable
    backend) and forgetting fal's hand-rolled loop — which is the one engine the
    keyframe path actually used live.
    """
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    backends = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, ImageBackend)
        and obj is not ImageBackend
    ]
    assert backends, f"no ImageBackend subclass found in {module_name}"
    for backend_cls in backends:
        sig = inspect.signature(backend_cls.result)
        assert "cancel_token" in sig.parameters, (
            f"{module_name}.{backend_cls.__name__}.result does not accept "
            f"cancel_token"
        )
```

Confirm the `Cancelled` exception name and import path before running:

```bash
rg -n 'class Cancelled|^class ' src/kinoforge/core/cancel.py
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run pytest tests/image_engines/test_cancellation.py -v`
Expected: FAIL — `test_abc_signature_accepts_a_cancel_token` fails (`'cancel_token' not in sig.parameters`) and the parametrized test fails for all four engines.

- [ ] **Step 3: Widen the ABC**

In `src/kinoforge/core/interfaces.py`, replace the `ImageBackend.result` declaration at line 855:

```python
    @abstractmethod
    def result(  # noqa: D102
        self, job_id: str, *, cancel_token: "CancelToken | None" = None
    ) -> Artifact: ...
```

`CancelToken` is already referenced in this module (`ImageEngine.provision`), so no new import is needed. `submit` is deliberately left alone: it is a single POST, and `RemoteSubmitPollBackend.submit` already does one `raise_if_set` before it (`remote_backend.py:205`).

- [ ] **Step 4: Forward the token in luma_agents and replicate**

`src/kinoforge/image_engines/luma_agents/__init__.py` line 252 and
`src/kinoforge/image_engines/replicate/__init__.py` line 171 — same edit in both:

```python
    def result(
        self, job_id: str, *, cancel_token: CancelToken | None = None
    ) -> Artifact:
        """Delegate to the inner submit-poll backend, forwarding the token."""
        return self._inner.result(job_id, cancel_token=cancel_token)
```

Add `CancelToken` to each module's `kinoforge.core.interfaces` import list (or import from `kinoforge.core.cancel`, matching whichever the file already uses).

These two get working cancellation for free: `RemoteSubmitPollBackend.result` already takes the keyword and honours it.

- [ ] **Step 5: Teach fal's hand-rolled loop the token**

`src/kinoforge/image_engines/fal/__init__.py` — change the signature at line 139 and the loop at 171-181. Copy the fallback pattern from `core/remote_backend.py:283-290` verbatim in spirit: **when no token is supplied, keep using the injected `self.sleep`**, because existing fal tests inject `sleep=lambda s: None` to keep ticks instant and a bare `token.wait(interval)` would make them slow or change their contract.

```python
    def result(
        self, job_id: str, *, cancel_token: CancelToken | None = None
    ) -> Artifact:
        """Poll fal status URL then fetch response URL; return image Artifact.

        Honors *cancel_token* at the top of every iteration and across the
        inter-poll wait. With no token the injected ``self.sleep`` is used
        unchanged, preserving the existing iteration-cap contract.

        Args:
            job_id: The fal request_id returned by :meth:`submit`.
            cancel_token: Optional :class:`CancelToken`. When set mid-poll the
                loop raises ``Cancelled`` on its next iteration.

        Returns:
            Artifact with ``url`` pointing at the first image.

        Raises:
            KinoforgeError: Job failed, timed out, or returned no images.
            Cancelled: ``cancel_token`` was set.
        """
        from kinoforge.core.cancel import _NULL_TOKEN

        token = cancel_token if cancel_token is not None else _NULL_TOKEN

        def _interpoll_wait(seconds: float) -> None:
            if cancel_token is None:
                self.sleep(seconds)
                return
            token.wait(seconds)
```

then, inside the existing loop, add the check as the first statement and swap the sleep:

```python
        for _ in range(self.max_polls):
            token.raise_if_set()
            status_data = self.http_get(status_url, headers)
            s = wire.interpret_status(str(status_data.get("status", "")))
            if s == wire.FalStatus.COMPLETED:
                break
            if s in (wire.FalStatus.FAILED, wire.FalStatus.UNKNOWN):
                raise KinoforgeError(f"fal image job {job_id} failed: {status_data}")
            _interpoll_wait(self.poll_interval_s)
        else:
            raise KinoforgeError(
                f"fal image job {job_id} timed out after {self.max_polls} polls"
            )
```

The rest of the method (response fetch, `images[0]["url"]`, the `Artifact`) is unchanged.

- [ ] **Step 6: Accept-and-ignore in the fake backend**

`src/kinoforge/image_engines/fake/__init__.py` line 60 — the fake is synchronous with no loop, so it accepts the keyword for ABC parity and discards it, matching the `del cancel_token` idiom the file already uses for `provision`:

```python
    def result(
        self, job_id: str, *, cancel_token: object | None = None
    ) -> Artifact:
        """Return a synthetic Artifact keyed off ``job_id``.

        Args:
            job_id: The job id returned by ``submit``.
            cancel_token: Ignored — there is no poll loop to interrupt.

        Returns:
            An ``Artifact`` with a filename derived from ``job_id``.
        """
        del cancel_token
        return Artifact(
            filename=f"fake-image-{job_id}.png",
            meta={"_kf_job_id": job_id, "_synthetic": True},
        )
```

- [ ] **Step 7: Pass the token from `KeyframeStage`**

In `src/kinoforge/pipeline/keyframe.py`, add a field to the dataclass (after `http_get_bytes`, before the Layer-4 sink fields):

```python
    cancel_token: CancelToken | None = None
```

with `from kinoforge.core.cancel import CancelToken` added under `TYPE_CHECKING` (or directly, matching the file's existing import style), and change the `result` call in `run()`:

```python
            artifact = self.image_backend.result(
                job_id, cancel_token=self.cancel_token
            )
```

Then, in `src/kinoforge/core/orchestrator.py`, pass the orchestrator's token when it constructs `KeyframeStage` (~line 2820):

```python
                state = KeyframeStage(
                    keyframe_cfg=cfg.keyframe,
                    image_engine=_kf_eng,
                    ...
                    cancel_token=cancel_token,
                ).run(state)
```

Keep the existing argument list; `cancel_token=cancel_token` is an addition. Do the same at `batch.py`'s `KeyframeStage(...)` construction if it builds one — check with:

```bash
rg -n 'KeyframeStage(' -F src/kinoforge
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `pixi run pytest tests/image_engines/ tests/pipeline/test_keyframe_stage.py -v`
Expected: PASS — the new cancellation tests plus all 45 pre-existing image-engine tests and all 15 keyframe-stage tests, **unmodified**.

- [ ] **Step 9: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/interfaces.py src/kinoforge/image_engines \
        src/kinoforge/pipeline/keyframe.py src/kinoforge/core/orchestrator.py \
        src/kinoforge/core/batch.py tests/image_engines/test_cancellation.py
git commit -m "fix(interfaces): ImageBackend.result could not be cancelled

ImageBackend.result took no cancel token, so a ~125 s Luma image poll ignored
the CLI's two-press SIGINT handler entirely — while
RemoteSubmitPollBackend.result underneath has honoured a token all along
(checks every iteration, waits on the token instead of sleeping). The
capability existed and the ABC had no parameter to reach it.

luma_agents and replicate now forward the token and get working cancellation
for free. fal's hand-rolled loop learns it, keeping the injected-sleep path for
callers that pass no token so existing fast-tick tests are unchanged. The fake
accepts and discards it for ABC parity.

KeyframeStage passes the orchestrator's token through, so the pre-existing
keyframe path improves rather than standing still.

The test asserts the POLL COUNT, not just that Cancelled was raised: a loop
that runs to completion and raises at the end passes an assert-raises test."
```

---

## Task 6: `generate_image()` and the t2i profile gate

**Goal:** The terminal image orchestration — resolve the stack, resolve the prompt, gate on the profile, submit, store, publish.

**Files:**
- Create: `src/kinoforge/core/image_run.py`
- Test: `tests/core/test_image_run.py` (create)

**Acceptance Criteria:**
- [ ] `generate_image(cfg, *, store, run_id, sink, namespace=None, image_engine=None, image_profile_provider=None, cancel_token=None) -> Artifact`
- [ ] Stores bytes at `store.put_bytes(run_id, "image.png", ...)`
- [ ] Publishes to the sink with `kind="image"` and `extension=".png"`, provider = image-engine registry name, model = `engine.model_identity(cfg_dict)`
- [ ] `sink=None` stores only and still returns the artifact
- [ ] Prompt precedence `--prompt` (passed in as `prompt_override`) > `cfg.image.prompt` > `cfg.prompt`; all-absent raises `ValidationError` naming both fixes
- [ ] Refuses with `ValidationError` when `"t2i" not in profile.supported_modes`, **before** `submit` is called
- [ ] Raises `ValidationError` when `cfg.image is None`
- [ ] Warns (not raises) when `model_identity` returns empty, and the published slug becomes `"unknown"`
- [ ] No `PipelineState`, no `GenerationRequest`, no `deploy_session` anywhere in the module

**Verify:** `pixi run pytest tests/core/test_image_run.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_image_run.py`:

```python
"""generate_image: the terminal image path (no pipeline, no compute)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact, CapabilityKey, ImageJob, ImageProfile

T2I_PROFILE = ImageProfile(
    name="fake-image", max_resolution=(1024, 1024), supported_modes={"t2i"}
)
VIDEO_ONLY_PROFILE = ImageProfile(
    name="wrong", max_resolution=(1024, 1024), supported_modes={"t2v"}
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-pixels"


def _cfg(**image_over: Any) -> Config:
    block = {"engine": "fake", "spec": {"model": "m"}, **image_over}
    return Config.model_validate({"image": block})


@dataclass
class _SpyBackend:
    """ImageBackend double recording whether submit was ever reached."""

    submits: list[ImageJob] = field(default_factory=list)

    def capabilities(self) -> ImageProfile:
        return T2I_PROFILE

    def inspect_capabilities(self) -> ImageProfile:
        return T2I_PROFILE

    def submit(self, job: ImageJob) -> str:
        self.submits.append(job)
        return "job-1"

    def result(self, job_id: str, *, cancel_token: Any = None) -> Artifact:
        return Artifact(filename="out.png", meta={"_synthetic": True})

    def endpoints(self) -> dict[str, str]:
        return {}


@dataclass
class _SpyEngine:
    """ImageEngine double with a controllable model_identity."""

    name: str = "fake"
    requires_compute: bool = False
    requires_local_weights: bool = False
    identity: str = "fake-model"
    backend_obj: _SpyBackend = field(default_factory=_SpyBackend)

    def provision(self, instance: Any, cfg: dict, *, cancel_token: Any = None) -> None:
        return None

    def backend(self, instance: Any, cfg: dict) -> _SpyBackend:
        return self.backend_obj

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        return T2I_PROFILE

    def validate_spec(self, job: ImageJob) -> None:
        if not job.prompt:
            raise ValidationError("spy: prompt is empty")

    def model_identity(self, cfg: dict) -> str:
        return self.identity


@dataclass
class _StubProvider:
    profile: ImageProfile = T2I_PROFILE

    def resolve(self, key: CapabilityKey) -> ImageProfile:
        return self.profile

    def discover(self, key: CapabilityKey, engine: Any, backend: Any) -> ImageProfile:
        return self.profile

    def verify(self, *a: Any, **k: Any) -> None:
        return None


@dataclass
class _SpySink:
    published: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, data: bytes, **kwargs: Any) -> str:
        self.published.append({"bytes": data, **kwargs})
        return f"/out/{kwargs.get('kind')}.png"


def _store(tmp_path: Any) -> Any:
    from kinoforge.stores.local import LocalArtifactStore

    return LocalArtifactStore(root=tmp_path)


def test_happy_path_stores_and_publishes(tmp_path: Any) -> None:
    """One prompt in, one stored PNG and one published PNG out.

    Bug this catches: publishing without storing (so `kinoforge gc` can never
    see the artifact) or storing without publishing (so the operator never
    gets a file).
    """
    from kinoforge.core.image_run import generate_image

    engine, sink = _SpyEngine(), _SpySink()
    artifact = generate_image(
        _cfg(prompt="a cat"),
        store=_store(tmp_path),
        run_id="image-20261003-120000",
        sink=sink,
        image_engine=engine,
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    assert artifact is not None
    assert len(sink.published) == 1
    published = sink.published[0]
    assert published["kind"] == "image"
    assert published["extension"] == ".png"
    assert published["provider"] == "fake"
    assert published["model"] == "fake-model"
    assert published["bytes"] == PNG


def test_sink_none_stores_only(tmp_path: Any) -> None:
    """--no-output-dir must still produce a stored artifact.

    Bug this catches: an unguarded self.sink.publish, which would make
    --no-output-dir crash with AttributeError on None.
    """
    from kinoforge.core.image_run import generate_image

    artifact = generate_image(
        _cfg(prompt="a cat"),
        store=_store(tmp_path),
        run_id="image-1",
        sink=None,
        image_engine=_SpyEngine(),
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    assert artifact is not None


@pytest.mark.parametrize(
    ("override", "block_prompt", "top_prompt", "expected"),
    [
        ("from-cli", "from-block", "from-top", "from-cli"),
        (None, "from-block", "from-top", "from-block"),
        (None, None, "from-top", "from-top"),
    ],
)
def test_prompt_precedence(
    tmp_path: Any,
    override: str | None,
    block_prompt: str | None,
    top_prompt: str | None,
    expected: str,
) -> None:
    """--prompt > cfg.image.prompt > cfg.prompt.

    Bug this catches: the precedence inverted so a config default silently
    overrides an explicit --prompt — the operator would be billed for an image
    of the wrong thing, with no error anywhere.
    """
    from kinoforge.core.image_run import generate_image

    data: dict[str, Any] = {"image": {"engine": "fake", "spec": {"model": "m"}}}
    if block_prompt is not None:
        data["image"]["prompt"] = block_prompt
    if top_prompt is not None:
        data["prompt"] = top_prompt

    engine = _SpyEngine()
    generate_image(
        Config.model_validate(data),
        store=_store(tmp_path),
        run_id="image-1",
        sink=None,
        prompt_override=override,
        image_engine=engine,
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    assert engine.backend_obj.submits[0].prompt == expected


def test_no_prompt_anywhere_is_refused_naming_both_fixes(tmp_path: Any) -> None:
    """A run with no resolvable prompt fails loudly at preflight.

    Bug this catches: submitting an empty prompt and paying the provider for
    whatever it decides that means.
    """
    from kinoforge.core.image_run import generate_image

    engine = _SpyEngine()
    with pytest.raises(ValidationError, match="--prompt"):
        generate_image(
            _cfg(),
            store=_store(tmp_path),
            run_id="image-1",
            sink=None,
            image_engine=engine,
            image_profile_provider=_StubProvider(),
            http_get_bytes=lambda url, headers: PNG,
        )
    assert engine.backend_obj.submits == []


def test_mode_gate_refuses_before_submit(tmp_path: Any) -> None:
    """A profile that does not support t2i stops the run BEFORE spending.

    Bug this catches: validating after submit, which still bills the provider.
    Asserting only pytest.raises would pass against that ordering, so the
    submits list is the real assertion here.
    """
    from kinoforge.core.image_run import generate_image

    engine = _SpyEngine()
    with pytest.raises(ValidationError, match="t2i"):
        generate_image(
            _cfg(prompt="a cat"),
            store=_store(tmp_path),
            run_id="image-1",
            sink=None,
            image_engine=engine,
            image_profile_provider=_StubProvider(profile=VIDEO_ONLY_PROFILE),
            http_get_bytes=lambda url, headers: PNG,
        )
    assert engine.backend_obj.submits == [], "submitted despite an unsupported mode"


def test_missing_image_block_is_refused(tmp_path: Any) -> None:
    """generate_image requires cfg.image.

    Bug this catches: an AttributeError on None deep in the function instead of
    a message telling the operator their config has no `image:` block.
    """
    from kinoforge.core.image_run import generate_image

    cfg = Config.model_validate(
        {
            "engine": {"kind": "fake", "precision": ""},
            "models": [{"kind": "base", "ref": "hf:a/b", "target": "checkpoints"}],
        }
    )
    with pytest.raises(ValidationError, match="image:"):
        generate_image(
            cfg,
            store=_store(tmp_path),
            run_id="image-1",
            sink=None,
            image_profile_provider=_StubProvider(),
        )


def test_empty_model_identity_warns_and_slugs_to_unknown(
    tmp_path: Any, caplog: Any
) -> None:
    """The successful-generations 17 trap: an empty identity became `_fal_unknown_`.

    Bug this catches: silently publishing `..._unknown_...` filenames, which is
    exactly what shipped for two keyframes in entry 17 and was invisible until
    a live run produced the files.
    """
    import logging

    from kinoforge.core.image_run import generate_image

    sink = _SpySink()
    with caplog.at_level(logging.WARNING):
        generate_image(
            _cfg(prompt="a cat"),
            store=_store(tmp_path),
            run_id="image-1",
            sink=sink,
            image_engine=_SpyEngine(identity=""),
            image_profile_provider=_StubProvider(),
            http_get_bytes=lambda url, headers: PNG,
        )
    # generate_image forwards the empty identity verbatim; LocalOutputSink is
    # what substitutes "unknown" (outputs/local.py), so the spy sees "".
    assert sink.published[0]["model"] == ""
    # NB getMessage(), not .message — LogRecord.message only exists once the
    # record has been formatted, so `r.message` raises AttributeError here.
    assert any(
        "model_identity" in r.getMessage() for r in caplog.records
    ), "no warning emitted for an empty model_identity"


def test_module_has_no_pipeline_machinery() -> None:
    """Structural guard: the design's central claim about this module.

    Bug this catches: reaching for PipelineState / deploy_session out of habit,
    which drags the dummy GenerationRequest(prompt="", mode="upscale") wart at
    orchestrator.py:2792 into a path that has nothing to chain.
    """
    from pathlib import Path

    import kinoforge.core.image_run as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("PipelineState", "deploy_session", "GenerationRequest"):
        assert forbidden not in source, (
            f"core/image_run.py references {forbidden}; the terminal image path "
            f"has nothing to chain and must not carry pipeline machinery"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_image_run.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kinoforge.core.image_run'`

- [ ] **Step 3: Write the implementation**

Create `src/kinoforge/core/image_run.py`:

```python
"""Terminal image generation: one prompt in, one artifact out.

No ``PipelineState``, no ``GenerationRequest``, no ``deploy_session``, no ledger
row and no heartbeat — every image engine declares ``requires_compute = False``,
so all of that is dead weight. ``PipelineState`` exists to CHAIN video stages;
a terminal image has nothing to chain, so it needs no state object to chain
through. Image-then-upscale would be file hand-off between two commands, the way
the existing video chains work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from kinoforge.core.errors import ValidationError
from kinoforge.core.image_stack import resolve_image_stack
from kinoforge.core.interfaces import ImageJob
from kinoforge.pipeline.artifact_bytes import artifact_bytes

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import (
        Artifact,
        ImageEngine,
        ImageProfileProvider,
    )
    from kinoforge.outputs.base import OutputSink
    from kinoforge.stores.base import ArtifactStore

logger = logging.getLogger(__name__)

_MODE = "t2i"
_STORE_FILENAME = "image.png"


def generate_image(
    cfg: "Config",
    *,
    store: "ArtifactStore",
    run_id: str,
    sink: "OutputSink | None",
    namespace: str | None = None,
    prompt_override: str | None = None,
    image_engine: "ImageEngine | None" = None,
    image_profile_provider: "ImageProfileProvider | None" = None,
    cancel_token: "CancelToken | None" = None,
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None,
) -> "Artifact":
    """Generate one image from ``cfg.image`` and publish it.

    Args:
        cfg: Loaded config carrying an ``image:`` block.
        store: Artifact store for the internal copy.
        run_id: Run identifier namespacing the stored artifact.
        sink: User-facing output sink, or ``None`` when publishing is disabled.
        namespace: Optional sink subdirectory.
        prompt_override: The CLI ``--prompt`` value; wins over config prompts.
        image_engine: Pre-constructed engine (test injection).
        image_profile_provider: Profile cache (test injection).
        cancel_token: Honoured across the engine's poll loop.
        http_get_bytes: Injectable HTTP GET seam for fetching the image bytes.

    Returns:
        The stored :class:`Artifact`.

    Raises:
        ValidationError: No ``image:`` block, no resolvable prompt, or the
            resolved profile does not support ``t2i``.
        UnknownAdapter: ``cfg.image.engine`` is not a registered image engine.
    """
    block = cfg.image
    if block is None:
        raise ValidationError(
            "generate_image requires an `image:` block in the config; "
            "this config has none"
        )

    prompt = _resolve_prompt(cfg, prompt_override)

    engine, backend, profile = resolve_image_stack(
        block,
        store=store,
        image_engine=image_engine,
        image_profile_provider=image_profile_provider,
    )

    # Gate BEFORE submit. This is the one consumer ImageProfile has ever had:
    # pipeline/keyframe.py holds the field and never reads it, and every
    # engine's validate_spec checks only spec.model plus a non-empty prompt.
    # Mirrors core/validation.py's mode check on the video side.
    if _MODE not in profile.supported_modes:
        raise ValidationError(
            f"image engine {block.engine!r} model "
            f"{block.spec.get('model', '?')!r} does not support {_MODE!r}; "
            f"profile {profile.name!r} supports: "
            f"{sorted(profile.supported_modes)}"
        )

    cfg_dict = block.model_dump()
    job = ImageJob(spec=block.spec, prompt=prompt, params=block.params)
    engine.validate_spec(job)

    job_id = backend.submit(job)
    artifact = backend.result(job_id, cancel_token=cancel_token)
    png_bytes = artifact_bytes(artifact, http_get_bytes)

    # kinoforge:public-name — a fixed identifier, not prompt-derived.
    stored = store.put_bytes(run_id, _STORE_FILENAME, png_bytes)

    if sink is not None:
        model = engine.model_identity(cfg_dict)
        if not model:
            logger.warning(
                "image engine %r returned an empty model_identity; the "
                "published filename will render its model slug as 'unknown' "
                "(see successful-generations.md entry 17)",
                block.engine,
            )
        sink.publish(
            png_bytes,
            prompt=prompt,
            extension=".png",
            namespace=namespace,
            provider=engine.name,
            model=model,
            kind="image",
        )
    return stored


def _resolve_prompt(cfg: "Config", prompt_override: str | None) -> str:
    """Resolve the effective prompt: CLI > ``image.prompt`` > top-level.

    CLI-over-config matches ``upscale --scale`` overriding ``cfg.upscale.scale``
    and ``interpolate --fps`` overriding ``cfg.interpolate.fps``.

    Args:
        cfg: The loaded config.
        prompt_override: The CLI-supplied prompt, if any.

    Returns:
        The non-empty prompt to submit.

    Raises:
        ValidationError: None of the three sources supplied a prompt.
    """
    block = cfg.image
    for candidate in (
        prompt_override,
        block.prompt if block is not None else None,
        cfg.prompt,
    ):
        if candidate and candidate.strip():
            return candidate
    raise ValidationError(
        "no prompt to generate from: pass --prompt, or set `image.prompt` "
        "(or top-level `prompt:`) in the config"
    )
```

Note `store.put_bytes` returns the stored artifact — confirm its return type and
adjust the `-> Artifact` annotation if it differs:

```bash
rg -n -A6 'def put_bytes' src/kinoforge/stores/base.py
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_image_run.py -v`
Expected: PASS — all tests, including the three prompt-precedence cases, the
before-submit gate assertion and the structural guard.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/image_run.py tests/core/test_image_run.py
git commit -m "feat(core): generate_image — the terminal image path

A plain function, not a pipeline stage: no PipelineState, no GenerationRequest,
no deploy_session, no ledger, no heartbeat. Every image engine declares
requires_compute=False, so all of that is dead weight, and avoiding
PipelineState also avoids replicating the dummy
GenerationRequest(prompt=\"\", mode=\"upscale\") wart at orchestrator.py:2792 —
a terminal image has nothing to chain.

Gives ImageProfile its first consumer anywhere in the tree: t2i is checked
against profile.supported_modes BEFORE submit, mirroring core/validation.py on
the video side. pipeline/keyframe.py has held that field since Layer R with a
'reserved for future spec validation' comment and never read it.

max_resolution stays unenforced on purpose: fal takes image_size, Luma takes
aspect_ratio, and params is an opaque pass-through, so a generic width/height
check is not expressible without a normalisation layer this does not add.

The mode-gate test asserts submit was never CALLED, not merely that it raised —
an engine that submits first and validates after would pass the weak version."
```

---

## Task 7: `--ephemeral` refused deliberately for image configs

**Goal:** Replace today's accidental refusal (an `("", None)` key miss) with an explicit, tested decision keyed on the image-engine name.

**Files:**
- Modify: `src/kinoforge/core/ephemeral.py` (add `IMAGE_EPHEMERAL_CAPABILITIES` near `EPHEMERAL_CAPABILITIES` at `:95`)
- Modify: `src/kinoforge/cli/_main.py:244-280` (`_preflight_error_block`, `_preflight_ephemeral`)
- Test: `tests/cli/test_cmd_image.py` (create — extended again in Task 8)

**Acceptance Criteria:**
- [ ] `IMAGE_EPHEMERAL_CAPABILITIES` maps each of `fake`/`fal`/`luma_agents`/`replicate` to a bool; only `fake` is `True`
- [ ] `_preflight_ephemeral` branches on `cfg.image is not None` and consults the image table
- [ ] The refusal names the **image** engine and gives a reason specific to it, not the video table's "engine: replicate / engine: runway" advice
- [ ] All three live image engines are refused; the refusal is table-driven in the test, not a single case
- [ ] A video config's ephemeral behaviour is unchanged — existing ephemeral tests unmodified

**Verify:** `pixi run pytest tests/cli/test_cmd_image.py tests/core/test_ephemeral.py -v` → all pass

**Steps:**

- [ ] **Step 1: Find the existing ephemeral test module name**

```bash
rg -l 'EPHEMERAL_CAPABILITIES' tests/
```

Use whatever file that returns in the Verify command instead of `tests/core/test_ephemeral.py` if the name differs.

- [ ] **Step 2: Write the failing test**

Create `tests/cli/test_cmd_image.py`:

```python
"""kinoforge image: the --ephemeral contract (Task 7) and the CLI (Task 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

IMAGE_CFG_YAML = """\
mode: t2i
prompt: "a cat in a meadow"

image:
  engine: {engine}
  spec:
    model: "{model}"

output:
  dir: out
"""


def _write_cfg(tmp_path: Path, engine: str, model: str = "m") -> Path:
    path = tmp_path / f"{engine}-t2i.yaml"
    path.write_text(IMAGE_CFG_YAML.format(engine=engine, model=model))
    return path


@pytest.mark.parametrize("engine", ["fal", "luma_agents", "replicate"])
def test_ephemeral_is_refused_for_every_live_image_engine(
    tmp_path: Path, engine: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusal is a DECISION, keyed on the image engine, not a key miss.

    Bug this catches: the refusal drifting back to the accidental ("", None)
    lookup miss it is today — which would silently start ALLOWING --ephemeral
    the moment anyone gave Config.engine a non-None default, with no provider
    scrub hook anywhere to honour it.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, engine)
    rc = main(["--ephemeral", "image", "-c", str(cfg), "--prompt", "x", "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--ephemeral" in err
    assert engine in err


def test_ephemeral_refusal_names_the_image_engine_not_video_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message must not offer the video table's remedies.

    Bug this catches: reusing _preflight_error_block verbatim, which tells the
    operator to switch to `engine: replicate` or `engine: runway` — neither of
    which is an IMAGE engine, so the advice is nonsense on this path.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, "luma_agents", model="uni-1")
    main(["--ephemeral", "image", "-c", str(cfg), "--prompt", "x", "--dry-run"])
    err = capsys.readouterr().err
    assert "engine: runway" not in err
    assert "luma_agents" in err


def test_fake_image_engine_permits_ephemeral(tmp_path: Path) -> None:
    """The in-process fake has no provider-side state, so it is trivially safe.

    Bug this catches: a blanket refusal for every image config, which would
    make the ephemeral path untestable offline.
    """
    from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

    assert IMAGE_EPHEMERAL_CAPABILITIES["fake"] is True
    assert IMAGE_EPHEMERAL_CAPABILITIES["fal"] is False
    assert IMAGE_EPHEMERAL_CAPABILITIES["luma_agents"] is False
    assert IMAGE_EPHEMERAL_CAPABILITIES["replicate"] is False


def test_image_table_covers_every_registered_image_engine() -> None:
    """Guard the guard: a new image engine must not default to "allowed".

    Bug this catches: adding a fifth image engine and forgetting the table, so
    `.get()` returns None and the operator gets a confusing refusal — or worse,
    a permissive default if the lookup is ever written with `.get(name, True)`.
    """
    import kinoforge._adapters  # noqa: F401  — self-registration
    from kinoforge.core import registry
    from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

    registered = set(registry._image_engines)
    assert registered, "no image engines registered; the assertion below is vacuous"
    missing = registered - set(IMAGE_EPHEMERAL_CAPABILITIES)
    assert not missing, (
        f"image engines with no IMAGE_EPHEMERAL_CAPABILITIES entry: "
        f"{sorted(missing)}"
    )
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pixi run pytest tests/cli/test_cmd_image.py -v`
Expected: FAIL — `ImportError: cannot import name 'IMAGE_EPHEMERAL_CAPABILITIES'`, and the CLI tests fail because the `image` subcommand does not exist yet (Task 8 adds it; these two tests go green together with Task 8's work, so run this file again at the end of Task 8).

- [ ] **Step 4: Add the table**

In `src/kinoforge/core/ephemeral.py`, immediately after `EPHEMERAL_CAPABILITIES` (line 95-113):

```python
# Image engines get their OWN table rather than rows in the dict above,
# because the two registries are independent namespaces: registry.py's
# register_image_engine docstring states names may legitimately collide with
# video-engine names, and "fake" already does. Keying both capability questions
# off one bare-name table would be a latent bug the moment fal-video and
# fal-image diverge on scrub support.
#
# Every value here is False except the in-process fake, and that is the honest
# state of the world, not caution: no image engine implements provider-side
# record deletion, and two of three cannot. Luma's agents API has no DELETE
# endpoint at all (successful-generations.md 15 — records purge via the
# dashboard). Replicate's predictions API does support deletion, but the IMAGE
# engine implements no scrub hook; flip its entry when one exists AND has been
# live-proven.
IMAGE_EPHEMERAL_CAPABILITIES: dict[str, bool] = {
    "fake": True,
    "fal": False,
    "luma_agents": False,
    "replicate": False,
}
```

- [ ] **Step 5: Branch the preflight**

In `src/kinoforge/cli/_main.py`, add an image-specific error block next to `_preflight_error_block`:

```python
def _preflight_image_error_block(engine: str) -> str:
    return (
        "ERROR: --ephemeral is not supported for this image configuration.\n"
        f"  image engine:  {engine}\n"
        f"  reason:        {engine} has no provider-side record-delete hook in "
        "kinoforge.\n"
        "\n"
        "  No image engine implements record deletion today, and two of the\n"
        "  three hosted ones cannot: Luma's agents API has no DELETE endpoint\n"
        "  (records purge via the dashboard) and fal exposes no delete path.\n"
        "\n"
        "  Drop --ephemeral to allow provider-side record retention."
    )
```

and branch at the top of `_preflight_ephemeral`, before the existing `engine_kind` line:

```python
    cfg = ctx.cfg
    if cfg is None:
        return None
    # Image configs carry no video engine at all, so the (engine, provider)
    # table below cannot answer for them — today they are refused only because
    # `cfg.engine.kind if cfg.engine else ""` misses on ("", None), which is an
    # accident rather than a decision. Answer deliberately instead.
    if cfg.image is not None:
        from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

        if IMAGE_EPHEMERAL_CAPABILITIES.get(cfg.image.engine, False):
            return None
        return _preflight_image_error_block(cfg.image.engine)

    engine_kind = cfg.engine.kind if cfg.engine else ""
```

Note the explicit `False` default: an unregistered or newly added image engine is refused, never silently permitted.

- [ ] **Step 6: Run the tests**

Run: `pixi run pytest tests/cli/test_cmd_image.py -v -k "fake_image_engine or table_covers"`
Expected: PASS for these two. The three CLI-invoking tests still fail until Task 8 adds the subcommand — that is expected and they are re-run there.

Run: `pixi run pytest tests/ -q -k ephemeral`
Expected: PASS, pre-existing ephemeral tests unmodified.

- [ ] **Step 7: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/ephemeral.py src/kinoforge/cli/_main.py \
        tests/cli/test_cmd_image.py
git commit -m "feat(ephemeral): refuse --ephemeral for image configs deliberately

Today an image config is refused only because _preflight_ephemeral reads
cfg.engine.kind if cfg.engine else \"\", so the lookup key becomes (\"\", None)
and misses. Right outcome, wrong mechanism — and nothing tested it, so it would
start silently ALLOWING --ephemeral the moment Config.engine got a non-None
default, with no scrub hook anywhere to honour it.

IMAGE_EPHEMERAL_CAPABILITIES is a SEPARATE table because the image and video
registries are independent namespaces (registry.py:244 — names may collide, and
'fake' already does). One bare-name table would be a latent bug the moment
fal-video and fal-image diverge.

The refusal also gets its own message: the video block advises switching to
engine: replicate or engine: runway, neither of which is an image engine.

Lookup defaults to False, so a fifth image engine is refused rather than
silently permitted, and a test asserts every registered engine has an entry."
```

---
## Task 8: Wire the `image` subcommand

**Goal:** `kinoforge image` parses, dispatches, is interruptible, and spends nothing on `--dry-run`.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` — subparser (near the `upscale` block at `:670`), `_DISPATCH` (`:147`), `_INTERRUPTIBLE_CMDS` (`:95`)
- Modify: `src/kinoforge/cli/_commands.py` — add `_cmd_image`; extract `_resolve_run_id`
- Test: `tests/cli/test_cmd_image.py` (extend from Task 7)
- Test (must stay unmodified and green): `tests/cli/test_session_global_flag_positions.py`

**Acceptance Criteria:**
- [ ] `kinoforge image -c CFG --prompt TEXT` parses; `--prompt` is **optional**
- [ ] `--output-dir` and `--no-output-dir` are mutually exclusive
- [ ] `"image"` is in `_DISPATCH` and in `_INTERRUPTIBLE_CMDS`
- [ ] `image` accepts all five session-globals in subcommand position (the U27 contract), and the matrix test picks the node up with that file unmodified
- [ ] `--dry-run` exits 0 and makes **zero** HTTP calls
- [ ] Missing `image:` block exits 2; unresolvable prompt exits 2; `Cancelled` exits 1
- [ ] `_resolve_run_id(args, prefix)` replaces the duplicated derivation at `_commands.py:955`, `:1120`, `:1237` and is used by `_cmd_image`

**Verify:** `pixi run pytest tests/cli/ -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_cmd_image.py`:

```python
def test_image_inherits_every_session_global() -> None:
    """The U27 contract: every leaf subcommand accepts all five session-globals.

    Asserted against the real parser rather than by importing the matrix test
    module (which would require tests/cli/__init__.py to exist). The matrix
    enumerates _build_parser() itself, so `image` joins it automatically — this
    test states the property that matters directly.

    Bug this catches: adding the subcommand somewhere the recursive propagation
    pass does not reach, which is the U27/U29 defect — a subcommand that runs
    non-ephemerally while reporting success.
    """
    from kinoforge.cli._main import _build_parser

    for flag, dest, value in (
        ("--state-dir", "state_dir", "/probe/state"),
        ("--env-file", "env_file", "/probe/env"),
        ("--vault", "vault", "/probe/vault.yaml"),
        ("--ephemeral", "ephemeral", True),
        ("--debug-show-secrets", "debug_show_secrets", True),
    ):
        argv = ["image", "-c", "cfg.yaml", flag]
        if value is not True:
            argv.append(str(value))
        args = _build_parser().parse_args(argv)
        assert getattr(args, dest) == value, (
            f"{flag} in subcommand position did not reach args.{dest} for "
            f"`image` — the session-global propagation missed this node"
        )


def test_image_is_interruptible() -> None:
    """Without this the SIGINT handler is never installed for `image`.

    Bug this catches: shipping Task 5's cancellation fix with no way to reach
    it — a ~125 s Luma poll would still ignore Ctrl-C because main() only
    installs the handler for commands in _INTERRUPTIBLE_CMDS.
    """
    from kinoforge.cli._main import _DISPATCH, _INTERRUPTIBLE_CMDS

    assert "image" in _DISPATCH
    assert "image" in _INTERRUPTIBLE_CMDS


def test_prompt_is_optional_at_the_parser() -> None:
    """A config carrying image.prompt must be runnable with no --prompt.

    Bug this catches: copying `generate`'s required=True --prompt, which would
    make every self-contained image config un-runnable as written.
    """
    from kinoforge.cli._main import _build_parser

    args = _build_parser().parse_args(["image", "-c", "cfg.yaml"])
    assert args.prompt is None


def test_output_dir_flags_are_mutually_exclusive() -> None:
    """Matches `generate`'s surface.

    Bug this catches: accepting both, leaving it ambiguous whether the operator
    wanted a custom directory or no publish at all.
    """
    from kinoforge.cli._main import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            ["image", "-c", "c.yaml", "--output-dir", "x", "--no-output-dir"]
        )


def test_dry_run_exits_zero_and_makes_no_http_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run must not touch the network.

    Bug this catches: a dry run that resolves the profile by LIVE-probing the
    engine — which costs money and defeats the flag's whole purpose. Asserting
    exit 0 alone would pass against that.
    """
    import urllib.request

    from kinoforge.cli._main import main

    calls: list[str] = []

    def _boom(*a: object, **k: object) -> object:
        calls.append("urlopen")
        raise AssertionError("dry-run made an HTTP call")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    cfg = _write_cfg(tmp_path, "fake")
    rc = main(["image", "-c", str(cfg), "--prompt", "a cat", "--dry-run"])
    assert rc == 0
    assert calls == []


def test_missing_image_block_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A video config handed to `image` is a precondition error, not a crash.

    Bug this catches: an AttributeError traceback instead of a message naming
    the missing block.
    """
    from kinoforge.cli._main import main

    cfg = tmp_path / "video.yaml"
    cfg.write_text(
        "engine:\n  kind: fake\n  precision: ''\n"
        "models:\n  - kind: base\n    ref: 'hf:a/b'\n    target: checkpoints\n"
    )
    rc = main(["image", "-c", str(cfg), "--prompt", "x"])
    assert rc == 2
    assert "image:" in capsys.readouterr().err


def test_no_prompt_anywhere_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preflight refusal surfaces as exit 2, not a traceback.

    Bug this catches: letting ValidationError escape main() as an unhandled
    exception, which exits 1 with a stack trace instead of 2 with the fix.
    """
    from kinoforge.cli._main import main

    cfg = tmp_path / "noprompt.yaml"
    cfg.write_text(
        "image:\n  engine: fake\n  spec:\n    model: 'm'\noutput:\n  dir: out\n"
    )
    rc = main(["image", "-c", str(cfg)])
    assert rc == 2
    assert "--prompt" in capsys.readouterr().err


def test_resolve_run_id_prefixes_and_honours_the_flag() -> None:
    """The extracted helper replaces three copies of the same derivation.

    Bug this catches: the four copies drifting — e.g. one of them forgetting to
    honour --run-id, which silently ignores an operator's explicit id.
    """
    import argparse

    from kinoforge.cli._commands import _resolve_run_id

    explicit = argparse.Namespace(run_id="my-run")
    assert _resolve_run_id(explicit, "image") == "my-run"

    derived = _resolve_run_id(argparse.Namespace(run_id=None), "image")
    assert derived.startswith("image-")
    assert len(derived) == len("image-") + 15  # YYYYmmdd-HHMMSS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/cli/test_cmd_image.py -v`
Expected: FAIL — `argparse` errors ("invalid choice: 'image'"), `"image" not in _DISPATCH`, and `ImportError` for `_resolve_run_id`.

- [ ] **Step 3: Add the subparser**

In `src/kinoforge/cli/_main.py`, inside `_build_parser`, immediately after the `interpolate` block (which ends around line 740) and before `# list`:

```python
    # image — terminal image generation. No compute flags: every image engine
    # declares requires_compute=False, so there is no pod to reuse or attach.
    p_image = sub.add_parser(
        "image", help="generate a single image from a prompt (no compute)"
    )
    p_image.add_argument("-c", "--config", required=True, metavar="PATH")
    p_image.add_argument(
        "--prompt",
        default=None,
        metavar="TEXT",
        help=(
            "prompt text; overrides cfg.image.prompt, which overrides the "
            "top-level cfg.prompt. Optional — a config carrying a prompt runs "
            "without it."
        ),
    )
    p_image_output = p_image.add_mutually_exclusive_group()
    p_image_output.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="user-facing output directory (overrides cfg.output.dir)",
    )
    p_image_output.add_argument(
        "--no-output-dir",
        action="store_true",
        help="disable user-facing publish; the image remains only in the store",
    )
    p_image.add_argument("--run-id", default=None, metavar="ID")
    p_image.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="emit the resolved plan to stdout and exit 0; no provider call",
    )
```

Add `"image": _cmd_image,` to `_DISPATCH` (after `"interpolate"`), import `_cmd_image` alongside the other `_cmd_*` imports, and add `"image"` to `_INTERRUPTIBLE_CMDS`:

```python
_INTERRUPTIBLE_CMDS: frozenset[str] = frozenset(
    {"generate", "batch", "upscale", "interpolate", "image"}
)
```

`_propagate_session_globals` needs **no** edit: U27's fix made it walk the parser tree recursively with no exception list, so the new node inherits all five session-globals automatically, and `_leaf_paths()` in the matrix test enumerates the real parser so `("image",)` is picked up without touching that file.

- [ ] **Step 4: Extract `_resolve_run_id` and add `_cmd_image`**

In `src/kinoforge/cli/_commands.py`, add the helper near `_build_sink`:

```python
def _resolve_run_id(args: argparse.Namespace, prefix: str) -> str:
    """Return the explicit ``--run-id`` or derive ``<prefix>-<local timestamp>``.

    Extracted from the three identical derivations in ``_cmd_generate``,
    ``_cmd_upscale`` and ``_cmd_interpolate``; ``_cmd_image`` would have been a
    fourth copy.

    Args:
        args: Parsed CLI arguments; ``run_id`` may be absent or ``None``.
        prefix: Run-kind prefix, e.g. ``"run"``, ``"upscale"``, ``"image"``.

    Returns:
        The run identifier.
    """
    explicit = getattr(args, "run_id", None)
    if explicit is not None:
        return str(explicit)
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
```

Then replace the three existing derivations (`:955` uses prefix `run`, `:1120` uses `upscale`, `:1237` uses `interpolate`) with calls to it, and add the handler:

```python
def _cmd_image(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``image`` subcommand — terminal image generation, no compute.

    Every image engine declares ``requires_compute = False``, so there is no
    provider, no ledger row and no lifecycle here: the handler resolves the
    sink and run id, then hands off to
    :func:`kinoforge.core.image_run.generate_image`.
    """
    from kinoforge.core.errors import Cancelled, KinoforgeError, ValidationError
    from kinoforge.core.image_run import generate_image

    if ctx.cfg is None:
        print("error: --config required for image", file=sys.stderr)
        return 2
    cfg = ctx.cfg
    if cfg.image is None:
        print(
            "error: --config must contain an `image:` block; "
            "see examples/configs/luma-uni1-t2i.yaml",
            file=sys.stderr,
        )
        return 2

    sink = _build_sink(cfg, args)
    run_id = _resolve_run_id(args, "image")

    if getattr(args, "dry_run", False):
        # Resolve the prompt only — no registry construction, no profile
        # resolution, no provider call. A dry run must cost nothing.
        prompt = (
            getattr(args, "prompt", None) or cfg.image.prompt or cfg.prompt or ""
        )
        if not prompt.strip():
            print(
                "error: no prompt to generate from: pass --prompt, or set "
                "`image.prompt` (or top-level `prompt:`) in the config",
                file=sys.stderr,
            )
            return 2
        print(f"[dry-run] engine={cfg.image.engine}")
        print(f"[dry-run] model={cfg.image.spec.get('model', '(unset)')}")
        print(f"[dry-run] params={cfg.image.params or '{}'}")
        print(f"[dry-run] prompt={prompt[:80]!r}")
        print(f"[dry-run] run_id={run_id}")
        print(f"[dry-run] sink={'disabled' if sink is None else 'enabled'}")
        return 0

    store = _build_store(cfg, ctx.state_dir)
    try:
        artifact = generate_image(
            cfg,
            store=store,
            run_id=run_id,
            sink=sink,
            prompt_override=getattr(args, "prompt", None),
            cancel_token=ctx.cancel_token,
        )
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Cancelled:
        print("image: cancelled", file=sys.stderr)
        return 1
    except KinoforgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"image completed — artifact {artifact.filename}")
    return 0
```

Confirm `ctx.cancel_token` is the right attribute name (`_main.py` passes `ctx.cancel_token` to `_install_sigint_handler`) and that `datetime` is already imported in `_commands.py`:

```bash
rg -n 'cancel_token' src/kinoforge/cli/context.py
rg -n '^from datetime|^import datetime' src/kinoforge/cli/_commands.py
```

Also add `_cmd_image` to `_commands.py`'s `__all__` if that module maintains one, and to `_main.py`'s import list.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pixi run pytest tests/cli/test_cmd_image.py -v`
Expected: PASS — including Task 7's three CLI-invoking ephemeral tests, which go green here.

Run: `pixi run pytest tests/cli/ -v`
Expected: PASS. `tests/cli/test_session_global_flag_positions.py` is **unmodified** and its parametrized case count has GROWN by 10 (2 test functions × 5 session-globals × the one new leaf) — that growth is the proof `image` entered the matrix, not a regression.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py \
        tests/cli/test_cmd_image.py
git commit -m "feat(cli): add the image subcommand

A sibling of upscale/interpolate, with no compute flags: every image engine
declares requires_compute=False, so --no-reuse / --attach-pod / --instance-id
would all be meaningless. --prompt is OPTIONAL and overrides cfg.image.prompt,
which overrides cfg.prompt — CLI-over-config, matching upscale --scale and
interpolate --fps.

image joins _INTERRUPTIBLE_CMDS, which is what makes the cancellation fix
reachable at all: main() installs the SIGINT handler only for commands in that
set, so without this the widened ImageBackend.result would have had no caller
passing a live token.

_propagate_session_globals needs no edit — U27's recursive no-exception-list
pass covers the new node automatically, and the matrix test enumerates the real
parser, so ('image',) is picked up with that file unmodified.

_resolve_run_id replaces three identical copies of the same derivation
(_cmd_generate, _cmd_upscale, _cmd_interpolate); image would have been a
fourth."
```

---

## Task 9: Example configs and documentation

**Goal:** Two runnable example configs plus the docs an operator reads to find this command.

**Files:**
- Create: `examples/configs/luma-uni1-t2i.yaml`
- Create: `examples/configs/fal-flux-schnell-t2i.yaml`
- Modify: `docs/configuration.md`, `docs/engines.md`, `README.md`, `PROGRESS.md`
- Test: `tests/test_examples.py` (unmodified; it discovers configs)

**Acceptance Criteria:**
- [ ] Both configs load and validate via `tests/test_examples.py` with that file unmodified
- [ ] Both carry **only** allowlisted keys (`mode`, `prompt`, `image`, `store`, `output`)
- [ ] `luma-uni1-t2i.yaml` pins `uni-1` with a comment recording the `uni-1-max` deferral
- [ ] `pixi run kinoforge image -c <either> --prompt x --dry-run` exits 0
- [ ] `docs/configuration.md` documents the `image:` block and names every refused key
- [ ] `docs/engines.md` describes image engines as a terminal path, not only a keyframe head
- [ ] README gains a usage example

**Verify:** `pixi run pytest tests/test_examples.py -v && pixi run kinoforge image -c examples/configs/fal-flux-schnell-t2i.yaml --prompt "a cat" --dry-run` → tests pass, dry-run exits 0

**Steps:**

- [ ] **Step 1: Write the configs**

`examples/configs/luma-uni1-t2i.yaml`:

```yaml
# Standalone image generation via Luma's agents API (UNI-1).
#
# `kinoforge image` is a terminal command: one prompt in, one PNG out. There is
# no compute block because every image engine is hosted (requires_compute =
# False) — no pod, no warm reuse, no lifecycle budget.
#
# Targets agents.lumalabs.ai/v1 (verified 2026-07-03 against
# docs.agents.lumalabs.ai). The retired dream-machine surface 403s for platform
# keys. Set the Luma platform API key in the environment before running.
#
# MODEL CHOICE: pinned to uni-1. The 2026-07-04 keyframe matrix
# (successful-generations.md 18) found uni-1-max the better default on quality
# — fewer destructive artifacts, clearer subjects, +15% latency — but left the
# pin on uni-1 because max-tier per-image pricing is not visible on the wire.
# Flip `model` to uni-1-max once the dashboard confirms the delta.
#
# Run:
#   pixi run -e live-hosted kinoforge image \
#     --config examples/configs/luma-uni1-t2i.yaml \
#     --prompt "$(cat examples/configs/prompts/field-realistic.txt)"

mode: t2i

image:
  engine: luma_agents
  spec:
    model: "uni-1"
  params:
    aspect_ratio: "16:9"

output:
  dir: output
```

`examples/configs/fal-flux-schnell-t2i.yaml`:

```yaml
# Standalone image generation via fal.ai (flux/schnell) — the fast, cheap one.
#
# ~5 s and ~$0.005 per image against Luma's ~2 min, so this is the config to
# iterate prompts with. Quality is lower; use luma-uni1-t2i.yaml for a keeper.
#
# Set the fal API key in the environment before running.
#
# Run:
#   pixi run -e live-hosted kinoforge image \
#     --config examples/configs/fal-flux-schnell-t2i.yaml \
#     --prompt "a cat in a sunlit meadow, 35mm film"

mode: t2i

image:
  engine: fal
  spec:
    model: "fal-ai/flux/schnell"

output:
  dir: output
```

- [ ] **Step 2: Verify they load and dry-run**

Run: `pixi run pytest tests/test_examples.py -v`
Expected: PASS, file unmodified. If it fails on a missing `engine:`/`models:`, Task 2's allowlist branch is not being reached — debug there, not here.

Run both dry-runs:

```bash
pixi run kinoforge image -c examples/configs/luma-uni1-t2i.yaml --prompt "a cat" --dry-run
pixi run kinoforge image -c examples/configs/fal-flux-schnell-t2i.yaml --prompt "a cat" --dry-run
```

Expected: both exit 0 and print the `[dry-run]` block. No network call.

- [ ] **Step 3: Check `doctor` on both**

```bash
pixi run kinoforge doctor -c examples/configs/luma-uni1-t2i.yaml
pixi run kinoforge doctor -c examples/configs/fal-flux-schnell-t2i.yaml
```

Expected: exits cleanly. This is the live confirmation of Task 3's fix against a real config rather than a synthesized one. If either crashes with `AttributeError`, a third unguarded `cfg.engine` dereference exists that the audit missed — fix it the same way and add it to Task 3's test parametrize list.

- [ ] **Step 4: Write the docs**

In `docs/configuration.md`, add an `image:` section after the `keyframe:` one covering: the four engine names; that `spec.model` is the provider's model id; that `params` is an opaque pass-through merged into the provider request body (so `aspect_ratio` works on Luma and `image_size` on fal, and neither is validated); and the **full list of refused keys** with the reason — an image run has no compute, no video engine and no model fetch, so those keys would be silently inert. State that `mode:` must be `t2i` or absent.

In `docs/engines.md`, under the image-engine material, add that image engines are now reachable two ways: as the head of a video pipeline via `keyframe:`, and terminally via `image:` + `kinoforge image`. Note `max_resolution` on `ImageProfile` is **not** enforced (fal takes `image_size`, Luma takes `aspect_ratio`; there is no params-normalisation layer), while `supported_modes` **is** gated before submit.

In `README.md`, add to the usage section:

```markdown
### Generate a single image

```bash
pixi run -e live-hosted kinoforge image \
  --config examples/configs/fal-flux-schnell-t2i.yaml \
  --prompt "a cat in a sunlit meadow, 35mm film"
```

Lands at `output/<ts>_image_<provider>_<model>_<slug>.png`. No compute is
provisioned — every image engine is a hosted API.
```

In `PROGRESS.md`, update the `## Pointers` entry added when the design landed: mark the plan path, and set the next action to the first unchecked task.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add examples/configs/luma-uni1-t2i.yaml examples/configs/fal-flux-schnell-t2i.yaml \
        docs/configuration.md docs/engines.md README.md PROGRESS.md
git commit -m "docs(image): two t2i example configs + operator documentation

luma-uni1-t2i.yaml pins uni-1 deliberately: the 2026-07-04 matrix
(successful-generations.md 18) rated uni-1-max better on quality but left the
pin because max-tier per-image pricing is not readable on the wire. The comment
records the flip condition.

fal-flux-schnell-t2i.yaml is the iteration config — ~5 s and ~$0.005 against
Luma's ~2 min.

No replicate config: that engine has never been live-fired, and shipping an
example would imply otherwise.

Docs state plainly that ImageProfile.max_resolution is NOT enforced (fal takes
image_size, Luma takes aspect_ratio, params is an opaque pass-through) while
supported_modes IS gated before submit, so no reader assumes dimension
validation exists."
```

---

## Task 10: Live-fire `kinoforge image` and record the entry

**Goal:** One real image from `kinoforge image` against Luma UNI-1, visually reviewed, with a `successful-generations.md` section recording it.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

Gate rationale: this is the only task that spends real money and the only one whose claim ("`kinoforge image` works") cannot be established offline. `CLAUDE.md` makes visual QA mandatory before reporting green, and makes a log entry mandatory for a new kinoforge command. A dry run, a mocked engine, or a passing offline suite is **not** an acceptable substitute and must not appear as an alternate close path.

**Files:**
- Modify: `successful-generations.md` (new section, not a "See also")
- Modify: `PROGRESS.md` (ship summary)
- Create: `tests/live/evidence/2026-10-03_kinoforge_image_luma_stdout.txt`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 **before** the spend, and the working tree is clean (Tasks 1-9 all committed)
- [ ] The live command exits 0 and publishes a PNG under `output/`
- [ ] The published filename contains `_image_` and a model slug that is **not** `unknown` (the §17 trap)
- [ ] The PNG is read and visually judged — artifacts, prompt adherence, subject clarity — and the verdict is written into the log entry with an explicit ⚠️ on anything not clearly high quality
- [ ] `ffprobe`/`file` confirms real PNG dimensions, recorded in the entry
- [ ] A new numbered section in `successful-generations.md` per that file's preamble schema, with the TOC line added — **not** a "See also" under §15, because "new kinoforge command" is its own capability axis even though the `(luma_agents, uni-1, t2i)` tuple is unchanged
- [ ] stdout captured to the evidence file with any pre-signed URL query string scrubbed (those carry short-lived tokens — §15 did the same)
- [ ] No pod or cluster was created (`pixi run kinoforge list` shows both empty lines), because this path provisions no compute at all

**Verify:** `pixi run -e live-hosted kinoforge image --config examples/configs/luma-uni1-t2i.yaml --prompt "$(cat examples/configs/prompts/field-realistic.txt)"` → exit 0, a PNG published under `output/`, filename containing `_image_luma_agents_uni-1_`

**Steps:**

- [ ] **Step 1: Confirm the scaffold is committed before spending**

CLAUDE.md's rule: anything whose purpose is to drive live spend must be committed BEFORE the spend, so a mid-spend crash cannot lose it.

```bash
git status --short
git log --oneline -9
```
Expected: clean tree, and Tasks 1-9's commits all present. If anything is uncommitted, commit it first.

- [ ] **Step 2: Preflight**

```bash
pixi run preflight
echo "preflight rc=$?"
```
Expected: `rc=0`. It checks credentials are present, zero active pods, clean tree. Do not proceed on a non-zero exit — read what it reported and fix that first.

- [ ] **Step 3: Run the live command**

The prompt is read verbatim from the standard file — no paraphrase, no per-run override — so this image is comparable with every other standard-prompt generation in the log.

```bash
pixi run -e live-hosted kinoforge image \
  --config examples/configs/luma-uni1-t2i.yaml \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  2>&1 | tee /tmp/kf-image-live.txt
```

Expected: exit 0 in roughly 100-200 s (§15 measured ~125 s for this model), ending with `image completed — artifact …`.

`-e live-hosted` is required: the hosted-engine dependencies live in that pixi feature environment only.

- [ ] **Step 4: Confirm the artifact and its filename**

```bash
ls -la output/*_image_*
file output/*_image_*.png
```

Expected: one new PNG. The filename must contain `_image_luma_agents_uni-1_`. If the model slug reads `unknown`, stop — that is §17's `model_identity` trap and it means Task 6's warning fired; fix `model_identity` before recording anything green.

- [ ] **Step 5: Visually QA the image — mandatory**

Read the PNG. Judge it the way the §18 matrix review did: artifacts (especially the frame-wide out-of-focus blobs §18 found on `uni-1`'s dawn-flight draw), prompt adherence, subject clarity, and whether anything in it would be destructive if it were used downstream as an i2v opener.

Write the verdict down. Anything not clearly high quality gets an explicit ⚠️ flag in the log entry. "It ran" is not a verdict.

- [ ] **Step 6: Confirm nothing was provisioned**

```bash
pixi run kinoforge list
```

Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` This path creates no compute, so anything else means `generate_image` reached orchestration machinery it must never touch.

- [ ] **Step 7: Scrub and save the evidence**

Luma returns pre-signed URLs carrying short-lived tokens. Strip every query string before committing the log, exactly as §15 did:

```bash
sed -E 's/\?[A-Za-z0-9&=_%.-]+/?<scrubbed>/g' /tmp/kf-image-live.txt \
  > tests/live/evidence/2026-10-03_kinoforge_image_luma_stdout.txt
rg -n 'Signature|Credential|X-Amz' tests/live/evidence/2026-10-03_kinoforge_image_luma_stdout.txt \
  || echo "clean — no signed-URL material"
```

- [ ] **Step 8: Write the log entry**

Add a new numbered section to `successful-generations.md` following that file's preamble schema, plus its TOC line. It must carry: the stack triple (`lumalabs.ai (agents API) / LumaAgentsImageEngine / uni-1`), mode `t2i`, the first-success SHA, local-TZ timestamp, the exact command, the config path, the output filename + byte size + sha256 + pixel dimensions, wall-clock, the visual-QA verdict from Step 5, the evidence path, and spend.

State the new capability axis explicitly: **the first generation in this log produced by a kinoforge command that terminates at an image.** §15 and §18 are the same `(luma_agents, uni-1, t2i)` tuple but were driven by a live test and a scratch matrix script respectively — there was no command. Note also that `--ephemeral` is refused on this path by design, so every `kinoforge image` run is loggable.

- [ ] **Step 9: Update PROGRESS and commit**

Update `PROGRESS.md`: mark the plan complete, record the ship summary, and set the next action. Then:

```bash
pixi run pre-commit run --all-files
git add successful-generations.md PROGRESS.md \
        tests/live/evidence/2026-10-03_kinoforge_image_luma_stdout.txt
git commit -m "docs(log): kinoforge image live-proven on Luma UNI-1

First generation in this log produced by a kinoforge command that TERMINATES at
an image. Entries 15 and 18 share the (luma_agents, uni-1, t2i) tuple but were
driven by a live test and a scratch matrix script — there was no command, which
is the capability axis this adds.

Frame QA recorded in the entry per CLAUDE.md: exit code plus pixel dimensions
cannot see pixels, and entry 18 proved objective metrics rate a blob-ridden
draw identical to a clean one.

Zero compute provisioned, confirmed via kinoforge list after the run — this
path has no provider, no ledger row and no lifecycle by construction."
```

```json:metadata
{"files": ["successful-generations.md", "PROGRESS.md", "tests/live/evidence/2026-10-03_kinoforge_image_luma_stdout.txt"], "verifyCommand": "pixi run -e live-hosted kinoforge image --config examples/configs/luma-uni1-t2i.yaml --prompt \"$(cat examples/configs/prompts/field-realistic.txt)\"", "acceptanceCriteria": ["pixi run preflight exits 0 before the spend and the tree is clean", "the live command exits 0 and publishes a PNG under output/", "the published filename contains _image_ and a model slug that is not 'unknown'", "the PNG is read and visually judged, verdict written into the log entry with explicit warning flags on anything not clearly high quality", "pixel dimensions confirmed and recorded", "a new numbered section plus TOC line in successful-generations.md, not a See-also under 15", "stdout captured to the evidence file with pre-signed URL query strings scrubbed", "pixi run kinoforge list shows both empty lines - no compute was provisioned"], "userGate": true, "tags": ["user-gate", "live-spend"], "requiresUserSpecification": false, "gateScope": "single-live-run", "failurePolicy": "stop-and-report", "modelTier": "frontier"}
```

---

## Dependencies

```
Task 1 (ImageConfig base)
  ├─> Task 2 (Config.image + allowlist)
  │     ├─> Task 3 (doctor survives engine=None)
  │     ├─> Task 6 (generate_image)
  │     └─> Task 7 (ephemeral refusal)
  └─> Task 4 (resolve_image_stack)
        └─> Task 6
Task 5 (cancel_token widening, independent)
  └─> Task 6
Task 6 + Task 7 ─> Task 8 (CLI wiring)
Task 8 + Task 3 ─> Task 9 (configs + docs)
Task 9 ─> Task 10 (live fire)
```

Task 5 has no prerequisites and can run in parallel with Tasks 1-4.
