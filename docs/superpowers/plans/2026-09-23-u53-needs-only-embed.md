# U53 Needs-Only Embed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring all thirteen shipped RunPod diffusers configs under RunPod's ~101 KB `podFindAndDeployOnDemand` env-payload ceiling by embedding only the `servers/` modules each pod actually imports, and add a guard that keeps them there.

**Architecture:** `_render_embed_lines` walks a *package directory*, so `embed_modules: ["kinoforge.engines.diffusers.servers"]` ships all seven files in `servers/` to every pod — including `minimax_h3_server.py`, `_lora.py` and `_av_io.py`, which exist for the Modal-only H3 server and are imported by no RunPod config. Replacing that one entry with three `embed_files` entries (a mechanism that already exists) removes ~39.4 KB from every config. A new closure test asserts the embedded set equals the imported set, in both directions, so the fat cannot return.

**Tech Stack:** Python 3.13, pytest, pydantic configs (YAML), RunPod GraphQL provider, `ast` for static import analysis.

**Spec:** `docs/superpowers/specs/2026-09-23-u53-needs-only-embed-design.md`

## Global Constraints

- **The outer gzip in `_encode_provision_script` stays.** It saves 26.2% (44,332 B measured on the worst config). U53's filed fix direction says to drop it; that is measurably backwards and this plan must not follow it. See spec §2.
- **Only the `kinoforge.engines.diffusers.servers` package embed changes.** The `kinoforge.upscalers.flashvsr`, `kinoforge.interpolators.rife` and `kinoforge.upscalers.spandrel` `embed_modules` entries stay whole-package, untouched. Spec §3.
- **The closure rule is the FULL closure (nested imports included, `if TYPE_CHECKING:` blocks skipped), restricted to modules under `kinoforge.engines.diffusers.servers`.** A module-level-only closure drops `servers._util_stats` — imported lazily inside a function but needed by every pod, because it backs the `/util` route CLAUDE.md's live-smoke polling rule depends on. Spec §4.
- **Modal configs are out of scope.** `modal-diffusers-minimax-h3-*` genuinely need all three dropped modules.
- **Golden ordering is load-bearing:** run `pixi run pre-commit run --all-files` **before** `tools/snapshot_launch_payloads.py`, never after. Regenerating first produces goldens the formatter then invalidates.
- **Live spend rules (CLAUDE.md):** `pixi run preflight` must exit 0 before any live task; any RED scaffold driving spend must be committed before the spend is invoked; poll `/util` for `gpuUtilPercent`, never `est_spend`; pass `--no-reuse`; verify teardown with `kinoforge list` **after** the orchestrator exits; frame-QA every output video before reporting green.
- Measured target figures (spec §1): worst config `88,575 B` (was `127,917`), all thirteen under `101,000`, headroom `12,425 B` to `51,021 B`.

**User decisions (already made):**
- "Needs-only embed" chosen over tar.gz consolidation and over fetching source at boot — *"Embed only the modules each config actually imports, keeping today's per-file gzip+base64 and the outer gzip."* (AskUserQuestion, 2026-09-23)
- Spec approved with no changes: *"no changes"* (2026-09-23).
- Work U53 first among the twelve open defects: *"push these commits then start on U53"* (2026-09-23).

---

### Task 1: Embed-closure guard test

**Goal:** A test that computes each RunPod diffusers config's `servers/` import closure from its rendered provision script and asserts the embedded set equals it, failing RED today on the three unimported modules.

**Files:**
- Create: `tests/providers/test_pod_embed_closure.py`
- Test: `tests/providers/test_pod_embed_closure.py` (self-testing)

**Acceptance Criteria:**
- [ ] The closure helper follows nested (function-level) imports and skips `if TYPE_CHECKING:` blocks
- [ ] Entry points are discovered from the rendered provision script's `python -m kinoforge…` lines, not hardcoded
- [ ] `test_every_imported_servers_module_is_embedded` passes on all thirteen configs today (nothing is currently under-embedded)
- [ ] `test_no_unimported_servers_module_is_embedded` FAILS on all thirteen configs today, naming `_av_io`, `_lora` and `minimax_h3_server`
- [ ] The module docstring records that the static closure is sound only because no embedded module dynamically imports a `kinoforge.*` module, and names the three `importlib.import_module` sites that were checked

**Verify:** `pixi run pytest tests/providers/test_pod_embed_closure.py -v` → `test_every_imported…` 13 passed, `test_no_unimported…` 13 failed naming the three modules

**Steps:**

- [ ] **Step 1: Write the test module**

