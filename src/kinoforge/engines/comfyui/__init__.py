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
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from kinoforge.core import frames, registry
from kinoforge.core.errors import FrameExtractionError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
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
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return dict(json.loads(resp.read().decode("utf-8")))


def _urllib_get_json(url: str) -> dict[str, Any]:
    """GET *url* and return the decoded JSON response.

    Args:
        url: Endpoint URL.

    Returns:
        Decoded JSON response as a Python dict.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310
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

    Args:
        url: Endpoint URL.

    Returns:
        Response body as bytes.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())


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
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the ComfyUI server, e.g.
                ``"http://localhost:8188"``.  No trailing slash.
            probe: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe
        self._sleep = sleep

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

        Args:
            job: The ``GenerationJob`` whose ``spec`` contains ``"graph"``
                and ``"node_overrides"``.

        Returns:
            The ``prompt_id`` string from the ComfyUI server response.
        """
        graph: dict[str, Any] = copy.deepcopy(job.spec.get("graph", {}))
        overrides: dict[str, Any] = job.spec.get("node_overrides", {})
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
                view_url = f"{self._base_url}/view?filename={filename}&type=output"
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
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_PROBE,
        flags_table: dict[str, dict[str, bool]] | None = None,
        comfyui_root: str = _DEFAULT_COMFYUI_ROOT,
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
                (used by extract_last_frame to fetch the rendered video).
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
        """
        self._run_cmd = run_cmd
        self._file_exists = file_exists
        self._route_file = route_file
        self._http_post = http_post
        self._http_get = http_get
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
        self._sleep = sleep
        self._probe = probe_profile
        self._flags_table: dict[str, dict[str, bool]] = flags_table or {}
        self._comfyui_root = comfyui_root

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        """Clone nodes, install requirements, route models, launch ComfyUI.

        Provision steps (in order):

        1. Clone each ``cfg["engine"]["comfyui"]["custom_nodes"][i]["git"]``
           and install its ``requirements.txt`` when present.
        2. Route each entry in ``cfg["models"]`` into the appropriate
           ComfyUI model subdirectory.
        3. Launch ComfyUI with ``cfg["engine"]["comfyui"]["launch_args"]``.

        Args:
            instance: The compute instance (unused; present for interface
                compliance).
            cfg: Runtime configuration dict.
        """
        del instance  # not used; comfyui runs on the local machine
        engine_block = cfg.get("engine", {})
        comfyui_cfg: dict[str, Any] = (
            engine_block.get("comfyui", {}) if isinstance(engine_block, dict) else {}
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
        if instance is not None and "comfyui" in instance.endpoints:
            base_url = instance.endpoints["comfyui"]
        else:
            base_url = "http://localhost:8188"
        return ComfyUIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe=self._probe,
            sleep=self._sleep,
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
        """Raise :class:`~kinoforge.core.errors.ValidationError` when spec keys are missing.

        Both ``"graph"`` and ``"node_overrides"`` are required keys on a
        ComfyUI job spec.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
                ``spec`` is checked.

        Raises:
            ValidationError: ``"graph"`` or ``"node_overrides"`` is absent
                from ``job.spec``.
        """
        required = {"graph", "node_overrides"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"ComfyUI job.spec is missing required keys: {sorted(missing)}"
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
        video_bytes = self._http_get_bytes(artifact.url)
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
