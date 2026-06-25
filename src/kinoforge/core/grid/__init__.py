"""Grid composition + N-generation orchestration.

See ``docs/superpowers/specs/2026-06-25-kinoforge-grid-design.md`` for the
full design. Public API:

- :class:`GridSpec` — pydantic model for the grid spec file.
- :func:`run_grid` — async entry point (cell resolution + execution + composition).
- :class:`GridResult` — return type of :func:`run_grid`.
"""

from __future__ import annotations
