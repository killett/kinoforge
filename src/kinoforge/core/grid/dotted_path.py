"""Dotted-path mutator for pydantic configs.

Supports ``.field`` (object attr) and ``[N]`` (list index) only. No
wildcards in v1 — the contract is explicit so future ``[*]`` semantics
have room to land without breaking v1 specs.

After mutation, the root model is re-validated via ``model_validate``
on its ``model_dump()``. This catches type errors AND field-level
constraint violations (negative strength, etc.) close to the source.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from kinoforge.core.grid.errors import DottedPathError

_INDEX_RE = re.compile(r"^([^\[]+)\[(\d+|\*)\]$")


def _parse_segment(seg: str) -> tuple[str, int | None]:
    """Split ``'loras[0]'`` into ``('loras', 0)``; ``'prompt'`` into ``('prompt', None)``."""
    m = _INDEX_RE.match(seg)
    if not m:
        return seg, None
    name, idx_str = m.group(1), m.group(2)
    if idx_str == "*":
        raise DottedPathError(
            f"wildcards not supported in v1: {seg!r} — declare each cell explicitly"
        )
    return name, int(idx_str)


def set_path(root: BaseModel, path: str, value: Any) -> BaseModel:  # noqa: ANN401
    """Return a new model with ``path`` set to ``value``; full re-validation.

    Args:
        root: The root pydantic model (cell's base cfg).
        path: Dotted path string. Examples:
            ``'prompt'`` — top-level scalar.
            ``'loras[0].strength'`` — nested list + nested field.
            ``'compute.lifecycle.lora_swap_re_probe_after_s'`` — deep field.
        value: New scalar value (int, float, str, bool, None).

    Returns:
        A new model with the override applied AND fully re-validated.

    Raises:
        DottedPathError: Empty path, unknown field, index out of range,
            or wildcard used.
        pydantic.ValidationError: Post-mutation re-validation rejected
            the resulting model (e.g. negative strength).
    """
    if not path:
        raise DottedPathError("empty path")

    segments = path.split(".")
    data = root.model_dump()
    cursor: Any = data

    for seg in segments[:-1]:
        name, idx = _parse_segment(seg)
        if not isinstance(cursor, dict) or name not in cursor:
            raise DottedPathError(f"no field {name!r} in path {path!r}")
        cursor = cursor[name]
        if idx is not None:
            if not isinstance(cursor, list):
                raise DottedPathError(f"{name!r} is not a list in path {path!r}")
            if idx >= len(cursor):
                raise DottedPathError(
                    f"index {idx} out of range for {name!r} (len={len(cursor)})"
                )
            cursor = cursor[idx]

    last_name, last_idx = _parse_segment(segments[-1])
    if last_idx is not None:
        if not isinstance(cursor, dict) or last_name not in cursor:
            raise DottedPathError(f"no field {last_name!r} in path {path!r}")
        target = cursor[last_name]
        if not isinstance(target, list):
            raise DottedPathError(f"{last_name!r} is not a list in path {path!r}")
        if last_idx >= len(target):
            raise DottedPathError(
                f"index {last_idx} out of range for {last_name!r} (len={len(target)})"
            )
        target[last_idx] = value
    else:
        if not isinstance(cursor, dict) or last_name not in cursor:
            raise DottedPathError(f"no field {last_name!r} in path {path!r}")
        cursor[last_name] = value

    return type(root).model_validate(data)
