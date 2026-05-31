"""HostedAPIEngine + HostedAPIBackend — the no-compute path.

Validates credentials and endpoint availability, then delegates generation
to a remote hosted inference API (e.g. fal.ai).  No GPU instance is ever
created; ``requires_compute=False`` and ``requires_local_weights=False``.

All I/O (HTTP GET/POST, sleep) is routed through injected callables so that
tests can spy without real side-effects.

Module-level self-registration places the factory in the global registry
under the ``"hosted"`` key so that ``registry.get_engine("hosted")()`` works.

Cfg shape under ``cfg["engine"]["hosted"]``:

.. code-block:: yaml

    engine:
      hosted:
        provider: fal                           # e.g. "fal"
        endpoint: https://fal.run/fal-ai/ltx-video
        model: ltx-2                            # hosted model id
        api_key_env: FAL_KEY                    # env-var name for the credential
        health_url: https://fal.run/health      # pinged by provision()
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from kinoforge.core import frames, registry
from kinoforge.core.assets import find_asset, set_by_dot_path
from kinoforge.core.errors import (
    AuthError,
    FrameExtractionError,
    KinoforgeError,
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
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum poll iterations in :meth:`HostedAPIBackend.result`.
_MAX_POLL = 120


# ---------------------------------------------------------------------------
# Real I/O helpers (default implementations)
# ---------------------------------------------------------------------------


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
    """GET *url* and return the raw response body as bytes."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())


def _walk_dot_path(data: dict[str, Any], path: str) -> str:
    """Walk dot-separated keys through *data*; return empty string on any miss.

    Args:
        data: The dict to walk.
        path: Dot-separated key path, e.g. ``"video.url"``. Empty path
            returns ``""``.

    Returns:
        The string at the walked path, or ``""`` if any step is missing,
        any intermediate node is not a dict, or the terminal value is not
        a string.

    Examples:
        >>> _walk_dot_path({"video": {"url": "X"}}, "video.url")
        'X'
        >>> _walk_dot_path({"video": {"url": "X"}}, "missing.url")
        ''
    """
    if not path:
        return ""
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return ""
        node = node[key]
    return node if isinstance(node, str) else ""


# ---------------------------------------------------------------------------
# Default probe profile
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="hosted",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# ---------------------------------------------------------------------------
# Default no-op credential provider
# ---------------------------------------------------------------------------


