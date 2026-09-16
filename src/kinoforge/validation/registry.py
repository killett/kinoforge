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
        # U47: a check's ``applies_to`` may REGISTER another check — that is not
        # hypothetical, ``provider_capabilities.applies_to`` reaches the provider
        # registry and importing it self-registers
        # ``skypilot_cloud_pin_supported`` and ``runpod_capacity_hint``. Iterating
        # ``self._checks.values()`` directly then raised
        # ``RuntimeError: dictionary changed size during iteration`` straight out
        # of ``validate_for_generate``, before a single CheckResult existed.
        #
        # Draining to a fixed point rather than iterating a snapshot, deliberately:
        # a snapshot stops the crash and silently SKIPS whatever registered during
        # the pass, so those two real checks would never run on the first
        # validation — and for ``validate_for_load`` the first validation is the
        # only one there is. Insertion order is preserved because ``dict`` keeps
        # it and the pending slice is taken in that order.
        out: list[Check] = []
        evaluated: set[str] = set()
        while True:
            pending = [
                (name, c)
                for name, c in list(self._checks.items())
                if name not in evaluated
            ]
            if not pending:
                return out
            for name, check in pending:
                evaluated.add(name)
                if (
                    categories is None or check.category in categories
                ) and check.applies_to(cfg):
                    out.append(check)

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
