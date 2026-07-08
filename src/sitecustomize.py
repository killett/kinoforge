"""Interpreter-startup hook: apply the vast SDK compat shim in EVERY process.

Python imports a top-level ``sitecustomize`` module at interpreter startup (unless
``-S`` is passed). ``src`` is on ``PYTHONPATH`` for every ``pixi run`` (see
``pixi.toml`` ``[activation.env] PYTHONPATH = "src"``), so this file runs at the
start of every Python process in the workspace — crucially including SkyPilot's
**API server subprocess** (``python -m sky.server.server``), which is where vast
provisioning actually executes.

Why this exists (root-caused 2026-07-07): the in-process shim in
``kinoforge.providers.skypilot.vast_compat`` patches ``VastAI`` only in the
kinoforge client process. But ``sky.launch`` runs inside the separately-spawned
API server, which never imports kinoforge, so the client-side patch is invisible
there and every vast launch dies with ``VastAI has no attribute client``. Patching
here — at interpreter startup, before sky's vast adapter is imported — makes the
fix reach the server process too.

The patch body is duplicated (not imported) from ``vast_compat.py`` on purpose:
importing ``kinoforge.providers.skypilot`` at every interpreter start would run the
provider package's registration side effects (and risk import cycles) on every
tool, test, and subprocess in the env. This leaf is tiny, dependency-free, and a
hard no-op when ``vastai_sdk`` is absent (the default env). Everything is wrapped
so a failure here can NEVER break interpreter startup.
"""

from __future__ import annotations


def _apply_vast_sdk_compat() -> None:
    """Add ``VastAI.client`` → ``self`` so sky's ``.client.api_key`` resolves.

    Mirrors ``kinoforge.providers.skypilot.vast_compat.apply_vast_sdk_compat``;
    kept self-contained so interpreter startup imports nothing but ``vastai_sdk``
    (and only when it is installed). Idempotent + self-disabling.
    """
    try:
        from vastai_sdk import VastAI  # type: ignore[import-not-found, unused-ignore]
    except Exception:  # noqa: BLE001 — sdk absent (default env) → nothing to patch
        return
    if getattr(VastAI, "client", None) is not None:
        return  # real client attr or a prior patch → leave untouched
    VastAI.client = property(lambda self: self)  # type: ignore[attr-defined, unused-ignore]


try:
    _apply_vast_sdk_compat()
except Exception:  # noqa: BLE001, S110 — startup hook must never raise
    pass
