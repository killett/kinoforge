"""DiffusersEngine + DiffusersBackend — adapter for a locally-running diffusers inference server.

All I/O (subprocess, HTTP, sleep) is routed through injected callables so that
tests can spy without any real side-effects.

Module-level self-registration puts the factory in the global registry under
the ``"diffusers"`` key so that ``registry.get_engine("diffusers")()`` works.
"""

from __future__ import annotations

import json
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum poll iterations in :meth:`DiffusersBackend.result`.
_MAX_POLL = 60

#: Default server base URL.
_DEFAULT_BASE_URL = "http://127.0.0.1:8000"


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
    name="diffusers",
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


class DiffusersBackend(GenerationBackend):
    """Live backend that communicates with a running diffusers inference server.

    All HTTP traffic goes through injected callables so tests can spy
    without starting a real server.

    Attributes:
        _http_post: Callable ``(url, body) -> dict`` for POST requests.
        _http_get: Callable ``(url) -> dict`` for GET requests.
        _base_url: Base URL of the diffusers server (no trailing slash).
        _probe: The ``ModelProfile`` returned by capability queries.
        _sleep: Injectable sleep function (default: ``time.sleep``).
    """

    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        base_url: str,
        probe_profile: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the diffusers server, e.g.
                ``"http://127.0.0.1:8000"``.  No trailing slash.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe_profile
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
        """POST the job spec to ``/generate`` and return the job ID.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is sent as the request body.

        Returns:
            The ``job_id`` string from the server response.
        """
        url = f"{self._base_url}/generate"
        response = self._http_post(url, dict(job.spec))
        return str(response["job_id"])

    def result(self, job_id: str) -> Artifact:
        """Poll ``/status/{job_id}`` until ``status == "done"``.

        Polls at most :data:`_MAX_POLL` times, sleeping between iterations
        using the injected *sleep* callable.

        Args:
            job_id: The job ID returned by a prior ``submit`` call.

        Returns:
            An ``Artifact`` whose ``filename`` comes from the server response
            and whose ``meta`` contains ``{"job_id": job_id}``.

        Raises:
            TimeoutError: The server did not return ``status == "done"``
                within the poll limit.
        """
        url = f"{self._base_url}/status/{job_id}"
        for _ in range(_MAX_POLL):
            data = self._http_get(url)
            if data.get("status") == "done":
                filename = str(data.get("filename", ""))
                artifact_url = str(data.get("url", ""))
                return Artifact(
                    filename=filename, url=artifact_url, meta={"job_id": job_id}
                )
            self._sleep(1.0)
        raise TimeoutError(
            f"Diffusers server did not complete job {job_id!r} within {_MAX_POLL} polls"
        )

    def endpoints(self) -> dict[str, str]:
        """Return the diffusers server endpoint map.

        Returns:
            A dict with ``"generate"`` and ``"status"`` endpoint URLs.
        """
        return {
            "generate": f"{self._base_url}/generate",
            "status": f"{self._base_url}/status",
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DiffusersEngine(GenerationEngine):
    """Generation engine adapter for a locally-provisioned diffusers inference server.

    Provisions the server by:

    1. Installing Python dependencies via ``pip install``.
    2. Launching the headless inference server with the configured command.

    All I/O is routed through injected callables (``run_cmd``, ``http_post``,
    ``http_get``, ``sleep``) so that tests can spy without real side-effects.

    Class attributes:
        name: Registry key ``"diffusers"``.
        requires_compute: ``True`` — a GPU instance is needed.
        requires_local_weights: ``True`` — weights must be provisioned locally.
    """

    name: str = "diffusers"
    requires_compute: bool = True
    requires_local_weights: bool = True

    def __init__(
        self,
        *,
        run_cmd: Callable[[list[str], str | None], None] = _subprocess_run,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] = _urllib_post_json,
        http_get: Callable[[str], dict[str, Any]] = _urllib_get_json,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_PROBE,
        declared_flags_map: dict[str, dict[str, bool]] | None = None,
    ) -> None:
        """Initialise the engine with all I/O seams as injectable callables.

        Args:
            run_cmd: Callable ``(argv, cwd) -> None`` for subprocess calls.
            http_post: Callable ``(url, json_body) -> dict`` for HTTP POST.
            http_get: Callable ``(url) -> dict`` for HTTP GET.
            http_get_bytes: Callable ``(url) -> bytes`` for raw-byte HTTP GET
                (used by extract_last_frame to fetch the rendered video).
            ffmpeg_run: Subprocess seam ``(argv, stdin) -> stdout`` used by
                extract_last_frame to decode the last frame via ffmpeg.
            sleep: Sleep callable used between polling iterations in
                :meth:`~DiffusersBackend.result`.
            probe_profile: ``ModelProfile`` returned by backend capability
                queries.
            declared_flags_map: Optional mapping from
                :meth:`~kinoforge.core.interfaces.CapabilityKey.derive` hex
                strings to ``dict[str, bool]`` strategy-flag dicts.
        """
        self._run_cmd = run_cmd
        self._http_post = http_post
        self._http_get = http_get
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
        self._sleep = sleep
        self._probe = probe_profile
        self._declared_flags_map: dict[str, dict[str, bool]] = declared_flags_map or {}

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        """Install pip deps and launch the headless diffusers inference server.

        Provision steps (in order):

        1. If ``cfg["engine"]["diffusers"]["pip"]`` is non-empty, run
           ``pip install`` with the declared package list.
        2. Launch the server with ``cfg["engine"]["diffusers"]["server_cmd"]``.

        Args:
            instance: The compute instance (unused; present for interface
                compliance).
            cfg: Runtime configuration dict.
        """
        del instance  # not used; server runs on the local machine
        engine_block = cfg.get("engine", {})
        diffusers_cfg: dict[str, Any] = (
            engine_block.get("diffusers", {}) if isinstance(engine_block, dict) else {}
        )
        pip_deps: list[str] = list(diffusers_cfg.get("pip", []))
        server_cmd: list[str] = list(diffusers_cfg.get("server_cmd", []))

        # Step 1 — install pip dependencies.
        if pip_deps:
            self._run_cmd(["pip", "install"] + pip_deps, None)

        # Step 2 — launch the headless inference server.
        if server_cmd:
            self._run_cmd(server_cmd, None)

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> DiffusersBackend:
        """Return a :class:`DiffusersBackend` wired to this engine's injected callables.

        The base URL is taken from
        ``cfg["engine"]["diffusers"]["base_url"]`` when present, otherwise
        defaults to :data:`_DEFAULT_BASE_URL`.

        Args:
            instance: The compute instance (unused currently).
            cfg: Runtime configuration dict.

        Returns:
            A :class:`DiffusersBackend` ready to accept jobs.
        """
        del instance
        engine_block = cfg.get("engine", {})
        diffusers_cfg: dict[str, Any] = (
            engine_block.get("diffusers", {}) if isinstance(engine_block, dict) else {}
        )
        base_url: str = str(diffusers_cfg.get("base_url", _DEFAULT_BASE_URL))
        return DiffusersBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe_profile=self._probe,
            sleep=self._sleep,
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise ``NotImplementedError`` — deferred to the profile provider.

        Args:
            key: Unused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "DiffusersEngine.profile_for is supplied by ModelProfileProvider"
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
        return dict(self._declared_flags_map.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Raise :class:`~kinoforge.core.errors.ValidationError` when spec keys are missing.

        Both ``"pipeline"`` (the diffusers pipeline class name) and
        ``"scheduler"`` (the scheduler name) are required keys on a
        diffusers job spec.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
                ``spec`` is checked.

        Raises:
            ValidationError: ``"pipeline"`` or ``"scheduler"`` is absent
                from ``job.spec``.
        """
        required = {"pipeline", "scheduler"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"Diffusers job.spec is missing required keys: {sorted(missing)}"
            )

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        Args:
            artifact: A clip Artifact returned by :meth:`DiffusersBackend.result`
                with ``url`` populated by the inference server.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty (server omitted
                the ``url`` field from its response), or the fetch or ffmpeg
                decode failed.
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
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("diffusers", lambda: DiffusersEngine())
