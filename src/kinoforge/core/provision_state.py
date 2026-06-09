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
    fd, tmp_name = tempfile.mkstemp(prefix=".provisioned.tmp.", dir=str(path.parent))
    try:
        # kinoforge:public-write — capability_key + instance_id only, no prompt-derived bytes
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
