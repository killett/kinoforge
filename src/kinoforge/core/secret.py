"""Lightweight Secret newtype used at the vault → orchestrator → engine boundary.

Carried inside narrow boundary code so unwrap sites are self-documenting.
Not used in any SPEC ABC; the ABCs stay str-typed per architecture choice C.
On-disk enforcement is the redaction registry + sink-canonical pattern in
core.redaction + the canonical write-site shape at every persistent sink.
"""

from __future__ import annotations

from typing import final


@final
class Secret:
    """A string that must never reach a persistent surface implicitly.

    Carried only at the vault → orchestrator → engine boundary. Logs, JSON,
    and f-strings see ``<Secret>``; the underlying value is reachable only via
    an explicit :meth:`reveal` call.

    Args:
        value: The string to wrap.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap ``value`` as a Secret."""
        self._value = value

    def reveal(self) -> str:
        """Return the wrapped string.

        Returns:
            The underlying string value.
        """
        return self._value

    def __repr__(self) -> str:
        """Return ``'<Secret>'`` so traceback locals never leak the value."""
        return "<Secret>"

    def __str__(self) -> str:
        """Return ``'<Secret>'`` so f-string interpolation never leaks the value."""
        return "<Secret>"

    def __eq__(self, other: object) -> bool:
        """Compare underlying values; cross-type comparisons return False."""
        if isinstance(other, Secret):
            return self._value == other._value
        return False

    def __hash__(self) -> int:
        """Hash the underlying value so Secret can be used as a dict key."""
        return hash(self._value)
