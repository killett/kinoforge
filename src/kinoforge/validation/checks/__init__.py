"""Built-in cfg validation checks.

Importing this package self-registers every built-in check on the
default registry. Tests that need a clean registry should construct
their own CheckRegistry instance instead.
"""

from kinoforge.validation.checks import (  # noqa: F401 — self-register
    heartbeat,
    lifecycle,
)
