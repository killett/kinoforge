# Compute Seam S3 — Setup/Run Split, and the Death of `_strip_trailing_exec` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the engine emit `(setup_steps, launch)` instead of one bash blob whose last line is a
provider-specific launch convention, so each provider composes its own launch and
`_strip_trailing_exec`'s substring heuristic — which is already silently wrong on two shipped
engines — can be deleted.

**Architecture:** Stage 3 of 5 from
`docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` §7. `RenderedProvision`
and `InstanceSpec` gain `setup_steps: tuple[SetupStep, ...]` and `launch: Launch | None`;
`provision_script`, `image_build_script`, `runtime_provision_script` and `run_cmd` are deleted at
the end. Each provider maps the pair its own way: RunPod concatenates the steps and appends the
launch line it needs for PID 1, SkyPilot puts steps in `Task.setup` and the launch in `Task.run`,
Modal partitions on `bakeable` and runs the rest at container start. The migration is additive
first and subtractive last, so every commit leaves the tree green.

**Tech Stack:** Python 3.13, pydantic v2, pytest, pixi, ruff, mypy. Providers: runpod (GraphQL),
skypilot (SDK), modal (SDK), local. Engines: diffusers, comfyui, fake, plus the composed
upscaler/interpolator engines (flashvsr, spandrel, rife).

---

## THE DESIGN DOC IS WRONG ABOUT ONE THING — READ THIS FIRST

§7 says the provider appends `exec <run_command>` and that this is "the PID-1 convention … a
RunPod deployment detail". That is true of exactly one engine. Verified at HEAD:

| Engine | Last line of the rendered script | Reconstructible from `run_cmd`? |
|---|---|---|
| comfyui (`engines/comfyui/__init__.py:1390`) | `cd /workspace/ComfyUI && exec python main.py <args>` | **No** — the `cd` is lost |
| diffusers (`engines/diffusers/__init__.py:1251-1257`) | `env PYTORCH_CUDA_ALLOC_CONF=… python -m …` — **no `exec`, deliberately** | **No** — an `exec` would be wrong |
| fake (`engines/fake/__init__.py:300-306`) | `echo fake` — no launch line at all | n/a |

The diffusers comment at `:1252-1256` states the reason: *"NO `exec` prefix. Bash must remain PID 1
so the EXIT trap can fire when the main server crashes."* A provider that blindly appends
`exec <run_cmd>` would re-exec over that bash and destroy the trap.

So `run_cmd` alone cannot carry a launch. **This plan adds `Launch(argv, workdir, exec_pid1)`** —
three properties of the WORKLOAD, not of a vendor — and every provider composes from it. That
reproduces all three shapes byte-identically and is what makes the deletion safe. The design doc
gets a correcting edit in Task 8, exactly as S1 and S2 did for their own corrections.

### Two live bugs this stage fixes, both confirmed from the committed goldens

Read `tests/providers/golden/launch_payloads/skypilot-lambda-diffusers-flashvsr-upscale.json` and
`…/skypilot-lambda-comfyui.json`:

1. **SkyPilot + diffusers launches the server twice — or hangs `Task.setup` forever.**
   `_strip_trailing_exec` matches on the substring `" exec "`. The diffusers launch line has no
   `exec`, so nothing is stripped: `task_config["setup"]` ENDS with
   `env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True python -m kinoforge.engines.diffusers.servers.wan_t2v_server`,
   and `task_config["run"]` is that same command again. Setup can never terminate, so `Task.run`
   either never starts or starts a second server on a port already bound. This is finding F1's
   mechanism with a duplicate launch on top.
2. **SkyPilot + comfyui runs `main.py` from the wrong directory.** Here the strip DOES fire — and
   takes `cd /workspace/ComfyUI` with it. `task_config["run"]` is
   `python main.py --listen 0.0.0.0 --port 8188`, executed from the login directory where
   `main.py` does not exist.

Both are the same root cause: a launch line is not a setup step, and squashing them into one
string means every provider has to guess where one ends and the other begins.

A third, latent one, recorded but NOT fixed here because nothing observable depends on it: Modal
composes `provision_script + "\nexec " + shlex.join(run_cmd)` (`providers/modal/_app.py:55-56`)
on top of a `runtime_script` that already ends with the diffusers launch line. The appended `exec`
is dead code that is only unreachable because the first launch blocks forever. Task 5 removes it
as a side effect of partitioning on `bakeable`.

---

**Global Constraints:**
- **Byte-identity is the ratchet, and it is asymmetric.** The RunPod and Modal goldens must stay
  byte-identical through every task: concatenating a config's steps in declaration order and
  appending its composed launch line must reproduce today's `provision_script` exactly. The
  SkyPilot goldens WILL change, exactly twice, and only in `task_config.setup` / `task_config.run`
  — those are the two bugs above. Any other movement is a regression, not a result.
- **Regenerating a golden is a reviewed act, never a way to make a failing test pass.** Task 4 and
  Task 5 each decode the payload, diff it, and paste the diff into their report.
- `kinoforge.core.*` must not import `kinoforge.providers.*` at module scope.
- Additive before subtractive: the old fields survive until Task 6. Every task before it leaves
  the full suite green.
- Google-style docstrings, full type hints, Conventional Commits in imperative mood, never
  `--no-verify`, `rg` not `grep`, pixi for everything, local timezone.

**User decisions (already made):**
- S1's ruling that UNSUPPORTED-field severity is by risk coverage governs every new `consumes()`
  row here, same as it did in S2.
- Live smokes are pre-authorised up to the session budget. The S2 smoke measured $0.0425 per run
  on `c6i.large`; S3's is the same SKU and the same config.
- S2's ruling that a region is pinned only next to its cloud stands; nothing here revisits it.

---

## File Structure

**Modified:**
- `src/kinoforge/core/interfaces.py` — `SetupStep`, `Launch`, `RenderedProvision.setup_steps` /
  `.launch`, `InstanceSpec.setup_steps` / `.launch`; the four old fields deleted in Task 6.
- `src/kinoforge/core/spec_builder.py` — thread the new pair; drop the old three.
- `src/kinoforge/engines/diffusers/__init__.py` — `_add` already tags every line `build` /
  `runtime` / `both`; that tagging becomes `SetupStep.bakeable` and the launch line moves out.
- `src/kinoforge/engines/comfyui/__init__.py`, `src/kinoforge/engines/fake/__init__.py` — same.
- `src/kinoforge/providers/runpod/__init__.py` — compose the launch line; declarations.
- `src/kinoforge/providers/skypilot/__init__.py` — steps → `Task.setup`, launch → `Task.run`;
  `_strip_trailing_exec` (`:323-347`) deleted.
- `src/kinoforge/providers/modal/__init__.py`, `src/kinoforge/providers/modal/_app.py` — partition
  on `bakeable`; stop appending a second `exec`.
- `src/kinoforge/providers/local/__init__.py` — declarations only.
- `src/kinoforge/cli/_commands.py:322`, `src/kinoforge/diagnostics/c30_probe.py:263-318` — the two
  non-provider readers of `rendered.script`.
