"""Grid executor: subprocess-per-cell, group-parallel, partial-failure-tolerant.

Each cell launches ``pixi run kinoforge generate`` as a subprocess. Same-
group cells run sequentially so the existing warm-reuse matcher reuses
the pod across calls (no ``--no-reuse`` on cells 0..N-2 of a group;
``--no-reuse`` on cell N-1 so the pod auto-destroys on group exit).

Cross-group cells run in parallel under a semaphore. A cell failure
ABORTS the rest of its group (pod state is unknown) but does NOT touch
other groups.

After all groups settle, a post-condition ``kinoforge list`` probe
confirms zero residual pods; a positive sighting raises the result
status to ``'teardown'`` so the operator sees the leak (the exact
class of failure the 2026-06-24 destroy-on-teardown fix exists to
prevent at the smoke layer).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from kinoforge.core.grid.compose import (
    LayoutCell,
    _check_ffmpeg,
    _resolve_layout,
    compose_grid_mp4,
    probe_inputs,
)
from kinoforge.core.grid.errors import FfmpegInvocationError, GridCellFailure
from kinoforge.core.grid.grouping import _PATH_GROUP_KEY, group_cells_by_capability_key
from kinoforge.core.grid.spec import GridSpec

_log = logging.getLogger(__name__)

_CellStatus = Literal["success", "failed", "aborted"]
_GridStatus = Literal["full", "partial", "budget", "ffmpeg", "teardown"]
_NO_RESIDUAL_RE = re.compile(
    r"\[instance overview\] No running instances\."
    r"|No instances recorded in ledger\.",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str, *, max_len: int = 30) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-")[:max_len] or "cell"


@dataclass
class _ResolvedCell:
    idx: int
    caption: str | None
    cfg_path: Path | None
    effective_cfg: object | None
    mp4_path: Path | None

    def capability_key(self) -> str | None:
        if self.effective_cfg is None:
            return None
        return _cell_capability_key(self)


@dataclass
class GridCellResult:
    """Per-cell outcome captured by :func:`run_grid`."""

    idx: int
    caption: str | None
    status: _CellStatus
    mp4_path: Path | None
    sha256: str | None
    cost_usd: float | None
    error: GridCellFailure | None = None


@dataclass
class GridResult:
    """Top-level grid invocation outcome."""

    grid_id: str
    status: _GridStatus
    cell_results: list[GridCellResult]
    composed_mp4_path: Path | None = None
    partial_dir: Path | None = None
    teardown_breadcrumb: str | None = None


def _cell_capability_key(cell: _ResolvedCell) -> str | None:
    """Derive the cell's capability_key from the effective Config.

    Mirrors :class:`kinoforge.core.interfaces.CapabilityKey` factors:
    base-model ref + ordered LoRA refs + engine kind + precision.
    LoRA *strength* is intentionally omitted so strength sweeps share
    one key (warm-reuse intra-group). VAE / scheduler / spec dims are
    also out by design — they're engine-side details that don't gate
    pod identity.

    Lives at module scope so tests can monkeypatch it. Path cells
    return None — they're degenerate and the grouping module folds
    them under the ``_PATH_GROUP_KEY`` sentinel.
    """
    cfg = cell.effective_cfg
    if cfg is None:
        return None
    try:
        from kinoforge.core.interfaces import CapabilityKey

        base_models = [
            m for m in getattr(cfg, "models", []) if getattr(m, "kind", None) == "base"
        ]
        base_ref = base_models[0].ref if base_models else ""
        loras = getattr(cfg, "loras", []) or []
        lora_refs = tuple(lo.ref for lo in loras)
        engine = getattr(cfg, "engine", None)
        engine_kind = getattr(engine, "kind", "") if engine is not None else ""
        precision = getattr(engine, "precision", "") if engine is not None else ""
        return CapabilityKey(
            base_model=base_ref,
            loras=lora_refs,
            engine=engine_kind,
            precision=precision,
        ).derive()
    except Exception:  # noqa: BLE001 — defensive fallback for cfg shape drift
        return str(cfg)


def _resolve_spec_cells(
    spec: GridSpec, *, grid_id: str, tmp_dir: Path
) -> list[_ResolvedCell]:
    """Apply overrides per cell; write each effective generation cfg to a tmp file.

    For ``generate:`` cells: loads the base kinoforge :class:`Config`,
    applies each dotted-path override via :func:`set_path`, dumps the
    resulting effective cfg to ``<tmp_dir>/cell_<i>.yaml``. This is the
    path the per-cell subprocess passes to ``kinoforge generate --config``.

    For ``path:`` cells: stats the target mp4 path; raises if missing.
    """
    import yaml

    from kinoforge.core.config import load_config
    from kinoforge.core.grid.dotted_path import set_path
    from kinoforge.core.grid.errors import GridCellPathMissing

    del grid_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[_ResolvedCell] = []
    for i, cell in enumerate(spec.cells):
        if cell.generate is not None:
            base = load_config(Path(cell.generate.config))
            effective: object = base
            for path, value in cell.generate.overrides.items():
                effective = set_path(effective, path, value)  # type: ignore[arg-type]
            cfg_path = tmp_dir / f"cell_{i}.yaml"
            cfg_path.write_text(  # kinoforge:public-write
                yaml.safe_dump(effective.model_dump(mode="json")),  # type: ignore[attr-defined]
            )
            resolved.append(
                _ResolvedCell(
                    idx=i,
                    caption=cell.caption,
                    cfg_path=cfg_path,
                    effective_cfg=effective,
                    mp4_path=None,
                )
            )
        else:
            if cell.path is None:
                raise GridCellPathMissing(
                    f"cell {i}: neither generate nor path set (model invariant violated)"
                )
            mp = Path(cell.path).resolve()
            if not mp.exists():
                raise GridCellPathMissing(f"cell {i} path missing: {mp}")
            resolved.append(
                _ResolvedCell(
                    idx=i,
                    caption=cell.caption,
                    cfg_path=None,
                    effective_cfg=None,
                    mp4_path=mp,
                )
            )
    return resolved


def _cell_output_dir(grid_id: str, cell_idx: int, output_dir: Path) -> Path:
    """Unique per-cell output dir so the post-run mp4 glob has exactly one match."""
    return output_dir / f"_grid_{grid_id}" / f"cell_{cell_idx}_out"


def _build_generate_cmd(
    cell: _ResolvedCell, *, grid_id: str, output_dir: Path, no_reuse: bool
) -> list[str]:
    """Construct the ``pixi run kinoforge generate ...`` argv for one cell.

    ``kinoforge generate`` requires ``--prompt`` at argparse level
    (``cli/_main.py:413``). Read the prompt from the effective cfg's
    top-level ``prompt:`` field. Raises if the cfg doesn't carry one.

    Each cell gets its OWN ``--output-dir`` (a unique subdir under the
    grid's tmp dir) so the post-run mp4 lookup is a single-file listdir
    instead of a fragile glob against the operator's shared
    ``output/`` directory. LocalOutputSink's filename pattern is
    ``<ts>_<provider>_<model>_<promptslug>.mp4`` — the ``--run-id`` is
    NOT embedded in the filename, so any glob against the shared dir
    will miss when multiple gens land in the same second.
    """
    if cell.cfg_path is None:
        raise ValueError(
            f"cell {cell.idx}: cfg_path is None — _build_generate_cmd called on a "
            f"path: cell, which should be routed through the no-compute branch"
        )
    cfg = cell.effective_cfg
    prompt = getattr(cfg, "prompt", None)
    if not prompt:
        raise ValueError(
            f"cell {cell.idx}: effective cfg has no top-level `prompt:` field; "
            f"grid CLI requires every generate cell's cfg to carry a default "
            f"prompt (kinoforge generate --prompt is required at argparse level). "
            f"Add `prompt: ...` to the base cfg or pass it as a per-cell override."
        )
    mode = getattr(cfg, "mode", None) or "t2v"
    run_id = f"{grid_id}__cell{cell.idx}"
    cell_out = _cell_output_dir(grid_id, cell.idx, output_dir)
    cell_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        "pixi",
        "run",
        "kinoforge",
        "generate",
        "--config",
        str(cell.cfg_path),
        "--prompt",
        str(prompt),
        "--mode",
        str(mode),
        "--run-id",
        run_id,
        "--output-dir",
        str(cell_out),
    ]
    if no_reuse:
        cmd.append("--no-reuse")
    return cmd


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _run_one_cell(
    cell: _ResolvedCell, *, grid_id: str, output_dir: Path, no_reuse: bool
) -> GridCellResult:
    """Run one ``generate:`` cell as a subprocess."""
    cmd = _build_generate_cmd(
        cell, grid_id=grid_id, output_dir=output_dir, no_reuse=no_reuse
    )
    proc = await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True,
        text=True,
        check=False,  # noqa: S603
    )
    if proc.returncode != 0:
        # Persist FULL stderr to disk for post-mortem inspection. The
        # executor would otherwise drop everything past 500 chars when
        # building the breadcrumb (the in-memory GridCellFailure only
        # carries the truncated tail). The stderr file lives next to
        # the per-cell tmp cfg under `_grid_<id>/cell_<idx>.stderr.txt`
        # and is the only durable record of the failure for the
        # operator's diff.
        if cell.cfg_path is not None:
            stderr_log = cell.cfg_path.with_suffix(".stderr.txt")
            try:
                stderr_log.write_text(  # kinoforge:public-write
                    proc.stderr or proc.stdout or "<no output>",
                )
            except OSError:
                pass
        err = GridCellFailure(
            idx=cell.idx,
            cfg_repr=f"cfg={cell.cfg_path}",
            exception_chain=RuntimeError(
                f"kinoforge generate exit={proc.returncode}: "
                f"{proc.stderr.strip()[:500]}"
            ),
        )
        return GridCellResult(
            idx=cell.idx,
            caption=cell.caption,
            status="failed",
            mp4_path=None,
            sha256=None,
            cost_usd=None,
            error=err,
        )
    cell_out = _cell_output_dir(grid_id, cell.idx, output_dir)
    matches = sorted(cell_out.glob("*.mp4"))
    if not matches:
        err = GridCellFailure(
            idx=cell.idx,
            cfg_repr=f"cfg={cell.cfg_path}",
            exception_chain=FileNotFoundError(
                f"no mp4 in per-cell output dir {cell_out}"
            ),
        )
        return GridCellResult(
            idx=cell.idx,
            caption=cell.caption,
            status="failed",
            mp4_path=None,
            sha256=None,
            cost_usd=None,
            error=err,
        )
    mp4 = matches[0]
    return GridCellResult(
        idx=cell.idx,
        caption=cell.caption,
        status="success",
        mp4_path=mp4,
        sha256=_sha256_file(mp4),
        cost_usd=None,
    )


async def _run_group(
    cells: list[_ResolvedCell],
    *,
    grid_id: str,
    output_dir: Path,
    sem: asyncio.Semaphore,
) -> list[GridCellResult]:
    """Run one group sequentially under the parallel-group semaphore."""
    async with sem:
        results: list[GridCellResult] = []
        aborted = False
        for i, cell in enumerate(cells):
            if aborted:
                results.append(
                    GridCellResult(
                        idx=cell.idx,
                        caption=cell.caption,
                        status="aborted",
                        mp4_path=None,
                        sha256=None,
                        cost_usd=None,
                    )
                )
                continue
            is_last = i == len(cells) - 1
            r = await _run_one_cell(
                cell,
                grid_id=grid_id,
                output_dir=output_dir,
                no_reuse=is_last,
            )
            results.append(r)
            if r.status == "failed":
                aborted = True
                _log.warning(
                    "grid cell %d failed; aborting remaining cells in group",
                    cell.idx,
                )
        return results


def _check_no_residual_pods(
    *, attempts: int = 6, delay_s: float = 5.0
) -> tuple[bool, str]:
    """Run ``pixi run kinoforge list`` repeatedly; return once clean OR timeout.

    Pod destruction on RunPod is asynchronous: when the last cell of a
    group exits via ``--no-reuse``, the orchestrator returns immediately
    after issuing the destroy GraphQL mutation, but the RunPod backend
    can take 10-30 s to actually remove the pod from its API and the
    local ledger sweeper hasn't yet purged the entry. A one-shot probe
    immediately after the group settles therefore false-positives on
    the warm pod that's mid-destroy.

    Retry up to ``attempts`` times with ``delay_s`` between, then bail.
    Total ceiling: ~30 s by default. Returns ``(clean, last_raw)``.

    Args:
        attempts: Maximum number of probe attempts. Default 6.
        delay_s: Seconds to sleep between attempts. Default 5.0.

    Returns:
        ``(True, raw)`` once the ledger reports no running instances;
        ``(False, raw)`` after ``attempts`` consecutive dirty probes.
    """
    import time as _time

    raw = ""
    for i in range(attempts):
        result = subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", "list"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        raw = result.stdout + "\n" + result.stderr
        clean = (
            bool(_NO_RESIDUAL_RE.search(result.stdout)) and "POD:" not in result.stdout
        )
        if clean:
            return True, raw
        if i < attempts - 1:
            _time.sleep(delay_s)
    return False, raw


def _move_to_partial_dir(
    results: list[GridCellResult], *, output_dir: Path, grid_id: str
) -> Path:
    """Copy per-cell mp4s into ``_grid_<id>_partial/`` for operator triage."""
    partial = output_dir / f"_grid_{grid_id}_partial"
    partial.mkdir(parents=True, exist_ok=True)
    for r in results:
        if r.mp4_path is None or not r.mp4_path.exists():
            continue
        slug = _slugify(r.caption or "")
        dest = partial / f"cell_{r.idx}_{slug}.mp4"
        shutil.copy2(r.mp4_path, dest)
    return partial


async def run_grid(
    *,
    spec: GridSpec,
    output_dir: Path,
    max_parallel_groups: int = 2,
    out_path: Path | None = None,
) -> GridResult:
    """Resolve cells, dispatch groups, optionally compose grid mp4.

    Args:
        spec: Loaded :class:`GridSpec` (via :meth:`GridSpec.load`).
        output_dir: Where per-cell mp4s land and where the composed
            mp4 is written by default.
        max_parallel_groups: Concurrency cap across groups.
        out_path: Explicit composed-mp4 destination; defaults to
            ``<output_dir>/grid_<ts>_<title-slug>.mp4``.

    Returns:
        A :class:`GridResult` whose ``status`` tells the caller which
        exit code to emit (see ``cli/_commands.py:_cmd_grid``).
    """
    _check_ffmpeg()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    grid_id = f"grid_{ts}_{hashlib.sha256(ts.encode()).hexdigest()[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = output_dir / f"_grid_{grid_id}"

    resolved = _resolve_spec_cells(spec, grid_id=grid_id, tmp_dir=tmp_dir)
    groups = group_cells_by_capability_key(resolved)

    sem = asyncio.Semaphore(max_parallel_groups)
    group_tasks = []
    for key, cells in groups.items():
        if key == _PATH_GROUP_KEY:
            continue
        group_tasks.append(
            _run_group(cells, grid_id=grid_id, output_dir=output_dir, sem=sem)
        )
    group_results = await asyncio.gather(*group_tasks) if group_tasks else []

    all_results: list[GridCellResult] = []
    for sub in group_results:
        all_results.extend(sub)
    for cell in groups.get(_PATH_GROUP_KEY, []):
        if cell.mp4_path is None:
            continue
        all_results.append(
            GridCellResult(
                idx=cell.idx,
                caption=cell.caption,
                status="success",
                mp4_path=cell.mp4_path,
                sha256=_sha256_file(cell.mp4_path),
                cost_usd=0.0,
            )
        )
    all_results.sort(key=lambda r: r.idx)

    clean, raw = _check_no_residual_pods()
    if not clean:
        breadcrumb = raw.strip()[:500]
        _log.error("grid teardown probe failed: %s", breadcrumb)
        partial = _move_to_partial_dir(
            all_results, output_dir=output_dir, grid_id=grid_id
        )
        return GridResult(
            grid_id=grid_id,
            status="teardown",
            cell_results=all_results,
            partial_dir=partial,
            teardown_breadcrumb=breadcrumb,
        )

    if any(r.status != "success" for r in all_results):
        partial = _move_to_partial_dir(
            all_results, output_dir=output_dir, grid_id=grid_id
        )
        return GridResult(
            grid_id=grid_id,
            status="partial",
            cell_results=all_results,
            partial_dir=partial,
        )

    layout = _resolve_layout(spec.layout, n=len(all_results))
    inputs = [r.mp4_path for r in all_results if r.mp4_path is not None]
    probes = probe_inputs(inputs)
    cells_meta = [LayoutCell(idx=r.idx, caption=r.caption) for r in all_results]
    title_slug = _slugify(spec.title or "untitled")
    composed = out_path if out_path else output_dir / f"grid_{ts}_{title_slug}.mp4"
    try:
        compose_grid_mp4(
            inputs=inputs,
            probes=probes,
            cells=cells_meta,
            layout=layout,
            out_path=composed,
        )
    except FfmpegInvocationError as e:
        _log.error("ffmpeg compose failed: %s", e)
        partial = _move_to_partial_dir(
            all_results, output_dir=output_dir, grid_id=grid_id
        )
        return GridResult(
            grid_id=grid_id,
            status="ffmpeg",
            cell_results=all_results,
            partial_dir=partial,
        )
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return GridResult(
        grid_id=grid_id,
        status="full",
        cell_results=all_results,
        composed_mp4_path=composed,
    )