```python
"""Behavior: a RunPod diffusers pod embeds exactly the ``servers/`` modules its
entry points import — no unimported module rides along, and no imported module
is missing.

Why this guard exists
---------------------
``_render_embed_lines`` (``engines/diffusers/__init__.py``) walks a PACKAGE
DIRECTORY, so ``embed_modules: ["kinoforge.engines.diffusers.servers"]`` shipped
all seven files in that package to every pod. Three of them —
``minimax_h3_server.py``, ``_lora.py``, ``_av_io.py`` — exist for the MiniMax-H3
server, which runs on Modal only, and are imported by no RunPod config. They cost
~39.4 KB of rendered env per config and pushed 8 of 13 shipped configs past the
~101 KB point where ``podFindAndDeployOnDemand`` returns a raw HTTP 500 with no
GraphQL error body (CLAUDE.md "Known infra gotchas"; U53).

Fixing the configs without a guard invites the fat straight back: the next module
added under ``servers/`` would ride onto every pod again, and the only symptom is
an unexplained 500 at create. Hence both directions are asserted.

The closure rule, and why it is NOT module-level-only
----------------------------------------------------
The closure follows ALL imports, including ones nested inside functions, and is
then restricted to modules under ``kinoforge.engines.diffusers.servers``.

An earlier draft used module-level imports only. That is wrong.
``wan_t2v_server``'s sole module-level ``kinoforge`` import is
``servers._video_io``; ``servers._util_stats`` is imported lazily inside a
function — yet every pod needs it, because it backs the ``/util`` route that
CLAUDE.md's live-smoke polling rule depends on. Module-level-only would declare
``_util_stats`` unimported and delete it from all thirteen configs.

The restriction to ``servers/`` does the narrowing the module-level rule was
reaching for: ``wan_t2v_server`` also lazily imports the flashvsr / rife /
spandrel / seedvr2 runtimes, but those live in ``upscalers.*`` and
``interpolators.*``, outside the restriction, and stay declared per config via
whole-package ``embed_modules`` entries this guard does not touch.

``if TYPE_CHECKING:`` blocks are skipped — those imports never execute.

Soundness assumption — RE-CHECK THIS IF IT EVER FAILS ODDLY
-----------------------------------------------------------
A static AST closure cannot see ``importlib.import_module`` with a computed name.
Checked 2026-09-23: the only such calls reachable from an embedded module are
``wan_t2v_server.py:1054`` and ``minimax_h3_server.py:442`` (both resolve the
operator-supplied ``KINOFORGE_DIFFUSERS_LOAD_STUB`` dotted path, a test seam) and
``upscalers/flashvsr/_runtime.py:362`` (resolves the third-party
``diffsynth.models.wan_video_dit``). None names a ``kinoforge.*`` module. If a
future module dynamically imports a sibling under ``servers/``, this guard will
happily delete it and the pod will die at first request — so a new
``import_module`` call in this package must be added to this list or given a
static import.
"""

from __future__ import annotations

import ast
import base64
import gzip
import re
from pathlib import Path

import pytest

from tests.providers.test_env_payload_ceiling import _runpod_diffusers_pod_configs
from tools.snapshot_launch_payloads import capture_payload

#: Repo ``src/`` root — module-name-to-path resolution walks this, not sys.path,
#: so the guard reads the SHIPPED source rather than whatever is importable.
_SRC = Path(__file__).resolve().parents[2] / "src"

#: The only package this guard governs. See the module docstring.
_SERVERS_PKG = "kinoforge.engines.diffusers.servers"


def _module_path(mod: str) -> Path | None:
    """Resolve a dotted module name to its file under ``src/``.

    Args:
        mod: Dotted module name, e.g. ``kinoforge.core.errors``.

    Returns:
        Path to the ``.py`` file (or the package ``__init__.py``), or ``None``
        when the name is not a module — e.g. a symbol pulled in by
        ``from x import y`` where ``y`` is a function, not a submodule.
    """
    rel = Path(mod.replace(".", "/"))
    for candidate in (_SRC / rel.with_suffix(".py"), _SRC / rel / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _kinoforge_imports(path: Path) -> set[str]:
    """Return every ``kinoforge.*`` name imported by ``path``, nested included.

    Walks the whole AST so imports inside functions count (``_util_stats`` is
    one). Skips ``if TYPE_CHECKING:`` bodies, whose imports never execute.
    Relative imports are resolved against the file's own package.

    Args:
        path: Source file to scan.

    Returns:
        Dotted names — a mix of real modules and symbols imported from them;
        :func:`_module_path` filters the non-modules out downstream.
    """
    found: set[str] = set()
    pkg = ".".join(path.relative_to(_SRC).parts[:-1])

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            return
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kinoforge"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else pkg
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if mod.startswith("kinoforge"):
                found.add(mod)
                found.update(f"{mod}.{a.name}" for a in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def _closure(roots: list[str]) -> set[str]:
    """Transitively expand ``roots`` over ``kinoforge.*`` imports.

    Args:
        roots: Dotted module names to start from.

    Returns:
        Every resolvable module reachable from ``roots``, roots included.
    """
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        path = _module_path(mod)
        if path is None:
            continue
        seen.add(mod)
        todo.extend(_kinoforge_imports(path))
    return seen


def _rendered_script(cfg_path: Path) -> str:
    """Return the provision script a config would send, decoded.

    ``_encode_provision_script`` gzips then base64s the script into
    ``KINOFORGE_PROVISION_SCRIPT``; this reverses both.

    Args:
        cfg_path: Path to a RunPod diffusers pod config.

    Returns:
        The decoded bash provision script.
    """
    env = {e["key"]: e["value"] for e in capture_payload(cfg_path)["input"]["env"]}
    blob = env["KINOFORGE_PROVISION_SCRIPT"]
    return gzip.decompress(base64.b64decode(blob)).decode("utf-8")


def _entry_points(script: str) -> list[str]:
    """Return the dotted modules the provision script runs via ``python -m``.

    Discovered rather than hardcoded so a config that changes its server_cmd or
    adds a build-phase ``python -m`` step is covered without editing this test.

    Args:
        script: Decoded provision script.

    Returns:
        Dotted ``kinoforge.*`` module names, in first-appearance order.
    """
    seen: list[str] = []
    for match in re.finditer(r"python3?\s+-m\s+(kinoforge[\w.]*)", script):
        mod = match.group(1)
        if mod not in seen:
            seen.append(mod)
    return seen


def _embedded_servers_modules(script: str) -> set[str]:
    """Return which ``servers/`` modules the script writes onto the pod.

    Both embed renderers end each write with ``> /tmp/kfsrv/<rel path>.py``;
    empty ancestor ``__init__.py`` files are created with ``touch`` and are
    deliberately NOT counted — they carry no code, only package-ness.

    Args:
        script: Decoded provision script.

    Returns:
        Dotted module names under :data:`_SERVERS_PKG`.
    """
    out: set[str] = set()
    pkg_rel = _SERVERS_PKG.replace(".", "/")
    for match in re.finditer(rf"> /tmp/kfsrv/({pkg_rel}/[\w/]+)\.py", script):
        out.add(match.group(1).replace("/", "."))
    return {m for m in out if not m.endswith(".__init__")}


def _servers_closure(cfg_path: Path) -> set[str]:
    """Return the ``servers/`` modules a config's pod entry points import.

    Args:
        cfg_path: Path to a RunPod diffusers pod config.

    Returns:
        Dotted module names under :data:`_SERVERS_PKG`.
    """
    script = _rendered_script(cfg_path)
    reachable = _closure(_entry_points(script))
    return {m for m in reachable if m.startswith(_SERVERS_PKG) and m != _SERVERS_PKG}


_CONFIGS = sorted(_runpod_diffusers_pod_configs())
_IDS = [p.stem for p in _CONFIGS]


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=_IDS)
def test_every_imported_servers_module_is_embedded(cfg_path: Path) -> None:
    """Nothing the pod imports from ``servers/`` is missing from the payload.

    Bug caught: trimming the embed set too far. The pod boots, ``/health``
    reports ready, and the first request that reaches the missing module dies
    with ``ModuleNotFoundError`` — a create-time HTTP 500 traded for a
    runtime failure after the GPU is already billing. This is the direction
    that protects against the fix in this plan being overzealous.

    Args:
        cfg_path: One shipped RunPod diffusers pod config.
    """
    script = _rendered_script(cfg_path)
    missing = sorted(_servers_closure(cfg_path) - _embedded_servers_modules(script))
    assert not missing, (
        f"{cfg_path.stem}: pod entry points {_entry_points(script)} import "
        f"{missing} from {_SERVERS_PKG}, but the provision script does not "
        f"embed them — the pod will boot and then fail at first use. Add them "
        f"to the config's embed_files."
    )


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=_IDS)
def test_no_unimported_servers_module_is_embedded(cfg_path: Path) -> None:
    """No ``servers/`` module the pod never imports rides along in the payload.

    Bug caught: the U53 breach itself, and its return. A package-directory
    embed ships every file in ``servers/``, so adding one module there inflates
    every RunPod diffusers config's env payload — whose only symptom past
    ~101 KB is a raw HTTP 500 at create with no GraphQL error body, the shape
    that cost a full session to root-cause on 2026-07-05.

    Args:
        cfg_path: One shipped RunPod diffusers pod config.
    """
    script = _rendered_script(cfg_path)
    extra = sorted(_embedded_servers_modules(script) - _servers_closure(cfg_path))
    assert not extra, (
        f"{cfg_path.stem}: provision script embeds {extra} from {_SERVERS_PKG}, "
        f"which no pod entry point ({_entry_points(script)}) imports. Each "
        f"unimported module is dead weight against RunPod's ~101 KB "
        f"create-mutation ceiling (U53). Embed only what is imported — see "
        f"docs/superpowers/specs/2026-09-23-u53-needs-only-embed-design.md."
    )
```

