"""Session-scoped .env loader for live tests.

Live-test modules gate themselves on ``os.getenv("KINOFORGE_LIVE_TESTS")
== "1"`` plus per-provider credential env vars (RUNPOD_API_KEY,
HF_TOKEN, etc.).  Those checks run at module import time during pytest
collection — before any kinoforge code, and before pixi's
``[activation.env]`` sees the test process.  Without this loader the
operator has to ``source .env`` (or otherwise export every key)
before running ``pixi run pytest tests/live/...``, which is brittle
and quietly skips tests when forgotten.

Loading is silent if ``.env`` is absent or the kinoforge package isn't
importable for any reason — tests then fall back to whatever the
operator has exported in the shell.  ``override`` is left ``False`` so
explicit exports always beat the file.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from kinoforge.core.dotenv_loader import load_env_file
except Exception:  # noqa: BLE001
    load_env_file = None  # type: ignore[assignment]

if load_env_file is not None:
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        load_env_file(env_file)
