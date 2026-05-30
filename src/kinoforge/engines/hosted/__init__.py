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

from kinoforge.core import registry
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
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            endpoint: Remote hosted inference endpoint URL.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._endpoint = endpoint
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
        """POST the job spec to the hosted endpoint and return the remote job ID.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is sent as the request body.

        Returns:
            The ``job_id`` (or task id) string from the API response.
        """
        response = self._http_post(self._endpoint, dict(job.spec))
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
                return Artifact(filename=filename, meta={"job_id": job_id})
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
        self._sleep = sleep
        self._probe = probe_profile
        self._declared_flags_map: dict[str, dict[str, bool]] = declared_flags_map or {}

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
        cred = self._creds.get(key_name)
        if cred is None:
            raise AuthError(f"missing {key_name}")

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
        return HostedAPIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            endpoint=endpoint,
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
        """Raise :class:`~kinoforge.core.errors.ValidationError` when spec keys are missing.

        Both ``"model"`` (the hosted model ID) and ``"params"`` (the API
        request body parameters) are required keys on a hosted job spec.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
                ``spec`` is checked.

        Raises:
            ValidationError: ``"model"`` or ``"params"`` is absent from
                ``job.spec``.
        """
        required = {"model", "params"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"Hosted job.spec is missing required keys: {sorted(missing)}"
            )


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("hosted", lambda: HostedAPIEngine())
