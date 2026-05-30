"""HuggingFace model source — resolves ``hf:<repo>:<path>`` refs.

Maps a HuggingFace file reference directly to its canonical resolve URL
(``https://huggingface.co/<repo>/resolve/main/<path>``) without any HTTP
calls.  The optional ``HF_TOKEN`` credential is attached to the returned
:class:`~kinoforge.core.interfaces.Artifact`'s ``headers`` so the downloader
can authenticate private-model file fetches.

No network access is required during ``resolve`` — the URL is constructed
from the ref itself.  Directory listing is **DEFERRED**.

Example ref formats::

    hf:Wan-AI/Wan2.2:diffusion/model.safetensors
    hf:org/model:weights/unet.bin
"""

from __future__ import annotations

import re

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource

# ---------------------------------------------------------------------------
# Ref pattern
# ---------------------------------------------------------------------------

# Matches anything starting with "hf:" followed by at least one non-colon
# character (the repo path), with an optional ":path" suffix.
_REF_RE = re.compile(r"^hf:[^:]+(:.*)?$")

_HF_BASE = "https://huggingface.co"


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class HuggingFaceSource(ModelSource):
    """Resolves ``hf:<repo>:<path>`` refs to a single HuggingFace resolve URL.

    The ref format is::

        hf:<org>/<model>:<path/to/file>

    A bare ``hf:<org>/<model>`` ref (no file path) raises
    :class:`~kinoforge.core.errors.ValidationError` — directory listing is
    deferred to a later task.

    Attributes:
        scheme: Registry scheme key — ``"hf"``.
    """

    scheme = "hf"

    def handles(self, ref: str) -> bool:
        """Return ``True`` when *ref* starts with ``hf:`` followed by a repo path.

        Args:
            ref: The model reference string to test.

        Returns:
            ``True`` if *ref* matches ``^hf:[^:]+(:.*)?$``.
        """
        return _REF_RE.match(ref) is not None

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve a HuggingFace ref to a single :class:`~kinoforge.core.interfaces.Artifact`.

        Parses ``hf:<repo>:<path>`` and constructs the canonical HuggingFace
        resolve URL.  No network requests are made.

        Args:
            ref: The model reference string (e.g.
                ``"hf:Wan-AI/Wan2.2:diffusion/model.safetensors"``).
            creds: Credential provider; reads ``HF_TOKEN`` from it.

        Returns:
            A list containing exactly one :class:`~kinoforge.core.interfaces.Artifact`
            whose ``url`` is the HuggingFace resolve URL for the file.

        Raises:
            ValidationError: *ref* is a bare repo ref with no file path.
                Directory listing is DEFERRED; callers must specify a file path
                (e.g. ``hf:org/model:path/to/file``).
        """
        # Strip the leading "hf:" scheme prefix.
        remainder = ref[len("hf:") :]

        # Split into at most two parts: <repo> and <path>.
        parts = remainder.split(":", 1)
        repo = parts[0]

        if len(parts) < 2 or not parts[1]:
            # DEFERRED: directory listing via HF API
            raise ValidationError(
                f"No file path in HuggingFace ref {ref!r} — "
                "specify a file path (hf:repo:path/to/file). "
                "Directory listing is not yet supported."
            )

        path = parts[1]
        filename = path.rsplit("/", 1)[-1]
        url = f"{_HF_BASE}/{repo}/resolve/main/{path}"

        token: str | None = creds.get("HF_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}

        return [Artifact(url=url, filename=filename, headers=headers)]


# Self-register on import so a single ``import kinoforge.sources.huggingface``
# is enough for ``source_for_ref()`` to route HuggingFace refs without an
# explicit register call.
registry.register_source(HuggingFaceSource())
