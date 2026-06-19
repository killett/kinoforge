"""CheckRegistry — plugin-style registration + filtering for Checks."""

from __future__ import annotations

from kinoforge.validation.protocol import Check, CheckCategory


class CheckRegistry:
    """Holds registered Check instances. Provides filtered iteration.

    Modeled on ``kinoforge.core.registry`` (the existing provider /
    engine / source registry) so operators recognise the pattern.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._checks: dict[str, Check] = {}

    def register(self, check: Check) -> None:
        """Register a check. Rejects duplicate names with ValueError.

        Duplicate-name rejection is deliberate: two modules silently
        overwriting each other's check would mask the kind of
        regression this whole system is designed to prevent.
        """
        if check.name in self._checks:
            raise ValueError(
                f"duplicate Check registration for name {check.name!r}; "
                f"each Check must have a unique name across all modules"
            )
        self._checks[check.name] = check

    def applicable(
        self,
        cfg: object,
        *,
        categories: frozenset[CheckCategory] | None = None,
    ) -> list[Check]:
        """Return checks whose category matches and applies_to(cfg) is True.

        Args:
            cfg: The kinoforge Config object being validated.
            categories: Filter to only these categories. ``None`` (the
                default) means "any category".

        Returns:
            List of applicable Check instances in registration order.
        """
        return [
            c
            for c in self._checks.values()
            if (categories is None or c.category in categories) and c.applies_to(cfg)
        ]

    def all_names(self) -> list[str]:
        """Return all registered check names in registration order."""
        return list(self._checks.keys())


_DEFAULT_REGISTRY = CheckRegistry()


def register(check: Check) -> None:
    """Register a check on the module-level default registry."""
    _DEFAULT_REGISTRY.register(check)


def default_registry() -> CheckRegistry:
    """Return the module-level default registry."""
    return _DEFAULT_REGISTRY
