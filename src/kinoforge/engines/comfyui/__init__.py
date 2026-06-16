"""ComfyUIEngine + ComfyUIBackend — real adapter for a locally-running ComfyUI server.

All I/O (subprocess, HTTP, filesystem, sleep) is routed through injected
callables so that tests can spy without any real side-effects.

Module-level self-registration puts the factory in the global registry under
the ``"comfyui"`` key so that ``registry.get_engine("comfyui")()`` works.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kinoforge.core import frames, registry

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
from kinoforge.core.assets import asset_bytes, find_asset
from kinoforge.core.errors import (
    AssetFetchError,
    FrameExtractionError,
    ProvisionFailed,
    ProvisionTimeout,
    ValidationError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
    RenderedProvision,
)
from kinoforge.engines.comfyui.nodes import clone_and_install

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Mapping from ``entry["target"]`` to ComfyUI model subdirectory.
TARGET_TO_SUBDIR: dict[str, str] = {
    "diffusion_models": "models/diffusion_models",
    "checkpoints": "models/checkpoints",
    "unet": "models/unet",
    "loras": "models/loras",
    "vae": "models/vae",
}

#: Maximum poll iterations in :meth:`ComfyUIBackend.result`.
#: Wan i2v at 81 frames + 20 steps + VAE decode on an RTX 3090 takes
#: ~3-5 minutes wall-clock (verified live attempt 10, pod bnclovbdaym3ld).
#: The previous default of 60 × 1s = 60s timed out long before the first
#: video frame was even decoded. 600 × _POLL_INTERVAL_S=3 = 30 min cap.
_MAX_POLL = 600

#: Seconds to sleep between :meth:`ComfyUIBackend.result` poll iterations.
_POLL_INTERVAL_S: float = 3.0

#: Default ComfyUI installation root (resolved at runtime, NOT at import time).
_DEFAULT_COMFYUI_ROOT = "ComfyUI"

#: Module-level logger; named so callers can filter via ``kinoforge.comfyui``.
_log = logging.getLogger("kinoforge.comfyui")

#: HTTP status codes treated as transient RunPod-proxy startup-window failures.
#: Phase 46 Task 7 root cause: live probe of pod xawdweboxapubz showed
#: ``/system_stats`` returning 200 (so wait_for_ready succeeds) while POST
#: ``/upload/image`` and parameterised paths like ``/history/{prompt_id}``
#: returned 404 for the first ~minute of pod lifetime, then 200 indefinitely
#: (50/50 sequential warm probes). 502/503/504 added defensively for normal
#: edge-layer transient failures.
_PROXY_TRANSIENT_CODES: frozenset[int] = frozenset({404, 502, 503, 504})

#: Backoff schedule for the ``submit()`` retry helper.  Caps total wait at
#: ~60 s — well under ``boot_timeout`` and large enough to cover the
#: startup-window race observed in live probes.
_SUBMIT_RETRY_BACKOFFS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 16.0)

#: Stock container image used when cfg does not specify one.
_DEFAULT_RUNPOD_IMAGE: str = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

#: Seconds to sleep between ready-check polls in :func:`ComfyUIEngine.wait_for_ready`.
_READY_POLL_INTERVAL_S: float = 5.0


# ---------------------------------------------------------------------------
# Module-level helpers for render_provision / wait_for_ready
# ---------------------------------------------------------------------------


def _extract_env_var(header_value: str) -> str | None:
    """Parse ``'Bearer $VAR'`` / ``'Bearer ${VAR}'`` and return the VAR name.

    Args:
        header_value: The value of an Authorization header, e.g.
            ``"Bearer $HF_TOKEN"`` or ``"Bearer ${CIVITAI_TOKEN}"``.

    Returns:
        The bare variable name (e.g. ``"HF_TOKEN"``), or ``None`` when the
        value doesn't match the expected pattern.
    """
    match = re.match(r"Bearer\s+\$\{?([A-Z_][A-Z0-9_]*)\}?", header_value)
    return match.group(1) if match else None


def _extract_port(launch_args: list[str]) -> str:
    """Return the value following ``'--port'`` in *launch_args*, default ``'8188'``.

    Args:
        launch_args: The ComfyUI launch argument list.

    Returns:
        Port string (e.g. ``"8188"``).
    """
    for i, arg in enumerate(launch_args[:-1]):
        if arg == "--port":
            return launch_args[i + 1]
    return "8188"


class _NullCredProvider(CredentialProvider):
    """Stub CredentialProvider used at render time; never returns real creds.

    ``render_provision`` calls ``source.resolve(ref, creds)`` to compute URLs +
    auth-header shapes (e.g. ``"Bearer $HF_TOKEN"``). The actual credential
    value is lifted onto ``spec.env`` by the orchestrator, NOT into the script.
    Returning ``"$KEY"`` here causes the source to produce the Authorization
    header with the literal env-var reference, which bash expands at runtime.
    """

    def get(self, key: str) -> str | None:
        """Return the literal env-var reference ``'$KEY'`` for any key.

        Args:
            key: Credential key (e.g. ``"HF_TOKEN"``).

        Returns:
            A string like ``"$HF_TOKEN"`` that bash will expand at runtime.
        """
        return f"${key}"


def _default_get_instance(_: str) -> Instance:
    """Stub seam — orchestrator must inject provider.get_instance for remote provision.

    Args:
        _: Instance ID (unused by the stub).

    Raises:
        NotImplementedError: Always — this seam must be wired by the orchestrator.
    """
    raise NotImplementedError(
        "ComfyUIEngine.get_instance seam not wired — "
        "orchestrator must inject provider.get_instance"
    )


# ---------------------------------------------------------------------------
# Real I/O helpers (default implementations)
# ---------------------------------------------------------------------------


def _subprocess_run(argv: list[str], cwd: str | None = None) -> None:
    """Run *argv* in a subprocess, raising ``subprocess.CalledProcessError`` on failure.

    Args:
        argv: Command and arguments.
        cwd: Working directory; ``None`` inherits the caller's cwd.
    """
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


_UA = "kinoforge-comfyui/0.1"


def _urllib_post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST *body* as JSON to *url* and return the decoded response.

    On 4xx/5xx the response body — which carries ComfyUI's structured
    validation errors for ``/prompt`` (e.g. node_errors lists missing
    inputs or type mismatches) — is read and re-raised as the
    ``HTTPError``'s message tail. Without this, ``urlopen`` discards
    the body and the caller sees only "HTTP Error 400: Bad Request"
    with no diagnostic content.

    Args:
        url: Endpoint URL.
        body: JSON-serialisable request body.

    Returns:
        Decoded JSON response as a Python dict.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return dict(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:4000]
        raise urllib.error.HTTPError(
            exc.url,
            exc.code,
            f"{exc.reason} -- body: {err_body}",
            exc.headers,
            None,
        ) from exc


def _urllib_get_json(url: str) -> dict[str, Any]:
    """GET *url* and return the decoded JSON response.

    Sets ``User-Agent: kinoforge-comfyui/0.1`` so requests survive
    edge-layer filtering. RunPod's proxy rejects the stdlib default
    ``Python-urllib/3.x`` with HTTP 403 — that masquerades as a benign
    "ComfyUI not ready yet" inside ``wait_for_ready``'s broad except,
    causing the ready check to spin until ``boot_timeout_s``. Live
    verification 2026-06-03 (diagnostic bfqt00tfd): same URL returns
    200 with the kinoforge UA and the proxy URL returns 1041 bytes of
    /system_stats JSON.

    Args:
        url: Endpoint URL.

    Returns:
        Decoded JSON response as a Python dict.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _path_exists(path: str) -> bool:
    """Return ``True`` when *path* exists on the filesystem.

    Args:
        path: Filesystem path to test.

    Returns:
        ``True`` if the path exists.
    """
    return os.path.exists(path)


