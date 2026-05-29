"""HTTP(S) model source — resolves direct download URLs to Artifacts."""

from __future__ import annotations

from kinoforge.core import registry
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource


class HTTPSource(ModelSource):
    """Resolves ``https://…`` / ``http://…`` refs to a single Artifact.

    The per-entry sha256 from a config ``ModelEntry`` is merged onto the
    resulting Artifact downstream by the provisioner (Task 10) — this source
    stays dumb about checksums.
    """

    scheme = "https"

    def handles(self, ref: str) -> bool:
        """Return True when ``ref`` starts with ``http://`` or ``https://``.

        Args:
            ref: The model reference string to test.

        Returns:
            ``True`` if ``ref`` uses the ``http`` or ``https`` scheme.
        """
        return ref.startswith(("https://", "http://"))

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Return a single-element list with an Artifact for ``ref``.

        Args:
            ref: The HTTP(S) URL to resolve.
            creds: Credentials provider (unused for plain HTTP; reserved for
                future Authorization-header support).

        Returns:
            ``[Artifact]`` with ``url`` set to ``ref`` and ``filename`` derived
            from the URL path (query string stripped).
        """
        del creds  # Unused; signature preserves the ModelSource contract.
        path = ref.split("?", 1)[0]
        filename = path.rsplit("/", 1)[-1]
        return [Artifact(url=ref, filename=filename)]


# Self-register on import so a single `import kinoforge.sources.http` is enough
# for source_for_ref() to route HTTP refs without an explicit register call.
registry.register_source(HTTPSource())
