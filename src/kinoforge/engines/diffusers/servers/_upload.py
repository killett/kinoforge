"""Streaming ``PUT /upload`` handler for pod servers.

Extracted by SHAPE from ``wan_t2v_server``'s inline handler (which keeps its
own copy — design §6.4) so a second server can honour the exact contract
``engines/_pod_http.PodHTTPClientMixin._upload_source`` cross-checks:
``Content-Type`` from an allowed set (415 otherwise), ``X-Filename`` sanitised
to a basename in ``[A-Za-z0-9._-]``, body streamed into a tempfile under a size
cap (413 + cleanup), atomic ``os.replace``, and a ``{"path", "size", "sha256"}``
reply the client compares against its own digest.

Imports: stdlib + fastapi only. This module rides onto the pod via
``embed_files``; the pod has no ``kinoforge.core``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import string
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

#: Characters a client-supplied filename may keep.
FILENAME_ALLOWED: frozenset[str] = frozenset(
    string.ascii_letters + string.digits + "._-"
)


def sanitize_filename(raw: str | None, *, fallback_suffix: str) -> str:
    """Return a safe basename for *raw*, or a random ``<hex8><fallback_suffix>``.

    Args:
        raw: The client's ``X-Filename`` header; may be absent or dirty.
        fallback_suffix: Suffix for the random fallback name (``".png"``).

    Returns:
        A non-empty basename made only of :data:`FILENAME_ALLOWED` characters.
    """
    if not raw:
        return f"{secrets.token_hex(4)}{fallback_suffix}"
    base = Path(raw).name  # strips every path component
    cleaned = "".join(c for c in base if c in FILENAME_ALLOWED)
    # ``Path("..").name == ".."`` (and ``Path("a/..").name``, ``Path("./..").name``
    # — both also resolve to ".."), and "." / "-" are both in FILENAME_ALLOWED, so
    # a header of exactly ".." (or "...", or any all-dots string) survives the
    # character filter above unchanged. `os.replace(tmp, dir / "..")` then raises
    # OSError (EBUSY: the parent directory is a live mount point), which, before
    # this check, escaped as a 500 with the already-written ``.part`` tempfile
    # left behind on the pod disk — the exact leak class this module guards
    # against for an oversize body. Treat any name that is nothing but dots as
    # empty and fall through to the random fallback.
    if not cleaned or cleaned.strip(".") == "":
        return f"{secrets.token_hex(4)}{fallback_suffix}"
    return cleaned


async def receive_upload(
    request: Request,
    *,
    upload_dir: Path,
    content_types: frozenset[str],
    max_bytes: int,
    fallback_suffix: str,
) -> dict[str, Any]:
    """Stream the body into *upload_dir*; return its path, size and sha256.

    Args:
        request: The incoming PUT.
        upload_dir: Pod-local directory; created ``0o700`` when absent.
        content_types: Accepted ``Content-Type`` values (media type only).
        max_bytes: Reject bodies longer than this with 413.
        fallback_suffix: Suffix for a missing/dirty ``X-Filename``.

    Returns:
        ``{"path": str, "size": int, "sha256": str}``.

    Raises:
        HTTPException: 415 on a disallowed Content-Type; 413 when the body
            exceeds *max_bytes* (the partial tempfile is removed first); 500
            when the final ``os.replace`` itself fails (also cleaned up first
            — see the module-level note on why publish lives inside the same
            guarded block as the stream).
    """
    ct = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ct not in content_types:
        raise HTTPException(
            status_code=415,
            detail=f"Content-Type must be one of {sorted(content_types)}, got {ct!r}",
        )
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_name = sanitize_filename(
        request.headers.get("x-filename"), fallback_suffix=fallback_suffix
    )
    fd, tmp_name = tempfile.mkstemp(dir=str(upload_dir), suffix=".part")
    tmp_path = Path(tmp_name)
    hasher = hashlib.sha256()
    written = 0
    final = upload_dir / safe_name
    try:
        # kinoforge:public-write — pod-local scratch, never the operator's host.
        with os.fdopen(fd, "wb") as fobj:  # kinoforge:public-write
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413, detail=f"upload exceeded {max_bytes} bytes"
                    )
                hasher.update(chunk)
                fobj.write(chunk)
        # The publish step lives INSIDE this same guarded block, not after it.
        # A failure here (ENOSPC, a directory already sitting at `final`, a
        # cross-device final dir) is exactly as able to leave the `.part`
        # tempfile behind as a failure during streaming, and the module
        # docstring promises cleanup for both.
        os.replace(tmp_path, final)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as e:
        # Translated to a clean HTTPException rather than left to escape as an
        # unhandled OSError: FastAPI would turn the latter into a 500 anyway,
        # but TestClient (and a real client reading the connection) sees a
        # raw server-side traceback instead of a normal error response.
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"failed to publish upload: {e}"
        ) from e
    except BaseException:
        # Anything else (client disconnect, cancellation, …) still must not
        # leave the `.part` tempfile behind; re-raised as-is, unlike OSError,
        # since FastAPI/Starlette already handle those shapes sensibly.
        tmp_path.unlink(missing_ok=True)
        raise
    return {"path": str(final), "size": written, "sha256": hasher.hexdigest()}