class _NullCredentialProvider(CredentialProvider):
    """Credential provider that always returns None.

    Used as the default when no ``creds`` argument is passed to
    :class:`HostedAPIEngine` so that ``provision`` simply raises
    :class:`~kinoforge.core.errors.AuthError` for every key lookup.
    """

    def get(self, key: str) -> str | None:
        """Return None for every key.

        Args:
            key: Credential key name (unused).

        Returns:
            Always ``None``.
        """
        return None


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class HostedAPIBackend(GenerationBackend):
    """Live backend that communicates with a remote hosted inference API.

    All HTTP traffic goes through injected callables so tests can spy
    without hitting a real service.

    Attributes:
        _http_post: Callable ``(url, body) -> dict`` for POST requests.
        _http_get: Callable ``(url) -> dict`` for GET/status requests.
        _endpoint: Remote inference endpoint URL.
        _probe: The ``ModelProfile`` returned by capability queries.
        _sleep: Injectable sleep function (default: ``time.sleep``).
    """

    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        endpoint: str,
        probe_profile: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        url_path: str = "",
        asset_paths: dict[str, str] | None = None,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            endpoint: Remote hosted inference endpoint URL.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            url_path: Dot-separated path into the polled response body that
                locates the rendered video URL (e.g. ``"video.url"``).
                Empty (default) leaves ``Artifact.url == ""``.
            asset_paths: Optional mapping from role name to dot-path in
                the request body where the matching asset's URI is
                written (e.g. ``{"init_image": "input.image_url"}``).
                Roles absent from this map are not injected.  URL
                passthrough only — ``submit`` never fetches the asset
                bytes; the hosted provider fetches the URL.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._endpoint = endpoint
        self._probe = probe_profile
        self._sleep = sleep
        self._url_path = url_path
        self._asset_paths: dict[str, str] = dict(asset_paths or {})

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
        """POST the job spec (with asset URIs injected) to the hosted endpoint.

        For each role declared in ``self._asset_paths``, look up the
        corresponding asset on ``job.segments[0]`` via
        :func:`~kinoforge.core.assets.find_asset` and write
        ``asset.ref.uri`` into a copy of the request body at the
        configured dot-path via
        :func:`~kinoforge.core.assets.set_by_dot_path`.  Roles absent
        from ``segments[0].assets`` are silently skipped.  ``job.spec``
        itself is never mutated.

        URL passthrough only — no asset bytes are fetched here; the
        hosted provider fetches the URI.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is the request body.

        Returns:
            The ``job_id`` (or task id) string from the API response.
        """
        body = dict(job.spec)
        for role, dot_path in self._asset_paths.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            set_by_dot_path(body, dot_path, asset.ref.uri)
        response = self._http_post(self._endpoint, body)
        return str(response["job_id"])

    def result(self, job_id: str) -> Artifact:
        """Poll the hosted endpoint for completion and return the produced Artifact.

        Polls at most :data:`_MAX_POLL` times, sleeping between iterations
        using the injected *sleep* callable.  The status URL is built by
        appending ``/status/<job_id>`` to the configured endpoint.

        Args:
            job_id: The job ID returned by a prior ``submit`` call.

        Returns:
            An ``Artifact`` whose ``filename`` comes from the API response
            and whose ``meta`` contains ``{"job_id": job_id}``.

        Raises:
            TimeoutError: The hosted API did not return ``status == "done"``
                within the poll limit.
        """
        status_url = f"{self._endpoint.rstrip('/')}/status/{job_id}"
        for _ in range(_MAX_POLL):
            data = self._http_get(status_url)
            if data.get("status") == "done":
                filename = str(data.get("filename", ""))
                artifact_url = _walk_dot_path(data, self._url_path)
                return Artifact(
                    filename=filename, url=artifact_url, meta={"job_id": job_id}
                )
            self._sleep(1.0)
        raise TimeoutError(
            f"Hosted API did not complete job {job_id!r} within {_MAX_POLL} polls"
        )

    def endpoints(self) -> dict[str, str]:
        """Return the hosted endpoint map.

        Returns:
            A dict with ``"inference"`` mapping to the configured endpoint URL.
        """
        return {"inference": self._endpoint}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class HostedAPIEngine(GenerationEngine):
    """Generation engine adapter for a remote hosted inference API.

    No compute instance is required or created.  ``provision`` validates
    credentials and pings the health endpoint; ``backend`` returns a
    :class:`HostedAPIBackend` wired to the configured endpoint.

    All I/O is routed through injected callables (``http_post``, ``http_get``,
    ``sleep``) so tests can spy without real side-effects.

    Class attributes:
        name: Registry key ``"hosted"``.
        requires_compute: ``False`` — no GPU instance is needed.
        requires_local_weights: ``False`` — weights live on the remote service.
    """

    name: str = "hosted"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        creds: CredentialProvider | None = None,
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
            creds: :class:`~kinoforge.core.interfaces.CredentialProvider` used
                to look up the API key during :meth:`provision`.  Defaults to a
                null provider that always returns ``None`` (forcing
                :class:`~kinoforge.core.errors.AuthError`).
            http_post: Callable ``(url, json_body) -> dict`` for HTTP POST.
            http_get: Callable ``(url) -> dict`` for HTTP GET.
            http_get_bytes: Callable ``(url) -> bytes`` for fetching binary
                content (video bytes) by URL.
            ffmpeg_run: Injectable subprocess seam ``(argv, stdin) -> stdout``
                used by :meth:`extract_last_frame`.
            sleep: Sleep callable used between polling iterations in
                :meth:`~HostedAPIBackend.result`.
            probe_profile: ``ModelProfile`` returned by backend capability
                queries.
            declared_flags_map: Optional mapping from
                :meth:`~kinoforge.core.interfaces.CapabilityKey.derive` hex
                strings to ``dict[str, bool]`` strategy-flag dicts.
        """
        self._creds: CredentialProvider = creds or _NullCredentialProvider()
        self._http_post = http_post
        self._http_get = http_get
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
        self._sleep = sleep
        self._probe = probe_profile
        # If the caller passes an explicit map (even {}), respect it.  When
        # omitted, fall back to the shipped default so a fresh hosted run
        # against examples/configs/hosted.yaml does not emit a verify-path
        # WARNING.
        self._declared_flags_map: dict[str, dict[str, bool]] = (
            dict(_DEFAULT_DECLARED_FLAGS_MAP)
            if declared_flags_map is None
            else declared_flags_map
        )
        # Asset-role -> request-body dot-path map, populated by ``backend``
        # from ``cfg["engine"]["hosted"]["asset_paths"]`` and mirrored
        # onto the engine so ``validate_spec`` can check it.
        self._asset_paths: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def key_base(self, cfg: dict[str, Any]) -> str:
        """Return the hosted model ID from *cfg*, used as the CapabilityKey base.

        Args:
            cfg: Runtime configuration dict containing
                ``cfg["engine"]["hosted"]["model"]``.

        Returns:
            The model ID string, e.g. ``"ltx-2"``.
        """
        engine_block = cfg.get("engine", {})
        hosted_cfg: dict[str, Any] = (
            engine_block.get("hosted", {}) if isinstance(engine_block, dict) else {}
        )
        return str(hosted_cfg.get("model", ""))

    # ------------------------------------------------------------------
    # GenerationEngine interface
    # ------------------------------------------------------------------

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        """Validate credentials and ping the health endpoint.

        Unlike compute engines, this method performs NO downloads and NO
        server launch.  The ``instance`` argument must be ``None`` — passing
        a non-None value raises :class:`~kinoforge.core.errors.KinoforgeError`
        because the hosted engine has no compute to configure.

        Validation steps (in order):

        1. Reject non-None instance (hosted skips compute entirely).
        2. Resolve the API key via the injected credential provider; raise
           :class:`~kinoforge.core.errors.AuthError` if absent.
        3. Ping ``cfg["engine"]["hosted"]["health_url"]`` via *http_get*; re-raise
           any exception as :class:`~kinoforge.core.errors.KinoforgeError`
           with the prefix ``"hosted endpoint unreachable: …"``.

        Args:
            instance: Must be ``None``.  Non-None raises
                :class:`~kinoforge.core.errors.KinoforgeError`.
            cfg: Runtime configuration dict.

        Raises:
            KinoforgeError: ``instance`` is not ``None``, or the health ping
                failed.
            AuthError: The API key env-var is unset.
        """
        if instance is not None:
            raise KinoforgeError(
                "HostedAPIEngine.provision: instance must be None "
                "(hosted engine has no compute to configure)"
            )

        engine_block = cfg.get("engine", {})
        hosted_cfg: dict[str, Any] = (
            engine_block.get("hosted", {}) if isinstance(engine_block, dict) else {}
        )

        # Step 1 — credential check.
        key_name: str = str(hosted_cfg.get("api_key_env", ""))
        if not key_name:
            raise AuthError(
                "engine.hosted.api_key_env is empty — set the env var name in your config"
            )
        cred = self._creds.get(key_name)
        if cred is None:
            raise AuthError(f"engine.hosted.api_key_env={key_name!r} is not set in env")

        # Step 2 — health ping.
        health_url: str = str(hosted_cfg.get("health_url", ""))
        try:
            self._http_get(health_url)
        except Exception as exc:
            raise KinoforgeError(f"hosted endpoint unreachable: {exc}") from exc

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> HostedAPIBackend:
        """Return a :class:`HostedAPIBackend` wired to this engine's injected callables.

        The endpoint URL is taken from ``cfg["engine"]["hosted"]["endpoint"]``.

        Args:
            instance: Ignored (no compute for hosted engines).
            cfg: Runtime configuration dict.

        Returns:
            A :class:`HostedAPIBackend` ready to accept jobs.
        """
        del instance  # hosted path: no instance needed
        engine_block = cfg.get("engine", {})
        hosted_cfg: dict[str, Any] = (
            engine_block.get("hosted", {}) if isinstance(engine_block, dict) else {}
        )
        endpoint: str = str(hosted_cfg.get("endpoint", ""))
        url_path: str = str(hosted_cfg.get("url_path", ""))
        asset_paths_raw = hosted_cfg.get("asset_paths", {})
        asset_paths: dict[str, str] = (
            {str(k): str(v) for k, v in asset_paths_raw.items()}
            if isinstance(asset_paths_raw, dict)
            else {}
        )
        # Mirror onto the engine so ``validate_spec`` (called from outside
        # via the ABC) can consult the same map.
        self._asset_paths = asset_paths
        return HostedAPIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            endpoint=endpoint,
            probe_profile=self._probe,
            sleep=self._sleep,
            url_path=url_path,
            asset_paths=asset_paths,
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise ``NotImplementedError`` — deferred to the profile provider.

        Args:
            key: Unused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "HostedAPIEngine.profile_for is supplied by ModelProfileProvider"
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
        """Raise :class:`~kinoforge.core.errors.ValidationError` for spec or asset gaps.

        Required spec keys: ``"model"`` (the hosted model ID) and
        ``"params"`` (the API request body parameters).  In addition,
        for every asset in ``job.segments[0].assets``, the asset's role
        must appear in ``self._asset_paths`` (populated from
        ``cfg["engine"]["hosted"]["asset_paths"]`` at backend
        construction).  Roles present without a configured injection
        path are a hard error — silent skip would let the engine submit
        a body missing the conditioning asset URI.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
                ``spec`` is checked.

        Raises:
            ValidationError: ``"model"`` or ``"params"`` is absent from
                ``job.spec``, or an asset role on ``segments[0]`` has no
                entry in ``asset_paths``.
        """
        required = {"model", "params"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"Hosted job.spec is missing required keys: {sorted(missing)}"
            )
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role not in self._asset_paths:
                raise ValidationError(
                    f"asset role {asset.role!r} present on segments[0] but "
                    f"engine.hosted.asset_paths has no mapping; declare "
                    f"asset_paths.{asset.role}: <dot.path> in YAML"
                )

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        The artifact's URL is populated by :meth:`HostedAPIBackend.result`
        from ``cfg["engine"]["hosted"]["url_path"]`` walked over the API
        response body. Providers vary on response shape; configure the
        path per provider.

        Args:
            artifact: A clip Artifact with ``url`` populated.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty (url_path
                unset, missing, or pointed at non-string), or the fetch or
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
# Default declared_flags entry matching the shipped examples/configs/hosted.yaml
# capability key.  Without this entry, JsonProfileCache.discover would log a
# DEBUG (post Layer I Task 2) on the fresh-cache warmup path, and the verify
# path would emit a WARNING the first time a hosted run completes against the
# canonical config.  Populating the default gives the hosted engine declared
# parity with the YAML capability so the verify path stays quiet too.
# ---------------------------------------------------------------------------

_HOSTED_DEFAULT_KEY = CapabilityKey(
    base_model="hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors",
    engine="hosted",
    precision="",
).derive()

_DEFAULT_DECLARED_FLAGS_MAP: dict[str, dict[str, bool]] = {
    _HOSTED_DEFAULT_KEY: {
        "supports_native_extension": False,
        "supports_joint_audio": False,
    },
}


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine(
    "hosted",
    lambda: HostedAPIEngine(declared_flags_map=dict(_DEFAULT_DECLARED_FLAGS_MAP)),
)
