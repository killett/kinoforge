"""CivitAI model source — resolves ``civitai:<modelId>[@<versionId>]`` refs.

Queries the CivitAI REST API and returns one :class:`~kinoforge.core.interfaces.Artifact`
per file listed in the model-version payload.  An optional ``CIVITAI_TOKEN``
credential is attached both to the HTTP request and to each artifact's headers
so the downloader can authenticate the file-download URL.

The HTTP transport is injected via the ``fetch`` constructor parameter so tests
can pass a spy/stub without touching the network at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinoforge.core import registry
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource

# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

_BASE = "https://civitai.com/api/v1"

FetchCallable = Callable[[str, dict[str, str]], dict[str, Any]]


def _urllib_fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """Fetch *url* with GET, parse JSON, return the decoded dict.

    Args:
        url: The endpoint URL.
        headers: HTTP request headers to include.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        AuthError: The server returned HTTP 401.
        KinoforgeError: Any other non-2xx HTTP error or network failure.
    """
    req = Request(url, headers=headers)  # noqa: S310
    try:
        with urlopen(req) as resp:  # noqa: S310 — only civitai.com HTTPS URLs used
            body: bytes = resp.read()
    except HTTPError as exc:
        if exc.code == 401:
            raise AuthError(f"CivitAI 401 Unauthorized for {url}") from exc
        raise KinoforgeError(f"CivitAI HTTP {exc.code} for {url}") from exc
    parsed: dict[str, Any] = json.loads(body.decode("utf-8"))
    return parsed


# ---------------------------------------------------------------------------
# Ref pattern
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^civitai:(\d+)(?:@(\d+))?$")


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class CivitAISource(ModelSource):
    """Resolves ``civitai:<modelId>[@<versionId>]`` refs via the CivitAI API.

    The optional ``fetch`` parameter injects the HTTP transport.  Tests pass a
    spy that returns canned dicts; the default ``_urllib_fetch_json`` uses
    ``urllib.request`` from the stdlib.

    Attributes:
        scheme: Registry scheme key — ``"civitai"``.
    """

    scheme = "civitai"

    def __init__(
        self,
        *,
        fetch: FetchCallable = _urllib_fetch_json,
    ) -> None:
        """Initialise the source with an optional transport override.

        Args:
            fetch: Callable ``(url, headers) -> dict`` used to perform HTTP
                requests.  Defaults to :func:`_urllib_fetch_json`.
        """
        self._fetch = fetch

    def handles(self, ref: str) -> bool:
        """Return ``True`` when *ref* matches ``civitai:<digits>[@<digits>]``.

        Args:
            ref: The model reference string to test.

        Returns:
            ``True`` if *ref* is a well-formed CivitAI ref.
        """
        return _REF_RE.match(ref) is not None

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve a CivitAI ref to a list of :class:`~kinoforge.core.interfaces.Artifact` s.

        Depending on whether a version ID is embedded in *ref*:

        - ``civitai:<modelId>`` — GETs ``/models/{modelId}``, picks the first
          entry in ``modelVersions``, then GETs ``/model-versions/{versionId}``.
        - ``civitai:<modelId>@<versionId>`` — GETs
          ``/model-versions/{versionId}`` directly.

        Each ``file`` entry in the version payload becomes one
        :class:`~kinoforge.core.interfaces.Artifact`.

        Args:
            ref: The model reference string (e.g. ``"civitai:1234@5678"``).
            creds: Credential provider; read ``CIVITAI_TOKEN`` from it.

        Returns:
            List of :class:`~kinoforge.core.interfaces.Artifact` objects, one
            per file in the resolved model version.

        Raises:
            AuthError: The API returned HTTP 401 (re-raised from the transport).
        """
        m = _REF_RE.match(ref)
        if m is None:
            raise ValueError(f"Not a valid CivitAI ref: {ref!r}")

        model_id_str, version_id_str = m.group(1), m.group(2)

        token: str | None = creds.get("CIVITAI_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}

        if version_id_str is None:
            # Model-only ref: resolve the first version first.
            model_url = f"{_BASE}/models/{model_id_str}"
            model_data = self._fetch(model_url, headers)
            first_version: dict[str, Any] = model_data["modelVersions"][0]
            version_id_str = str(first_version["id"])

        version_url = f"{_BASE}/model-versions/{version_id_str}"
        version_data = self._fetch(version_url, headers)

        artifacts: list[Artifact] = []
        for file_entry in version_data.get("files", []):
            size_kb: float | None = file_entry.get("sizeKB")
            size_bytes: int | None = (
                int(size_kb * 1024) if size_kb is not None else None
            )

            raw_sha256: str = file_entry.get("hashes", {}).get("SHA256", "") or ""
            sha256: str | None = raw_sha256.lower() if raw_sha256 else None

            artifacts.append(
                Artifact(
                    url=file_entry["downloadUrl"],
                    filename=file_entry["name"],
                    size=size_bytes,
                    sha256=sha256,
                    headers=dict(headers),
                )
            )

        return artifacts


# Self-register on import so a single ``import kinoforge.sources.civitai`` is
# enough for ``source_for_ref()`` to route CivitAI refs without an explicit
# register call.
registry.register_source(CivitAISource())
