"""Resumable, checksum-verifying parallel HTTP downloader.

Uses only stdlib: ``hashlib``, ``urllib.request``, ``concurrent.futures``,
``pathlib``, ``os``, ``threading``.

The HTTP transport is injected via a ``fetch`` callable, making it trivially
replaceable in tests by pointing at a loopback server instead of the real
network.

# DEFERRED: aria2c fast path — opportunistic shutil.which("aria2c") escape hatch
# not implemented in this task; add KINOFORGE_USE_ARIA2=1 check when aria2c
# integration is scoped.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from kinoforge.core.errors import KinoforgeError
from kinoforge.core.interfaces import Artifact

# Type alias for the injected fetch callable.
# Returns (status_code, body_bytes, response_headers).
FetchCallable = Callable[[str, dict[str, str]], tuple[int, bytes, dict[str, str]]]

_CHUNK = 8192  # 8 KiB read buffer for sha256 streaming


def sha256_file(path: Path) -> str:
    """Compute the hex-encoded SHA-256 digest of *path* using streaming reads.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hex string of the SHA-256 digest.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _urllib_fetch(
    url: str,
    headers: dict[str, str],
) -> tuple[int, bytes, dict[str, str]]:
    """Thin stdlib wrapper around ``urllib.request``.

    Sends a GET request to *url* with the provided *headers* and returns the
    response as a ``(status, body, response_headers)`` triple.  Only 200 and
    206 are treated as success; all other status codes raise
    :class:`~kinoforge.core.errors.KinoforgeError`.

    Args:
        url: Fully-qualified URL to fetch.
        headers: Extra request headers (e.g. ``{"Range": "bytes=N-"}``).

    Returns:
        A tuple of ``(http_status: int, body: bytes, resp_headers: dict)``.

    Raises:
        KinoforgeError: On non-2xx status or network-level ``URLError``.
    """
    req = Request(url, headers=headers)  # noqa: S310
    try:
        with urlopen(req) as resp:  # noqa: S310
            status: int = resp.status
            body: bytes = resp.read()
            resp_headers: dict[str, str] = {
                k.lower(): v for k, v in resp.headers.items()
            }
    except URLError as exc:
        raise KinoforgeError(f"HTTP request failed for {url!r}: {exc}") from exc

    if status not in (200, 206):
        raise KinoforgeError(f"Unexpected HTTP status {status} for {url!r}")
    return status, body, resp_headers


def download_one(
    artifact: Artifact,
    dest: Path,
    *,
    fetch: FetchCallable = _urllib_fetch,
) -> Artifact:
    """Download *artifact* into *dest*, resuming from a ``.part`` file if present.

    Behaviour summary:
    - **Skip path**: if ``dest/<filename>`` already exists AND either no
      ``sha256`` is specified (filename-based trust) OR ``sha256`` matches the
      on-disk file, return immediately — ZERO HTTP traffic.
    - **Corrupt target**: if ``dest/<filename>`` exists but sha256 does NOT
      match, delete it and fall through to a fresh download.
    - **Resume path**: if ``dest/<filename>.part`` exists, send
      ``Range: bytes=<size>-`` and append the response body to the ``.part``
      file.
    - **Fresh path**: otherwise fetch without a Range header and write the
      body to the ``.part`` file.
    - After writing, if ``artifact.sha256`` is set, verify the assembled
      ``.part``.  On mismatch the ``.part`` is deleted and
      :class:`~kinoforge.core.errors.KinoforgeError` is raised.

    Corrupt ``.part`` strategy (AC #5):
        We do NOT pre-validate the ``.part`` before appending.  After appending
        the server response, the full sha256 is checked.  If it fails (because
        the prefix bytes were garbage), ``.part`` is deleted and
        ``KinoforgeError`` is raised.  The *next* call finds no ``.part`` and
        downloads cleanly from scratch.  This keeps the code simple and
        produces a correct file after at most two calls (or one call if the
        first call's Range response is also garbage — not the server's fault).

    Args:
        artifact: Source descriptor; ``filename``, ``url``, and optionally
            ``sha256`` are used.
        dest: Directory into which the final file is written.
        fetch: Injectable HTTP callable (default: :func:`_urllib_fetch`).
            Signature: ``(url, headers) -> (status, body, resp_headers)``.

    Returns:
        A new :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set to
        the absolute path of the downloaded file.

    Raises:
        KinoforgeError: On sha256 mismatch or HTTP transport failure.
    """
    target_path = dest / artifact.filename
    part_path = Path(str(target_path) + ".part")

    # ------------------------------------------------------------------
    # Skip path
    # ------------------------------------------------------------------
    if target_path.exists():
        if artifact.sha256 is None:
            # No checksum — trust the filename, skip entirely.
            return replace(artifact, uri=str(target_path))
        if sha256_file(target_path) == artifact.sha256:
            # Checksum matches — idempotent skip, no HTTP.
            return replace(artifact, uri=str(target_path))
        # Checksum mismatch on existing target — discard and re-download.
        target_path.unlink()

    # ------------------------------------------------------------------
    # Decide whether to resume or start fresh
    # ------------------------------------------------------------------
    req_headers: dict[str, str] = {}

    if part_path.exists():
        n = part_path.stat().st_size
        req_headers["Range"] = f"bytes={n}-"

    _status, body, _resp_headers = fetch(artifact.url, req_headers)

    # ------------------------------------------------------------------
    # Write body
    # ------------------------------------------------------------------
    if part_path.exists():
        # Append resume bytes.
        with part_path.open("ab") as fh:
            fh.write(body)
    else:
        # Fresh write.
        with part_path.open("wb") as fh:
            fh.write(body)

    # ------------------------------------------------------------------
    # Verify checksum
    # ------------------------------------------------------------------
    if artifact.sha256 is not None:
        actual = sha256_file(part_path)
        if actual != artifact.sha256:
            part_path.unlink(missing_ok=True)
            raise KinoforgeError(
                f"sha256 mismatch for {artifact.filename!r}: "
                f"expected {artifact.sha256}, got {actual}"
            )

    # ------------------------------------------------------------------
    # Atomically promote .part → final file
    # ------------------------------------------------------------------
    os.replace(part_path, target_path)
    return replace(artifact, uri=str(target_path))


def download_all(
    artifacts: list[Artifact],
    dest: Path,
    *,
    max_workers: int = 4,
    fetch: FetchCallable = _urllib_fetch,
) -> list[Artifact]:
    """Download multiple artifacts concurrently.

    Uses :class:`concurrent.futures.ThreadPoolExecutor` with *max_workers*
    threads.  Results are returned in the same order as *artifacts*.

    Args:
        artifacts: List of artifacts to download.
        dest: Common destination directory.
        max_workers: Maximum number of concurrent download threads.
        fetch: Injectable HTTP callable forwarded to each
            :func:`download_one` call.

    Returns:
        List of updated :class:`~kinoforge.core.interfaces.Artifact` instances
        (one per input, in input order) with ``uri`` set to the absolute
        on-disk path.

    Raises:
        KinoforgeError: Propagated from any failing :func:`download_one` call.
    """
    results: list[Artifact] = [cast(Artifact, None)] * len(artifacts)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(download_one, artifact, dest, fetch=fetch): idx
            for idx, artifact in enumerate(artifacts)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = (
                future.result()
            )  # re-raises on exception; all slots filled on success.

    return results
