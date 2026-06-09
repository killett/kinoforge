"""AWS Bedrock generic video-generation engine.

Talks to Bedrock's async-invocation video API via boto3 bedrock-runtime,
authed by the Layer 1 :class:`~kinoforge.core.auth.AWSSigV4` strategy.

``boto3`` is lazy-imported inside :func:`_default_session_factory` to
preserve the core-import-ban invariant (see
``tests/test_core_invariant.py``); tests inject a fake session factory.

The engine takes a YAML-supplied ``model_input_template`` dict where
``"${PROMPT}"`` is recursively substituted with the actual prompt at
submit time.  This lets a single engine handle Nova Reel, Luma Ray v2,
and any future Bedrock video model — new providers are config-only.

Self-registers under the engine name ``"bedrock_video"`` on module import.
"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable
from typing import Any

from kinoforge.core import registry
from kinoforge.core.auth import AuthStrategy, AWSSigV4
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.prompt_routing import resolve_prompt

# Default no-op ModelProfile until ModelProfileProvider resolves the real one.
_DEFAULT_STUB_PROFILE = ModelProfile(
    name="bedrock-video-stub",
    fps=24,
    max_frames=150,  # ~5s @ 30fps or ~6s @ 24fps
    max_resolution=(1280, 720),
    supported_modes={"t2v"},
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _default_session_factory(**kwargs: Any) -> Any:  # noqa: ANN401
    """Build a real boto3 Session — lazy-imported only when called.

    Tests inject a fake factory so this never fires under unit test.

    Args:
        **kwargs: Passed verbatim to ``boto3.Session()``.

    Returns:
        A ``boto3.Session`` instance.
    """
    import boto3  # noqa: PLC0415 — lazy: tests inject a fake and never trip this

    return boto3.Session(**kwargs)


def _substitute_prompt(template: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Recursively deep-copy *template* and replace every ``"${PROMPT}"`` value.

    Walks the template dict recursively.  Any string value that equals
    ``"${PROMPT}"`` is replaced with *prompt*.  Other value types are
    preserved untouched.  Uses :func:`copy.deepcopy` to avoid mutating
    the caller's config.

    Args:
        template: The ``model_input_template`` dict from the YAML config.
        prompt: The resolved prompt string to substitute in.

    Returns:
        A new dict with all ``"${PROMPT}"`` occurrences replaced.
    """

    def _walk(obj: Any) -> Any:  # noqa: ANN401
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item) for item in obj]
        if obj == "${PROMPT}":
            return prompt
        return obj

    result: dict[str, Any] = _walk(copy.deepcopy(template))
    return result


