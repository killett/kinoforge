"""``TextEngine`` for the transformers-backed pod server (design §5.2).

Client + provision fragment only; the model runs in
``engines/diffusers/servers/text_server.py``. Every pod-facing call binds the
port from ``cfg["text"]["port"]`` first, so one engine object can talk to a
server on any port — the seam the hooks spec's sidecar launch needs.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import TextGenerationFailed, ValidationError
from kinoforge.core.interfaces import (
    Instance,
    RenderedProvision,
    TextEngine,
    TextHealth,
    TextJob,
    TextResult,
)
from kinoforge.core.text_request import base_model_ref
from kinoforge.engines._pod_http import PodHTTPClientMixin, http_json, submit_and_poll

_USER_AGENT = "kinoforge-text-transformers/0.1"
_DEFAULT_PORT = 8000


def _port(cfg: dict[str, object]) -> int:
    """Return ``cfg["text"]["port"]``, defaulting to :data:`_DEFAULT_PORT`.

    Args:
        cfg: Runtime configuration dict.

    Returns:
        The configured text-server port, or 8000 when ``text`` is absent or
        not a mapping.
    """
    block = cfg.get("text") or {}
    return (
        int(block.get("port", _DEFAULT_PORT))
        if isinstance(block, dict)
        else _DEFAULT_PORT
    )


class TransformersTextEngine(PodHTTPClientMixin, TextEngine):
    """Pod client for ``text_server.py`` plus its boot fragment."""

    name = "transformers"
    requires_compute = True
    _pod_user_agent = _USER_AGENT

    def __init__(self) -> None:
        """Default to the standard text-server port until ``_bind`` runs."""
        self._port_str = str(_DEFAULT_PORT)

    # -- port seam -----------------------------------------------------------

    def _bind(self, cfg: dict[str, object]) -> None:
        """Remember ``text.port`` for the next ``_base_url`` call.

        Args:
            cfg: Runtime configuration dict.
        """
        self._port_str = str(_port(cfg))

    def _base_url(self, instance: Instance) -> str:
        """Port-aware override of the mixin's default-port lookup.

        Args:
            instance: Compute instance exposing the pod server endpoint.

        Returns:
            Base URL for the bound text port (or the first endpoint as
            fallback), without a trailing slash.

        Raises:
            ValueError: No endpoint is available on the instance.
        """
        endpoints = instance.endpoints or {}
        url = endpoints.get(self._port_str) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"{type(self).__name__}: instance {instance.id} has no endpoint for "
                f"port {self._port_str}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")

    # -- TextEngine ----------------------------------------------------------

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Export the model id and port for ``text_server.py``.

        No pip, no embeds: those are explicit in ``engine.diffusers.pip`` /
        ``embed_files`` like every other config, and the embed-closure test
        enforces the set. The diffusers engine composes these lines in the
        RUNTIME phase (design §5.3).

        Args:
            cfg: Runtime configuration dict.

        Returns:
            A :class:`RenderedProvision` carrying the two ``export`` lines
            and the bound port.
        """
        model_id = base_model_ref(cfg).removeprefix("hf:")
        port = _port(cfg)
        lines = [
            f"export KINOFORGE_TEXT_MODEL_ID={shlex.quote(model_id)}",
            f"export KINOFORGE_TEXT_PORT={port}",
        ]
        return RenderedProvision(
            script="\n".join(lines) + "\n",
            image="",
            ports=[str(port)],
            env_required=["HF_TOKEN"],
        )

    def health(self, instance: Instance | None, cfg: dict[str, object]) -> TextHealth:
        """GET ``/health`` → :class:`TextHealth`.

        Args:
            instance: Compute instance exposing the pod server endpoint.
            cfg: Runtime configuration dict, read for ``text.port``.

        Returns:
            The pod's health as a :class:`TextHealth`.

        Raises:
            ValueError: ``instance`` is ``None``.
        """
        if instance is None:
            raise ValueError("TransformersTextEngine requires a compute instance")
        self._bind(cfg)
        payload = _http_json(method="GET", url=f"{self._base_url(instance)}/health")
        return TextHealth(
            ready=bool(payload.get("ready")),
            model=str(payload.get("model", "")),
            supported_modes=frozenset(
                str(m) for m in payload.get("supported_modes", [])
            ),
        )

    def upload_image(
        self, instance: Instance | None, local_path: Path, cfg: dict[str, object]
    ) -> str:
        """PUT ``/upload`` (sha256 cross-checked by the mixin); return the pod path.

        Args:
            instance: Compute instance exposing the pod server endpoint.
            local_path: Local image file to upload.
            cfg: Runtime configuration dict, read for ``text.port``.

        Returns:
            The pod-side path of the uploaded image (no ``file://`` prefix).

        Raises:
            ValueError: ``instance`` is ``None``.
        """
        if instance is None:
            raise ValueError("TransformersTextEngine requires a compute instance")
        self._bind(cfg)
        return self._upload_source(
            instance, Path(local_path), media="image"
        ).removeprefix("file://")

    def complete(
        self,
        instance: Instance | None,
        job: TextJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> TextResult:
        """POST ``/text``, poll ``/text/status/{id}``, map ``result``.

        Args:
            instance: Compute instance exposing the pod server endpoint.
            job: The text-generation job to submit.
            cfg: Runtime configuration dict, read for ``text.port``.
            cancel_token: Optional cooperative-cancellation token.

        Returns:
            The completed job's :class:`TextResult`.

        Raises:
            ValueError: ``instance`` is ``None``.
            TextGenerationFailed: The pod reported ``state == "error"``.
        """
        self.validate_spec(job)
        if instance is None:
            raise ValueError("TransformersTextEngine requires a compute instance")
        self._bind(cfg)
        payload: dict[str, Any] = {
            "prompt": job.prompt,
            "system": job.system,
            "images": list(job.images),
            "params": dict(job.params),
        }
        result, elapsed_s = submit_and_poll(
            label_prefix="text",
            base_url=self._base_url(instance),
            endpoint="/text",
            payload=payload,
            http_json=_http_json,
            make_error=lambda job_id, server_error: TextGenerationFailed(
                job_id, str(server_error)
            ),
            cancel_token=cancel_token,
        )
        usage_raw = result.get("usage") or {}
        return TextResult(
            text=str(result["text"]),
            finish_reason=str(result.get("finish_reason", "")),
            usage={str(k): int(v) for k, v in dict(usage_raw).items()},
            model=str(result.get("model", "")),
            elapsed_s=elapsed_s,
        )

    def validate_spec(self, job: TextJob) -> None:
        """Refuse an empty prompt or non-mapping params before any HTTP.

        Args:
            job: The text-generation job to validate.

        Raises:
            ValidationError: The prompt is empty/whitespace, or ``params``
                is not a mapping.
        """
        if not job.prompt.strip():
            raise ValidationError("text: prompt is empty")
        if not isinstance(job.params, dict):
            raise ValidationError("text: params must be a mapping")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """The repo tail of the base ref (``Qwen3-0.6B``); ``""`` when absent.

        Args:
            cfg: Runtime configuration dict.

        Returns:
            The last path segment of the ``hf:`` base ref, or ``""`` when the
            config has no ``kind: base`` model entry.
        """
        try:
            ref = base_model_ref(cfg)
        except ValidationError:
            return ""
        return ref.removeprefix("hf:").rstrip("/").rsplit("/", 1)[-1]


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Module-level seam (tests patch it) delegating to the shared helper.

    Args:
        method: HTTP method (``"GET"`` / ``"POST"``).
        url: Full endpoint URL.
        payload: JSON body to send; ``None`` sends no body.

    Returns:
        Decoded JSON response object.
    """
    return http_json(method=method, url=url, payload=payload, user_agent=_USER_AGENT)


registry.register_text_engine("transformers", TransformersTextEngine)