- [ ] **Step 2: Run the test — confirm one direction passes and the other fails**

Run: `pixi run pytest tests/providers/test_pod_embed_closure.py -v`

Expected: `test_every_imported_servers_module_is_embedded` — **13 passed** (nothing is under-embedded today). `test_no_unimported_servers_module_is_embedded` — **13 failed**, each naming exactly:

```
['kinoforge.engines.diffusers.servers._av_io',
 'kinoforge.engines.diffusers.servers._lora',
 'kinoforge.engines.diffusers.servers.minimax_h3_server']
```

If any config names a different set, STOP and report — the config inventory has changed since the spec was measured.

- [ ] **Step 3: Commit the RED guard**

```bash
git add tests/providers/test_pod_embed_closure.py
git commit -m "test: add embed-closure guard for RunPod diffusers pods (RED)

Asserts each config embeds exactly the servers/ modules its pod entry
points import. The under-embed direction passes today; the over-embed
direction fails on all 13 configs, naming minimax_h3_server.py, _lora.py
and _av_io.py — the three H3-only modules a package-directory embed ships
to every RunPod pod (U53)."
```

---

### Task 2: Live RED probe — does the oversized create actually 500?

**Goal:** Record, before any config changes, whether `runpod-diffusers-spandrel-x2-upscale` (112,831 B) is refused by `podFindAndDeployOnDemand` today — the red half of the live proof, at $0.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before the run
- [ ] The probe uses the **shipped** `kinoforge upscale` path — no bespoke tool, so there is no scaffold to commit and no hand-written GraphQL to drift from the real mutation
- [ ] The literal failure (or success) is captured verbatim to `tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt` and committed
- [ ] If the create SUCCEEDS, `--no-reuse` auto-destroys the pod, `kinoforge list` confirms it AFTER the orchestrator exits, and the result is reported as "the ~101 KB figure is looser than recorded" rather than dropped
- [ ] The rendered env byte count is recorded alongside the outcome, so the evidence file is self-describing

**Verify:** `cat tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt` → shows the measured byte count and either a create failure, or a created pod plus confirmed teardown

**Steps:**

- [ ] **Step 1: Record the pre-fix measurement and confirm the config resolves**

```bash
mkdir -p tests/live/evidence/2026-09-23-u53-env-payload
pixi run python -c "
from pathlib import Path
from tests.providers.test_env_payload_ceiling import _rendered_env_bytes
p = Path('examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml')
print(f'config       : {p.stem}')
print(f'rendered env : {_rendered_env_bytes(p):,} B')
print('ceiling      : ~101,000 B (CLAUDE.md \'Known infra gotchas\')')
" | tee tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt

pixi run kinoforge upscale \
  --config examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
  --video examples/configs/grids/_fixtures/wan21_strength_cell0.mp4 \
  --dry-run
```

Expected: `112,831 B`, and `--dry-run` emits a resolved plan and exits 0 with no pod work. `--dry-run` is a real flag on this subcommand (verified via `kinoforge upscale --help`).

- [ ] **Step 2: Preflight**

```bash
git status --porcelain
pixi run preflight
```