- `tests/providers/test_field_consumption_parity.py` — `_SPEC_PORTABLE` gains the new names, loses
  the old.
- `README.md`, `SPEC.md`, `docs/breaking-changes.md`, `PROGRESS.md`, and the design doc's §7.

**Created:**
- `tests/core/test_setup_steps.py`, `tests/engines/test_engine_setup_steps.py`,
  `tests/live/test_compute_seam_s3_setup_run_smoke.py`.

---

## Task 0: `SetupStep`, `Launch`, and the combine helper

**Goal:** Add the two portable dataclasses and the one function that defines what "concatenating
steps reproduces the old script" means, with nothing consuming them yet.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py`
- Test: `tests/core/test_setup_steps.py`

**Acceptance Criteria:**
- [ ] `SetupStep(script: str, bakeable: bool = False)` and
      `Launch(argv: tuple[str, ...], workdir: str = "", exec_pid1: bool = False)` are frozen
      dataclasses in `core/interfaces.py`.
- [ ] `combine_steps(steps)` joins the step scripts with `"\n"`, skipping empty ones, and
      `render_launch(launch)` returns the single bash line a provider would append —
      `"cd <workdir> && exec <argv>"`, `"exec <argv>"`, `"cd <workdir> && <argv>"` or `"<argv>"`
      depending on the two flags.
- [ ] `render_launch` shell-quotes `workdir` but NOT `argv`: the three shipped engines build their
      argv from already-safe literals and one of them (`env VAR=… python -m …`) is not a bare argv
      at all. A test pins that, so the choice is visible rather than incidental.
- [ ] `render_launch(None)` raises rather than returning `""` — a provider asking to render a
      launch that does not exist is a bug in the provider, not an empty string.
- [ ] All 31 goldens byte-identical (nothing consumes any of this yet).

**Verify:** `pixi run python -m pytest tests/core/test_setup_steps.py tests/providers/test_launch_payload_goldens.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_setup_steps.py
"""Behavior: a provisioning script is a LIST of steps plus a launch, not one blob.

The blob is what forced `_strip_trailing_exec` to guess where setup ends and the
server starts, by substring-matching " exec " on the last line. That guess is
already wrong on both shipped engines: comfyui's launch carries a `cd` the strip
discards, and diffusers' launch has no `exec` at all, so the strip does not fire
and the server command stays inside SkyPilot's `Task.setup`.
"""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import Launch, SetupStep, combine_steps, render_launch


def test_steps_combine_in_declaration_order() -> None:
    """Order is the whole contract — it is what reproduces today's script."""
    steps = (SetupStep("a"), SetupStep("b", bakeable=True), SetupStep("c"))
    assert combine_steps(steps) == "a\nb\nc"


def test_empty_steps_are_skipped_not_rendered_as_blank_lines() -> None:
    """Bug caught: an engine that emits an empty step for a disabled feature
    would otherwise insert a blank line and move every RunPod golden."""
    assert combine_steps((SetupStep("a"), SetupStep(""), SetupStep("c"))) == "a\nc"


def test_bakeable_defaults_to_false() -> None:
    """Bug caught: defaulting to True would bake a runtime step — an `export`
    or a keep-alive trap — into an image where it runs once and never again."""
    assert SetupStep("x").bakeable is False


@pytest.mark.parametrize(
    ("launch", "expected"),
    [
        (Launch(("python", "main.py")), "python main.py"),
        (Launch(("python", "main.py"), exec_pid1=True), "exec python main.py"),
        (
            Launch(("python", "main.py"), workdir="/workspace/ComfyUI"),
            "cd /workspace/ComfyUI && python main.py",
        ),
        (
            Launch(("python", "main.py"), workdir="/workspace/ComfyUI", exec_pid1=True),
            "cd /workspace/ComfyUI && exec python main.py",
        ),
    ],
)
def test_render_launch_covers_all_four_shapes(launch: Launch, expected: str) -> None:
    """Bug caught: collapsing the two flags into one would force every engine
    into comfyui's shape and destroy the diffusers EXIT trap (see the plan's
    header table)."""
    assert render_launch(launch) == expected


def test_render_launch_quotes_the_workdir_but_not_the_argv() -> None:
    """The asymmetry is deliberate, so pin it.

    Bug caught: quoting argv would wrap the diffusers launch — which is
    `env VAR=v python -m mod`, not a bare argv — into a single quoted word that
    bash would try to execute as one filename.
    """
    rendered = render_launch(Launch(("env", "A=1", "python"), workdir="/a b"))
    assert rendered == "cd '/a b' && env A=1 python"


def test_render_launch_refuses_none() -> None:
    """Bug caught: returning "" for a missing launch lets a provider silently
    ship a script that starts no server."""
    with pytest.raises(ValueError, match="no launch"):
        render_launch(None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_setup_steps.py -q`
Expected: FAIL — `ImportError: cannot import name 'SetupStep' from 'kinoforge.core.interfaces'`.

- [ ] **Step 3: Implement**

In `core/interfaces.py`, next to `RenderedProvision`:

```python
@dataclass(frozen=True)
class SetupStep:
    """One provisioning step, with the only property a provider needs to route it.

    compute-seam S3. Replaces the ``build_script`` / ``runtime_script`` pair,
    which named Modal's pipeline stages rather than a property of the step.

    Attributes:
        script: Bash fragment. May be multi-line. Empty steps are skipped by
            :func:`combine_steps`, so an engine can emit one unconditionally
            for a feature that is switched off.
        bakeable: True when the step is safe to run at IMAGE-BUILD time —
            installs, weight fetches, anything idempotent that produces no
            per-container state. Providers that provision at runtime ignore
            the flag by construction and declare that they do.
    """

    script: str
    bakeable: bool = False


@dataclass(frozen=True)
class Launch:
    """How to start the workload once the setup steps have run.

    Three properties of the WORKLOAD, not of a provider. ``exec_pid1`` in
    particular is not a RunPod detail: the diffusers engine deliberately does
    NOT exec, because bash must stay PID 1 for its EXIT trap to fire when the
    server dies (``engines/diffusers/__init__.py``). A provider that appended
    ``exec`` unconditionally would delete that trap.

    Attributes:
        argv: The command, pre-split. NOT shell-quoted when rendered — see
            :func:`render_launch`.
        workdir: Directory to ``cd`` into first; ``""`` means "wherever the
            setup left us". comfyui needs ``/workspace/ComfyUI``.
        exec_pid1: Replace the shell with the command, so it becomes PID 1.
    """

    argv: tuple[str, ...] = ()
    workdir: str = ""
    exec_pid1: bool = False


def combine_steps(steps: "Sequence[SetupStep]") -> str:
    """Return the steps as one bash script, in declaration order.

    Args:
        steps: The steps to concatenate. Empty scripts are skipped.

    Returns:
        The joined script, without a trailing newline.
    """
    return "\n".join(step.script for step in steps if step.script)


def render_launch(launch: "Launch | None") -> str:
    """Return the single bash line that starts *launch*.

    ``argv`` is joined verbatim rather than shell-quoted. Every shipped engine
    builds it from literals it has already quoted where needed, and the
    diffusers launch is an ``env VAR=value python -m module`` prefix form that
    quoting would collapse into one unrunnable word. ``workdir`` IS quoted,
    because it is a path an operator could plausibly write with a space in it.

    Args:
        launch: The launch to render.

    Returns:
        One line of bash.

    Raises:
        ValueError: *launch* is None — a caller asking to start a workload
            that declares no launch is a bug, and an empty string would hide
            it behind a container that boots and serves nothing.
    """
    if launch is None:
        raise ValueError("cannot render a launch line: this spec declares no launch")
    command = " ".join(launch.argv)
    if launch.exec_pid1:
        command = f"exec {command}"
    if launch.workdir:
        return f"cd {shlex.quote(launch.workdir)} && {command}"
    return command
```

