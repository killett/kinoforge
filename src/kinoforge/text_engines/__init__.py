"""Text engines — controller-side clients + provision fragments for `kinoforge text`.

Each sub-package self-registers via ``registry.register_text_engine`` and is
imported exactly once from ``kinoforge._adapters``. Design:
``docs/superpowers/specs/2026-10-04-text-command-design.md`` §5.
"""
