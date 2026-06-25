"""Grid composition — ffmpeg subprocess shell-out, no Python bindings.

The drawtext filter's special-char escaping is the bug-magnet: un-escaped
``:`` truncates the caption at the first colon (silent mis-parse, no
warning). This module owns the escape contract.
"""

from __future__ import annotations

_DRAWTEXT_ESCAPED = {
    "\\": r"\\",
    ":": r"\:",
    "'": r"\'",
    "%": r"\%",
    "\n": r"\n",
}


def _escape_drawtext(s: str) -> str:
    r"""Escape special chars for ffmpeg ``drawtext`` filter ``text=`` arg.

    The drawtext filter parses ``:`` as an option separator and ``\`` as
    an escape introducer, so un-escaped values silently corrupt the caption
    (e.g. ``"strength=0.5"`` truncates to ``"strength=0"``).

    Args:
        s: Raw caption string from the user's grid spec.

    Returns:
        ``s`` with every special char replaced by its escaped form.
        Backslash MUST be processed first to avoid double-escaping the
        escapes inserted for the other chars.
    """
    out = s.replace("\\", _DRAWTEXT_ESCAPED["\\"])
    for ch in (":", "'", "%", "\n"):
        out = out.replace(ch, _DRAWTEXT_ESCAPED[ch])
    return out
