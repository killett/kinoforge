"""Environment-variable credential provider."""

import os

from kinoforge.core.interfaces import CredentialProvider


class EnvCredentialProvider(CredentialProvider):
    """Resolves secrets from process environment variables."""

    def get(self, key: str) -> str | None:
        """Return ``os.environ[key]`` or ``None`` if unset.

        Args:
            key: Environment variable name.

        Returns:
            The variable's current value, or ``None`` when the variable is unset.
        """
        return os.environ.get(key)
