"""kinoforge — vendor-agnostic video-generation provisioning & orchestration."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path


def _resolve_version() -> str:
    """Resolve kinoforge's version string.

    Primary: ``importlib.metadata.version`` (works for both editable and
    wheel installs). Fallback: parse ``pyproject.toml`` from the repo
    root — used when running from an un-installed source tree.

    Returns:
        The kinoforge version string (e.g. ``"0.1.0"``).
    """
    try:
        return importlib.metadata.version("kinoforge")
    except importlib.metadata.PackageNotFoundError:
        here = Path(__file__).resolve()
        for parent in here.parents:
            pyproject = parent / "pyproject.toml"
            if pyproject.is_file():
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                version = data.get("project", {}).get("version")
                if isinstance(version, str):
                    return version
                break
        return "0.0.0+unknown"


__version__ = _resolve_version()
