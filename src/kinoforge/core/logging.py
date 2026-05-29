"""Structured stdlib logging helper."""

import logging

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger; idempotent across calls.

    Args:
        name: Sub-logger name; will be prefixed with ``kinoforge.``.

    Returns:
        A logger configured with a single stream handler if first call; otherwise
        returns the existing logger unchanged.
    """
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root = logging.getLogger("kinoforge")
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        _CONFIGURED = True
    return logging.getLogger(f"kinoforge.{name}")
