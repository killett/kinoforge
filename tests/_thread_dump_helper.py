"""Pure formatter for the post-session thread-dump diagnostic.

Extracted from tests/conftest.py:329-390 so the formatter can be
unit-tested without spawning pytest. The hook itself stays in
conftest.py (pytest's discovery boundary) and delegates to this module
for the dump string.
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from typing import Any


def _build_dump(threads: list[Any], exitstatus: int) -> str:
    """Format a post-session thread dump as a single multi-line string.

    The output is identical to what the previous inline body in
    tests/conftest.py produced, modulo: this function takes the thread
    list as an argument (so callers can inject fakes), and it appends
    the linux ``/proc/self/fd/`` inventory unconditionally on linux
    (the caller is responsible for fast-pathing the empty-leak case
    before invoking).

    Args:
        threads: The live thread objects (or duck-typed stand-ins with
            ``name``, ``ident``, ``daemon``, ``is_alive`` attributes) to
            include in the dump.
        exitstatus: The exit status pytest will return. Echoed in the
            banner so a green vs. red session is distinguishable in CI
            logs.

    Returns:
        A multi-line string ending with a newline. Structure:

        - One ``=== POST-SESSION THREAD DUMP === pid=... exitstatus=... n_threads=...`` banner.
        - One ``  thread name=... ident=... daemon=... alive=...`` line per
          thread, followed by either the formatted Python stack or a
          C-extension fallback marker.
        - On linux: one trailing ``  open fds: N → [...]`` line. On
          macOS / non-linux: omitted.
    """
    import threading  # noqa: PLC0415  — kept local for symmetry with the hook

    main_ident = threading.main_thread().ident
    frames = sys._current_frames()

    buf = io.StringIO()
    buf.write(
        f"=== POST-SESSION THREAD DUMP === pid={os.getpid()} "
        f"exitstatus={exitstatus} n_threads={len(threads)}\n"
    )
    for t in threads:
        marker = " (main)" if t.ident == main_ident else ""
        buf.write(
            f"  thread name={t.name!r} ident={t.ident} "
            f"daemon={t.daemon} alive={t.is_alive()}{marker}\n"
        )
        frame = frames.get(t.ident) if t.ident is not None else None
        if frame is None:
            buf.write("    <no Python frame — likely in C extension>\n")
            continue
        for line in traceback.format_stack(frame):
            buf.write("    " + line.rstrip() + "\n")

    try:
        fds = sorted(int(e) for e in os.listdir("/proc/self/fd/"))
        buf.write(f"  open fds: {len(fds)} → {fds}\n")
    except OSError:
        # macOS / non-linux — no /proc. Skip the FD inventory.
        pass

    return buf.getvalue()