def _shutil_move(src: str, dst_dir: str) -> None:
    """Move *src* into directory *dst_dir*, creating it if necessary.

    Args:
        src: Source file path.
        dst_dir: Destination directory.
    """
    os.makedirs(dst_dir, exist_ok=True)
    shutil.move(src, dst_dir)


def _urllib_get_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body as bytes.

    Sets the same UA as :func:`_urllib_get_json` so byte-fetches (e.g.
    artifact URLs proxied via RunPod) survive edge-layer filtering.

    Args:
        url: Endpoint URL.

    Returns:
        Response body as bytes.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return bytes(resp.read())


def _escape_quoted_string(s: str) -> str:
    r"""Escape *s* for use inside an RFC 2183 quoted-string header value.

    Doubles backslashes and quotes (``\`` -> ``\\``, ``"`` -> ``\"``)
    so the value can be safely interpolated into a Content-Disposition
    ``filename="..."`` parameter. Rejects values containing a CR or LF
    so an attacker-controlled filename cannot inject extra headers into
    the multipart envelope.

    Args:
        s: The raw string to escape.

    Returns:
        The escaped string ready for interpolation.

    Raises:
        ValueError: ``s`` contains a CR or LF character.
    """
    if "\r" in s or "\n" in s:
        raise ValueError(f"filename contains newline: {s!r}")
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _urllib_post_multipart(
    url: str,
    *,
    field_name: str,
    filename: str,
    content: bytes,
) -> str:
    """Default multipart POST helper for ``/upload/image``.

    Sends a single-part multipart form with the given field name and
    filename, content type ``application/octet-stream``. Reads the JSON
    response and returns ``response["name"]`` per the ComfyUI server
    contract.

    The multipart boundary is generated per call via
    :func:`secrets.token_hex` so asset bytes that happen to contain a
    static prefix can never terminate the body prematurely. The
    ``"----kinoforge-"`` prefix is retained so the boundary remains
    recognisable in packet captures.

    The supplied *filename* is escaped per RFC 2183 before being
    interpolated into the Content-Disposition header; a filename
    containing CR or LF raises :class:`ValueError` (header injection
    guard).

    Args:
        url: Upload endpoint URL.
        field_name: Form field name (ComfyUI expects ``"image"``).
        filename: Filename hint passed in the Content-Disposition header.
        content: Raw bytes to upload.

    Returns:
        The server-side filename string from the response JSON.

    Raises:
        ValueError: ``filename`` contains a CR or LF character.
        AssetFetchError: The server returned a body that is not valid
            JSON, or a body that lacks the required ``"name"`` field.
        urllib.error.URLError: Transport failure (wrapped by the caller
            as :class:`~kinoforge.core.errors.AssetFetchError`).
        OSError: Lower-level socket failure (wrapped likewise).
    """
    safe_filename = _escape_quoted_string(filename)
    boundary = f"----kinoforge-{secrets.token_hex(16)}"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{safe_filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        # RunPod's edge proxy rejects requests with the stdlib default
        # Python-urllib/<ver> UA with HTTP 403 (see commit 8058dc2 for
        # the same fix on _urllib_post_json / _urllib_get_json). Live
        # smoke verified 2026-06-03 (pod qiw1joekrijjay): /upload/image
        # returns 403 without this header, 200 with it.
        "User-Agent": _UA,
    }
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AssetFetchError(
            f"ComfyUI /upload/image returned invalid JSON: {e}"
        ) from e
    try:
        name = payload["name"]
    except (KeyError, TypeError) as e:
        raise AssetFetchError(
            f"ComfyUI /upload/image response missing 'name' field: {payload!r}"
        ) from e
    return str(name)


# ---------------------------------------------------------------------------
# Default probe profile
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="comfyui",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def _extract_poll_fields(
    envelope: dict[str, Any],
    job_id: str | None = None,
) -> tuple[str, int | None, str | None]:
    """Pull (status, queue_pos, exec_node) from a ComfyUI history envelope.

    ComfyUI ships two envelope shapes:

    * **Real** ``/history/{prompt_id}``: per-job dict nested under the
      ``prompt_id`` key (``{prompt_id: {"status": {...}, "outputs": {...}}}``).
      Returns ``{}`` while the job is still queued or executing — only
      populates after completion (success OR error).
    * **Flat** (test fixtures, some legacy capture tools): ``status``
      and ``outputs`` at the top level.

    Phase 51 made parser dual-shape — Phase 50 only handled flat envelopes,
    which caused real production runs to report ``status="unknown"`` for
    the entire job and gated the ``/queue`` probe out of firing.

    ``queue_pos`` is always returned as ``None`` here — it is populated by
    a separate ``/queue`` probe in the poll loop when ``status`` is
    ``"queued"`` OR ``"unknown"`` (the latter is the steady state during
    execution, when ``/history`` is empty).

    Args:
        envelope: Decoded JSON body from ``/history/{prompt_id}``.
        job_id: Optional prompt_id. When supplied and the top-level
            ``status`` key is absent, the parser descends into
            ``envelope[job_id]["status"]`` (real production shape).
            When ``None``, only the flat shape is read — preserves
            legacy callers + the empty-envelope ``"unknown"`` fallback.

    Returns:
        ``(status_str, queue_pos, exec_node)`` triple.
    """
    if not isinstance(envelope, dict):
        return ("unknown", None, None)
    status_block = envelope.get("status")
    if not isinstance(status_block, dict):
        if job_id is not None:
            nested = envelope.get(job_id, {})
            if isinstance(nested, dict):
                status_block = nested.get("status", {})
            else:
                status_block = {}
        else:
            status_block = {}
    if not isinstance(status_block, dict):
        status_block = {}
    status = status_block.get("status_str", "unknown")
    exec_info = status_block.get("exec_info", {})
    if not isinstance(exec_info, dict):
        exec_info = {}
    exec_node = exec_info.get("current_node")
    return str(status), None, exec_node


def _extract_queue_position(
    envelope: dict[str, Any],
    job_id: str,
) -> int | None:
    """Best-effort lookup of *job_id* position in ComfyUI's ``/queue`` envelope.

    Args:
        envelope: Decoded JSON body from ``/queue``.
        job_id: The ``prompt_id`` whose position should be located.

    Returns:
        Zero-based position (running entries first, then pending), or
        ``None`` when *job_id* is not present.
    """
    if not isinstance(envelope, dict):
        return None
    running = envelope.get("queue_running", []) or []
    pending = envelope.get("queue_pending", []) or []
    for idx, entry in enumerate(running):
        if _entry_matches(entry, job_id):
            return idx
    for idx, entry in enumerate(pending):
        if _entry_matches(entry, job_id):
            return len(running) + idx
    return None


def _entry_matches(entry: object, job_id: str) -> bool:
    """Return True when a ``/queue`` entry refers to *job_id*.

    ComfyUI queue entries are typically ``[number, prompt_id, ...]``.

    Args:
        entry: A single entry from ``queue_running`` or ``queue_pending``.
        job_id: The ``prompt_id`` to match against.

    Returns:
        ``True`` when ``entry[1] == job_id``.
    """
    if isinstance(entry, list) and len(entry) >= 2:
        return bool(entry[1] == job_id)
    return False