`preflight` must exit 0 and the tree must be clean (Task 1's guard is already committed). If preflight fails, STOP and report which check failed.

There is deliberately **no scaffold to commit** in this task: the probe is the shipped CLI. CLAUDE.md's RED-scaffold rule applies to generated tools that drive spend; there is no such tool here, which is the point — a hand-built GraphQL body could diverge from the mutation the provider actually sends, and then a "refused" result would prove nothing about the real path.

- [ ] **Step 3: Attempt the create on the un-fixed config**

```bash
pixi run kinoforge upscale \
  --config examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
  --video examples/configs/grids/_fixtures/wan21_strength_cell0.mp4 \
  --no-reuse \
  2>&1 | tee -a tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt
```

Expected (the recorded hypothesis): the create fails with a raw HTTP 500 whose body is *not* a GraphQL `errors[]` document. **Cost $0** — RunPod validates the request body before booking hardware.

If the create instead SUCCEEDS, the run proceeds to a real upscale (~4 min, ~$0.08) and `--no-reuse` destroys the pod at the end. That is an acceptable outcome, not a failure: it means the ceiling is looser than recorded, and it also delivers Task 7's green half early. Record it as such.

- [ ] **Step 4: Verify teardown regardless of outcome**

```bash
pixi run kinoforge list 2>&1 | tee -a tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt
```

Expected: BOTH `[instance overview] No running instances.` AND `No instances recorded in ledger.` A mid-run "No running instances" line is NOT proof. If either line shows a pod: `pixi run kinoforge destroy --id <pod-id>`, then re-check and append.

- [ ] **Step 5: Commit the evidence**

```bash
git add tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt
git commit -m "test(evidence): U53 red half — pre-fix create outcome captured

runpod-diffusers-spandrel-x2-upscale at 112,831 B, against the ~101 KB
create-mutation ceiling. Driven through the shipped kinoforge upscale path so
the result speaks about the real mutation, not a hand-built body."
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["112,831", "rendered env"], ["No running instances", "No instances recorded in ledger"]], "files": ["tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt"], "verifyCommand": "cat tests/live/evidence/2026-09-23-u53-env-payload/red-oversized-create.txt", "acceptanceCriteria": ["preflight exits 0 and tree is clean before the run", "probe uses the shipped kinoforge upscale path, no bespoke tool", "literal create outcome captured verbatim to the evidence file", "kinoforge list AFTER the orchestrator exits shows both no-instances lines", "rendered env byte count recorded beside the outcome"], "modelTier": "standard"}
```

---

### Task 3: Switch the thirteen configs to needs-only embeds

**Goal:** Replace each RunPod diffusers config's `kinoforge.engines.diffusers.servers` package embed with `embed_files` entries for the three modules its pod imports, turning Task 1's guard green.

**Files:**
- Modify: `examples/configs/runpod-diffusers-flashvsr-1080p-upscale.yaml:40-45`
- Modify: `examples/configs/runpod-diffusers-flashvsr-x4-torch26-upscale.yaml:38-43`
- Modify: `examples/configs/runpod-diffusers-flashvsr-x4-upscale.yaml:57-65`
- Modify: `examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml:32-38`
- Modify: `examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml:31-39`
- Modify: `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml:33`
- Modify: `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml:41`
- Modify: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale.yaml:53-61`
- Modify: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml:60-68`
- Modify: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml:40-41`
- Modify: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale.yaml:41-49`
- Modify: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml:41-42`
- Modify: `examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml:71-72`

**Acceptance Criteria:**
- [ ] Every config's `embed_modules` list no longer contains `"kinoforge.engines.diffusers.servers"`; `embed_modules` is removed entirely where that was its only entry
- [ ] Every config's `embed_files` contains the three server modules, merged into any existing `embed_files` list rather than duplicating the key
- [ ] `kinoforge.upscalers.flashvsr`, `kinoforge.interpolators.rife` and `kinoforge.upscalers.spandrel` `embed_modules` entries are unchanged where present
- [ ] Both directions of Task 1's guard pass on all thirteen configs
- [ ] Every config still loads (`load_config` raises nothing)
- [ ] Measured rendered env for the worst config (`runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale`) is 88,575 B ± 200 B

**Verify:** `pixi run pytest tests/providers/test_pod_embed_closure.py tests/test_examples.py -v` → all passed

**Steps:**

- [ ] **Step 1: Apply the edit to one config first and measure**

For `examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml`, replace lines 71-72:

```yaml
    embed_modules:
      - "kinoforge.engines.diffusers.servers"
```

with:

```yaml
    # Needs-only embed (U53). A whole-package embed of
    # kinoforge.engines.diffusers.servers also shipped minimax_h3_server.py,
    # _lora.py and _av_io.py — ~39.4 KB of env payload for modules only the
    # Modal-only H3 server imports, which pushed 8 of 13 RunPod diffusers
    # configs past the ~101 KB create-mutation ceiling. Enforced by
    # tests/providers/test_pod_embed_closure.py; see
    # docs/superpowers/specs/2026-09-23-u53-needs-only-embed-design.md.
    embed_files:
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
      - "kinoforge.engines.diffusers.servers._util_stats"
      - "kinoforge.engines.diffusers.servers._video_io"
```

- [ ] **Step 2: Measure that one config before touching the rest**

```bash
pixi run python -c "
from pathlib import Path
from tests.providers.test_env_payload_ceiling import _rendered_env_bytes
p = Path('examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml')
print(f'{_rendered_env_bytes(p):,} B')
"
```

Expected: `49,979 B` ± 200 B (was 89,389 B). If the number is far off, STOP — the embed did not take effect, or an ancestor `__init__.py` is being written with content.

- [ ] **Step 3: Apply the same change to the remaining twelve configs**

The three `embed_files` entries are identical in every config. Two shapes to handle:

*Shape A — `embed_modules` had only the servers entry* (`wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke`, `wan-2_1-1_3b-t2v-strength-grid`, `wan-2_2-14b-t2v-lora-flexible-warm-reuse-release`, `wan-2_2-14b-t2v-strength-grid`, `wan-2_2-14b-t2v`): delete the `embed_modules` key entirely and add the `embed_files` block above. Note two of these use the inline form `embed_modules: ["kinoforge.engines.diffusers.servers"]` on a single line.

*Shape B — `embed_modules` also lists an upscaler/interpolator package* (`flashvsr-1080p-upscale`, `flashvsr-x4-torch26-upscale`, `flashvsr-x4-upscale`, `rife-60fps-interpolate`, `spandrel-x2-upscale`, `wan-2_2-14b-t2v-flashvsr-1080p-upscale`, `wan-2_2-14b-t2v-flashvsr-upscale`, `wan-2_2-14b-t2v-spandrel-upscale`): remove ONLY the `"kinoforge.engines.diffusers.servers"` line from `embed_modules`, leave the other entry, and append the three server modules to the config's **existing** `embed_files` list — do not add a second `embed_files` key. Example for `spandrel-x2-upscale`:

```yaml
    embed_modules:
      # SpandrelRuntime + SpandrelEngine for the on-pod /upscale handler.
      - "kinoforge.upscalers.spandrel"
    embed_files:
      # SpandrelRuntime imports kinoforge.core.errors + .scale_target at
      # the first /upscale call. Single-file embeds keep the bootstrap
      # script under RunPod's env-payload ceiling.
      - "kinoforge.core.errors"
      - "kinoforge.core.scale_target"
      # Needs-only embed (U53) — see
      # docs/superpowers/specs/2026-09-23-u53-needs-only-embed-design.md.
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
      - "kinoforge.engines.diffusers.servers._util_stats"
      - "kinoforge.engines.diffusers.servers._video_io"
```

- [ ] **Step 4: Run the guard — both directions must now pass**

Run: `pixi run pytest tests/providers/test_pod_embed_closure.py -v`
Expected: **26 passed** (13 configs × 2 directions), 0 failed.

- [ ] **Step 5: Print the full measured table**

```bash
pixi run python -c "
from tests.providers.test_env_payload_ceiling import (
    _rendered_env_bytes, _runpod_diffusers_pod_configs)
for p in sorted(_runpod_diffusers_pod_configs()):
    n = _rendered_env_bytes(p)
    print(f'{n:>10,}  {\"under\" if n < 101_000 else \"OVER \"}  {p.stem}')
"
```

Expected: all thirteen `under`, worst `88,575`. Keep this output — Task 4 copies it into `_BASELINE_BYTES`.

- [ ] **Step 6: Confirm configs still load and examples tests pass**

Run: `pixi run pytest tests/test_examples.py -v`
Expected: PASS (includes `test_diffusers_lora_configs_pip_install_peft`, which must stay green).

- [ ] **Step 7: Commit**

```bash
git add examples/configs/runpod-diffusers-*.yaml
git commit -m "fix(runpod): embed only the servers/ modules each pod imports

Every RunPod diffusers config declared a whole-package embed of
kinoforge.engines.diffusers.servers, which walks the package directory and
so shipped all seven files — including minimax_h3_server.py, _lora.py and
_av_io.py, which exist for the Modal-only H3 server and are imported by no
RunPod config. That is ~39.4 KB of rendered env per config, and it pushed
8 of 13 shipped configs past the ~101 KB point where
podFindAndDeployOnDemand returns a raw HTTP 500 with no GraphQL error body.

Replaced with embed_files entries for the three modules wan_t2v_server's
import closure actually reaches: wan_t2v_server, _util_stats, _video_io.
_util_stats is imported lazily but is not optional — it backs /util, which
the live-smoke polling rule depends on.

Worst config 127,917 -> 88,575 B; all thirteen now under the ceiling with
12,425-51,021 B of headroom. No encoding change: the outer gzip stays (it
saves 26.2%, contrary to U53's filed fix direction).

Closes the over-embed direction of tests/providers/test_pod_embed_closure.py."
```

---

### Task 4: Re-baseline the ratchet and retire the breach exemption

**Goal:** Lower `_BASELINE_BYTES` to the new measurements and replace the dated `_SAFE_AS_OF_2026_09_22` name list with an unconditional "every RunPod diffusers config is under the ceiling" assertion.

**Files:**
- Modify: `tests/providers/test_env_payload_ceiling.py` (module docstring, `_BASELINE_BYTES`, `_SAFE_AS_OF_2026_09_22`, `test_configs_safe_as_of_2026_09_22_never_cross_the_ceiling`)

**Acceptance Criteria:**
- [ ] `_BASELINE_BYTES` holds the thirteen new measurements from Task 3 Step 5, every value under 101,000
- [ ] `_SAFE_AS_OF_2026_09_22` is deleted
- [ ] `test_configs_safe_as_of_2026_09_22_never_cross_the_ceiling` is replaced by a test parametrised over **every** RunPod diffusers pod config asserting `measured < _RUNPOD_CEILING_BYTES`
- [ ] The module docstring's "As of 2026-09-22 these configs are ALREADY OVER the ~101 KB ceiling" section and its eight-name list are replaced by a record of the U53 fix
- [ ] The re-baselining instructions at the end of the docstring still work verbatim

**Verify:** `pixi run pytest tests/providers/test_env_payload_ceiling.py -v` → all passed, 13 ceiling tests

**Steps:**

- [ ] **Step 1: Replace the docstring's breach section**

Delete the paragraph beginning *"As of 2026-09-22 these configs are ALREADY OVER the ~101 KB ceiling"* together with its eight-name bullet list and the following *"Shrinking what gets embedded into every pod's boot script is its own project"* paragraph. Replace with:

```
That breach is CLOSED as of 2026-09-23 (U53). The cause was not the encoding
but what got embedded: ``embed_modules: ["kinoforge.engines.diffusers.servers"]``
walks a package DIRECTORY, so every RunPod diffusers pod carried
``minimax_h3_server.py``, ``_lora.py`` and ``_av_io.py`` — ~39.4 KB per config,
for modules only the Modal-only H3 server imports. The configs now name the
three server modules they actually import via ``embed_files``, which took the
worst config from 127,917 B to 88,575 B and every config under the ceiling.
``tests/providers/test_pod_embed_closure.py`` is what keeps them there; this
module guards the byte budget, that one guards the embed set.
```

- [ ] **Step 2: Replace `_BASELINE_BYTES` with the Task 3 measurements**

```python
#: Committed snapshot of each shipped RunPod diffusers pod config's measured
#: rendered-env size, in bytes, re-measured 2026-09-23 after the U53 needs-only
#: embed fix (was ~39.4 KB higher per config; 8 entries were over the ceiling).
#: The ratchet test below asserts current measurements never exceed these.
_BASELINE_BYTES: dict[str, int] = {
    "runpod-diffusers-flashvsr-1080p-upscale": 88_521,
    "runpod-diffusers-flashvsr-x4-torch26-upscale": 88_553,
    "runpod-diffusers-flashvsr-x4-upscale": 88_521,
    "runpod-diffusers-rife-60fps-interpolate": 76_497,
    "runpod-diffusers-spandrel-x2-upscale": 73_369,
    "runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke": 49_987,
    "runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid": 49_987,
    "runpod-diffusers-wan-2_2-14b-t2v": 49_979,
    "runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale": 88_575,
    "runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale": 88_575,
    "runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release": 49_991,
    "runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale": 73_423,
    "runpod-diffusers-wan-2_2-14b-t2v-strength-grid": 49_991,
}
```

**Use the numbers Task 3 Step 5 actually printed, not these**, if they differ — these are the spec's measurements and should match, but the printed values are authoritative. If any printed value differs by more than 200 B from the above, STOP and report before editing.

- [ ] **Step 3: Delete `_SAFE_AS_OF_2026_09_22` and replace its test**

Delete the `_SAFE_AS_OF_2026_09_22` frozenset and its docstring comment entirely. Replace `test_configs_safe_as_of_2026_09_22_never_cross_the_ceiling` with:

```python
@pytest.mark.parametrize("stem", sorted(_BASELINE_BYTES))
def test_no_runpod_diffusers_config_crosses_the_ceiling(stem: str) -> None:
    """EVERY RunPod diffusers config measures under the create-mutation ceiling.

    Unconditional, and deliberately not satisfiable by editing
    ``_BASELINE_BYTES`` — it re-measures the config and compares against the
    hard-coded :data:`_RUNPOD_CEILING_BYTES`. This replaces the dated
    ``_SAFE_AS_OF_2026_09_22`` name list, which existed only because eight
    configs were over the ceiling and could not be asserted about; U53 closed
    that breach on 2026-09-23, so the exemption is gone and the guard applies
    to all thirteen.

    Bug caught: a new embed (a module under ``servers/``, a widened pip list, a
    longer boot script) pushes a config back over the edge. The ratchet test
    above would pass if someone bumped that config's baseline; this one cannot
    be widened at all, and the failure it prevents is a raw HTTP 500 at pod
    create with no GraphQL error body to explain it.

    Args:
        stem: Config filename stem, one entry of ``_BASELINE_BYTES``.
    """
    cfg_path = _configs_by_stem()[stem]
    measured = _rendered_env_bytes(cfg_path)
    assert measured < _RUNPOD_CEILING_BYTES, (
        f"{stem}: rendered env {measured} B crossed the ~{_RUNPOD_CEILING_BYTES} "
        f"B RunPod create-mutation ceiling (CLAUDE.md 'Known infra gotchas') "
        f"— this WILL raw-500 the pod create with no GraphQL error body to "
        f"explain why. Shrink what the config embeds; do not raise the ceiling."
    )
```

- [ ] **Step 4: Update the docstring's numbered list of what the tests assert**

Point 3 currently describes the frozen name list. Replace its text with a description of the unconditional test from Step 3, keeping points 1 and 2 as they are.

- [ ] **Step 5: Run the test file**

Run: `pixi run pytest tests/providers/test_env_payload_ceiling.py -v`
Expected: `test_baseline_covers_every_shipped_runpod_diffusers_config` PASS, 13 ratchet tests PASS, 13 ceiling tests PASS. No references to `_SAFE_AS_OF_2026_09_22` remain: `rg -n '_SAFE_AS_OF' tests/` returns nothing.

- [ ] **Step 6: Commit**

```bash
git add tests/providers/test_env_payload_ceiling.py
git commit -m "test: re-baseline env-payload ratchet and retire the U53 exemption

_BASELINE_BYTES drops ~39.4 KB per config after the needs-only embed fix.
_SAFE_AS_OF_2026_09_22 — a frozen five-name list that existed only because
eight configs were over the ceiling and could not be asserted about — is
deleted, and its parametrised test replaced by an unconditional one: every
RunPod diffusers config measures under _RUNPOD_CEILING_BYTES. That is
strictly stronger, and cannot be widened by a baseline bump."
```

---

### Task 5: Re-snapshot the launch-payload goldens

**Goal:** Regenerate the affected launch-payload goldens and `_golden_provision.json` so the golden tests reflect the new embed set.

**Files:**
- Modify: `tests/providers/golden/launch_payloads/runpod-diffusers-*.json` (the 13 RunPod diffusers goldens)

**Acceptance Criteria:**
- [ ] `pixi run pre-commit run --all-files` is run and green BEFORE the snapshot tool (ordering rule — regenerating first produces goldens the formatter invalidates)
- [ ] Exactly the 13 RunPod diffusers goldens change; the other 27 are byte-identical
- [ ] `tests/engines/diffusers/_golden_provision.json` is **unchanged** — its two configs are Modal, which this plan does not touch (verified, not assumed)
- [ ] `pixi run pytest tests/providers/test_launch_payload_goldens.py tests/engines/diffusers/test_render_provision_split.py -v` passes

**Verify:** `pixi run pytest tests/providers/test_launch_payload_goldens.py tests/engines/diffusers/test_render_provision_split.py -v` → all passed

**Steps:**

- [ ] **Step 1: Formatting first, then regenerate**

```bash
pixi run pre-commit run --all-files
pixi run python -m tools.snapshot_launch_payloads
```

Expected: pre-commit green, then `wrote 40 goldens to tests/providers/golden/launch_payloads`.

- [ ] **Step 2: Confirm only the 13 RunPod diffusers goldens moved**

```bash
git status --porcelain tests/providers/golden/launch_payloads/ | wc -l
git status --porcelain tests/providers/golden/launch_payloads/
```

Expected: 13 modified files, every one matching `runpod-diffusers-*`. If a non-RunPod or non-diffusers golden moved, STOP and report — this change should not reach them.

- [ ] **Step 3: Confirm `_golden_provision.json` did NOT move**

```bash
git status --porcelain tests/engines/diffusers/_golden_provision.json
```

Expected: **no output.** That golden is built from two configs — `examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml` and `examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml` (`test_render_provision_split.py:27-28`) — both of which are Modal and therefore out of this plan's scope. There is no regeneration tool for this file; the test reads it directly, so it must be left alone.

If it DID move, STOP: a Modal config was edited by mistake, or the embed renderer itself changed. Neither is in scope — revert and report.

- [ ] **Step 4: Run the golden tests**

Run: `pixi run pytest tests/providers/test_launch_payload_goldens.py tests/engines/diffusers/test_render_provision_split.py -v`
Expected: all passed.

- [ ] **Step 5: Run the full suite**

Run: `pixi run test -q`
Expected: 0 failed. Record the pass count. Any failure here is in scope for this task — the embed change can reach tests that assert on provision-script content.

- [ ] **Step 6: Commit**

```bash
git add tests/providers/golden/launch_payloads/
git commit -m "test(golden): re-snapshot payloads after the U53 needs-only embed

The 13 RunPod diffusers launch payloads shrink by ~39.4 KB each; the other
27 goldens are byte-identical, and _golden_provision.json does not move at
all — it is built from two Modal configs this change does not touch."
```

---

### Task 6: Correct the three stale documentation claims

**Goal:** Fix the "~4× headroom" claim in CLAUDE.md, its duplicate as a source comment, and U53's inverted fix direction in PROGRESS.md.

**Files:**
- Modify: `CLAUDE.md:372-386` ("Known infra gotchas" → the RunPod 500 subsection)
- Modify: `src/kinoforge/providers/runpod/__init__.py:1316-1322` (the comment inside `_encode_provision_script`)
- Modify: `PROGRESS.md:694` (U53's table row) and `PROGRESS.md:3356-3368` (the suggested-order entry)

**Acceptance Criteria:**
- [ ] CLAUDE.md states the measured behaviour: the outer gzip saves ~26% (44,332 B on the worst config), it must NOT be dropped, and the real lever is what gets embedded
- [ ] The `_encode_provision_script` comment no longer claims ~4× headroom and points at the closure guard
- [ ] U53's PROGRESS.md row is marked FIXED with the commit, and its "Fix direction: drop the outer gzip" sentence is corrected to record that the direction was measured backwards
- [ ] The suggested-order entry at `PROGRESS.md:3360` no longer tells the next session to drop the outer gzip
- [ ] The STATUS INDEX open/fixed counts are updated: U53 moves from OPEN to fixed, so "forty-five are fixed or answered" becomes forty-six and "ten more are OPEN" becomes nine
- [ ] No other U-item row is altered

**Verify:** `rg -n '4× headroom|4x headroom' CLAUDE.md src/ PROGRESS.md` → no matches outside a correction note

**Steps:**

- [ ] **Step 1: Correct CLAUDE.md**

Replace the first bullet under `### RunPod create returns a raw HTTP 500 when the env payload exceeds ~101 KB`:

```markdown
- The provider **gzips the provision script before base64**
  (`_create_pod`, commit `5418c35`): `dockerArgs` decodes with
  `base64 -d | gzip -d`. **Measured 2026-09-23: that outer gzip saves 26.2%**
  (168,920 B plain base64 → 124,588 B gzipped, on the then-worst config).
  **Do NOT drop it** — base64 packs 64 symbols into 8-bit bytes, so gzip
  recovers the expected ~25%. An earlier note here claimed "~74 KB script →
  ~72 KB base64, ~4× headroom"; that was wrong, and headroom was in fact
  NEGATIVE for 8 of 13 shipped RunPod diffusers configs until U53 was fixed.
- **The lever is what gets embedded, not the encoding.** U53's cause was
  `embed_modules: ["kinoforge.engines.diffusers.servers"]` walking a package
  DIRECTORY, so every pod carried `minimax_h3_server.py`, `_lora.py` and
  `_av_io.py` — ~39.4 KB for modules only the Modal-only H3 server imports.
  Configs now name the server modules they import via `embed_files`;
  `tests/providers/test_pod_embed_closure.py` asserts the embedded set equals
  the imported set in both directions, and
  `tests/providers/test_env_payload_ceiling.py` asserts every config stays
  under the ceiling.
```

- [ ] **Step 2: Correct the source comment**

In `src/kinoforge/providers/runpod/__init__.py`, replace the comment inside `_encode_provision_script` that currently ends *"Gzip cuts it to ~72 KB base64 (~4× headroom for future script growth)."* with:

```python
            # Gzip BEFORE base64: RunPod's podFindAndDeployOnDemand mutation
            # returns a raw HTTP 500 (not a GraphQL error) once the total env
            # payload exceeds ~101 KB (root-caused live 2026-07-05).
            #
            # Measured 2026-09-23 (U53): this outer gzip saves 26.2% — 168,920 B
            # of plain base64 becomes 124,588 B — because base64 packs 64
            # symbols into 8-bit bytes and gzip recovers the expected ~25%.
            # An earlier version of this comment claimed "~4x headroom for
            # future script growth"; that was wrong, and 8 of 13 shipped RunPod
            # diffusers configs were over the ceiling until the embed set was
            # trimmed. Do NOT drop this gzip. What actually controls the budget
            # is which modules each config embeds — see
            # tests/providers/test_pod_embed_closure.py.
```

- [ ] **Step 3: Correct U53's PROGRESS.md row**

On the U53 row (`PROGRESS.md:694`), change the status cell from `FILED 2026-09-22, OPEN — pre-existing, found and ratcheted (not fixed) by Task 9 of the MiniMax-H3 LoRA shared seam build` to `FILED 2026-09-22, **FIXED 2026-09-23** by the needs-only embed change`. Then replace the row's final sentence — `Fix direction: drop the outer gzip (near-zero payoff today) and spend the reclaimed margin, or actually shrink what each config embeds` — with:

```
**Fixed by shrinking what gets embedded, and the filed fix direction was
measured BACKWARDS.** "Drop the outer gzip (near-zero payoff today)" is wrong:
measured 2026-09-23, that gzip saves 26.2% (168,920 B plain base64 -> 124,588 B),
because base64 packs 64 symbols into 8-bit bytes. Dropping it would have taken
the worst config from 127,917 B to ~172,200 B. The gzip stays. The actual cause
was `_render_embed_lines` walking a package DIRECTORY, so every RunPod diffusers
pod carried `minimax_h3_server.py`, `_lora.py` and `_av_io.py` (~39.4 KB) for
modules only the Modal-only H3 server imports. Configs now name their three
imported server modules via `embed_files`: worst config 127,917 -> 88,575 B, all
13 under the ceiling with 12,425-51,021 B of headroom, `_SAFE_AS_OF_2026_09_22`
retired for an unconditional guard, and
`tests/providers/test_pod_embed_closure.py` asserting embedded == imported in
both directions. Spec:
`docs/superpowers/specs/2026-09-23-u53-needs-only-embed-design.md`.
```

- [ ] **Step 4: Correct the suggested-order entry**

At `PROGRESS.md:3360`, the numbered item 1 for U53 ends with *"Note while working it: CLAUDE.md's '~4x headroom' claim for the gzip fix is ROTTED — the outer gzip buys ~0 because the script body is already base64-of-gzip. Fix that note in the same pass."* Replace that sentence with:

```
**CLOSED 2026-09-23** — and the direction suggested here was backwards: the
outer gzip saves 26.2%, not ~0, and stays. Fixed by trimming the embed set
instead. CLAUDE.md's rotted "~4x headroom" note is corrected.
```

- [ ] **Step 5: Update the STATUS INDEX counts**

At `PROGRESS.md:594`, `Forty-five are fixed or answered` becomes `Forty-six are fixed or answered` and `U53` is added to the enumeration in that parenthetical. At `PROGRESS.md:607`, `ten more are OPEN` becomes `nine more are OPEN` and `U53` is removed from that list.

Then insert this at the front of the STATUS INDEX's "Provenance, newest first" paragraph (`PROGRESS.md:473`), immediately after `(current as of 2026-09-23. Provenance, newest first:`, matching the existing entries' voice:

```
**U53 FILED + FIXED** — every shipped RunPod diffusers config was past the ~101 KB
create-mutation ceiling because `embed_modules` walks a package DIRECTORY, so every
pod carried `minimax_h3_server.py`, `_lora.py` and `_av_io.py` (~39.4 KB) for modules
only the Modal-only H3 server imports; fixed offline by naming the three imported
server modules via `embed_files` (worst config 127,917 -> 88,575 B, all 13 under the
ceiling), live-proven on `runpod-diffusers-spandrel-x2-upscale`, and guarded both
directions by `tests/providers/test_pod_embed_closure.py`. The filed fix direction
("drop the outer gzip, near-zero payoff") was measured BACKWARDS — that gzip saves
26.2% and stays. Before that:
```

Keep the existing `**U60 FILED, OPEN** — …` text immediately after, so the chain reads newest-first unbroken.

- [ ] **Step 6: Verify no stale claim survives**

```bash
rg -n '4× headroom|4x headroom|near-zero payoff' CLAUDE.md src/ PROGRESS.md
```

Expected: matches ONLY inside the correction notes written above (which quote the old claim to refute it). No live claim remains.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md src/kinoforge/providers/runpod/__init__.py PROGRESS.md
git commit -m "docs: correct the rotted ~4x-headroom claim in all three places

CLAUDE.md, the duplicate comment in _encode_provision_script, and U53's
PROGRESS row all asserted the outer gzip buys almost nothing and should be
dropped. Measured: it saves 26.2% (168,920 B plain base64 -> 124,588 B) and
dropping it would have taken the worst config to ~172,200 B. All three now
record the measurement, say the gzip stays, and point at what actually
controls the budget. U53 marked FIXED; STATUS INDEX counts updated."
```

---

### Task 7: Live GREEN proof — the create succeeds and the pod serves

**Goal:** Prove on real RunPod hardware that a previously-over-ceiling config now creates successfully AND that the pod boots and serves without the three dropped modules.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/evidence/2026-09-23-u53-env-payload/green-create-and-upscale.txt`
- Modify: `successful-generations.md` (only if this run introduces a new capability axis — see Step 7)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before any spend
- [ ] The working tree is clean and Tasks 1-6 are committed before the run
- [ ] `runpod-diffusers-spandrel-x2-upscale` — the same config Task 2 probed — creates a pod successfully (no HTTP 500)
- [ ] The pod reaches ready and serves an `/upscale` against `examples/configs/grids/_fixtures/wan21_strength_cell0.mp4`, producing a playable output video
- [ ] `/util` was polled every 60-90 s and `gpuUtilPercent` read non-zero during the upscale; `est_spend` was NOT used as the health signal
- [ ] Frames were extracted with `kinoforge.core.frames.ffmpeg_frames_by_count` and visually reviewed; the verdict is recorded in the evidence file
- [ ] `--no-reuse` was passed, and `pixi run kinoforge list` AFTER the orchestrator exited shows BOTH `[instance overview] No running instances.` AND `No instances recorded in ledger.`
- [ ] The evidence file cites Task 2's red result explicitly, so the before/after pair is readable in one place
- [ ] Total spend recorded and under $1.00

**Verify:** `cat tests/live/evidence/2026-09-23-u53-env-payload/green-create-and-upscale.txt` → shows create success, non-zero GPU util, frame-QA verdict, and the post-run `kinoforge list` output

**Steps:**

- [ ] **Step 1: Preflight and confirm a clean tree**

```bash
git status --porcelain
pixi run preflight
```

Expected: no output from `git status`, and `preflight` exits 0. If the tree is dirty, commit or stash before spending — CLAUDE.md forbids launching on an uncommitted tree.

- [ ] **Step 2: Re-run the RED probe's measurement to show the delta**

```bash
pixi run python -c "
from pathlib import Path
from tests.providers.test_env_payload_ceiling import _rendered_env_bytes
p = Path('examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml')
print(f'spandrel-x2-upscale now: {_rendered_env_bytes(p):,} B (was 112,831 B)')
"
```

Expected: `73,369 B` ± 200 B.

- [ ] **Step 3: Run the upscale, polling utilisation**

```bash
pixi run kinoforge upscale \
  --config examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
  --video examples/configs/grids/_fixtures/wan21_strength_cell0.mp4 \
  --no-reuse \
  2>&1 | tee tests/live/evidence/2026-09-23-u53-env-payload/green-create-and-upscale.txt
```

Flags verified against `kinoforge upscale --help`: `-c/--config`, `--video PATH_OR_URL`, `--scale TARGET`, `--no-reuse`, `--attach-pod`, `--dry-run`. `--no-reuse` and `--attach-pod` are mutually exclusive.

While it runs, poll every 60-90 s in a second shell:

```bash
pixi run python -c "
from kinoforge.core.dotenv_loader import load_env_file; load_env_file()
import os
from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint
print(RunPodGraphQLUtilEndpoint(api_key=os.environ['RUNPOD_API_KEY']).probe('<pod-id>'))
"
```

**Act on the probe, do not just log it.** GPU 0% for 3 consecutive probes while an upscale is supposedly in flight, or CPU 0% with flat memory during boot → pull `curl -s https://<pod-id>-8001.proxy.runpod.net/bootstrap.log | tail -40`, destroy the pod, and fail fast. Record every probe reading in the evidence file.

- [ ] **Step 4: Verify teardown AFTER the orchestrator exits**

```bash
pixi run kinoforge list
```

Expected: BOTH `[instance overview] No running instances.` AND `No instances recorded in ledger.` A mid-run "No running instances" line is NOT proof. If either line shows a pod: `pixi run kinoforge destroy --id <pod-id>`, then re-check.

- [ ] **Step 5: Frame-QA the output**

```bash
pixi run python -c "
from pathlib import Path
from kinoforge.core.frames import ffmpeg_frames_by_count
out = sorted(Path('output').glob('*spandrel*.mp4'))[-1]
print(out)
print(ffmpeg_frames_by_count(out, 5))
"
```

Then Read the extracted frames and judge: artifacts, temporal coherence, and — this being an upscale — fidelity against the 480² source. Record the verdict in the evidence file. Anything not clearly high quality gets an explicit ⚠️ flag. **Exit code and ffprobe dimensions are NOT sufficient** — the 2026-07-03 FlashVSR smokes were reported green on dims across 24+ attempts and every output was false-colour garbage.

- [ ] **Step 6: Write the before/after summary into the evidence file**

Append to `tests/live/evidence/2026-09-23-u53-env-payload/green-create-and-upscale.txt`:

```
=== U53 before/after ===
RED  (pre-fix, see red-oversized-create.txt): 112,831 B -> <status recorded there>
GREEN (post-fix, this run):                    73,369 B -> pod created, /upscale served
GPU util readings: <list>
Frame QA: <verdict>
Post-run kinoforge list: <both lines>
Spend: $<amount>
```

- [ ] **Step 7: Decide whether `successful-generations.md` needs an entry**

Per CLAUDE.md, a new section is required only if this run introduces a new capability axis (new mode / provider / engine / model / YAML shape / command). A spandrel upscale on RunPod is an existing `(runpod, diffusers, spandrel, upscale)` tuple, so this is a **"See also" line under the existing TOC entry**, not a new section. Add that line. If no such entry exists, add a full section per the file's preamble schema.

- [ ] **Step 8: Commit the evidence**

```bash
git add tests/live/evidence/2026-09-23-u53-env-payload/ successful-generations.md
git commit -m "test(evidence): U53 green half — over-ceiling config now creates and serves

runpod-diffusers-spandrel-x2-upscale went 112,831 -> 73,369 B. The create
succeeds, the pod boots and serves /upscale without minimax_h3_server.py,
_lora.py or _av_io.py on disk, GPU util read non-zero through the upscale,
and frame QA is recorded. Pairs with red-oversized-create.txt."
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["RED", "red-oversized-create", "112,831"], ["GREEN", "pod created", "73,369"]], "files": ["tests/live/evidence/2026-09-23-u53-env-payload/green-create-and-upscale.txt", "successful-generations.md"], "verifyCommand": "cat tests/live/evidence/2026-09-23-u53-env-payload/green-create-and-upscale.txt", "acceptanceCriteria": ["preflight exits 0 and tree is clean before spend", "the previously-over-ceiling config creates a pod with no HTTP 500", "pod serves /upscale and produces a playable output video", "gpuUtilPercent polled every 60-90s and read non-zero during the upscale", "frames extracted and visually reviewed, verdict recorded", "--no-reuse passed and kinoforge list AFTER exit shows both no-instances lines", "evidence file cites Task 2's red result", "spend recorded and under $1.00"], "modelTier": "standard"}
```

---

## Task dependency order

```
Task 1 (guard, RED, offline)
   └─> Task 2 (live RED probe, $0) ── MUST run before Task 3 changes the configs
          └─> Task 3 (configs, guard GREEN)
                 └─> Task 4 (ratchet re-baseline)
                        └─> Task 5 (goldens)
                               └─> Task 6 (docs)
                                      └─> Task 7 (live GREEN proof)
```

Task 2's position is load-bearing: once Task 3 lands, the oversized create body no longer exists and the red half of the proof is unobservable.
