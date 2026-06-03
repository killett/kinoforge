"""ComfyUIEngine + ComfyUIBackend — real adapter for a locally-running ComfyUI server.

All I/O (subprocess, HTTP, filesystem, sleep) is routed through injected
callables so that tests can spy without any real side-effects.

Module-level self-registration puts the factory in the global registry under
the ``"comfyui"`` key so that ``registry.get_engine("comfyui")()`` works.
"""

from __future__ import annotations

import copy
import json
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
from typing import Any

from kinoforge.core import frames, registry
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
_MAX_POLL = 60

#: Default ComfyUI installation root (resolved at runtime, NOT at import time).
_DEFAULT_COMFYUI_ROOT = "ComfyUI"

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
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


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
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
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
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the ComfyUI server, e.g.
                ``"http://localhost:8188"``.  No trailing slash.
            probe: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            http_get_bytes: Byte fetcher used by ``submit`` to resolve
                ``http``/``https`` asset URIs (``file://`` is read by
                :func:`kinoforge.core.assets.asset_bytes` via stdlib
                :class:`pathlib.Path`).
            http_post_file: Multipart POST callable
                ``(url, *, field_name, filename, content) -> str``
                returning the server-side filename. Defaults to the
                stdlib urllib multipart helper
                :func:`_urllib_post_multipart`.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe
        self._sleep = sleep
        self._http_get_bytes = http_get_bytes
        self._http_post_file = http_post_file

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

    def submit(self, job: GenerationJob) -> str:
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

        Returns:
            The ``prompt_id`` string from the ComfyUI server response.

        Raises:
            AssetFetchError: Asset URI fetch failed (raised from
                :func:`~kinoforge.core.assets.asset_bytes`) or upload to
                ``/upload/image`` failed.
        """
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
            try:
                uploaded_name = self._http_post_file(
                    upload_url,
                    field_name="image",
                    filename=asset.ref.filename or f"{role}.png",
                    content=payload,
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
        # ``node_overrides[node_id]["inputs"]["text"]`` from spec wins.
        prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
        if prompt_node_ids:
            from kinoforge.core.prompt_routing import resolve_prompt

            prompt = resolve_prompt(job)
            if prompt is not None:
                for _role, node_id in prompt_node_ids.items():
                    node_patch = overrides.setdefault(str(node_id), {})
                    inputs = node_patch.setdefault("inputs", {})
                    inputs.setdefault("text", prompt)

        # Deep-merge overrides into the graph at the node level.
        for node_id, node_patch in overrides.items():
            if node_id in graph:
                _deep_merge(graph[node_id], node_patch)
            else:
                graph[node_id] = copy.deepcopy(node_patch)
        url = f"{self._base_url}/prompt"
        response = self._http_post(url, {"prompt": graph})
        return str(response["prompt_id"])

    def result(self, job_id: str) -> Artifact:
        """Poll ``/history/{job_id}`` until outputs are available.

        Polls at most :data:`_MAX_POLL` times, sleeping between iterations
        using the injected *sleep* callable.

        Args:
            job_id: The ``prompt_id`` returned by a prior ``submit`` call.

        Returns:
            An ``Artifact`` whose ``filename`` is the first filename from the
            first node's file list, and whose ``meta`` contains
            ``{"prompt_id": job_id}``.

        Raises:
            TimeoutError: The server did not return outputs within the poll
                limit.
        """
        url = f"{self._base_url}/history/{job_id}"
        for _ in range(_MAX_POLL):
            data = self._http_get(url)
            entry = data.get(job_id, {})
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
            self._sleep(1.0)
        raise TimeoutError(
            f"ComfyUI did not complete prompt {job_id!r} within {_MAX_POLL} polls"
        )

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
        requires_local_weights: ``True`` — weights must be provisioned locally.
    """

    name: str = "comfyui"
    requires_compute: bool = True
    requires_local_weights: bool = True

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

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
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

        lines: list[str] = [
            "set -euo pipefail",
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
            "cd /workspace",
            f"[ ! -d ComfyUI ] && git clone --depth 1 --branch {branch} {repo} ComfyUI",
            "cd ComfyUI && pip install -q -r requirements.txt",
        ]

        for node in custom_nodes:
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
            auth_header = ""
            for hk, hv in (artifact.headers or {}).items():
                if hk.lower() == "authorization":
                    env_var = _extract_env_var(hv)
                    if env_var:
                        env_required.append(env_var)
                        auth_header = f' -H "Authorization: Bearer ${env_var}"'
            lines.append(f"mkdir -p {subdir}")
            lines.append(
                f"[ ! -f {subdir}/{filename} ] && "
                f"curl -L --fail{auth_header} '{artifact.url}' -o {subdir}/{filename}"
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

        Raises:
            ProvisionFailed: Pod entered terminal status before ready.
            ProvisionTimeout: ``timeout_s`` elapsed without a successful ready check.
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
            cfg: Runtime configuration dict (unused currently).

        Returns:
            A :class:`ComfyUIBackend` ready to accept jobs.
        """
        del cfg
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
        return ComfyUIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe=self._probe,
            sleep=self._sleep,
            http_get_bytes=self._http_get_bytes,
            http_post_file=self._http_post_file,
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

    Args:
        outputs: The ``outputs`` dict from a ComfyUI history response.

    Returns:
        The ``filename`` value from the first node's first file entry.

    Raises:
        KeyError: No filename found in the outputs structure.
    """
    for node_data in outputs.values():
        files = node_data.get("files", [])
        if files:
            return str(files[0]["filename"])
    raise KeyError("No filename found in ComfyUI outputs")


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("comfyui", lambda: ComfyUIEngine())
