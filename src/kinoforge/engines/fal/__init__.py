"""fal.ai sibling engine (queue API).

A standalone :class:`~kinoforge.core.interfaces.GenerationEngine` targeting the
fal.ai queue API.  Composes shared helpers (``set_by_dot_path``, ``find_asset``)
from the core library; does NOT inherit from
:class:`~kinoforge.engines.hosted.HostedAPIEngine` because their wire shapes
differ — fal.ai uses a two-step queue-then-poll pattern.

Self-registers under ``"fal"`` on import.

Cfg shape under ``cfg["engine"]["fal"]``:

.. code-block:: yaml

    engine:
      fal:
        endpoint: fal-ai/wan/v2.2/t2v               # model path
        queue_base: https://queue.fal.run           # queue API base URL
        api_key_env: FAL_KEY                        # env-var name for the credential
        url_path: video.url                         # dot-path to the URL in result body
        health_url: https://queue.fal.run/health    # optional; pinged by provision()
        asset_paths:                                 # optional role -> dot-path map
          init_image: image_url
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from os.path import basename
from typing import Any
from urllib.parse import urlparse

from kinoforge.core import registry
from kinoforge.core.assets import find_asset, set_by_dot_path
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.engines.fal.wire import (
    FalStatus,
    build_response_url,
    build_status_url,
    extract_result_url,
    interpret_status,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default max poll iterations in :meth:`FalBackend.result`.
_MAX_POLL_DEFAULT = 600


# ---------------------------------------------------------------------------
# Default I/O seams
# ---------------------------------------------------------------------------


def _urllib_post_json(
    url: str, body: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    """POST ``body`` as JSON with ``headers`` to ``url`` and return decoded JSON.

    Args:
        url: Endpoint URL.
        body: JSON-serialisable request body.
        headers: HTTP headers (caller controls Authorization + Content-Type).

    Returns:
        Decoded JSON response.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — caller controls URL
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        raw = resp.read()
    decoded: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return decoded


