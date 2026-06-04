"""Resumable, checksum-verifying parallel HTTP downloader.

Uses stdlib (``hashlib``, ``urllib.request``, ``concurrent.futures``,
``pathlib``, ``os``, ``threading``, ``shutil``, ``subprocess``,
``logging``) plus the optional ``aria2c`` system binary as a transparent
fast-path.

The HTTP transport is injected via a ``fetch`` callable, making it
trivially replaceable in tests by pointing at a loopback server instead
of the real network.  The aria2c subprocess transport is injected via
``which_aria2`` (detect) and ``run_aria2`` (invoke) callables for the
same reason.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from kinoforge.core.errors import KinoforgeError
from kinoforge.core.interfaces import Artifact

logger = logging.getLogger(__name__)

# Type alias for the injected fetch callable.
# Returns (status_code, body_bytes, response_headers).
FetchCallable = Callable[[str, dict[str, str]], tuple[int, bytes, dict[str, str]]]

# Type aliases for the aria2c seams.
# - WhichCallable: returns the absolute path to aria2c, or None when absent.
# - RunAriaCallable: spawns aria2c to download `url` -> `part_path` with
#   the given HTTP headers; raises KinoforgeError on any failure.
WhichCallable = Callable[[], str | None]
RunAriaCallable = Callable[[str, Path, dict[str, str]], None]

_CHUNK = 8192  # 8 KiB read buffer for sha256 streaming

# aria2c invocation knobs.  Battle-tested defaults for HuggingFace /
# CivitAI CDNs; not operator-tunable in this layer (YAGNI).
_ARIA2_BASE_ARGS: tuple[str, ...] = (
    "-x",
    "16",
    "-s",
    "16",
    "-k",
    "1M",
    "--file-allocation=none",
    "--max-tries=3",
    "--retry-wait=2",
    "--allow-overwrite=true",
    "--auto-file-renaming=false",
    "--summary-interval=0",
    "--console-log-level=warn",
)


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


def _shutil_which_aria2() -> str | None:
    """Return the absolute path to aria2c, or ``None`` when not on ``PATH``.

    Returns:
        Result of :func:`shutil.which`.  No caching — repeat callers pay
        a ``PATH`` walk per call (~30us on Linux, negligible against the
        per-file subprocess spawn cost).
    """
    return shutil.which("aria2c")


def _subprocess_run_aria2(
    url: str,
    part_path: Path,
    headers: dict[str, str],
) -> None:
    """Spawn ``aria2c`` to download *url* into *part_path*.

    Behaviour:
    - Uses :data:`_ARIA2_BASE_ARGS` plus ``-d``/``-o`` for the output path
      and one ``--header`` flag per header.
    - Wall-clock timeout is 3600s (one hour); larger files at typical
      saturated bandwidth (200+ Mbps) complete inside this window.
    - On non-zero exit, missing binary, or wall-clock timeout, raises
      :class:`~kinoforge.core.errors.KinoforgeError`.

    Args:
        url: Fully-qualified source URL.
        part_path: Target ``.part`` file.  ``part_path.parent`` must
            already exist; aria2c does NOT create directories.
        headers: HTTP request headers (e.g. ``{"Authorization": "Bearer ..."}``).

    Raises:
        KinoforgeError: On any aria2c failure path.
    """
    header_args: list[str] = []
    for key, value in headers.items():
        header_args.extend(["--header", f"{key}: {value}"])
    cmd = [
        "aria2c",
        *_ARIA2_BASE_ARGS,
        "-d",
        str(part_path.parent),
        "-o",
        part_path.name,
        *header_args,
        url,
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=3600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KinoforgeError(f"aria2c spawn failed for {url!r}: {exc}") from exc
    if result.returncode != 0:
        raise KinoforgeError(
            f"aria2c failed for {url!r} (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )


def download_one(
    artifact: Artifact,
    dest: Path,
    *,
    fetch: FetchCallable = _urllib_fetch,
    which_aria2: WhichCallable = _shutil_which_aria2,
    run_aria2: RunAriaCallable = _subprocess_run_aria2,
) -> Artifact:
    """Download *artifact* into *dest*, resuming from a ``.part`` file if present.

    Transport selection (added in Phase 29):
    - If ``which_aria2()`` returns a non-``None`` path, the aria2c
      subprocess is used to fetch the entire file in one shot
      (multi-connection per file, configurable via :data:`_ARIA2_BASE_ARGS`).
    - On aria2c success, the existing sha256 verify + atomic rename runs
      unchanged.
    - On aria2c failure, ``KinoforgeError`` is raised in this task; the
      silent stdlib fallback is added in T3.
    - When ``which_aria2()`` returns ``None`` (binary absent or test stub),
      the stdlib branch runs exactly as before.

    See module docstring for the rest of the skip / resume / verify
    behaviour.

    Args:
        artifact: Source descriptor; ``filename``, ``url``, and optionally
            ``sha256`` are used.
        dest: Directory into which the final file is written.
        fetch: Injectable stdlib HTTP callable (default: :func:`_urllib_fetch`).
        which_aria2: Detector callable returning the absolute path of
            ``aria2c`` or ``None`` (default: :func:`_shutil_which_aria2`).
        run_aria2: Subprocess invoker for aria2c (default:
            :func:`_subprocess_run_aria2`).

    Returns:
        A new :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set to
        the absolute path of the downloaded file.

    Raises:
        KinoforgeError: On sha256 mismatch, aria2c subprocess failure, or
            stdlib HTTP transport failure.
    """
    target_path = dest / artifact.filename
    part_path = Path(str(target_path) + ".part")

    # ------------------------------------------------------------------
    # Skip path (unchanged)
    # ------------------------------------------------------------------
    if target_path.exists():
        if artifact.sha256 is None:
            return replace(artifact, uri=str(target_path))
        if sha256_file(target_path) == artifact.sha256:
            return replace(artifact, uri=str(target_path))
        target_path.unlink()

    # ------------------------------------------------------------------
    # Transport branch
    # ------------------------------------------------------------------
    _aria_bin = which_aria2()
    if _aria_bin is not None:
        # Pre-delete any pre-existing .part so aria2c starts fresh.
        # Resume / Range stays a stdlib-branch responsibility.
        part_path.unlink(missing_ok=True)
        # headers: passthrough deferred to a future layer (spec Q6).
        run_aria2(artifact.url, part_path, {})
    else:
        # Stdlib branch (unchanged from pre-Phase-29).
        req_headers: dict[str, str] = {}
        if part_path.exists():
            n = part_path.stat().st_size
            req_headers["Range"] = f"bytes={n}-"

        _status, body, _resp_headers = fetch(artifact.url, req_headers)

        if part_path.exists():
            with part_path.open("ab") as fh:
                fh.write(body)
        else:
            with part_path.open("wb") as fh:
                fh.write(body)

    # ------------------------------------------------------------------
    # Verify checksum (unchanged)
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
    # Atomic promote (unchanged)
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