def _retry_proxy_call[T](
    label: str,
    url: str,
    fn: Callable[[], T],
    sleep: Callable[[float], None],
) -> T:
    """Run *fn* with bounded retry on RunPod-proxy transient HTTP codes.

    Each transient failure (codes in :data:`_PROXY_TRANSIENT_CODES`) logs a
    WARNING and sleeps per :data:`_SUBMIT_RETRY_BACKOFFS`. Non-transient
    HTTPError and non-HTTPError exceptions propagate immediately. After the
    backoff schedule exhausts, the final transient HTTPError is re-raised.

    Args:
        label: Short tag for log lines (e.g. ``"submit.upload"``).
        url: URL passed to *fn*; included in WARNING messages.
        fn: Zero-arg callable performing the HTTP request.
        sleep: Injected sleep seam; receives backoff seconds.

    Returns:
        The successful return value of *fn*.

    Raises:
        urllib.error.HTTPError: The last transient HTTPError after the
            backoff schedule exhausts, or a non-transient HTTPError on
            any attempt.
    """
    last_exc: urllib.error.HTTPError | None = None
    attempts = 1 + len(_SUBMIT_RETRY_BACKOFFS)
    for attempt_idx, delay in enumerate((0.0,) + _SUBMIT_RETRY_BACKOFFS):
        if delay > 0:
            sleep(delay)
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code not in _PROXY_TRANSIENT_CODES:
                raise
            _log.warning(
                "[comfyui.%s] transient HTTPError url=%s code=%d "
                "attempt=%d/%d next_backoff=%.1fs",
                label,
                url,
                exc.code,
                attempt_idx + 1,
                attempts,
                _SUBMIT_RETRY_BACKOFFS[attempt_idx]
                if attempt_idx < len(_SUBMIT_RETRY_BACKOFFS)
                else 0.0,
            )
            last_exc = exc
    # last_exc is always set after the first transient-codes branch above,
    # but mypy needs the explicit guard. A non-transient path returns or
    # raises directly inside the loop.
    if last_exc is None:  # pragma: no cover — unreachable
        raise RuntimeError(
            "_retry_proxy_call exited loop without recording an HTTPError"
        )
    raise last_exc