`shlex` and `Sequence` are already imported in that module; confirm before adding either.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_setup_steps.py tests/providers/test_launch_payload_goldens.py -q`
Expected: all PASS, no golden modified (`git status --short tests/providers/golden/` is empty).

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/interfaces.py tests/core/test_setup_steps.py
git commit -m "feat(core): add SetupStep and Launch, the portable setup/run pair"
```

---

## Task 1: `RenderedProvision` and `InstanceSpec` carry the pair, additively

**Goal:** Give the new fields a home on both dataclasses and thread them through `spec_builder`,
with the old fields still populated, so nothing changes behaviour yet.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py`, `src/kinoforge/core/spec_builder.py`
- Test: `tests/core/test_setup_steps.py` (extend)

**Acceptance Criteria:**
- [ ] `RenderedProvision` gains `setup_steps: tuple[SetupStep, ...] = ()` and
      `launch: Launch | None = None`; `InstanceSpec` gains the same two.
- [ ] `build_instance_spec` copies both through, and continues to populate
      `provision_script` / `image_build_script` / `runtime_provision_script` / `run_cmd` exactly
      as it does today.
- [ ] An engine that emits neither new field produces a spec whose `setup_steps` is `()` and whose
      `launch` is `None` — no synthesised defaults, because a synthesised launch is precisely the
      guess this stage removes.
- [ ] `tests/providers/test_field_consumption_parity.py` is green with `setup_steps` and `launch`
      added to `_SPEC_PORTABLE` and declared UNSUPPORTED by all four providers — nothing reads
      them yet, and declaring otherwise would be the flattery the guard exists to catch.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/core tests/providers -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_spec_builder_threads_the_new_pair_and_keeps_the_old_fields() -> None:
    """Additive migration: both representations coexist until Task 6.

    Bug caught: switching the spec over in one commit would strand every one of
    the 54 test modules that construct an InstanceSpec with provision_script.
    """
    from kinoforge.core.interfaces import (
        Launch,
        Lifecycle,
        Offer,
        RenderedProvision,
        SetupStep,
    )
    from kinoforge.core.spec_builder import build_instance_spec
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "bf16"},
            "spec": {"model": "m", "precision": "bf16"},
            "models": [{"kind": "base", "ref": "hf:org/repo", "target": "checkpoints"}],
            "compute": {"provider": "runpod", "image": "i"},
        }
    )
    rendered = RenderedProvision(
        script="install\nrun-server",
        run_cmd=["run-server"],
        image="img:tag",
        ports=["8000"],
        env_required=[],
        setup_steps=(SetupStep("install", bakeable=True),),
        launch=Launch(("run-server",)),
    )
    spec = build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        offer=Offer(id="g", gpu_type="g", vram_gb=80, cuda="12.4", cost_rate_usd_per_hr=1.0),
        engine_name="diffusers",
        key_hash="abc",
        image="fallback:img",
        lifecycle=Lifecycle(),
        env={},
        run_id="run-1",
    )
    assert spec.setup_steps == (SetupStep("install", bakeable=True),)
    assert spec.launch == Launch(("run-server",))
    # The old representation is untouched — that is what keeps the tree green.
    assert spec.provision_script == "install\nrun-server"
    assert spec.run_cmd == ["run-server"]


def test_an_engine_that_emits_no_steps_gets_no_synthesised_launch() -> None:
    """Bug caught: defaulting `launch` to Launch(tuple(run_cmd)) would put a
    plausible-looking launch on every legacy spec and hide, rather than surface,
    the engines that have not been migrated yet."""
    from kinoforge.core.interfaces import RenderedProvision

    rendered = RenderedProvision(
        script="echo hi", run_cmd=["sleep", "infinity"], image="i", ports=[], env_required=[]
    )
    assert rendered.setup_steps == ()
    assert rendered.launch is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_setup_steps.py -k threads_the_new_pair -q`
Expected: FAIL — `TypeError: RenderedProvision.__init__() got an unexpected keyword argument 'setup_steps'`.

- [ ] **Step 3: Implement**

Add to `RenderedProvision` (after `runtime_script`, so field order stays append-only):

```python
    setup_steps: tuple[SetupStep, ...] = ()
    launch: Launch | None = None
```

Add the same two to `InstanceSpec`, and in `build_instance_spec`'s `InstanceSpec(...)` call:

```python
        setup_steps=tuple(rendered.setup_steps),
        launch=rendered.launch,
```

Then declare both fields in every provider's `consumes()` as UNSUPPORTED, with a comment saying
which task turns each one CONSUMED — runpod and skypilot in Tasks 3 and 4, modal in Task 5, local
never:

```python
            "setup_steps": u,  # S3 Task 3 turns this CONSUMED
            "launch": u,  # S3 Task 3 turns this CONSUMED
```

and add both names to `_SPEC_PORTABLE` in `tests/providers/test_field_consumption_parity.py`.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core tests/providers -q`
Expected: all PASS. The parity guard's `test_declaration_covers_exactly_the_portable_field_set`
proves all four providers declared the two new rows.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core tests/core/test_setup_steps.py tests/providers/test_field_consumption_parity.py src/kinoforge/providers
git commit -m "feat(core): carry setup_steps and launch alongside the legacy script fields"
```

---

## Task 2: The diffusers engine emits steps and a launch

**Goal:** Convert the engine whose `_add("build"|"runtime"|"both", …)` tagging is already a
`bakeable` flag in disguise, and prove the conversion is byte-identical to today's script.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py:1128-1273`
- Test: `tests/engines/test_engine_setup_steps.py`

**Acceptance Criteria:**
- [ ] `render_provision` returns `setup_steps` whose combination equals today's `script` MINUS the
      final launch line, and a `launch` that renders back to exactly that line.
- [ ] `combine_steps(setup_steps) + "\n" + render_launch(launch) == script` for every shipped
      diffusers config — asserted over the configs, not over a hand-written fixture.
- [ ] `bakeable=True` for exactly the lines `_add` tagged `build` or `both`; the `both` case stays
      in BOTH buckets, as it does today (the embedded module tree is needed at bake time by the
      weights fetch and at runtime by the server).
- [ ] `launch.exec_pid1 is False` — the EXIT trap depends on it, and a test says so in those words.
- [ ] `script`, `build_script` and `runtime_script` keep their current values; this task adds, it
      does not remove.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/engines/test_engine_setup_steps.py tests/providers/test_launch_payload_goldens.py -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/engines/test_engine_setup_steps.py
