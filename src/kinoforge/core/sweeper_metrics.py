"""Layer W: pure renderers for ``kinoforge sweeper status`` and ``metrics``.

Three output shapes share a single input (entry dict from
``ledger.read('sweeper:<host>')`` + ``cfg.sweeper.interval_s`` +
``clock.now()``):

  - :func:`render_status_human` (entry, *, host, interval_s, now) -> str
  - :func:`render_status_json`  (entry, *, host, interval_s, now) -> str
  - :func:`render_metrics_prom` (entry, *, host, interval_s) -> str

Pure: no I/O, no threading, no global state. The CLI does the ledger
read; this module folds.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

_DEFERRED_REASONS: tuple[tuple[str, str], ...] = (
    ("session-claim", "deferred_session_claim"),
    ("heartbeat-unknown-skipped", "deferred_heartbeat_unknown_skipped"),
    ("heartbeat-substrate-missing", "deferred_heartbeat_substrate_missing"),
)


def _running(entry: Mapping[str, Any] | None) -> bool:
    """A daemon is running when an entry exists and carries a non-zero pid."""
    if entry is None:
        return False
    pid = entry.get("pid")
    try:
        return bool(int(pid)) if pid is not None else False
    except (TypeError, ValueError):
        return False


def _stats_view(
    entry: Mapping[str, Any] | None,
    *,
    interval_s: float,
    now: float,
) -> dict[str, Any]:
    """Project the ledger entry into the dict consumed by all three renderers."""
    if entry is None:
        return {
            "running": False,
            "pid": None,
            "last_sweep_ts": None,
            "last_sweep_age_s": None,
            "stale": False,
            "sweeps_total": 0,
            "destroys_total": 0,
            "errors_total": 0,
            "deferred_total": {label: 0 for label, _ in _DEFERRED_REASONS},
        }
    pid_raw = entry.get("pid")
    try:
        pid = int(pid_raw) if pid_raw is not None else None
    except (TypeError, ValueError):
        pid = None
    last_tick = entry.get("heartbeat_thread_tick")
    if last_tick is None:
        return {
            "running": False,
            "pid": pid,
            "last_sweep_ts": None,
            "last_sweep_age_s": None,
            "stale": False,
            "sweeps_total": int(entry.get("sweeps_total", 0)),
            "destroys_total": int(entry.get("destroys_total", 0)),
            "errors_total": int(entry.get("errors_total", 0)),
            "deferred_total": {
                label: int(entry.get(key, 0)) for label, key in _DEFERRED_REASONS
            },
        }
    last_tick_f = float(last_tick)
    age = int(now - last_tick_f)
    stale = (now - last_tick_f) > 3.0 * interval_s
    return {
        "running": _running(entry),
        "pid": pid,
        "last_sweep_ts": last_tick_f,
        "last_sweep_age_s": age,
        "stale": stale,
        "sweeps_total": int(entry.get("sweeps_total", 0)),
        "destroys_total": int(entry.get("destroys_total", 0)),
        "errors_total": int(entry.get("errors_total", 0)),
        "deferred_total": {
            label: int(entry.get(key, 0)) for label, key in _DEFERRED_REASONS
        },
    }


def render_status_json(
    entry: Mapping[str, Any] | None,
    *,
    host: str,
    interval_s: float,
    now: float,
) -> str:
    """Render the §4.6.3 stable JSON schema."""
    view = _stats_view(entry, interval_s=interval_s, now=now)
    out: dict[str, Any] = {
        "host": host,
        "pid": view["pid"],
        "running": view["running"],
        "last_sweep_ts": (
            datetime.fromtimestamp(view["last_sweep_ts"]).astimezone().isoformat()
            if view["last_sweep_ts"] is not None
            else None
        ),
        "last_sweep_age_s": view["last_sweep_age_s"],
        "interval_s": int(interval_s) if interval_s == int(interval_s) else interval_s,
        "stale": view["stale"],
        "sweeps_total": view["sweeps_total"],
        "destroys_total": view["destroys_total"],
        "deferred_total": view["deferred_total"],
        "errors_total": view["errors_total"],
    }
    return json.dumps(out, sort_keys=False)


def render_status_human(
    entry: Mapping[str, Any] | None,
    *,
    host: str,
    interval_s: float,
    now: float,
) -> str:
    """Render sibling-of-``kinoforge status`` key=value lines."""
    view = _stats_view(entry, interval_s=interval_s, now=now)
    iv = int(interval_s) if interval_s == int(interval_s) else interval_s
    lines = [
        f"host={host}",
        f"running={str(view['running']).lower()}",
        f"pid={view['pid'] if view['pid'] is not None else 'none'}",
        (
            "last_sweep_ts="
            f"{datetime.fromtimestamp(view['last_sweep_ts']).astimezone().isoformat()}"
            if view["last_sweep_ts"] is not None
            else "last_sweep_ts=none"
        ),
        (
            f"last_sweep_age_s={view['last_sweep_age_s']}"
            if view["last_sweep_age_s"] is not None
            else "last_sweep_age_s=none"
        ),
        f"interval_s={iv}",
        f"stale={str(view['stale']).lower()}",
        f"sweeps_total={view['sweeps_total']}",
        f"destroys_total={view['destroys_total']}",
    ]
    for label, _ in _DEFERRED_REASONS:
        key = "deferred_" + label.replace("-", "_")
        lines.append(f"{key}={view['deferred_total'][label]}")
    lines.append(f"errors_total={view['errors_total']}")
    return "\n".join(lines) + "\n"


def render_metrics_prom(
    entry: Mapping[str, Any] | None,
    *,
    host: str,
    interval_s: float,
) -> str:
    """Render Prometheus text exposition (textfile-collector cron target).

    Sibling of B2 ``kinoforge cost --prom`` prefix (``kinoforge_*``). LF
    line endings; UTF-8.
    """
    view = _stats_view(entry, interval_s=interval_s, now=0.0)
    parts: list[str] = []

    if view["last_sweep_ts"] is not None:
        parts.extend(
            [
                "# HELP kinoforge_sweeper_last_sweep_ts Unix timestamp of most recent successful sweep.",
                "# TYPE kinoforge_sweeper_last_sweep_ts gauge",
                f'kinoforge_sweeper_last_sweep_ts{{host="{host}"}} {int(view["last_sweep_ts"])}',
                "",
            ]
        )
    parts.extend(
        [
            "# HELP kinoforge_sweeper_sweeps_total Cumulative sweeps since daemon start.",
            "# TYPE kinoforge_sweeper_sweeps_total counter",
            f'kinoforge_sweeper_sweeps_total{{host="{host}"}} {view["sweeps_total"]}',
            "",
            "# HELP kinoforge_sweeper_destroys_total Cumulative pods destroyed since daemon start.",
            "# TYPE kinoforge_sweeper_destroys_total counter",
            f'kinoforge_sweeper_destroys_total{{host="{host}"}} {view["destroys_total"]}',
            "",
            "# HELP kinoforge_sweeper_deferred_total Sweeps that skipped a pod for a known reason.",
            "# TYPE kinoforge_sweeper_deferred_total counter",
        ]
    )
    for label, _ in _DEFERRED_REASONS:
        parts.append(
            f'kinoforge_sweeper_deferred_total{{host="{host}",reason="{label}"}} '
            f"{view['deferred_total'][label]}"
        )
    parts.extend(
        [
            "",
            "# HELP kinoforge_sweeper_errors_total Per-tick exceptions caught by the loop body.",
            "# TYPE kinoforge_sweeper_errors_total counter",
            f'kinoforge_sweeper_errors_total{{host="{host}"}} {view["errors_total"]}',
            "",
            "# HELP kinoforge_sweeper_interval_s Configured sweep cadence.",
            "# TYPE kinoforge_sweeper_interval_s gauge",
            (
                f'kinoforge_sweeper_interval_s{{host="{host}"}} '
                f"{int(interval_s) if interval_s == int(interval_s) else interval_s}"
            ),
            "",
        ]
    )
    return "\n".join(parts)
