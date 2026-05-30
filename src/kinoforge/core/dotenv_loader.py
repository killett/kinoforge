"""Load environment variables from a project-root .env file.

Single-purpose module. Exposes one function, :func:`load_env_file`, which is
called once at CLI startup (see :func:`kinoforge.cli.main`) to populate
``os.environ`` with values from a ``.env`` file. Every downstream secret
consumer — :class:`kinoforge.core.credentials.EnvCredentialProvider`, the
boto3 default credential chain, the google-cloud-storage default credential
chain — reads ``os.environ`` unchanged.

Design contract:
- Shell-set values win (``override=False`` default).
- Default path is ``Path.cwd() / ".env"``; absent default file is a silent no-op.
- An explicitly-passed missing path raises :class:`FileNotFoundError`.
- INFO log on successful load shows the path + key count, never values.

See ``docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md`` for the
full design contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_log = logging.getLogger(__name__)


def load_env_file(path: Path | None = None, *, override: bool = False) -> None:
    """Load environment variables from a .env file into ``os.environ``.

    Args:
        path: Path to the .env file. Defaults to ``Path.cwd() / ".env"``.
            When the default path does not exist, the call is a silent no-op.
            When an explicit *path* is provided and does not exist, raises
            :class:`FileNotFoundError`.
        override: When ``False`` (default), existing ``os.environ`` values
            win and ``.env`` only fills unset keys. When ``True``, ``.env``
            values overwrite existing ``os.environ`` values.

            The CLI always calls with the default ``False``; ``override=True``
            is exposed for library users who explicitly want ``.env`` to
            clobber existing values.

    Raises:
        FileNotFoundError: When *path* is explicitly provided but does not
            exist on disk.
    """
    explicit = path is not None
    resolved = path if path is not None else Path.cwd() / ".env"

    if not resolved.exists():
        if explicit:
            raise FileNotFoundError(resolved)
        return

    parsed = dotenv_values(resolved)
    load_dotenv(resolved, override=override)
    _log.info("loaded .env from %s (%d keys)", resolved, len(parsed))