class BedrockVideoBackend(GenerationBackend):
    """Backend that talks to Bedrock async-invoke for any Bedrock video model.

    Attributes:
        _client: bedrock-runtime client (real or test-double).
        _cfg: the kinoforge runtime config dict.
        _inflight: ``{job_id: invocationArn}`` populated by :meth:`submit`.
        _sleep: poll-sleep seam.
        _poll_backoff_s: sleep durations between polls (caps at the last value).
    """

    _poll_backoff_s: tuple[float, ...] = (2.0, 4.0, 8.0, 8.0)

    def __init__(
        self,
        *,
        client: Any,  # noqa: ANN401
        cfg: dict[str, Any],
        sleep: Callable[[float], None] = time.sleep,
        profile: ModelProfile = _DEFAULT_STUB_PROFILE,
    ) -> None:
        """Initialise with client, config, and optional injectable seams.

        Args:
            client: bedrock-runtime client (real boto3 client or test double).
            cfg: kinoforge runtime config dict.
            sleep: Sleep callable threaded into the poll loop.
            profile: ``ModelProfile`` returned by capability queries.
        """
        self._client = client
        self._cfg = cfg
        self._inflight: dict[str, str] = {}
        self._sleep = sleep
        self._profile = profile

    # ------------------------------------------------------------------
    # GenerationBackend interface
    # ------------------------------------------------------------------

    def capabilities(self) -> ModelProfile:
        """Return the stub ModelProfile.

        Returns:
            The current :class:`ModelProfile`.
        """
        return self._profile

    def inspect_capabilities(self) -> ModelProfile:
        """Return the stub ModelProfile (no live probe).

        Returns:
            The current :class:`ModelProfile`.
        """
        return self._profile

    def endpoints(self) -> dict[str, str]:
        """Return the Bedrock async-invoke endpoint identifier.

        Returns:
            ``{"bedrock": "bedrock-runtime"}``
        """
        return {"bedrock": "bedrock-runtime"}

    def submit(self, job: GenerationJob) -> str:
        """Invoke Bedrock async and return an opaque job ID.

        Calls ``bedrock_runtime.start_async_invoke`` with the model input
        derived from ``cfg.engine.bedrock_video.model_input_template``
        after substituting ``"${PROMPT}"`` with the resolved prompt.
        The returned ``invocationArn`` is stored keyed by the generated
        job ID.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` to run.

        Returns:
            An opaque job-ID string; pass to :meth:`result` to poll.
        """
        bv_cfg = self._cfg["engine"]["bedrock_video"]
        prompt = resolve_prompt(job) or ""
        model_input = _substitute_prompt(bv_cfg["model_input_template"], prompt)
        output_cfg: dict[str, Any] = {
            "s3OutputDataConfig": {"s3Uri": bv_cfg["output_s3_uri"]}
        }
        if bv_cfg.get("output_kms_key_id"):
            output_cfg["s3OutputDataConfig"]["kmsKeyId"] = bv_cfg["output_kms_key_id"]
        resp = self._client.start_async_invoke(
            modelId=bv_cfg["model_id"],
            modelInput=model_input,
            outputDataConfig=output_cfg,
        )
        job_id = str(uuid.uuid4())
        self._inflight[job_id] = resp["invocationArn"]
        return job_id

    def result(self, job_id: str) -> Artifact:
        """Poll ``get_async_invoke`` until status is Completed or Failed.

        Args:
            job_id: The opaque job ID returned by :meth:`submit`.

        Returns:
            An :class:`~kinoforge.core.interfaces.Artifact` with
            ``uri`` set to
            ``{output_s3_uri}/{invocation_id}/output.mp4``.

        Raises:
            KinoforgeError: If ``job_id`` was not submitted, the invocation
                failed, or the poll loop exhausted.
        """
        arn = self._inflight.get(job_id)
        if arn is None:
            raise KinoforgeError(
                f"bedrock_video job {job_id!r} not found — was submit() called?"
            )
        # Build a finite poll sequence with a bounded backoff that caps.
        backoff_iter = iter(self._poll_backoff_s + (self._poll_backoff_s[-1],) * 50)
        for sleep_s in backoff_iter:
            status_resp = self._client.get_async_invoke(invocationArn=arn)
            status = status_resp.get("status")
            if status == "Completed":
                invocation_id = arn.rsplit("/", 1)[-1]
                prefix = self._cfg["engine"]["bedrock_video"]["output_s3_uri"].rstrip(
                    "/"
                )
                return Artifact(
                    uri=f"{prefix}/{invocation_id}/output.mp4",
                    filename="output.mp4",
                )
            if status == "Failed":
                raise KinoforgeError(
                    f"Bedrock video invocation failed: "
                    f"{status_resp.get('failureMessage', 'no message')}"
                )
            self._sleep(sleep_s)
        raise KinoforgeError(f"Bedrock video poll loop exhausted for {arn!r}")


