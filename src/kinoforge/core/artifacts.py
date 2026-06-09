"""Helper for opaque store-side filenames.

Every ``ArtifactStore.put_bytes(run_id, name, payload)`` call site uses
:func:`opaque_store_name` so the on-disk filename is derived purely from
content hash — never from prompt-derived material. AC2 of the CI invariant
test (Task 19) enforces this at merge time.
"""

from __future__ import annotations

import hashlib
import re

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}")


def opaque_store_name(payload: bytes, original_ext: str) -> str:
    r"""Return a store-side filename derived purely from ``payload``'s sha256.

    Args:
        payload: The bytes being persisted.
        original_ext: An extension like ``".mp4"``. Verified against
            ``\.[A-Za-z0-9]{1,5}``; dropped otherwise.

    Returns:
        ``<16-hex>[.ext]`` — no prompt-derived material ever appears in the
        returned name.
    """
    digest = hashlib.sha256(payload).hexdigest()[:16]
    safe_ext = original_ext if _EXT_RE.fullmatch(original_ext or "") else ""
    return f"{digest}{safe_ext}"
