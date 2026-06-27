"""Per-grid cost sidecar — writes ``<grid_out>.cost.json`` per spec §6.

Polled by the grid executor between cells to detect cap-trip; written
unconditionally at grid exit (success, partial, or abort) so the
operator's post-mortem always has the cost breakdown.

Schema (mirrors spec §6):

```json
{
  "grid_id": "grid-...",
  "spec_path": "<redacted>",
  "out_mp4": "/path/to/grid.mp4",
  "total_cost_usd": 0.42,
  "budget_cap_usd": 2.0,
  "wall_time_s": 123.4,
  "groups": [
    {
      "key": "<wak-hex>",
      "pod_id": "pod-...",
      "provider": "runpod",
      "cost_per_hr_usd": 0.79,
      "wall_time_s": 90.0,
      "cost_usd": 0.02,
      "cell_indices": [0, 1, 2],
      "cells": [
        {
          "idx": 0, "gen_wall_time_s": 10.0, "swap_wall_time_s": 0.0,
          "status": "ok", "mp4_sha256": "...", "size_bytes": 12345
        },
        {"idx": 2, "status": "budget_killed", "error": "cap reached"}
      ]
    }
  ]
}
```
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kinoforge.core.redaction import RedactionRegistry


def _coerce_aware(ts: datetime) -> datetime:
    """Treat naive datetimes as local-TZ; return a TZ-aware datetime.

    Sidecar arithmetic mixes timestamps from three sources — the
    builder's own ``datetime.now()`` (TZ-aware), provision records read
    from disk (TZ-aware ISO-8601 with offset), and test fixtures
    (often naive). Coercing every boundary input to aware avoids
    `can't subtract offset-naive and offset-aware datetimes`.
    """
    return ts if ts.tzinfo is not None else ts.astimezone()


@dataclass
class _CellRecord:
    idx: int
    gen_wall_time_s: float = 0.0
    swap_wall_time_s: float = 0.0
    status: str = "ok"
    mp4_sha256: str | None = None
    size_bytes: int | None = None
    error: str | None = None


@dataclass
class _GroupRecord:
    key: str
    pod_id: str
    provider: str
    cost_per_hr_usd: float
    provision_ts: datetime
    cells: list[_CellRecord] = field(default_factory=list)


class CostSidecarBuilder:
    """Build and write the per-grid ``<grid_out>.cost.json`` sidecar.

    Wall-clock cost is computed as ``(now - provision_ts) * cost_per_hr / 3600``
    per group, summed across groups. Clamps at 0 for inverted-clock cases.

    Lifecycle: instantiate per ``run_grid()`` invocation; call
    :meth:`start_group` at each pod cold-boot, :meth:`record_cell` after each
    successful generation, :meth:`mark_cell_error` when a cell is killed
    (budget, failure, abort); call :meth:`write` once at executor exit.
    """

    def __init__(
        self,
        grid_id: str,
        spec_path: Path,
        out_mp4: Path,
        budget_cap_usd: float,
    ) -> None:
        """Initialize a sidecar builder for one grid invocation."""
        self.grid_id = grid_id
        self.spec_path = spec_path
        self.out_mp4 = out_mp4
        self.budget_cap_usd = budget_cap_usd
        self._groups: dict[str, _GroupRecord] = {}
        self._start_ts = datetime.now().astimezone()

    def start_group(
        self,
        group_key: str,
        pod_id: str,
        provider: str,
        cost_per_hr: float,
        *,
        provision_ts: datetime | None = None,
    ) -> None:
        """Record the pod-cold-boot timestamp + cost rate for a group."""
        ts = provision_ts if provision_ts is not None else datetime.now().astimezone()
        self._groups[group_key] = _GroupRecord(
            key=group_key,
            pod_id=pod_id,
            provider=provider,
            cost_per_hr_usd=cost_per_hr,
            provision_ts=_coerce_aware(ts),
        )

    def record_cell(
        self,
        group_key: str,
        *,
        cell_idx: int,
        gen_wall_time_s: float,
        swap_wall_time_s: float,
        status: str = "ok",
        mp4_sha256: str | None = None,
        size_bytes: int | None = None,
    ) -> None:
        """Append a successful cell's result to a group."""
        self._groups[group_key].cells.append(
            _CellRecord(
                idx=cell_idx,
                gen_wall_time_s=gen_wall_time_s,
                swap_wall_time_s=swap_wall_time_s,
                status=status,
                mp4_sha256=mp4_sha256,
                size_bytes=size_bytes,
            )
        )

    def mark_cell_error(
        self,
        group_key: str,
        *,
        cell_idx: int,
        status: str,
        error: str,
    ) -> None:
        """Append a failed/killed cell record (no mp4, just status+error)."""
        self._groups[group_key].cells.append(
            _CellRecord(idx=cell_idx, status=status, error=error)
        )

    def group_cost(self, group_key: str, *, now: datetime | None = None) -> float:
        """Return wall-clock cost for one group; clamps at 0 for inverted clock."""
        g = self._groups[group_key]
        n = _coerce_aware(now) if now is not None else datetime.now().astimezone()
        elapsed = (n - g.provision_ts).total_seconds()
        return max(0.0, elapsed) * g.cost_per_hr_usd / 3600.0

    def total_cost_usd(self, *, now: datetime | None = None) -> float:
        """Sum of :meth:`group_cost` across every started group."""
        n = _coerce_aware(now) if now is not None else datetime.now().astimezone()
        return sum(self.group_cost(k, now=n) for k in self._groups)

    def write(self, out_path: Path, *, now: datetime | None = None) -> None:
        """Emit the sidecar JSON at ``out_path`` (parent dirs created)."""
        n = _coerce_aware(now) if now is not None else datetime.now().astimezone()
        reg = RedactionRegistry.instance()
        data = {
            "grid_id": self.grid_id,
            "spec_path": reg.redact(str(self.spec_path)),
            "out_mp4": str(self.out_mp4),
            "total_cost_usd": round(self.total_cost_usd(now=n), 6),
            "budget_cap_usd": self.budget_cap_usd,
            "wall_time_s": max(0.0, (n - self._start_ts).total_seconds()),
            "groups": [
                {
                    "key": g.key,
                    "pod_id": g.pod_id,
                    "provider": g.provider,
                    "cost_per_hr_usd": g.cost_per_hr_usd,
                    "wall_time_s": max(0.0, (n - g.provision_ts).total_seconds()),
                    "cost_usd": round(self.group_cost(g.key, now=n), 6),
                    "cell_indices": [c.idx for c in g.cells],
                    "cells": [
                        {
                            k: v
                            for k, v in {
                                "idx": c.idx,
                                "gen_wall_time_s": c.gen_wall_time_s,
                                "swap_wall_time_s": c.swap_wall_time_s,
                                "status": c.status,
                                "mp4_sha256": c.mp4_sha256,
                                "size_bytes": c.size_bytes,
                                "error": c.error,
                            }.items()
                            if v is not None
                        }
                        for c in g.cells
                    ],
                }
                for g in self._groups.values()
            ],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(  # kinoforge:public-write
            json.dumps(data, indent=2) + "\n"
        )