def _urllib_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """GET ``url`` with optional ``headers`` and return decoded JSON.

    Args:
        url: Endpoint URL.
        headers: HTTP headers.

    Returns:
        Decoded JSON response.
    """
    req = urllib.request.Request(  # noqa: S310 — caller controls URL
        url, method="GET", headers=headers or {}
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        raw = resp.read()
    decoded: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return decoded


# ---------------------------------------------------------------------------
# Default stub profile (real shape; real values come from ModelProfileProvider)
# ---------------------------------------------------------------------------

_DEFAULT_STUB_PROFILE = ModelProfile(
    name="fal-stub",
    max_frames=120,
    fps=24,
    supported_modes={"t2v", "i2v"},
    max_resolution=(1024, 1024),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class FalBackend(GenerationBackend):
    """Live backend wired to a fal.ai queue endpoint.

    All HTTP traffic is routed through injected callables so tests can spy
    without hitting the real service.  ``submit`` records the per-request
    status/response URLs (or constructs fall-backs) in an internal map so
    ``result`` can poll the same job.

    Attributes:
        _endpoint: Endpoint path (e.g. ``"fal-ai/wan/v2.2/t2v"``).
        _queue_base: Queue API base URL.
        _api_key: Bearer credential (sent as ``Authorization: Key <api_key>``).
        _url_path: Dot-separated path through the result body to the URL.
        _asset_paths: Role -> request-body dot-path map.
        _profile: ``ModelProfile`` returned by capability queries.
        _http_post: POST callable ``(url, body, headers) -> dict``.
        _http_get: GET callable ``(url, headers) -> dict``.
        _sleep: Sleep callable used between status polls.
        _max_poll: Maximum status-poll iterations before TimeoutError.
        _jobs: Per-request URL state populated by ``submit``.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        queue_base: str,
        api_key: str,
        url_path: str,
        asset_paths: dict[str, str],
        profile: ModelProfile,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
        | None = None,
        http_get: Callable[[str, dict[str, str] | None], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_poll: int = _MAX_POLL_DEFAULT,
    ) -> None:
        """Initialise the backend with wire parameters and injectable I/O seams.

        Args:
            endpoint: Model endpoint path (e.g. ``"fal-ai/wan/v2.2/t2v"``).
            queue_base: Queue API base URL (e.g. ``"https://queue.fal.run"``).
            api_key: Resolved API key sent in the Authorization header.
            url_path: Dot-path through the result body to the URL string.
            asset_paths: Mapping from conditioning-asset role to a dot-path
                in the request body where the asset's URI should be written.
            profile: ``ModelProfile`` returned by ``capabilities()``.
            http_post: POST callable for submitting jobs.
            http_get: GET callable for status polls and result fetches.
            sleep: Sleep callable invoked between status polls.
            max_poll: Maximum number of status-poll iterations before
                raising :class:`TimeoutError`.
        """
        self._endpoint = endpoint
        self._queue_base = queue_base
        self._api_key = api_key
        self._url_path = url_path
        self._asset_paths = dict(asset_paths)
        self._profile = profile
        self._http_post = http_post or _urllib_post_json
        self._http_get = http_get or _urllib_get_json
        self._sleep = sleep or time.sleep
        self._max_poll = max_poll
        # request_id -> {"status_url": ..., "response_url": ...}
        self._jobs: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # GenerationBackend interface
    # ------------------------------------------------------------------

    def capabilities(self) -> ModelProfile:
        """Return the injected ``ModelProfile``."""
        return self._profile

    def inspect_capabilities(self) -> ModelProfile:
        """Return the injected ``ModelProfile`` (no live probe)."""
        return self._profile

    def _auth_headers(self) -> dict[str, str]:
        """Return the per-request Authorization header dict."""
        return {"Authorization": f"Key {self._api_key}"}

    def submit(self, job: GenerationJob) -> str:
        """POST the spec to ``{queue_base}/{endpoint}`` and return the request_id.

        For each role in ``self._asset_paths``, look up the matching asset on
        ``job.segments[0]`` via :func:`~kinoforge.core.assets.find_asset` and
        write its ``ref.uri`` into a copy of the body at the configured
        dot-path.  ``job.spec`` is not mutated.

        After the POST, the (server-supplied or constructed) status_url and
        response_url for the request are recorded on the backend so
        :meth:`result` can poll the same job.

        Args:
            job: The generation job to submit.

        Returns:
            The ``request_id`` string returned by the queue.
        """
        body = dict(job.spec)
        for role, dot_path in self._asset_paths.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            set_by_dot_path(body, dot_path, asset.ref.uri)
        full_url = f"{self._queue_base.rstrip('/')}/{self._endpoint}"
        headers = {
            "Content-Type": "application/json",
            **self._auth_headers(),
        }
        response = self._http_post(full_url, body, headers)
        request_id = str(response["request_id"])
        status_url = build_status_url(
            submit_response=response,
            queue_base=self._queue_base,
            endpoint=self._endpoint,
            request_id=request_id,
        )
        response_url = build_response_url(
            submit_response=response,
            queue_base=self._queue_base,
            endpoint=self._endpoint,
            request_id=request_id,
        )
        self._jobs[request_id] = {
            "status_url": status_url,
            "response_url": response_url,
        }
        return request_id

    def result(self, job_id: str) -> Artifact:
        """Poll the recorded status_url until COMPLETED, then GET the result URL.

        Args:
            job_id: The request_id returned by a prior ``submit`` call.

        Returns:
            An :class:`~kinoforge.core.interfaces.Artifact` whose ``url``
            is the extracted result URL, ``filename`` is the URL's basename
            (or the URL itself when no path is present), and ``meta`` holds
            ``{"request_id": job_id}``.

        Raises:
            KinoforgeError: ``submit`` was not called for ``job_id``, the
                queue returned a FAILED status, or the queue returned an
                unrecognised status string.
            TimeoutError: The job did not complete within ``self._max_poll``
                iterations.
        """
        urls = self._jobs.get(job_id)
        if urls is None:
            raise KinoforgeError(f"fal job {job_id!r} not found — was submit() called?")
        status_url = urls["status_url"]
        response_url = urls["response_url"]

        for _ in range(self._max_poll):
            data = self._http_get(status_url, self._auth_headers())
            status_str = str(data.get("status", ""))
            cls = interpret_status(status_str)
            if cls is FalStatus.COMPLETED:
                result_data = self._http_get(response_url, self._auth_headers())
                url = extract_result_url(result_data, self._url_path)
                filename = basename(urlparse(url).path) or url
                return Artifact(
                    filename=filename,
                    url=url,
                    meta={"request_id": job_id},
                )
            if cls is FalStatus.FAILED:
                raise KinoforgeError(
                    f"fal job {job_id!r} failed: {data.get('logs', [])}"
                )
            if cls is FalStatus.UNKNOWN:
                raise KinoforgeError(
                    f"fal job {job_id!r} unknown status: {status_str!r}"
                )
            # PENDING: sleep and loop.
            self._sleep(1.0)
        raise TimeoutError(
            f"fal job {job_id!r} did not complete within {self._max_poll} polls"
        )

    def endpoints(self) -> dict[str, str]:
        """Return the queue-endpoint map.

        Returns:
            ``{"queue": "<queue_base>/<endpoint>"}``.
        """
        return {"queue": f"{self._queue_base.rstrip('/')}/{self._endpoint}"}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class FalEngine(GenerationEngine):
    """Generation engine adapter for the fal.ai queue API.

    No compute instance is required.  ``provision`` validates credentials
    and (optionally) pings a health endpoint.  ``backend`` returns a
    :class:`FalBackend` wired to the configured endpoint.

    All I/O is routed through injected callables so tests can spy without
    real side-effects.

    Class attributes:
        name: Registry key ``"fal"``.
        requires_compute: ``False`` — no GPU instance is needed.
        requires_local_weights: ``False`` — weights live on fal.ai.
    """

    name: str = "fal"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        creds: CredentialProvider | None = None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
        | None = None,
        http_get: Callable[[str, dict[str, str] | None], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
        probe_profile: ModelProfile = _DEFAULT_STUB_PROFILE,
        declared_flags_map: dict[str, dict[str, bool]] | None = None,
    ) -> None:
        """Initialise the engine with injectable I/O seams.

        Args:
            creds: :class:`~kinoforge.core.interfaces.CredentialProvider`
                used to resolve the API key.  Defaults to
                :class:`~kinoforge.core.credentials.EnvCredentialProvider`.
            http_post: POST callable ``(url, body, headers) -> dict``.
            http_get: GET callable ``(url, headers) -> dict``.
            sleep: Sleep callable used between backend status polls.
            probe_profile: Stub :class:`ModelProfile` returned by
                ``backend.capabilities()`` until the real profile is
                resolved by :class:`~kinoforge.core.interfaces.ModelProfileProvider`.
            declared_flags_map: Optional mapping from
                :meth:`~kinoforge.core.interfaces.CapabilityKey.derive`
                hex strings to ``dict[str, bool]`` strategy-flag dicts.
        """
        self._creds: CredentialProvider = creds or EnvCredentialProvider()
        self._http_post = http_post or _urllib_post_json
        self._http_get = http_get or _urllib_get_json
        self._sleep = sleep or time.sleep
        self._probe = probe_profile
        self._declared_flags_map: dict[str, dict[str, bool]] = dict(
            declared_flags_map or {}
        )

    # ------------------------------------------------------------------
    # GenerationEngine interface
    # ------------------------------------------------------------------

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        """Validate credentials and (optionally) probe the health endpoint.

        ``instance`` must be ``None`` — fal has no compute to configure.
        The API key env var name is resolved from
        ``cfg["engine"]["fal"]["api_key_env"]`` (default ``"FAL_KEY"``).
        If ``cfg["engine"]["fal"]["health_url"]`` is non-empty, it is GET-ed
        once; any failure is wrapped in
        :class:`~kinoforge.core.errors.KinoforgeError`.

        Args:
            instance: Must be ``None``.
            cfg: Runtime configuration dict.

        Raises:
            KinoforgeError: ``instance`` is not ``None`` or the health probe failed.
            AuthError: The API key env var is unset.
        """
        if instance is not None:
            raise KinoforgeError(
                "FalEngine.provision: instance must be None "
                "(fal engine has no compute to configure)"
            )
        engine_block = cfg.get("engine", {})
        fal_cfg: dict[str, Any] = (
            engine_block.get("fal", {}) if isinstance(engine_block, dict) else {}
        )
        key_name = str(fal_cfg.get("api_key_env", "") or "FAL_KEY")
        cred = self._creds.get(key_name)
        if cred is None:
            raise AuthError(f"engine.fal.api_key_env={key_name} is not set in env")
        health_url = str(fal_cfg.get("health_url", "") or "")
        if health_url:
            try:
                self._http_get(health_url, None)
            except Exception as exc:
                raise KinoforgeError(f"fal endpoint unreachable: {exc}") from exc

    def backend(self, instance: Instance | None, cfg: dict[str, Any]) -> FalBackend:
        """Return a :class:`FalBackend` wired with the resolved api_key + cfg.

        Args:
            instance: Ignored (no compute for fal engines).
            cfg: Runtime configuration dict containing ``cfg["engine"]["fal"]``.

        Returns:
            A :class:`FalBackend` ready to accept jobs.
        """
        del instance  # fal path: no instance needed
        engine_block = cfg.get("engine", {})
        fal_cfg: dict[str, Any] = (
            engine_block.get("fal", {}) if isinstance(engine_block, dict) else {}
        )
        endpoint = str(fal_cfg.get("endpoint", ""))
        queue_base = str(fal_cfg.get("queue_base", "https://queue.fal.run"))
        api_key_env = str(fal_cfg.get("api_key_env", "FAL_KEY"))
        url_path = str(fal_cfg.get("url_path", ""))
        asset_paths_raw = fal_cfg.get("asset_paths", {}) or {}
        asset_paths: dict[str, str] = (
            {str(k): str(v) for k, v in asset_paths_raw.items()}
            if isinstance(asset_paths_raw, dict)
            else {}
        )
        api_key = self._creds.get(api_key_env) or ""

        return FalBackend(
            endpoint=endpoint,
            queue_base=queue_base,
            api_key=api_key,
            url_path=url_path,
            asset_paths=asset_paths,
            profile=self._probe,
            http_post=self._http_post,
            http_get=self._http_get,
            sleep=self._sleep,
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise :class:`NotImplementedError` — deferred to ModelProfileProvider.

        Args:
            key: Unused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "FalEngine.profile_for is supplied by ModelProfileProvider"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        """Return the strategy flags declared for ``key``, or ``{}`` if unknown.

        Args:
            key: The :class:`~kinoforge.core.interfaces.CapabilityKey` to look up.

        Returns:
            A ``dict[str, bool]`` strategy-flag dict; a copy so caller mutation
            does not affect the engine state.
        """
        return dict(self._declared_flags_map.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Raise :class:`ValidationError` if the spec lacks a non-empty prompt.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` to check.

        Raises:
            ValidationError: ``job.spec`` has no ``"prompt"`` key or the
                value is empty.
        """
        prompt = job.spec.get("prompt", "")
        if not isinstance(prompt, str) or not prompt:
            raise ValidationError(
                "fal engine requires a non-empty 'prompt' in job.spec"
            )


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("fal", FalEngine)
