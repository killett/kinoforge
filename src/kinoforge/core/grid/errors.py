"""Grid-specific exception hierarchy.

Every class subclasses :class:`kinoforge.core.errors.KinoforgeError` so the
broad-except sites in ``cli/_commands.py`` continue to catch them uniformly.
"""

from __future__ import annotations

from kinoforge.core.errors import KinoforgeError


class GridSpecUnderRepoError(KinoforgeError):
    """Raised when a grid spec path resolves under the active git repo."""


class GridCellPathMissing(KinoforgeError):
    """Raised when a ``path:`` cell references an mp4 that doesn't exist."""


class GridCellFailure(KinoforgeError):
    """Single-cell generation failure breadcrumb.

    Attributes:
        idx: 0-based cell index in the spec's ``cells:`` list.
        cfg_repr: Short, redacted representation of the cell's effective cfg.
        exception_chain: The original exception raised by the cell's subprocess.
    """

    def __init__(self, idx: int, cfg_repr: str, exception_chain: BaseException) -> None:
        """Capture the cell index, a short cfg breadcrumb, and the upstream exception."""
        self.idx = idx
        self.cfg_repr = cfg_repr
        self.exception_chain = exception_chain
        super().__init__(f"cell {idx}: {exception_chain}")


class GridBudgetExceeded(KinoforgeError):
    """Raised when cumulative grid spend crosses ``budget_cap_usd``."""


class FfmpegNotFoundError(KinoforgeError):
    """Raised when the ``ffmpeg`` binary is not on PATH at grid entry."""


class FfmpegInvocationError(KinoforgeError):
    """Raised when ``ffmpeg`` exits non-zero during composition."""


class DottedPathError(KinoforgeError):
    """Raised when a dotted-path override fails to resolve or apply."""
