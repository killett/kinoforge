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
    """Default capability_key derivation.

    Lives at module scope so tests can monkeypatch it without poking
    into :mod:`kinoforge.core.interfaces`. The real implementation
    delegates to ``derive_capability_key_from_cfg`` once the executor
    is wired into ``deploy_session``.
    """
    if cell.effective_cfg is None:
        return None
    return str(cell.effective_cfg)


def _resolve_spec_cells(
    spec: GridSpec, *, grid_id: str, tmp_dir: Path
) -> list[_ResolvedCell]:
    """Apply overrides per cell; write each effective cfg to a tmp file."""
    import yaml

    from kinoforge.core.grid.errors import GridCellPathMissing

    del grid_id  # logged elsewhere; retained for symmetry with other call sites
    tmp_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[_ResolvedCell] = []
    for i, cell in enumerate(spec.cells):
        if cell.generate is not None:
            cfg_path = tmp_dir / f"cell_{i}.yaml"
            cfg_path.write_text(yaml.safe_dump(cell.generate.model_dump(mode="json")))
            resolved.append(
                _ResolvedCell(
                    idx=i,
                    caption=cell.caption,
                    cfg_path=cfg_path,
                    effective_cfg=cell.generate,
                    mp4_path=None,
                )
            )
        else:
            if cell.path is None:  # GridCell model_validator guarantees this
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


def _build_generate_cmd(
    cell: _ResolvedCell, *, grid_id: str, output_dir: Path, no_reuse: bool
) -> list[str]:
    """Construct the ``pixi run kinoforge generate ...`` argv for one cell."""
    if cell.cfg_path is None:
        raise ValueError(
            f"cell {cell.idx}: cfg_path is None — _build_generate_cmd called on a "
            f"path: cell, which should be routed through the no-compute branch"
        )
    run_id = f"{grid_id}__cell{cell.idx}"
    cmd = [
        "pixi",
        "run",
        "kinoforge",
        "generate",
        "--config",
        str(cell.cfg_path),
        "--mode",
        "t2v",
        "--run-id",
        run_id,
        "--output-dir",
        str(output_dir),
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
    matches = sorted(output_dir.glob(f"*{grid_id}__cell{cell.idx}*.mp4"))
    if not matches:
        err = GridCellFailure(
            idx=cell.idx,
            cfg_repr=f"cfg={cell.cfg_path}",
            exception_chain=FileNotFoundError(
                f"no mp4 matched glob *{grid_id}__cell{cell.idx}*.mp4 in {output_dir}"
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


def _check_no_residual_pods() -> tuple[bool, str]:
    """Run ``pixi run kinoforge list``; return ``(clean, raw_output)``."""
    result = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "list"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    raw = result.stdout + "\n" + result.stderr
    clean = bool(_NO_RESIDUAL_RE.search(result.stdout)) and "POD:" not in result.stdout
    return clean, raw


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
