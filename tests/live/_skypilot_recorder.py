"""SkyPilot SDK return-shape recording proxy.

Test-scope only. Production code never imports this module.

Wraps the real `sky` module so each method call delegates, then JSON-
serializes the return value via :func:`_to_jsonable` (which strips
volatile fields with a sentinel) into ``<fixture_dir>/<method_name>.json``.
PR reviewers diff those files — the diff IS the review surface.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "launched_at",
        "cluster_name_on_cloud",
        "internal_ip",
        "external_ip",
        "handle",
        "head_ip",
    }
)

_VOLATILE_SENTINEL: str = "<volatile>"


def _to_jsonable(obj: Any) -> Any:  # noqa: ANN401
    """Convert an SDK return value to a JSON-serialisable form.

    Handles dataclasses, enums, ``pathlib.Path``, ``datetime``, and arbitrary
    nested dicts/lists. Keys in :data:`VOLATILE_KEYS` are replaced with
    :data:`_VOLATILE_SENTINEL` so PR diffs surface shape changes, not noise.

    Args:
        obj: Any return value from an SDK call.

    Returns:
        A value safe to pass to ``json.dumps`` with ``default=str``.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _to_jsonable(dataclasses.asdict(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {
            k: (_VOLATILE_SENTINEL if k in VOLATILE_KEYS else _to_jsonable(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(item) for item in obj]
    return obj


class _RecordingProxy:
    """Wraps a real SDK object; records each method call's return value.

    On every attribute access, returns a wrapper that calls the underlying
    method, JSON-serialises the result to ``<fixture_dir>/<name>.json``
    (last-call-wins), and returns the original result unchanged.

    The proxy is duck-compatible with anything that exposes attributes via
    ``getattr`` (modules, instances). It does not record non-callable
    attribute accesses.
    """

    def __init__(self, real: Any, fixture_dir: Path) -> None:
        """Construct the proxy.

        Args:
            real: The object whose method calls should be recorded.
            fixture_dir: Directory in which to write ``<name>.json`` files.
                Created if it does not exist.
        """
        self._real = real
        self._fixture_dir = fixture_dir
        fixture_dir.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """Return a wrapper around ``self._real.<name>``."""
        target = getattr(self._real, name)
        if not callable(target):
            return target

        def _wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            result = target(*args, **kwargs)
            payload = _to_jsonable(result)
            fixture_path = self._fixture_dir / f"{name}.json"
            fixture_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n"
            )
            return result

        return _wrapper