"""Behavior: an engine's steps + launch reproduce its script exactly.

This is the invariant that makes S3 safe to land. If concatenating the steps and
appending the rendered launch does not reproduce today's byte stream, the RunPod
goldens move — and a moved RunPod golden in this stage means a real boot-script
change nobody asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import combine_steps, render_launch

_DIFFUSERS_CONFIGS = [
    p
    for p in sorted(Path("examples/configs").glob("*.yaml"))
    if "diffusers" in p.name
]


def _render(path: Path):
    """Return the RenderedProvision a shipped config produces."""
    from tools.snapshot_launch_payloads import render_for_config

    return render_for_config(path)


@pytest.mark.parametrize("cfg_path", _DIFFUSERS_CONFIGS, ids=lambda p: p.stem)
def test_steps_plus_launch_reproduce_the_script(cfg_path: Path) -> None:
    """The ordering invariant, over every shipped diffusers config."""
    rendered = _render(cfg_path)
    assert rendered.launch is not None, f"{cfg_path} rendered no launch"
    rebuilt = combine_steps(rendered.setup_steps) + "\n" + render_launch(rendered.launch)
    assert rebuilt == rendered.script


@pytest.mark.parametrize("cfg_path", _DIFFUSERS_CONFIGS, ids=lambda p: p.stem)
def test_bakeable_steps_reproduce_the_build_script(cfg_path: Path) -> None:
    """`bakeable` is exactly the old build bucket.

    Bug caught: a step mis-tagged bakeable gets baked into the image and never
    re-runs per container — an `export` or a keep-alive trap tagged that way is
    silently absent at runtime, which is a boot failure nobody can see in a diff.
    """
    rendered = _render(cfg_path)
    bakeable = combine_steps(tuple(s for s in rendered.setup_steps if s.bakeable))
    assert bakeable == rendered.build_script


def test_diffusers_launch_does_not_exec_because_bash_must_stay_pid_1() -> None:
    """Bug caught: exec_pid1=True here replaces the bash that owns the EXIT
    trap, so a crashed server would leave a pod alive with nothing listening —
    the failure mode the trap was added to end."""
    rendered = _render(Path("examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml"))
    assert rendered.launch is not None
    assert rendered.launch.exec_pid1 is False
    assert rendered.launch.workdir == ""
```

> Executor note: `render_for_config` does not exist yet. Add it to
> `tools/snapshot_launch_payloads.py` as a thin wrapper around whatever that module already does
> to get from a config path to a `RenderedProvision` — read `build_spec` and lift the engine-render
> half out, rather than duplicating it. If the render is not separable, call `build_spec` and read
> the spec's fields instead, and adjust the test to match.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/engines/test_engine_setup_steps.py -q`
Expected: FAIL — `AssertionError: … rendered no launch`, because `render_provision` still returns
the default `launch=None`.

- [ ] **Step 3: Implement**

In `render_provision`, keep `lines` / `build_lines` / `runtime_lines` exactly as they are — they
are what proves byte-identity — and accumulate steps beside them. Change `_add` to also record the
step, and stop appending the server command to the line buckets, emitting it as the launch instead:

```python
        steps: list[SetupStep] = [SetupStep("\n".join(_preamble))]

        def _add(phase: str, *new: str) -> None:
            """Append line(s) to the combined stream, the phase buckets, AND steps."""
            for ln in new:
                lines.append(ln)
                if phase in ("build", "both"):
                    build_lines.append(ln)
                if phase in ("runtime", "both"):
                    runtime_lines.append(ln)
            steps.append(SetupStep("\n".join(new), bakeable=phase in ("build", "both")))
```

Note the preamble seeds BOTH `lines` and `runtime_lines` today (`:1134-1136`), so its step is
`bakeable=False`. At the launch site (`:1251-1257`), replace the `_add("runtime", …)` call with a
launch, keeping the combined-stream append so `script` is unchanged:

```python
        launch: Launch | None = None
        if server_cmd:
            # NO exec: bash must remain PID 1 so the EXIT trap fires when the
            # server crashes. That is a property of this workload, which is why
            # it now rides on Launch rather than on a provider's convention.
            launch = Launch(argv=tuple(server_cmd), exec_pid1=False)
            lines.append(" ".join(server_cmd))
            runtime_lines.append(" ".join(server_cmd))
```

and pass `setup_steps=tuple(steps), launch=launch` to `RenderedProvision(...)`.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/engines/test_engine_setup_steps.py tests/providers -q`
Expected: all PASS, `git status --short tests/providers/golden/` empty.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/engines/diffusers tools/snapshot_launch_payloads.py tests/engines/test_engine_setup_steps.py
git commit -m "feat(diffusers): emit setup steps and a launch alongside the combined script"
```

---

## Task 3: The comfyui and fake engines emit steps and a launch

**Goal:** Convert the remaining two engines. comfyui is the one with a `workdir`; fake is the one
with no launch line in its script at all, which is its own finding.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py:1390`,
  `src/kinoforge/engines/fake/__init__.py:300-306`
- Test: `tests/engines/test_engine_setup_steps.py` (extend)

**Acceptance Criteria:**
- [ ] comfyui returns `Launch(argv=("python", "main.py", *launch_args), workdir="/workspace/ComfyUI", exec_pid1=True)`,
      and `combine_steps(steps) + "\n" + render_launch(launch) == script`.
- [ ] comfyui's steps are a single `SetupStep` carrying everything up to but excluding the launch
      line, `bakeable=False` — the engine has no build/runtime split today and inventing one here
      would be a behaviour change wearing a refactor's clothes.
- [ ] fake returns `setup_steps=(SetupStep("echo fake"),)` and
      `Launch(("sleep", "infinity"))`. **Its `script` gains no launch line**, because it has none
      today — so for fake alone the reproduce-the-script invariant does NOT hold, and the test says
      so explicitly rather than being quietly skipped.
- [ ] The invariant test from Task 2 is widened to every shipped config with a `compute:` block
      except the fake one, and names fake in the exclusion with the reason.
- [ ] All 31 goldens byte-identical, `local-fake.json` included.

**Verify:** `pixi run python -m pytest tests/engines/test_engine_setup_steps.py tests/providers/test_launch_payload_goldens.py -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_comfyui_launch_carries_the_workdir_the_strip_used_to_eat() -> None:
    """Bug caught, and it is live today: `_strip_trailing_exec` removes
    `cd /workspace/ComfyUI && exec python main.py …` wholesale, so SkyPilot's
    Task.run is `python main.py` from the login directory — where main.py does
    not exist. The workdir has to survive as data.
    """
    from pathlib import Path

    rendered = _render(Path("examples/configs/skypilot-lambda-comfyui.yaml"))
    assert rendered.launch is not None
    assert rendered.launch.workdir == "/workspace/ComfyUI"
    assert rendered.launch.exec_pid1 is True
    assert rendered.launch.argv[:2] == ("python", "main.py")
    rebuilt = combine_steps(rendered.setup_steps) + "\n" + render_launch(rendered.launch)
    assert rebuilt == rendered.script


def test_fake_engine_declares_a_launch_its_script_never_had() -> None:
    """The fake engine is the exception to the reproduce-the-script invariant.

    Its script is `echo fake` and its run_cmd is `sleep infinity`, and the two
    have never been connected: on RunPod the container runs `echo fake` and
    exits. Declaring the launch makes that reachable for the first time. It
    moves no golden because the only config using this engine runs on the local
    provider, which starts nothing.
    """
    from pathlib import Path

    rendered = _render(Path("examples/configs/local-fake.yaml"))
    assert rendered.setup_steps == (SetupStep("echo fake"),)
    assert rendered.launch == Launch(("sleep", "infinity"))
    assert "sleep" not in rendered.script
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/engines/test_engine_setup_steps.py -k "comfyui or fake" -q`
Expected: FAIL — both `rendered.launch` are None.

- [ ] **Step 3: Implement**

comfyui, at `:1388-1397`:

```python
        port: str = _extract_port(launch_args_raw)
        run_cmd: list[str] = ["python", "main.py"] + launch_args_raw
        setup_steps = (SetupStep("\n".join(lines)),)
        launch = Launch(
            argv=tuple(run_cmd), workdir="/workspace/ComfyUI", exec_pid1=True
        )
        lines.append(f"cd /workspace/ComfyUI && exec {' '.join(run_cmd)}")

        return RenderedProvision(
            script="\n".join(lines),
            run_cmd=run_cmd,
            image=image,
            ports=[port],
            env_required=sorted(set(env_required)),
            setup_steps=setup_steps,
            launch=launch,
        )
```

Note the ordering: `setup_steps` is built from `lines` BEFORE the launch line is appended, which is
what makes the invariant hold.

fake, at `:300-306`:

```python
        return RenderedProvision(
            script="echo fake",
            run_cmd=["sleep", "infinity"],
            image=image,
            ports=["8000"],
            env_required=[],
            setup_steps=(SetupStep("echo fake"),),
            launch=Launch(("sleep", "infinity")),
        )
```

- [ ] **Step 4: Widen the invariant test**

Change `_DIFFUSERS_CONFIGS` to every top-level config with a `compute:` block, excluding
`local-fake.yaml` with the reason in a comment, and rename the parametrize accordingly.

- [ ] **Step 5: Run and commit**

Run: `pixi run python -m pytest tests/engines tests/providers -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/engines tests/engines/test_engine_setup_steps.py
git commit -m "feat(engines): comfyui and fake emit setup steps and a launch"
```

---

## Task 4: RunPod composes its own launch line

**Goal:** Make the provider that owns the PID-1 convention be the one that writes it, with the wire
bytes unmoved.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py:907-1010`
- Test: `tests/providers/test_runpod_setup_steps.py`

**Acceptance Criteria:**
- [ ] `_build_create_pod_body` builds `dockerArgs` from
      `combine_steps(spec.setup_steps) + "\n" + render_launch(spec.launch)` when `setup_steps` is
      non-empty, and falls back to `spec.provision_script` when it is not — the fallback dies in
      Task 6 and a comment says so.
- [ ] All 20 RunPod goldens byte-identical, proven by decoding
      `KINOFORGE_PROVISION_SCRIPT` (gzip+base64) and comparing the decoded script, not just the
      envelope.
- [ ] `setup_steps` and `launch` are declared CONSUMED on runpod, each with a wire proof in
      `_WIRE_PROOFS` that mutates the spec and observes the decoded script follow.
- [ ] A spec with `setup_steps` and `launch=None` raises rather than shipping a server-less
      container — the `render_launch` contract from Task 0, exercised through the provider.

**Verify:** `pixi run python -m pytest tests/providers -q` → all pass, 31 goldens unmoved

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/test_runpod_setup_steps.py
"""Behavior: RunPod composes the launch line the engine used to embed."""

from __future__ import annotations

import base64
import gzip

import pytest

from kinoforge.core.interfaces import Launch, SetupStep


def _decoded_script(body: dict) -> str:
    """Return the provision script RunPod put on the wire."""
    env = {e["key"]: e["value"] for e in body["variables"]["input"]["env"]}
    blob = env["KINOFORGE_PROVISION_SCRIPT"]
    return gzip.decompress(base64.b64decode(blob)).decode("utf-8")


def test_runpod_appends_the_rendered_launch_to_the_steps(runpod_spec_factory) -> None:
    """Bug caught: composing setup and launch in the wrong order, or dropping
    the newline between them, produces a script whose last setup line and whose
    server command run as one unparseable command."""
    spec = runpod_spec_factory(
        setup_steps=(SetupStep("step-one"), SetupStep("step-two")),
        launch=Launch(("serve", "--port", "8000"), workdir="/srv", exec_pid1=True),
    )
    sent: list[dict] = []
    provider = _provider(sent)
    provider.create_instance(spec)
    assert _decoded_script(sent[0]) == (
        "step-one\nstep-two\ncd /srv && exec serve --port 8000"
    )


def test_runpod_refuses_steps_without_a_launch(runpod_spec_factory) -> None:
    """Bug caught: a container that provisions and then exits, with the pod
    still billing and nothing listening on 8000."""
    spec = runpod_spec_factory(setup_steps=(SetupStep("step-one"),), launch=None)
    with pytest.raises(ValueError, match="no launch"):
        _provider([]).create_instance(spec)
```

> Executor note: `tests/providers/` has no shared spec factory today. Build `runpod_spec_factory`
> and `_provider` in this module, modelled on `tests/providers/test_runpod_create_pod_cloud_type.py`
> — read that file for the real constructor signature and the fake transport shape rather than
> inventing them.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/providers/test_runpod_setup_steps.py -q`
Expected: FAIL — the decoded script is the legacy `provision_script`, ignoring the steps.

- [ ] **Step 3: Implement**

In `_build_create_pod_body`, before the `_encode_provision_script` call at `:921`:

```python
        # S3: the engine emits steps + a launch; the trailing `exec` is RunPod's
        # own PID-1 convention, composed here rather than baked into the engine's
        # script. The `or` fallback covers specs built before Task 6 deletes
        # provision_script, and dies with it.
        if spec.setup_steps:
            script = combine_steps(spec.setup_steps) + "\n" + render_launch(spec.launch)
        else:
            script = spec.provision_script
        docker_args = self._encode_provision_script(env, script)
```

Flip the two `consumes()` rows to `c` with a comment naming the composition site, and add the wire
proofs to `_WIRE_PROOFS["runpod"]`:

```python
        "setup_steps": _tracks(
            lambda ln: "kf-probe-step-marker" in _runpod_provision_script(ln),
            probe={"setup_steps": (SetupStep("echo kf-probe-step-marker"),)},
            expected=True,
        ),
        "launch": _tracks(
            lambda ln: _runpod_provision_script(ln).rstrip().endswith("kf-probe-launch"),
            probe={"launch": Launch(("kf-probe-launch",))},
            expected=True,
        ),
```

- [ ] **Step 4: Prove the goldens did not move**

Run: `pixi run python tools/snapshot_launch_payloads.py && git status --short tests/providers/golden/`
Expected: EMPTY. If any RunPod golden moved, STOP and report BLOCKED — the composition is not
reproducing the engine's bytes, and that is the finding.

- [ ] **Step 5: Run and commit**

Run: `pixi run python -m pytest tests/providers tests/core -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/providers/runpod tests/providers/test_runpod_setup_steps.py tests/providers/test_field_consumption_parity.py
git commit -m "feat(runpod): compose the PID-1 launch line from spec.launch"
```

---

## Task 5: SkyPilot splits setup from run, and `_strip_trailing_exec` is deleted

**Goal:** Fix both live bugs by giving `Task.setup` the steps and `Task.run` the launch, and delete
the substring heuristic that caused them.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py:323-347` (delete), `:998-1007`
- Modify: `tests/providers/test_skypilot_provision_script.py` (the strip's own tests)
- Modify: `tests/providers/golden/launch_payloads/skypilot-*.json` (regenerated — **sanctioned**)

**Acceptance Criteria:**
- [ ] `_strip_trailing_exec` and its tests are gone; `rg -n '_strip_trailing_exec' src/ tests/`
      returns nothing.
- [ ] `task_config["setup"]` is the watchdog arm plus `combine_steps(spec.setup_steps)`, and
      `task_config["run"]` is `render_launch(spec.launch)` — NOT `shlex.quote`-joined `run_cmd`,
      because that is what dropped comfyui's `cd`.
- [ ] The diffusers SkyPilot golden's `setup` no longer ends with the server command, and its `run`
      carries it exactly once. State the before/after lines in the report.
- [ ] The comfyui SkyPilot golden's `run` becomes `cd /workspace/ComfyUI && exec python main.py …`.
- [ ] Exactly the SkyPilot goldens move (5 of 31: `skypilot-cpu`, `skypilot-gpu`,
      `skypilot-lambda-comfyui`, `skypilot-lambda-diffusers-flashvsr-upscale`,
      `skypilot-vast-diffusers-flashvsr-upscale`) — check the count and name any others; the 20
      RunPod and 6 Modal goldens must not move.
- [ ] For each moved golden, the only deltas are inside `task_config.setup` and `task_config.run`.
      `envs`, `resources` and `launch_kwargs` are byte-identical — proven with a structural diff,
      not by eye.

**Verify:** `pixi run python -m pytest tests/providers -q` and the diff review in Step 4

**Steps:**

- [ ] **Step 1: Capture the before-state**

```bash
pixi run python - <<'PY'
import json
for n in ("skypilot-lambda-comfyui", "skypilot-lambda-diffusers-flashvsr-upscale"):
    tc = json.load(open(f"tests/providers/golden/launch_payloads/{n}.json"))["task_config"]
    print("===", n)
    print("  setup last:", [l for l in tc["setup"].rstrip().split("\n") if l.strip()][-1][:120])
    print("  run       :", str(tc.get("run"))[:120])
PY
```

Record both. Step 4 compares against them.

- [ ] **Step 2: Write the failing test**

```python
def test_skypilot_setup_ends_with_setup_not_with_the_server(skypilot_launch) -> None:
    """Bug caught, live at HEAD: the diffusers launch line has no `exec`, so
    `_strip_trailing_exec`'s " exec " substring test never fired and the server
    command stayed inside Task.setup. Setup then cannot terminate, and Task.run
    launches the same server a second time.
    """
    task_config = skypilot_launch("examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml")
    setup_last = [l for l in task_config["setup"].rstrip().split("\n") if l.strip()][-1]
    assert "wan_t2v_server" not in setup_last
    assert task_config["run"].count("wan_t2v_server") == 1


def test_skypilot_run_keeps_the_comfyui_workdir(skypilot_launch) -> None:
    """Bug caught, live at HEAD: the strip removed `cd /workspace/ComfyUI &&`
    along with the exec, so Task.run ran main.py from the login directory."""
    task_config = skypilot_launch("examples/configs/skypilot-lambda-comfyui.yaml")
    assert task_config["run"].startswith("cd /workspace/ComfyUI && exec python main.py")
```

> Executor note: build `skypilot_launch` on `tools.snapshot_launch_payloads.capture_launch`, which
> already returns the captured `task_config` — see how `tests/providers/test_field_consumption_parity.py`
> uses `_sky_task`.

- [ ] **Step 3: Implement**

Delete `_strip_trailing_exec` entirely, then at `:998-1007`:

```python
        setup_parts: list[str] = [
            watchdog.RENDER_ARM(deadline_epoch=deadline_epoch, now=launch_epoch)
        ]
        if spec.setup_steps:
            setup_parts.append(combine_steps(spec.setup_steps))
        elif spec.provision_script:
            # Legacy path; dies with provision_script in Task 6.
            setup_parts.append(spec.provision_script)
        task_config["setup"] = "\n".join(setup_parts)
        if spec.launch is not None:
            task_config["run"] = render_launch(spec.launch)
        elif spec.run_cmd:
            task_config["run"] = " ".join(shlex.quote(c) for c in spec.run_cmd)
```

Delete the strip's tests from `tests/providers/test_skypilot_provision_script.py`; keep any test in
that file that is about something else.

- [ ] **Step 4: Regenerate and review the goldens**

```bash
pixi run python tools/snapshot_launch_payloads.py
git status --short tests/providers/golden/launch_payloads/
git diff tests/providers/golden/launch_payloads/
```

Expected: exactly the five `skypilot-*.json`. Then prove the untouched keys really are untouched:

```bash
pixi run python - <<'PY'
import json, subprocess
for n in ("skypilot-cpu", "skypilot-gpu", "skypilot-lambda-comfyui",
          "skypilot-lambda-diffusers-flashvsr-upscale",
          "skypilot-vast-diffusers-flashvsr-upscale"):
    p = f"tests/providers/golden/launch_payloads/{n}.json"
    old = json.loads(subprocess.run(["git", "show", f"HEAD:{p}"],
                                    capture_output=True, text=True, check=True).stdout)
    new = json.load(open(p))
    moved = [k for k in ("envs", "resources", "name") if old["task_config"].get(k) != new["task_config"].get(k)]
    moved += ["launch_kwargs"] if old["launch_kwargs"] != new["launch_kwargs"] else []
    print(n, "unexpected key movement:", moved or "none")
PY
```

Expected: `none` for all five. Paste this output and the setup/run before-after lines into the
report.

- [ ] **Step 5: Run and commit**

Run: `pixi run python -m pytest tests/providers tests/core -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/providers/skypilot tests/providers
git commit -m "fix(skypilot): split setup from run and delete _strip_trailing_exec"
```

---

## Task 6: Modal partitions on `bakeable`

**Goal:** Replace the `image_build_script` / `runtime_provision_script` pair with the flag that
means the same thing, and stop the provider appending a second launch.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py:214-262`,
  `src/kinoforge/providers/modal/_app.py:55-56`
- Test: `tests/providers/modal/test_setup_steps.py`

**Acceptance Criteria:**
- [ ] Modal's `image_build_script` equivalent is `combine_steps(bakeable steps)` and its boot
      script is `combine_steps(non-bakeable steps)`, matching today's `build_script` /
      `runtime_script` values exactly for every Modal config.
- [ ] `_app.py` no longer appends `"exec " + shlex.join(run_cmd)`; the launch line comes from
      `render_launch` and is passed through the request. Note in the commit that the old appended
      `exec` was unreachable dead code for diffusers (the runtime script's own launch blocked
      first), so this removes a latent double-launch rather than changing observed behaviour.
- [ ] All 6 Modal goldens byte-identical, or — if the dead `exec` line was inside a golden — the
      ONLY delta is its removal, decoded and shown in the report.
- [ ] `setup_steps` and `launch` are CONSUMED on modal with wire proofs; `image_build_script` and
      `runtime_provision_script` rows are gone from every provider's `consumes()`.

**Verify:** `pixi run python -m pytest tests/providers -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_modal_bakes_exactly_the_bakeable_steps(modal_launch) -> None:
    """Bug caught: partitioning on the wrong side of the flag either bakes a
    runtime `export` into the image (where it is lost per container) or leaves
    a multi-GB weights fetch at container start, which is the boot stall the
    fast-boot image bake existed to fix."""
    from kinoforge.core.interfaces import combine_steps

    request, rendered = modal_launch("examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml")
    bakeable = tuple(s for s in rendered.setup_steps if s.bakeable)
    runtime = tuple(s for s in rendered.setup_steps if not s.bakeable)
    assert request["image_build_script"] == combine_steps(bakeable)
    assert request["provision_script"] == combine_steps(runtime)


def test_modal_boot_script_launches_once(modal_launch) -> None:
    """Bug caught: `_app.py` appended `exec <run_cmd>` on top of a runtime
    script that already ended with the diffusers launch. Harmless only because
    the first one blocks forever — which is a latent double-launch, not a
    design."""
    request, _ = modal_launch("examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml")
    boot = request["provision_script"] + "\n" + request["launch_line"]
    assert boot.count("wan_t2v_server") == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/providers/modal/test_setup_steps.py -q`
Expected: FAIL — `KeyError: 'launch_line'`; the request has no such field yet.

- [ ] **Step 3: Implement**

In `ModalProvider.create_instance`, replace the `boot_script` line at `:246-247` and the
`image_build_script=` pass-through at `:259`:

```python
        if spec.setup_steps:
            boot_script = combine_steps(tuple(s for s in spec.setup_steps if not s.bakeable))
            build_script = combine_steps(tuple(s for s in spec.setup_steps if s.bakeable)) or None
            launch_line = render_launch(spec.launch)
        else:
            # Legacy path; dies with the old fields in Task 7.
            boot_script = spec.runtime_provision_script or spec.provision_script
            build_script = spec.image_build_script
            launch_line = "exec " + shlex.join(spec.run_cmd)
```

Add `launch_line: str` to `ModalAppRequest` and, in `_app.py`, replace `:55-56`:

```python
    return f"{req.provision_script}\n{req.launch_line}\n"
```

- [ ] **Step 4: Regenerate and review**

```bash
pixi run python tools/snapshot_launch_payloads.py
git diff tests/providers/golden/launch_payloads/
```

Expected: no Modal golden moves, or exactly the removed `exec` line. Paste the diff.

- [ ] **Step 5: Run and commit**

Run: `pixi run python -m pytest tests/providers tests/core -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/providers/modal tests/providers
git commit -m "feat(modal): partition setup steps on bakeable instead of two script fields"
```

---

## Task 7: Delete the old fields

**Goal:** Remove `provision_script`, `image_build_script`, `runtime_provision_script` and
`run_cmd` now that every producer and consumer has moved, and fix the stale comment S1 deferred.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py`, `src/kinoforge/core/spec_builder.py`, all four
  providers, `src/kinoforge/cli/_commands.py:322`,
  `src/kinoforge/diagnostics/c30_probe.py:263-318`
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (the stale
  `requirements.disk_gb` comment S1 left for this stage)
- Modify: the ~54 test modules that construct these fields

**Acceptance Criteria:**
- [ ] `rg -n 'provision_script|image_build_script|runtime_provision_script|run_cmd' src/` returns
      only `setup_steps` / `launch` call sites and the diagnostics probe's own local variable.
- [ ] `RenderedProvision` keeps `script` — the diagnostics probe and `kinoforge doctor` render a
      one-off script that has no steps — but it is documented as a rendering of the steps, not as
      the thing providers boot. If a reader of `script` remains that is NOT diagnostics, it moves
      to `combine_steps` instead.
- [ ] The `wan_t2v_server.py` comment naming `requirements.disk_gb` is corrected to
      `placement.disk_gb`. This is the change S1 deferred BECAUSE it dirties ~20 goldens; here the
      goldens are being regenerated anyway, so it is free.
- [ ] Goldens: the RunPod ones move by exactly the comment change inside the gzip blob. Decode and
      show that the only delta is that comment.
- [ ] `pixi run test` green; `pixi run typecheck` and `pixi run lint` clean.

**Verify:** `pixi run test && pixi run typecheck && pixi run lint`

**Steps:**

- [ ] **Step 1: Delete the fields**

Remove the four fields from `RenderedProvision` and `InstanceSpec`, delete every legacy branch the
previous tasks left behind (each is marked with a "dies in Task 7" comment), and delete the
matching `consumes()` rows and `_SPEC_PORTABLE` entries.

- [ ] **Step 2: Fix the deferred comment**

```bash
rg -n 'requirements\.disk_gb' src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

Change it to `placement.disk_gb`.

- [ ] **Step 3: Migrate the tests**

Run the suite and fix what breaks. Expect roughly 54 modules; most construct an `InstanceSpec` with
`provision_script="..."` and need `setup_steps=(SetupStep("..."),)` plus a `launch=`. Do NOT add a
launch where the test does not need one — a test that only cares about setup should pass
`launch=None` and never reach `render_launch`.

- [ ] **Step 4: Regenerate and review the goldens**

```bash
pixi run python tools/snapshot_launch_payloads.py
pixi run python - <<'PY'
import base64, gzip, json, subprocess
p = "tests/providers/golden/launch_payloads/runpod-diffusers-wan-2_2-14b-t2v.json"
def script(doc):
    env = {e["key"]: e["value"] for e in doc["input"]["env"]}
    return gzip.decompress(base64.b64decode(env["KINOFORGE_PROVISION_SCRIPT"])).decode()
old = script(json.loads(subprocess.run(["git","show",f"HEAD:{p}"],capture_output=True,text=True,check=True).stdout))
new = script(json.load(open(p)))
import difflib
print("\n".join(difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=1)))
PY
```

Expected: one changed comment line. Paste it. Anything else is a regression.

- [ ] **Step 5: Run and commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(core): delete provision_script, the two script splits, and run_cmd"
```

---

## Task 8: Live smoke — setup terminates and the server starts (USER GATE)

**Goal:** Prove on real infrastructure that the split boots the same config the same way, and that
`Task.setup` now terminates instead of hanging on a server command.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current
> conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or
> by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been
> re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_compute_seam_s3_setup_run_smoke.py`,
  `tests/live/_s3_smoke_evidence.json`

**Acceptance Criteria:**
- [ ] The RED scaffold is COMMITTED BEFORE any live spend — project durability rule.
- [ ] `pixi run preflight` exits 0 before the run, and its exit code is recorded in the evidence.
- [ ] `examples/configs/skypilot-cpu.yaml` launches through `build_provider_for`, reaches `ready`,
      and its captured `task_config["setup"]` contains NO line matching the config's own launch
      command — the thing that was true of comfyui by luck and false of diffusers.
- [ ] `task_config["run"]` equals `render_launch(spec.launch)` for that config, captured live and
      compared against the value computed offline from the same config.
- [ ] Teardown convergent and verified AFTER the process exits: both `kinoforge list` lines,
      `sky status`, and EC2 state. Reuse the S1/S2 teardown helper by importing it from
      `tests/live/test_compute_seam_s1_smoke.py`; say so in the report.
- [ ] Utilisation polled every 60–90 s and recorded; spend recorded and under $0.06 for the run.
      S2 measured $0.0425 on this SKU; the ceiling allows for one slow image pull.
- [ ] **Explicitly out of scope, and stated in the evidence:** the diffusers-on-SkyPilot double
      launch is fixed and proven OFFLINE (Task 5's goldens and tests). Proving it live needs a GPU
      config and roughly $1; that is a deliberate deferral, not an oversight.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s3_setup_run_smoke.py -v -s` → PASS, followed by `pixi run kinoforge list` showing both empty lines.

**Steps:**

- [ ] **Step 1: Write the scaffold and commit it RED**

Model it on `tests/live/test_compute_seam_s2_region_smoke.py`, which is green and already carries
the util-poll loop, the evidence shape and the imported teardown. Two structural differences: the
claims are about `task_config["setup"]` / `["run"]` rather than about region, and the offline
expectation for `run` is computed from the same config via `render_launch` so the assertion
compares two independently-derived values rather than asserting a literal.

```bash
git add tests/live/test_compute_seam_s3_setup_run_smoke.py
git commit -m "test(live): add the RED S3 setup/run split smoke scaffold"
```

- [ ] **Step 2: Preflight**

Run: `pixi run preflight` → expect exit 0. If it exits 1 on a transient RunPod SSL handshake
timeout, re-run — that was observed twice during S2 and is recorded in PROGRESS.

- [ ] **Step 3: Run the smoke, polling throughout**

Run: `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s3_setup_run_smoke.py -v -s`

Do NOT run other `aws` commands while it is in flight — S2 proved a concurrent
`describe-instances` pushes the smoke's own query past its 120 s ceiling.

- [ ] **Step 4: Verify teardown after exit**

```bash
pixi run kinoforge list
pixi run -e live-skypilot sky status
```

Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` AND
`No existing clusters.`

- [ ] **Step 5: Write evidence and commit**

Follow `tests/live/_s2_smoke_evidence.json`'s shape. Local timezone, no credential value anywhere.

```bash
pixi run pre-commit run --all-files
git add tests/live/_s3_smoke_evidence.json tests/live/test_compute_seam_s3_setup_run_smoke.py
git commit -m "test(live): S3 setup/run split verified live"
```

---

## Task 9: Documentation, the design-doc correction, and PROGRESS

**Goal:** Leave the record accurate, including the one place the design doc was wrong.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` §7
- Modify: `README.md`, `SPEC.md`, `docs/breaking-changes.md`, `PROGRESS.md`

**Acceptance Criteria:**
- [ ] §7's table and its "appended by the provider as a trailing `exec <run_command>`" claim are
      corrected inline: `Launch` carries `workdir` and `exec_pid1` because comfyui needs a `cd` and
      diffusers must NOT exec. Follow the convention S1 and S2 used — correct the text in place and
      say when and why, rather than leaving a doc that contradicts the code.
- [ ] `docs/breaking-changes.md` gains an S3 entry: `RenderedProvision.script` is no longer what
      providers boot, and a custom engine that returns only `script` now provisions nothing. Name
      the migration: emit `setup_steps` and `launch`.
- [ ] README and `SPEC.md` describe the pair wherever they currently describe the single script.
- [ ] `rg -n '_strip_trailing_exec|image_build_script|runtime_provision_script'` returns nothing
      outside dated `docs/superpowers/**`.
- [ ] PROGRESS's RESUME SNAPSHOT records S3 shipped, the two live bugs it fixed with the evidence,
      which goldens moved and why, the live smoke result, and the next action (write the S4 plan:
      realized-rate check and the `find_offers` inversion).
- [ ] The S1/S2 follow-ups this stage closed are marked closed; the ones it did not (RunPod/Modal
      region wiring, the 11 ungated `tests/live` modules, the golden ratchet's non-recursive glob)
      stay listed.

**Verify:** `pixi run test && pixi run typecheck && pixi run lint` → all green; the `rg` check above returns clean.

**Steps:**

- [ ] **Step 1: Correct the design doc** §7, table and prose.
- [ ] **Step 2: Sweep for stale references**

```bash
rg -n '_strip_trailing_exec|image_build_script|runtime_provision_script|provision_script' \
   README.md SPEC.md DESIGN.md docs/ --glob '!docs/superpowers/**'
```

- [ ] **Step 3: Update README + SPEC** with the steps/launch pair.
- [ ] **Step 4: Add the S3 entry to `docs/breaking-changes.md`**, following the S2 entry's format.
- [ ] **Step 5: Update PROGRESS.md** — new RESUME SNAPSHOT block, previous one demoted.
- [ ] **Step 6: Full suite, then commit**

```bash
pixi run test && pixi run typecheck && pixi run lint
pixi run pre-commit run --all-files
git add -A
git commit -m "docs(compute-seam): document the setup/run split and correct the design doc"
```

---

## Self-Review Notes

**Spec coverage:** §7 (setup/run split, `_strip_trailing_exec` deleted, `SetupStep.bakeable`
replacing the two script fields) → Tasks 0–7. §10's round-trip requirement → the byte-identity
invariant in Tasks 2–4 and the reviewed regenerations in Tasks 5–7. §10's per-stage live smoke →
Task 8.

**Where this plan departs from the design, deliberately:** §7 assumes the launch line is
`exec <run_command>` and that a provider can compose it from `run_cmd`. Two of three shipped
engines disprove that (header table). `Launch(argv, workdir, exec_pid1)` is the correction; Task 9
writes it back into the doc.

**Deliberately NOT in this plan:** renaming `run_cmd` to `run_command` per the design's end-state
block — `Launch.argv` supersedes it, so the rename would be churn on a field being deleted. The
realized-rate check and the `find_offers` inversion are S4. Wiring `region` on RunPod or Modal
stays where S2 left it.

**The ordering that matters:** Tasks 2–3 (engines emit) must land before Tasks 4–6 (providers
consume), or a provider reads an empty `setup_steps` and falls back forever. Task 7 (delete) must
land after all of them, or the tree goes red for every one of the ~54 test modules that construct
the old fields. Task 5 is the only task permitted to move a SkyPilot golden; Task 7 is the only one
permitted to move a RunPod golden.
