"""Process-wide redaction registry + logging filter.

The registry is the single source of truth for tokens that must be
substituted on every persistent surface (logs, JSON files, stdout, error
blocks). The vault loader is the only writer; sinks (Ledger, profile cache,
batch summary, OutputSink.publish, _save_fixture, etc.) are readers.

Empty registry == public-by-design passthrough (the standard test prompt
path).
"""

from __future__ import annotations

import hashlib
import logging
import re

_PLACEHOLDER_RE = re.compile(r"<.+?:.+?>")
_MIN_TOKEN_LEN = 4


def _short_id(token: str) -> str:
    """Return a deterministic 6-char hex suffix for a token.

    Used in placeholders to distinguish multiple tokens of the same kind.

    Args:
        token: The token to hash.

    Returns:
        First 6 hex chars of ``sha256(token)``.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:6]


def _path_is_exempt(path: tuple[str, ...], exempt_paths: frozenset[str]) -> bool:
    """Return whether *path* ends with any dotted pattern in *exempt_paths*.

    Suffix matching so a caller names the field relative to the structure it
    owns (``"lora_inventory.*.ref"``) rather than the whole payload shape.

    Args:
        path: Key segments walked so far; list indices appear as ``*``.
        exempt_paths: Dotted patterns.

    Returns:
        True when the walk has reached an exempt node.
    """
    if not path:
        return False
    for pattern in exempt_paths:
        segments = tuple(pattern.split("."))
        if len(segments) <= len(path) and path[-len(segments) :] == segments:
            return True
    return False


class RedactionRegistry:
    """Singleton holding the active vault's sensitive tokens.

    Use ``RedactionRegistry.instance()`` to access; never instantiate directly.
    """

    _singleton: RedactionRegistry | None = None

    def __init__(self) -> None:
        """Initialise an empty token map. Use :meth:`instance` in callers."""
        self._tokens: dict[str, str] = {}

    @classmethod
    def instance(cls) -> RedactionRegistry:
        """Return the process-wide registry, creating it on first access.

        Returns:
            The shared :class:`RedactionRegistry` instance.
        """
        if cls._singleton is None:
            cls._singleton = RedactionRegistry()
        return cls._singleton

    def add(self, token: str, *, kind: str, replacement: str | None = None) -> None:
        """Register ``token`` for substitution.

        Args:
            token: The exact string to substitute. Must be at least 4 chars,
                not whitespace-only, and not match the placeholder pattern.
            kind: A label describing the token category, e.g.
                ``'prompt:positive'``, ``'lora:ref'``, ``'output'``.
            replacement: Override the default placeholder. If ``None``,
                ``f'<{kind}:{short_id}>'`` is used.

        Raises:
            ValueError: If ``token`` fails the length / whitespace /
                placeholder rules.
        """
        if len(token) < _MIN_TOKEN_LEN:
            raise ValueError(
                f"redaction token must be at least {_MIN_TOKEN_LEN} chars: {token!r}"
            )
        if not token.strip():
            raise ValueError(f"redaction token cannot be whitespace-only: {token!r}")
        if _PLACEHOLDER_RE.search(token):
            raise ValueError(f"redaction token matches placeholder pattern: {token!r}")
        if token in self._tokens:
            return  # idempotent
        self._tokens[token] = (
            replacement if replacement is not None else f"<{kind}:{_short_id(token)}>"
        )

    def add_many(self, tokens: list[tuple[str, str]]) -> None:
        """Bulk-register ``(token, kind)`` pairs.

        Args:
            tokens: A list of ``(token, kind)`` pairs to register via
                :meth:`add`.
        """
        for token, kind in tokens:
            self.add(token, kind=kind)

    def redact(self, s: str) -> str:
        """Substitute every registered token in ``s`` with its placeholder.

        Tokens are applied longest-first to avoid partial overlap.
        Case-sensitive.

        Args:
            s: The string to scan.

        Returns:
            A new string with substitutions applied; identity if no tokens
            are registered.
        """
        if not self._tokens:
            return s
        result = s
        for token in sorted(self._tokens, key=len, reverse=True):
            if token in result:
                result = result.replace(token, self._tokens[token])
        return result

    def redact_json(
        self, obj: object, *, exempt_paths: frozenset[str] = frozenset()
    ) -> object:
        """Deep-walk ``obj`` and redact every string leaf.

        Recursively handles ``dict``, ``list``, and ``tuple``; passes through
        non-string scalar leaves untouched. Always returns a new structure;
        never mutates the input.

        Args:
            obj: Any JSON-shaped Python value.
            exempt_paths: Dotted key paths whose values are copied VERBATIM
                rather than walked (U54). A list index contributes the literal
                segment ``*``, and a pattern matches as a SUFFIX of the walked
                path — so ``"lora_inventory.*.ref"`` matches
                ``entries.*.lora_inventory.*.ref`` without the caller having to
                know the payload's outer shape. Matching the full path rather
                than the leaf key name is deliberate: a bare ``ref`` rule would
                silently extend the carve-out to any future structure that
                happens to carry that key.

                Use this ONLY for a field the system must read back. One-way
                redaction of a round-trip field is a bug regardless of
                confidentiality policy — the field becomes useless, and a
                corrupted value is not a privacy win over an absent one. Where
                a value genuinely must not reach disk, do not write it.

        Returns:
            A copy of ``obj`` with all string leaves redacted except those at
            an exempt path.
        """
        return self._redact_json(obj, (), exempt_paths)

    def _redact_json(
        self, obj: object, path: tuple[str, ...], exempt_paths: frozenset[str]
    ) -> object:
        """Recursive half of :meth:`redact_json`, tracking the key path.

        Args:
            obj: The current node.
            path: Key segments walked so far; list indices contribute ``*``.
            exempt_paths: See :meth:`redact_json`.

        Returns:
            The redacted node, or *obj* verbatim when *path* is exempt.
        """
        if exempt_paths and _path_is_exempt(path, exempt_paths):
            return obj
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {
                k: self._redact_json(v, (*path, str(k)), exempt_paths)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._redact_json(v, (*path, "*"), exempt_paths) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self._redact_json(v, (*path, "*"), exempt_paths) for v in obj)
        return obj

    def clear_session(self) -> None:
        """Drop every registered token.

        Called by the CLI at orchestrator exit so a long-running test session
        can load multiple vaults sequentially.
        """
        self._tokens.clear()

    @property
    def is_active(self) -> bool:
        """Return ``True`` iff at least one token is registered."""
        return bool(self._tokens)


class RedactingLogFilter(logging.Filter):
    """A ``logging.Filter`` that redacts every record before it is formatted.

    Installed on the root ``kinoforge`` logger at CLI entry. Child loggers
    (``kinoforge.engines.fake``, etc.) inherit it automatically.

    Args:
        registry: The active registry. Usually
            :meth:`RedactionRegistry.instance`.
        bypass: When ``True``, the filter is a passthrough. Only set by
            ``--debug-show-secrets`` (forbidden under ``--ephemeral``).
    """

    def __init__(self, registry: RedactionRegistry, *, bypass: bool = False) -> None:
        """Wrap ``registry`` in a logging.Filter."""
        super().__init__()
        self._registry = registry
        self._bypass = bypass

    def filter(self, record: logging.LogRecord) -> bool:
        """Substitute tokens in ``record.msg`` and string ``record.args``.

        Args:
            record: The log record about to be emitted.

        Returns:
            ``True`` — the filter never drops records; it only rewrites them.
        """
        if self._bypass:
            return True
        if isinstance(record.msg, str):
            record.msg = self._registry.redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._registry.redact(a) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._registry.redact(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True