class ComfyUIBackend(GenerationBackend):
    """Live backend that communicates with a running ComfyUI server.

    All HTTP traffic goes through injected callables so tests can spy
    without starting a real server.

    Attributes:
        _http_post: Callable ``(url, body) -> dict`` for POST requests.
        _http_get: Callable ``(url) -> dict`` for GET requests.
        _base_url: Base URL of the ComfyUI server (no trailing slash).
        _probe: The ``ModelProfile`` returned by capability queries.
        _sleep: Injectable sleep function (default: ``time.sleep``).
    """

    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        base_url: str,
        probe: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        http_post_file: Callable[..., str] = _urllib_post_multipart,
        poll_interval_s: float = _POLL_INTERVAL_S,
        poll_timeout_s: float = 1800.0,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the ComfyUI server, e.g.
                ``"http://localhost:8188"``.  No trailing slash.
            probe: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
                Retained for the ``_retry_proxy_call`` backoff path
                (Phase 47 startup-window 404 retries); the inter-poll
                wait in :meth:`result` uses ``cancel_token.wait`` so
                cooperative cancellation is honored promptly.
            http_get_bytes: Byte fetcher used by ``submit`` to resolve
                ``http``/``https`` asset URIs (``file://`` is read by
                :func:`kinoforge.core.assets.asset_bytes` via stdlib
                :class:`pathlib.Path`).
            http_post_file: Multipart POST callable
                ``(url, *, field_name, filename, content) -> str``
                returning the server-side filename. Defaults to the
                stdlib urllib multipart helper
                :func:`_urllib_post_multipart`.
            poll_interval_s: Wait between :meth:`result` poll ticks
                (seconds). Default :data:`_POLL_INTERVAL_S` matches the
                pre-cancel-token behavior.
            poll_timeout_s: Hard upper bound (seconds) on a single
                :meth:`result` poll wait. Exceeding it raises
                :class:`TimeoutError` whose message carries
                ``last_status`` + ``exec_node`` for self-diagnosis.
                Default 1800 s (30 min) covers Wan 14B on A5000-class
                GPUs (~25-40 min observed) while bounding pathological
                hangs. Phase 50's original 600 s default killed a
                healthy run on pod ``2fhv2v3cccs98d`` at 602.8 s while
                the GPU was at 100% — see Phase 51 in ``PROGRESS.md``.
                Operators override per-config via
                ``engine.comfyui.poll_timeout_s`` for slower setups.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe
        self._sleep = sleep
        self._http_get_bytes = http_get_bytes
        self._http_post_file = http_post_file
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s

    def capabilities(self) -> ModelProfile:
        """Return the injected probe profile.

        Returns:
            The ``ModelProfile`` supplied at construction time.
        """
        return self._probe

    def inspect_capabilities(self) -> ModelProfile:
        """Return the injected probe profile unchanged.

        Returns:
            The ``ModelProfile`` supplied at construction time.
        """
        return self._probe

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        """POST the merged workflow graph to ``/prompt`` and return the prompt ID.

        The workflow graph is taken from ``job.spec["graph"]``.  The keys in
        ``job.spec["node_overrides"]`` are deep-merged onto the graph so that
        per-job parameter overrides take effect while unspecified nodes remain
        unchanged.

        Layer F: for each role in ``job.spec.get("asset_node_ids", {})``,
        find the matching asset on ``segments[0]`` via
        :func:`~kinoforge.core.assets.find_asset`, resolve its URI to bytes
        via :func:`~kinoforge.core.assets.asset_bytes`, upload via
        ``self._http_post_file`` to ``/upload/image``, and patch
        ``node_overrides[<node_id>]["inputs"]["image"]`` with the returned
        server-side filename. The existing graph + override merge then runs
        unchanged so pre-Layer-F templates keep working.

        Args:
            job: The ``GenerationJob`` whose ``spec`` contains ``"graph"``
                and ``"node_overrides"`` (and optionally ``"asset_node_ids"``).
            cancel_token: Optional :class:`CancelToken`. Checked once
                at the top of submit so an operator Ctrl-C between
                ``deploy_session`` setup and the first ``/prompt`` POST
                raises :class:`Cancelled` instead of charging an asset
                upload + prompt enqueue. In-flight ``/upload/image``
                retries are not interruptible (each call is bounded by
                ``_SUBMIT_RETRY_BACKOFFS`` and runs in the calling
                thread); :meth:`result` is where cooperative cancel
                buys the operator their shell back.

        Returns:
            The ``prompt_id`` string from the ComfyUI server response.

        Raises:
            Cancelled: ``cancel_token`` was set when ``submit`` was
                entered.
            AssetFetchError: Asset URI fetch failed (raised from
                :func:`~kinoforge.core.assets.asset_bytes`) or upload to
                ``/upload/image`` failed.
        """
        from kinoforge.core.cancel import _NULL_TOKEN

        (cancel_token or _NULL_TOKEN).raise_if_set()
        graph: dict[str, Any] = copy.deepcopy(job.spec.get("graph", {}))
        overrides: dict[str, Any] = copy.deepcopy(job.spec.get("node_overrides", {}))
        asset_node_ids: dict[str, str] = job.spec.get("asset_node_ids", {})

        for role, node_id in asset_node_ids.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            payload = asset_bytes(
                asset.ref.uri,
                http_get_bytes=self._http_get_bytes,
            )
            upload_url = f"{self._base_url}/upload/image"
            upload_filename = asset.ref.filename or f"{role}.png"

            def _upload(
                _url: str = upload_url,
                _name: str = upload_filename,
                _bytes: bytes = payload,
            ) -> str:
                return self._http_post_file(
                    _url,
                    field_name="image",
                    filename=_name,
                    content=_bytes,
                )

            try:
                uploaded_name = _retry_proxy_call(
                    "submit.upload",
                    upload_url,
                    _upload,
                    self._sleep,
                )
            except (urllib.error.URLError, OSError) as e:
                raise AssetFetchError(
                    f"ComfyUI /upload/image failed for role {role!r}: {e}"
                ) from e
            node_patch = overrides.setdefault(str(node_id), {})
            inputs = node_patch.setdefault("inputs", {})
            inputs["image"] = uploaded_name

        # Layer J: route the user prompt into the configured text-encoder
        # nodes. Reads ``spec["prompt_node_ids"]`` — mirrors
        # ``asset_node_ids`` — and writes via ``setdefault`` so an explicit
        # ``node_overrides[node_id]["inputs"][<field>]`` from spec wins.
        #
        # ``spec["prompt_input_field"]`` selects the per-node input key
        # the prompt lands on. Default ``"text"`` matches ComfyUI's stock
        # ``CLIPTextEncode``; the kijai Wan workflow's
        # ``WanVideoTextEncode`` uses ``"positive_prompt"``. Live
        # verification 2026-06-03 (HEAD 36820f1): first green MP4
        # rendered the kijai-baked-in default prompt ("an old man
        # stroking his beard thoughtfully") because the user's prompt
        # landed on a non-existent ``text`` field while
        # ``positive_prompt`` retained kijai's hard-coded value.
        prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
        if prompt_node_ids:
            from kinoforge.core.prompt_routing import resolve_prompt

            prompt = resolve_prompt(job)
            prompt_input_field: str = job.spec.get("prompt_input_field", "text")
            if prompt is not None:
                for _role, node_id in prompt_node_ids.items():
                    node_patch = overrides.setdefault(str(node_id), {})
                    inputs = node_patch.setdefault("inputs", {})
                    inputs.setdefault(prompt_input_field, prompt)

        # Deep-merge overrides into the graph at the node level.
        for node_id, node_patch in overrides.items():
            if node_id in graph:
                _deep_merge(graph[node_id], node_patch)
            else:
                graph[node_id] = copy.deepcopy(node_patch)
        url = f"{self._base_url}/prompt"
        response = _retry_proxy_call(
            "submit.prompt",
            url,
            lambda: self._http_post(url, {"prompt": graph}),
            self._sleep,
        )
        return str(response["prompt_id"])

    def result(
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        """Poll ``/history/{job_id}`` until outputs are available.

        Honors *cancel_token* both at the top of every iteration (cheap
        ``raise_if_set`` check before any I/O) and across the inter-poll
        wait (``cancel_token.wait`` in place of ``time.sleep`` so a
        Ctrl-C lands within ~``poll_interval_s``). Every iteration emits
        one structured ``INFO`` log line of the form::

            comfyui poll job=<id> elapsed=<s>s status=<str>
                          queue_pos=<n|None> exec_node=<name|None>

        so the operator can self-diagnose where a stall is parked.

        Bounded by ``poll_timeout_s`` (see constructor) — exceeding it
        raises :class:`TimeoutError` whose message contains the last
        observed ``status`` and ``exec_node`` for triage.

        Args:
            job_id: The ``prompt_id`` returned by a prior ``submit`` call.
            cancel_token: Optional :class:`CancelToken`. When ``None``,
                a sentinel that is never set is used so existing callers
                that pass no token see unchanged behavior. When set
                mid-poll, raises :class:`Cancelled` promptly.

        Returns:
            An ``Artifact`` whose ``filename`` is the first filename from the
            first node's file list, and whose ``meta`` contains
            ``{"prompt_id": job_id}``.

        Raises:
            Cancelled: ``cancel_token`` was set.
            TimeoutError: ``poll_timeout_s`` elapsed without outputs.
        """
        from kinoforge.core.cancel import _NULL_TOKEN

        token = cancel_token if cancel_token is not None else _NULL_TOKEN

        # Token-aware wait: when a real token is supplied, use Event.wait
        # so a mid-wait set() returns promptly. When the caller passes no
        # token (legacy callers + the existing unit-test suite that
        # injects ``sleep=lambda s: None`` to stop the loop from blocking
        # on real time), fall back to the injected sleep so test ticks
        # stay instant.
        def _interpoll_wait(seconds: float) -> bool:
            if cancel_token is None:
                self._sleep(seconds)
                return False
            return token.wait(seconds)

        url = f"{self._base_url}/history/{job_id}"
        queue_url = f"{self._base_url}/queue"
        start = time.monotonic()
        last_status: str = "unknown"
        queue_pos: int | None = None
        exec_node: str | None = None
        last_transient: urllib.error.HTTPError | None = None
        poll_idx = 0
        while True:
            token.raise_if_set()
            elapsed = time.monotonic() - start
            # Belt-and-braces: preserve the legacy _MAX_POLL iteration cap
            # so existing tests that inject ``sleep=lambda s: None`` (which
            # makes the wall-clock check trivially false until ~600s of
            # real wall-clock elapses) still terminate. Production calls
            # land first on the poll_timeout_s check below.
            if poll_idx >= _MAX_POLL or elapsed > self._poll_timeout_s:
                # If we have an unresolved transient HTTPError, re-raise
                # it in preference to a TimeoutError so the pre-existing
                # `test_result_raises_after_persistent_404` contract
                # (and the Phase 47 production-diagnostic message) is
                # preserved.
                if last_transient is not None:
                    raise last_transient
                raise TimeoutError(
                    f"comfyui poll timed out after {elapsed:.1f}s "
                    f"(job={job_id}, last_status={last_status!r}, "
                    f"exec_node={exec_node!r})"
                )
            try:
                # Preserve Phase 47 _retry_proxy_call wrapping so a
                # RunPod proxy startup-window 404 keeps polling rather
                # than raising. Token + timeout checks live OUTSIDE the
                # retry wrapper (above) so cancellation cannot be
                # swallowed by a transient-retry storm.
                data = _retry_proxy_call(
                    "result.history",
                    url,
                    lambda: self._http_get(url),
                    self._sleep,
                )
            except urllib.error.HTTPError as exc:
                # Non-transient HTTPError: log + propagate (today's
                # behavior). Transient codes are absorbed by
                # _retry_proxy_call itself; if it exhausted its backoff
                # schedule it re-raises the final transient — record
                # and continue polling within poll_timeout_s.
                if exc.code in _PROXY_TRANSIENT_CODES:
                    _log.warning(
                        "[comfyui.result] transient HTTPError exhausted "
                        "elapsed=%.1fs url=/history/%s code=%d reason=%s",
                        elapsed,
                        job_id,
                        exc.code,
                        str(exc.reason)[:200],
                    )
                    last_transient = exc
                    poll_idx += 1
                    if _interpoll_wait(self._poll_interval_s):
                        token.raise_if_set()
                    continue
                _log.warning(
                    "[comfyui.result] HTTPError elapsed=%.1fs url=%s code=%d reason=%s",
                    elapsed,
                    url,
                    exc.code,
                    str(exc.reason)[:200],
                )
                raise
            last_status, queue_pos, exec_node = _extract_poll_fields(data, job_id)
            # Phase 51: also probe /queue when status is "unknown". Real
            # ComfyUI returns ``{}`` from /history while the job is queued
            # or executing — without the "unknown" arm here, queue_pos
            # stayed None for the entire run, leaving the operator unable
            # to distinguish a healthy long sampler tick from a job
            # ComfyUI has lost (server restarted, prompt_id forgotten).
            if last_status in ("queued", "unknown"):
                try:
                    queue_envelope = self._http_get(queue_url)
                    queue_pos = _extract_queue_position(queue_envelope, job_id)
                except Exception:  # noqa: BLE001
                    queue_pos = None
            _log.info(
                "comfyui poll job=%s elapsed=%.1fs status=%s queue_pos=%s exec_node=%s",
                job_id,
                elapsed,
                last_status,
                queue_pos,
                exec_node,
            )
            # Envelope shape varies: ComfyUI's real ``/history/{id}`` is
            # keyed by ``prompt_id`` at the top with ``{"outputs": ...}``
            # one level down; some test fixtures (and the timeout/log
            # tests in this commit) put ``outputs`` at the top. Tolerate
            # both. ``last_transient`` is silenced once we see real data.
            outputs = data.get("outputs")
            if not outputs:
                entry = data.get(job_id, {})
                if isinstance(entry, dict):
                    outputs = entry.get("outputs")
            if outputs:
                filename = _first_filename(outputs)
                encoded_fn = urllib.parse.quote(filename, safe="")
                view_url = f"{self._base_url}/view?filename={encoded_fn}&type=output"
                return Artifact(
                    filename=filename,
                    url=view_url,
                    meta={"prompt_id": job_id},
                )
            if last_transient is not None:
                last_transient = None
            poll_idx += 1
            # Inter-poll wait. With a real token, threading.Event.wait
            # returns True promptly on set(); without one, the injected
            # sleep keeps the legacy test contract (sleep=lambda s: None
            # → instant tick).
            if _interpoll_wait(self._poll_interval_s):
                token.raise_if_set()

    def endpoints(self) -> dict[str, str]:
        """Return the ComfyUI endpoint map.

        Returns:
            A dict with ``"generate"`` (``/prompt``) and ``"history"``
            (``/history``) keys.
        """
        return {
            "generate": f"{self._base_url}/prompt",
            "history": f"{self._base_url}/history",
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ComfyUIEngine(GenerationEngine):
    """Generation engine adapter for a locally-provisioned ComfyUI server.

    Provisions the server by:

    1. Cloning git custom-node repositories.
    2. Installing per-node ``requirements.txt`` when present.
    3. Routing model files into the appropriate ComfyUI subdirectories.
    4. Launching ComfyUI with the configured arguments.

    All I/O is routed through injected callables (``run_cmd``, ``http_post``,
    ``http_get``, ``file_exists``, ``route_file``, ``sleep``) so that tests
    can spy without real side-effects.

    Class attributes:
        name: Registry key ``"comfyui"``.
        requires_compute: ``True`` — a GPU instance is needed.
        requires_local_weights: ``False`` — weights are downloaded ON THE POD
            by the Layer Q :meth:`render_provision` curl bootstrap, not on
            the controller. The provisioner's local-download step would
            otherwise pull ~20-30 GB of Wan/diffusion weights through the
            controller container before delegating to :meth:`provision`,
            doubling wall-clock cost and risking OOM on lightweight
            containers (observed in capture-tool runs; see
            ``tools/capture_object_info.py:55``). Engines that genuinely
            need local-bytes-before-pod-upload should override to ``True``.
            Tracked architecturally as B20 (``WeightProvisioning`` enum) in
            PROGRESS.md.
    """

    name: str = "comfyui"
    requires_compute: bool = True
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        run_cmd: Callable[[list[str], str | None], None] = _subprocess_run,
        file_exists: Callable[[str], bool] = _path_exists,
        route_file: Callable[[str, str], None] = _shutil_move,
        http_post: Callable[[str, Any], dict[str, Any]] = _urllib_post_json,
        http_get: Callable[[str], dict[str, Any]] = _urllib_get_json,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        http_post_file: Callable[..., str] = _urllib_post_multipart,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_PROBE,
        flags_table: dict[str, dict[str, bool]] | None = None,
        comfyui_root: str = _DEFAULT_COMFYUI_ROOT,
        # NEW — Layer Q: injected by orchestrator for remote provision
        get_instance: Callable[[str], Instance] | None = None,
    ) -> None:
        """Initialise the engine with all I/O seams as injectable callables.

        Args:
            run_cmd: Callable ``(argv, cwd) -> None`` for subprocess calls.
            file_exists: Callable ``(path) -> bool`` for path existence checks.
            route_file: Callable ``(src, dst_dir) -> None`` to move/link model
                files into place.
            http_post: Callable ``(url, json_body) -> dict`` for HTTP POST.
            http_get: Callable ``(url) -> dict`` for HTTP GET.
            http_get_bytes: Callable ``(url) -> bytes`` for raw-byte HTTP GET
                (used by extract_last_frame to fetch the rendered video and
                by Layer F asset wiring to resolve http(s) asset URIs).
            http_post_file: Multipart POST callable
                ``(url, *, field_name, filename, content) -> str`` used by
                Layer F asset wiring to upload bytes to
                ``/upload/image`` and obtain the server-side filename.
            ffmpeg_run: Subprocess seam ``(argv, stdin) -> stdout`` used by
                extract_last_frame to decode the last frame via ffmpeg.
            sleep: Sleep callable used between polling iterations in
                :meth:`~ComfyUIBackend.result`.
            probe_profile: ``ModelProfile`` returned by backend capability
                queries.
            flags_table: Optional mapping from
                :meth:`~kinoforge.core.interfaces.CapabilityKey.derive` hex
                strings to ``dict[str, bool]`` strategy-flag dicts.
            comfyui_root: Absolute or relative path to the ComfyUI installation
                directory.  Defaults to ``"ComfyUI"`` (relative to cwd).
            get_instance: Callable ``(instance_id) -> Instance`` injected by the
                orchestrator for remote provision status checks. When ``None``,
                defaults to :func:`_default_get_instance` which raises
                ``NotImplementedError`` to ensure the orchestrator wires it.
        """
        self._run_cmd = run_cmd
        self._file_exists = file_exists
        self._route_file = route_file
        self._http_post = http_post
        self._http_get = http_get
        self._http_get_bytes = http_get_bytes
        self._http_post_file = http_post_file
        self._ffmpeg_run = ffmpeg_run
        self._sleep = sleep
        self._probe = probe_profile
        self._flags_table: dict[str, dict[str, bool]] = flags_table or {}
        self._comfyui_root = comfyui_root
        self._get_instance: Callable[[str], Instance] = (
            get_instance if get_instance is not None else _default_get_instance
        )

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, Any],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """Clone nodes, install requirements, route models, launch ComfyUI (local).

        For local instances (``instance is None`` or
        ``instance.provider == "local"``), runs the original local code path:

        1. Clone each ``cfg["engine"]["comfyui"]["custom_nodes"][i]["git"]``
           and install its ``requirements.txt`` when present.
        2. Route each entry in ``cfg["models"]`` into the appropriate
           ComfyUI model subdirectory.
        3. Launch ComfyUI with ``cfg["engine"]["comfyui"]["launch_args"]``.

        For remote instances (any other provider), the boot script was already
        executed via the provider's boot path; this method simply polls
        :meth:`wait_for_ready` until ComfyUI reports ready.

        Args:
            instance: The compute instance. ``None`` or ``provider == "local"``
                triggers the local code path; any other provider triggers the
                remote polling path.
            cfg: Runtime configuration dict.
            cancel_token: C29 cooperative cancellation. Forwarded into the
                remote-path :meth:`wait_for_ready` call so a boot-phase reap
                raises ``Cancelled`` cleanly. ``None`` preserves pre-C29
                behaviour.
        """
        if instance is None or instance.provider == "local":
            # ---- local path (unchanged from pre-Layer-Q) ----
            engine_block = cfg.get("engine", {})
            comfyui_cfg: dict[str, Any] = (
                engine_block.get("comfyui", {})
                if isinstance(engine_block, dict)
                else {}
            )
            custom_nodes: list[dict[str, Any]] = comfyui_cfg.get("custom_nodes", [])
            launch_args: list[str] = comfyui_cfg.get("launch_args", [])
            models_raw = cfg.get("models", [])
            models: list[dict[str, Any]] = (
                list(models_raw) if isinstance(models_raw, list) else []
            )

            # Step 1 — clone nodes and install requirements.
            clone_and_install(
                node_entries=custom_nodes,
                comfyui_root=self._comfyui_root,
                run_cmd=self._run_cmd,
                file_exists=self._file_exists,
            )

            # Step 2 — route model files.
            for entry in models:
                src: str = entry["src"]
                target: str = entry["target"]
                subdir = TARGET_TO_SUBDIR.get(target, f"models/{target}")
                dst_dir = os.path.join(self._comfyui_root, subdir)
                self._route_file(src, dst_dir)

            # Step 3 — launch ComfyUI.
            self._run_cmd(
                ["python", "main.py"] + list(launch_args),
                self._comfyui_root,
            )
            return

        # ---- remote path: script ran via provider boot; just wait for ready ----
        lifecycle_block = cfg.get("lifecycle", {})
        boot_timeout_s = float(
            lifecycle_block.get("boot_timeout_s", 900.0)
            if isinstance(lifecycle_block, dict)
            else 900.0
        )
        self.wait_for_ready(
            instance,
            http_get=self._http_get,
            sleep=self._sleep,
            get_instance=self._get_instance,
            timeout_s=boot_timeout_s,
            cancel_token=cancel_token,
        )

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Render a self-contained first-boot bootstrap script for a remote pod.

        The rendered script is idempotent on warm pods: each clone/download is
        guarded by ``[ ! -d ... ]`` / ``[ ! -f ... ]``. Credentials are
        referenced via ``$VAR`` and lifted onto ``spec.env`` by the
        orchestrator; the script string never contains plaintext token values.

        Note:
            Sources that require live HTTP for ``resolve()`` (e.g. ``CivitAISource``)
            WILL hit the network at render time. Pass real credentials via the
            configured CredentialProvider before calling. Pure-rendering sources
            (``HuggingFaceSource``) work offline.

        Args:
            cfg: Runtime configuration dict, same shape as ``provision``.

        Returns:
            A :class:`RenderedProvision` ready for orchestrator wiring.
        """
        cfg_dict: dict[str, Any] = {k: v for k, v in cfg.items()}
        engine_block = cfg_dict.get("engine", {})
        comfyui_cfg: dict[str, Any] = (
            engine_block.get("comfyui", {}) if isinstance(engine_block, dict) else {}
        )
        repo: str = comfyui_cfg.get("repo", "https://github.com/comfyanonymous/ComfyUI")
        branch: str = comfyui_cfg.get("branch", "master")
        custom_nodes: list[dict[str, Any]] = list(comfyui_cfg.get("custom_nodes", []))
        launch_args_raw: list[str] = list(comfyui_cfg.get("launch_args", []))
        if not launch_args_raw:
            launch_args_raw = ["--listen", "0.0.0.0", "--port", "8188"]  # noqa: S104

        image: str = comfyui_cfg.get("image", _DEFAULT_RUNPOD_IMAGE)
        models_raw: list[dict[str, Any]] = list(cfg_dict.get("models", []))
        # C28 B2: any image whose name starts with kinoforge/wan-comfyui:
        # is a pre-baked container with ComfyUI + all custom-node clones +
        # all pip installs already laid down at build time. Skip those
        # bootstrap steps; the image's CMD layer + the selfterm bootstrap
        # + the model-download loop + `exec python main.py` still emit.
        slim_mode: bool = image.startswith("kinoforge/wan-comfyui:")

        # C28 C1: pure-bash helper for resilient model downloads. 3-attempt
        # retry, exponential backoff (5/10/15 s), partial-file cleanup
        # between attempts, optional sha256 verify, optional HF_TOKEN
        # bearer header. Defined unconditionally so EVERY download (model
        # or future asset) gets the retry semantics — fixes the H3 curl
        # flake without requiring image rebuilds.
        kinoforge_download_helper: list[str] = [
            "_kinoforge_download() {",
            "  local url=$1; local out=$2",
            "  local expected_sha=${3:-}",
            "  local token_env=${4:-}",
            '  local token_val=""',
            '  if [ -n "$token_env" ] && [ -n "${!token_env:-}" ]; then',
            '    token_val="${!token_env}"',
            "  fi",
            "  local out_dir out_base",
            '  out_dir=$(dirname "$out")',
            '  out_base=$(basename "$out")',
            "  local attempt",
            "  for attempt in 1 2 3; do",
            "    if command -v aria2c >/dev/null 2>&1; then",
            "      local ar_args=(-x16 -s16 --allow-overwrite=true "
            "--continue=true --console-log-level=warn "
            "--summary-interval=30)",
            '      [ -n "$token_val" ] && ar_args+=('
            '--header="Authorization: Bearer $token_val")',
            '      [ -n "$expected_sha" ] && ar_args+=('
            "--checksum=sha-256=$expected_sha)",
            '      if aria2c "${ar_args[@]}" -d "$out_dir" -o "$out_base" "$url"; then',
            "        return 0",
            "      fi",
            "    else",
            '      rm -f "${out}.partial"',
            "      local cu_args=()",
            '      [ -n "$token_val" ] && cu_args+=('
            '-H "Authorization: Bearer $token_val")',
            '      if curl -L --fail --retry 0 -C - "${cu_args[@]}" '
            '"$url" -o "${out}.partial"; then',
            '        if [ -n "$expected_sha" ]; then',
            "          local actual",
            "          actual=$(sha256sum \"${out}.partial\" | awk '{print $1}')",
            '          if [ "$actual" != "$expected_sha" ]; then',
            "            sleep $((5 * attempt))",
            "            continue",
            "          fi",
            "        fi",
            '        mv "${out}.partial" "$out"',
            "        return 0",
            "      fi",
            "    fi",
            "    sleep $((5 * attempt))",
            "  done",
            "  return 1",
            "}",
        ]

        # C28 A2: diagnostic_mode prepends an EXIT trap that captures the boot
        # log + system snapshots and uploads to S3 on failure. Pure-additive —
        # when False/absent, the rendered script is byte-identical to the
        # pre-C28 baseline. AWS creds are NEVER named in the script body; they
        # ride on pod env (overlaid via spec.diagnostic_env, see C28 A1.5) and
        # are consumed by `aws s3 cp` via the boto/CLI default chain.
        diagnostic_mode: bool = bool(cfg_dict.get("diagnostic_mode", False))
        trap_preamble: list[str] = []
        if diagnostic_mode:
            trap_preamble = [
                "set -euo pipefail",
                # Pre-install awscli so the EXIT trap below can complete its
                # `aws s3 cp` upload — the runpod/pytorch:2.4 base image does
                # NOT ship the CLI. Phase A v1 silently failed PUT for this
                # reason (sidecar Hn classification). Install runs in ~5-10s
                # on first boot; subsequent boots are no-op when warm-reuse
                # lands on a pod that already has it. `|| true` so a network
                # blip here doesn't kill the actual generation work.
                "command -v aws >/dev/null 2>&1 || "
                "pip install -q awscli >/dev/null 2>&1 || true",
                # Pre-install aria2 so _kinoforge_download takes the
                # parallel-multi-segment path (root cause of Phase A v2
                # 25-min Wan 14B stall — single-stream curl was capped at
                # ~2 MB/s; aria2c -x16 -s16 typically pushes 20+ MB/s on
                # the same HF endpoint).
                "command -v aria2c >/dev/null 2>&1 || "
                "(apt-get update -qq && apt-get install -y -qq aria2 "
                ">/dev/null 2>&1) || true",
                # Q8 diagnostic: force tee to line-buffer so the last few
                # seconds of script output land in /tmp/boot.log before the
                # EXIT trap reads tail. Without stdbuf, tee block-buffers
                # ~4KB and loses content if bash exits abruptly.
                "exec > >(stdbuf -oL -eL tee -a /tmp/boot.log) 2>&1",
                "trap '_kinoforge_diag_capture $?' EXIT",
                # Q5 diagnostic: trace every command with wall-clock timestamp
                # so boot.log shows exactly when each line ran. Also force
                # tee to flush via stdbuf so the last commands before exit
                # land in /tmp/boot.log before the trap reads tail -500.
                # Remove after the C28-restart-loop root cause is isolated.
                "export PS4='+ [$(date +%T.%N)] '",
                "set -x",
                "_kinoforge_diag_capture() {",
                "  local rc=$1",
                # Q8: give tee a beat to flush any buffered output before
                # we read tail. Cheap insurance against the buffering race.
                "  sync; sleep 0.5; sync",
                "  local last_line",
                "  last_line=$(tail -1 /tmp/boot.log 2>/dev/null || true)",
                "  {",
                "    echo '===== rc ====='; echo \"$rc\";",
                "    echo '===== last_line ====='; echo \"$last_line\";",
                "    echo '===== nvidia-smi ====='; nvidia-smi || true;",
                "    echo '===== df -h ====='; df -h || true;",
                "    echo '===== free -m ====='; free -m || true;",
                "    echo '===== ls -la models/diffusion_models ====='; "
                "ls -la /workspace/ComfyUI/models/diffusion_models 2>/dev/null"
                " || true;",
                "    echo '===== dpkg -l torch ====='; "
                "dpkg -l 2>/dev/null | grep -iE 'torch|cuda' || true;",
                "    echo '===== boot.log ====='; "
                "tail -500 /tmp/boot.log 2>/dev/null || true;",
                "    echo '===== p.sh wc/tail ====='; "
                "wc -lc /tmp/p.sh 2>/dev/null || true; "
                "tail -8 /tmp/p.sh 2>/dev/null || true;",
                "    echo '===== selfterm.log ====='; "
                "cat /tmp/selfterm.log 2>/dev/null || true;",
                "    echo '===== ps ====='; ps auxf 2>/dev/null || true;",
                "  } > /tmp/diag.txt",
                '  if [ -n "${KINOFORGE_DIAG_BUCKET:-}" ]; then',
                "    aws s3 cp /tmp/diag.txt "
                '"s3://${KINOFORGE_DIAG_BUCKET}/${KINOFORGE_DIAG_PREFIX}/'
                'diag-$(date -u +%Y%m%dT%H%M%SZ).txt" || true',
                "  fi",
                "}",
            ]

        lines: list[str] = [
            *(trap_preamble if trap_preamble else ["set -euo pipefail"]),
            # Selfterm watchdog — launch BEFORE bootstrap so the dead-man
            # window + max-lifetime cap fire even when the long clone / pip /
            # curl phase hangs. KINOFORGE_SELFTERM_SCRIPT is injected by
            # RunPodProvider.create_instance as plain Python source; this
            # writes it to /tmp/selfterm.py and detaches via nohup so it
            # survives the final `exec python main.py`.
            'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then '
            "python3 -c \"import os; open('/tmp/selfterm.py','w')"
            ".write(os.environ['KINOFORGE_SELFTERM_SCRIPT'])\" && "
            "nohup python3 /tmp/selfterm.py > /tmp/selfterm.log 2>&1 & "
            "fi",
            *kinoforge_download_helper,
            # C28 C2 (Phase A v2 finding): ensure aria2 is installed BEFORE
            # the model-download loop so _kinoforge_download takes the
            # -x16 -s16 fast path (typical 10-20x speedup vs single-stream
            # curl on the HF CDN). `|| true` so a network blip here does
            # NOT kill the actual generation work — the helper has a
            # curl fallback for that case.
            "command -v aria2c >/dev/null 2>&1 || "
            "(apt-get update -qq && apt-get install -y -qq aria2 "
            ">/dev/null 2>&1) || true",
            "cd /workspace",
        ]
        if not slim_mode:
            lines.append(
                f"[ ! -d ComfyUI ] && git clone --depth 1 --branch "
                f"{branch} {repo} ComfyUI",
            )
            lines.append("cd ComfyUI && pip install -q -r requirements.txt")
        else:
            # Pre-baked image already has /workspace/ComfyUI; ensure the
            # working directory matches so the model-download loop's
            # relative `models/<subdir>/<filename>` paths still resolve.
            lines.append("cd /workspace/ComfyUI")

        for node in custom_nodes if not slim_mode else ():
            node_url: str = node["git"]
            node_name: str = node_url.rstrip("/").split("/")[-1]
            if node_name.endswith(".git"):
                node_name = node_name[: -len(".git")]
            ref: str | None = node.get("ref")
            if ref:
                lines.append(
                    f"[ ! -d custom_nodes/{node_name} ] && "
                    f"git clone {node_url} custom_nodes/{node_name} && "
                    f"cd custom_nodes/{node_name} && git checkout {ref} && cd ../.."
                )
            else:
                lines.append(
                    f"[ ! -d custom_nodes/{node_name} ] && "
                    f"git clone --depth 1 {node_url} custom_nodes/{node_name}"
                )
            lines.append(
                f"[ -f custom_nodes/{node_name}/requirements.txt ] && "
                f"pip install -q -r custom_nodes/{node_name}/requirements.txt || true"
            )

        env_required: list[str] = []
        for entry in models_raw:
            # Accept canonical pydantic-dump key "ref" (Config.models[i].ref)
            # or legacy hand-crafted "src" key still used by some existing
            # render_provision unit tests. Real YAML flows through
            # cfg.model_dump() and only emits "ref"; the legacy fallback keeps
            # the older fixtures green without forcing a sweep.
            src_ref: str = entry.get("ref", entry.get("src", ""))
            if not src_ref:
                raise KeyError(
                    f"model entry missing 'ref' (or legacy 'src') key: {entry!r}"
                )
            target: str = entry["target"]
            subdir = TARGET_TO_SUBDIR.get(target, f"models/{target}")
            source = registry.source_for_ref(src_ref)
            artifacts = source.resolve(src_ref, _NullCredProvider())
            artifact = artifacts[0]
            filename: str = entry.get("filename") or artifact.filename
            # Surface the bearer-token env var requirement so the orchestrator
            # validates it BEFORE pod boot. C28 C2: the bearer header is
            # appended by _kinoforge_download via bash indirect expansion
            # (`${!token_env}`) — pass the env var NAME as arg 4, not the
            # value, so neither name nor value leaks into the script body.
            token_env_name = ""
            for hk, hv in (artifact.headers or {}).items():
                if hk.lower() == "authorization":
                    env_var = _extract_env_var(hv)
                    if env_var:
                        env_required.append(env_var)
                        token_env_name = env_var
            lines.append(f"mkdir -p {subdir}")
            sha = artifact.sha256 or ""
            lines.append(
                f"[ ! -f {subdir}/{filename} ] && "
                f"_kinoforge_download '{artifact.url}' "
                f"'{subdir}/{filename}' '{sha}' '{token_env_name}'"
            )

        port: str = _extract_port(launch_args_raw)
        run_cmd: list[str] = ["python", "main.py"] + launch_args_raw
        lines.append(f"cd /workspace/ComfyUI && exec {' '.join(run_cmd)}")

        return RenderedProvision(
            script="\n".join(lines),
            run_cmd=run_cmd,
            image=image,
            ports=[port],
            env_required=sorted(set(env_required)),
        )

    def wait_for_ready(
        self,
        instance: Instance,
        *,
        http_get: Callable[[str], dict[str, Any]],
        sleep: Callable[[float], None],
        get_instance: Callable[[str], Instance],
        timeout_s: float,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """Poll ``GET <comfyui>/system_stats`` until 200, status terminal, or timeout.

        Port-key heuristic: prefer ``"8188"`` key in ``instance.endpoints``,
        fall back to the first key present.

        Args:
            instance: The just-created compute instance.
            http_get: HTTP GET seam — raises on error, returns dict on success.
            sleep: Sleep seam used between polls.
            get_instance: Provider lookup for status checks between polls.
            timeout_s: Maximum total wait.
            cancel_token: C29 cooperative cancellation. Checked at the top of
                each poll iteration before any I/O so a boot-phase heartbeat
                reap (or operator Ctrl-C) raises ``Cancelled`` cleanly.
                Default ``None`` preserves pre-C29 behaviour.

        Raises:
            ProvisionFailed: Pod entered terminal status before ready.
            ProvisionTimeout: ``timeout_s`` elapsed without a successful ready check.
            Cancelled: ``cancel_token`` was set during the wait.
        """
        if not instance.endpoints:
            raise ProvisionFailed(
                f"pod {instance.id!r} has no endpoints — cannot construct ready URL"
            )
        port_key = (
            "8188"
            if "8188" in instance.endpoints
            else next(iter(instance.endpoints), "8188")
        )
        base = instance.endpoints.get(port_key, "")
        ready_url = f"{base.rstrip('/')}/system_stats"

        start = time.monotonic()
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            now = time.monotonic()
            if now - start >= timeout_s:
                raise ProvisionTimeout(
                    f"engine ready check timed out after {timeout_s:.0f}s "
                    f"for pod {instance.id!r}"
                )
            try:
                http_get(ready_url)
                return
            except Exception:  # noqa: BLE001, S110
                pass
            current = get_instance(instance.id)
            if current.status in ("terminated", "stopped"):
                raise ProvisionFailed(
                    f"pod {instance.id!r} entered terminal status "
                    f"{current.status!r} before ready"
                )
            sleep(_READY_POLL_INTERVAL_S)

    def backend(self, instance: Instance | None, cfg: dict[str, Any]) -> ComfyUIBackend:
        """Return a :class:`ComfyUIBackend` wired to this engine's injected callables.

        The base URL is derived from the instance's ``"comfyui"`` endpoint
        when present, otherwise defaults to ``"http://localhost:8188"``.

        Args:
            instance: The compute instance whose endpoints are consulted.
            cfg: Runtime configuration dict. Read for
                ``engine.comfyui.poll_timeout_s`` to bound the
                ``.result`` poll wait; absent (or non-dict shapes
                fed by unit tests) fall back to the
                :class:`ComfyUIBackend` constructor default.

        Returns:
            A :class:`ComfyUIBackend` ready to accept jobs.
        """
        base_url = "http://localhost:8188"
        if instance is not None and instance.endpoints:
            # Prefer a logical "comfyui" alias if present, then fall back to
            # the canonical port "8188" key populated by RunPodProvider's
            # eager-endpoints code (and by other providers using the
            # port-numbered convention), then to whatever first endpoint
            # exists. Without this fallback, post-wait_for_ready callers
            # (capture_object_info, GenerateClipStage) would target
            # http://localhost:8188 on the controller — connection refused.
            for key in ("comfyui", "8188"):
                if key in instance.endpoints:
                    base_url = instance.endpoints[key]
                    break
            else:
                base_url = next(iter(instance.endpoints.values()), base_url)

        # Pull poll_timeout_s from cfg.engine.comfyui.poll_timeout_s with a
        # tolerant fallback chain for the assorted cfg shapes tests pass
        # in. Production callers feed a pydantic-dumped dict, so the
        # nested .get() chain wins; ad-hoc dicts in tests fall through
        # to the constructor default.
        poll_timeout_s: float = 1800.0
        engine_block = cfg.get("engine", {}) if isinstance(cfg, dict) else {}
        if isinstance(engine_block, dict):
            comfyui_cfg = engine_block.get("comfyui", {})
            if isinstance(comfyui_cfg, dict):
                raw = comfyui_cfg.get("poll_timeout_s", poll_timeout_s)
                try:
                    poll_timeout_s = float(raw)
                except (TypeError, ValueError):
                    poll_timeout_s = 1800.0

        return ComfyUIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe=self._probe,
            sleep=self._sleep,
            http_get_bytes=self._http_get_bytes,
            http_post_file=self._http_post_file,
            poll_timeout_s=poll_timeout_s,
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise ``NotImplementedError`` — deferred to Task 12.

        Args:
            key: Unused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "ComfyUIEngine.profile_for is supplied by ModelProfileProvider in Task 12"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        """Return the strategy flags declared for *key*, or ``{}`` if unknown.

        Args:
            key: The :class:`~kinoforge.core.interfaces.CapabilityKey` to
                look up.

        Returns:
            A ``dict[str, bool]`` of strategy flags, or ``{}`` when no entry
            exists for this key.
        """
        return dict(self._flags_table.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Raise :class:`~kinoforge.core.errors.ValidationError` on a malformed spec.

        Both ``"graph"`` and ``"node_overrides"`` are required keys on a
        ComfyUI job spec.

        Layer F: in addition, for every asset on ``job.segments[0]`` the
        asset's role must appear as a key in
        ``job.spec.get("asset_node_ids", {})`` — the template author must
        declare which graph node receives each conditioning asset.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
                ``spec`` is checked.

        Raises:
            ValidationError: ``"graph"`` or ``"node_overrides"`` is absent
                from ``job.spec``, or an asset role on ``segments[0]`` has
                no entry in ``spec["asset_node_ids"]``.
        """
        required = {"graph", "node_overrides"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"ComfyUI job.spec is missing required keys: {sorted(missing)}"
            )
        if not job.segments:
            return
        asset_node_ids: dict[str, str] = job.spec.get("asset_node_ids", {})
        for asset in job.segments[0].assets:
            if asset.role not in asset_node_ids:
                raise ValidationError(
                    f"asset role {asset.role!r} present on segments[0] but "
                    f"spec.asset_node_ids has no mapping; add "
                    f"asset_node_ids.{asset.role}: <node_id> to the spec"
                )

        prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
        if prompt_node_ids:
            from kinoforge.core.prompt_routing import resolve_prompt

            if resolve_prompt(job) is None:
                raise ValidationError(
                    "comfyui spec.prompt_node_ids is configured but no "
                    "prompt found in job.spec or segments[0] — set "
                    "spec.prompt, set segments[0].prompt, or clear "
                    "spec.prompt_node_ids"
                )

    def model_identity(self, cfg: dict[str, object]) -> str:
        """ComfyUI identity is the filename stem of the kind=base model entry."""
        models = cfg.get("models", []) or []
        if not isinstance(models, list):
            return ""
        for entry in models:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == "base":
                ref = str(entry.get("ref", "") or "")
                if not ref:
                    return ""
                tail = ref.rsplit(":", 1)[-1] if ":" in ref else ref
                basename = tail.rsplit("/", 1)[-1]
                return basename.rsplit(".", 1)[0] if "." in basename else basename
        return ""

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        Args:
            artifact: A clip Artifact returned by :meth:`ComfyUIBackend.result`
                with ``url`` populated.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty, or the fetch or
                ffmpeg decode failed.
        """
        if not artifact.url:
            raise FrameExtractionError(
                f"{type(self).__name__}: artifact.url is empty; "
                "cannot fetch video bytes"
            )
        try:
            video_bytes = self._http_get_bytes(artifact.url)
        except Exception as exc:
            raise FrameExtractionError(
                f"{type(self).__name__}: fetch from {artifact.url!r} failed: {exc}"
            ) from exc
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    """Recursively merge *patch* into *base* in-place.

    Dict values are merged recursively; all other values are overwritten.

    Args:
        base: The dict to merge into (mutated in place).
        patch: The dict providing override values.
    """
    for key, value in patch.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _first_filename(outputs: dict[str, Any]) -> str:
    """Extract the first ``filename`` from a ComfyUI outputs dict.

    ComfyUI node-output naming varies by node type. ``VHS_VideoCombine``
    (the kijai Wan workflow's output sink) emits ``gifs`` even when the
    file is an .mp4 — the key name is legacy. Image-only nodes use
    ``images``; some custom nodes use ``files`` or ``videos``. Walk
    these keys in priority order. Verified live 2026-06-03 (pod
    rb6pi9cozjvf1g): the kijai i2v workflow's node 30 history entry
    has ``{"gifs": [{"filename": "WanVideoWrapper_I2V_00001.mp4", ...}]}``.

    Args:
        outputs: The ``outputs`` dict from a ComfyUI history response.

    Returns:
        The ``filename`` value from the first node's first file entry.

    Raises:
        KeyError: No filename found in the outputs structure.
    """
    _OUTPUT_KEYS = ("files", "videos", "gifs", "images")
    for node_data in outputs.values():
        for key in _OUTPUT_KEYS:
            files = node_data.get(key, [])
            if files:
                return str(files[0]["filename"])
    raise KeyError("No filename found in ComfyUI outputs")


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("comfyui", lambda: ComfyUIEngine())
