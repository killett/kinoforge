"""Frame-interpolation engines — sibling of ``kinoforge.upscalers``.

Each engine self-registers with ``kinoforge.core.registry`` on import of its
subpackage (e.g. ``kinoforge.interpolators.rife``). Adapters are wired into the
aggregate registration surface via ``kinoforge._adapters``.
"""
