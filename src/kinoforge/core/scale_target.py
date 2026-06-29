"""Polymorphic scale target for video upscaling.

v1 supports ``kind="factor"`` (any positive float). ``kind="height"`` parses
but is refused by every v1 consumer (``UpscaleStage``, ``SeedVR2Runtime``)
with NotYetImplementedError. The CLI surface is final on day one; a future
session adds height-target arithmetic plus the swappable downscale method.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_FACTOR_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)x$")
_HEIGHT_RE = re.compile(r"^([0-9]+)p$")


@dataclass(frozen=True)
class ScaleTarget:
    """Polymorphic scale target.

    Grammar:
      ``"2x"``, ``"4x"``, ``"1.5x"`` → ``ScaleTarget(kind="factor", value=2.0)``
      ``"1080p"``, ``"720p"`` → ``ScaleTarget(kind="height", value=1080.0)``

    v1 engines MUST raise NotYetImplementedError on ``kind="height"``.

    Attributes:
        kind: Either ``"factor"`` or ``"height"``.
        value: Positive numeric value; semantics depend on ``kind``.
    """

    kind: Literal["factor", "height"]
    value: float

    @classmethod
    def parse(cls, raw: str) -> ScaleTarget:
        """Parse a raw CLI / cfg token into a ScaleTarget.

        Args:
            raw: User-supplied scale token (e.g. ``"2x"``, ``"1080p"``).

        Returns:
            Parsed ScaleTarget. ``kind="factor"`` or ``kind="height"``.

        Raises:
            ValueError: Token does not match the ``Nx`` / ``Np`` grammar, or
                resolves to a non-positive value.
        """
        m = _FACTOR_RE.match(raw)
        if m is not None:
            value = float(m.group(1))
            if value <= 0:
                raise ValueError(
                    f"scale factor must be positive; got {raw!r} -> {value}"
                )
            return cls(kind="factor", value=value)

        m = _HEIGHT_RE.match(raw)
        if m is not None:
            iv = int(m.group(1))
            if iv <= 0:
                raise ValueError(f"scale height must be positive; got {raw!r} -> {iv}")
            return cls(kind="height", value=float(iv))

        raise ValueError(
            f"unrecognised scale token {raw!r}; expected `Nx` or `Np` token "
            f"(e.g. '2x', '1.5x', '1080p')"
        )