class BedrockVideoEngine(GenerationEngine):
    """Engine adapter for any AWS Bedrock async-video model.

    No GPU instance is required; credentials are provided by the Layer 1
    :class:`~kinoforge.core.auth.AWSSigV4` strategy via
    ``auth.client_kwargs()``.

    Class attributes:
        name: Registry key ``"bedrock_video"``.
        requires_compute: ``False`` — no GPU instance needed.
        requires_local_weights: ``False`` — weights live on Bedrock.
    """

    name: str = "bedrock_video"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        auth_strategy: AuthStrategy | None = None,
        boto3_session_factory: Callable[..., Any] = _default_session_factory,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_STUB_PROFILE,
    ) -> None:
        """Initialise with optional injection seams.

        Args:
            auth_strategy: Layer 1 AuthStrategy; defaults to
                :class:`~kinoforge.core.auth.AWSSigV4` with ``region_name``
                resolved from ``cfg`` at provision time.
            boto3_session_factory: Callable returning a boto3.Session-like
                object. Tests inject a fake.
            sleep: Sleep callable threaded into :class:`BedrockVideoBackend`.
            probe_profile: Stub :class:`ModelProfile` returned by the
                backend until the real profile is resolved.
        """
        self._auth: AuthStrategy | None = auth_strategy
        self._session_factory = boto3_session_factory
        self._sleep = sleep
        self._probe = probe_profile
        self._client: Any = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_auth(self, cfg: dict[str, Any]) -> AuthStrategy:
        """Return the configured auth strategy, defaulting to AWSSigV4.

        Args:
            cfg: Runtime config dict.

        Returns:
            An :class:`~kinoforge.core.auth.AuthStrategy` instance.
        """
        if self._auth is not None:
            return self._auth
        region = cfg["engine"]["bedrock_video"]["region_name"]
        return AWSSigV4(region_name=region, service_name="bedrock-runtime")

    # ------------------------------------------------------------------
    # GenerationEngine interface
    # ------------------------------------------------------------------

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        """Build the bedrock-runtime client after health-checking credentials.

        ``instance`` must be ``None``; Bedrock video is a hosted API.

        Args:
            instance: Must be ``None``; raises :class:`KinoforgeError` otherwise.
            cfg: Runtime config dict.

        Raises:
            KinoforgeError: ``instance`` is not ``None``.
            AuthError: Credentials are absent or the health check fails.
        """
        if instance is not None:
            raise KinoforgeError(
                "BedrockVideoEngine.provision: instance must be None (requires_compute=False)"
            )
        auth = self._resolve_auth(cfg)
        if not auth.credentials_present():
            raise AuthError(
                f"bedrock_video: credentials not present (strategy={type(auth).__name__})"
            )
        health = auth.health_check()
        if not health.ok:
            raise AuthError(f"bedrock_video: health check failed — {health.reason}")
        session = self._session_factory(**auth.client_kwargs())
        region = cfg["engine"]["bedrock_video"]["region_name"]
        self._client = session.client("bedrock-runtime", region_name=region)
        self._auth = auth

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> BedrockVideoBackend:
        """Return a :class:`BedrockVideoBackend` wired to the provisioned client.

        Args:
            instance: Ignored (no compute for Bedrock video).
            cfg: Runtime config dict.

        Returns:
            A :class:`BedrockVideoBackend` ready to accept jobs.

        Raises:
            KinoforgeError: :meth:`provision` has not been called.
        """
        if self._client is None:
            raise KinoforgeError("BedrockVideoEngine.backend called before provision()")
        return BedrockVideoBackend(
            client=self._client, cfg=cfg, sleep=self._sleep, profile=self._probe
        )

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        """Raise :class:`NotImplementedError` — deferred to ModelProfileProvider.

        Args:
            key: Unused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "BedrockVideoEngine.profile_for is supplied by ModelProfileProvider"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        """Return ``{}`` — no declared flags for Bedrock video.

        Args:
            key: Unused.

        Returns:
            An empty dict.
        """
        return {}

    def validate_spec(self, job: GenerationJob) -> None:
        """No-op spec validation — Bedrock video only requires a prompt.

        Args:
            job: The :class:`~kinoforge.core.interfaces.GenerationJob` to check.
        """

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Bedrock identity is the Bedrock model id (e.g. ``luma.ray-v2:0``)."""
        engine_block = cfg.get("engine", {})
        if not isinstance(engine_block, dict):
            return ""
        bv_block = engine_block.get("bedrock_video", {})
        if not isinstance(bv_block, dict):
            return ""
        return str(bv_block.get("model_id", "") or "")


# ---------------------------------------------------------------------------
# Module-level self-registration
# ---------------------------------------------------------------------------

registry.register_engine("bedrock_video", lambda: BedrockVideoEngine())
