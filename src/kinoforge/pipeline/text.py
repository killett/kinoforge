"""TextStage — the terminal stage behind ``kinoforge text`` (design §7.1).

Order is the contract: **health, then upload, then submit.** The pod's
``/health`` is the only place the model's TRUE modes are known (the diffusers
probe never reads the pod — design §4.3), so the stage checks it before a
single image byte moves. Stores ``response.txt`` + ``response.json`` under the
run id, publishes both to the sink, and returns the state with
``artifacts["text"]`` set; the orchestrator returns that artifact without
re-publishing it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge import __version__
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    PipelineState,
    TextEngine,
    TextJob,
)
from kinoforge.core.text_request import base_model_ref, declared_modes
from kinoforge.outputs.base import OutputSink
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)

# kinoforge:public-name — fixed identifiers, not prompt-derived (image_run precedent).
_STORE_TEXT = "response.txt"  # kinoforge:public-name
_STORE_JSON = "response.json"  # kinoforge:public-name


def _local_path(artifact: Artifact) -> Path:
    """Resolve a ``--image`` artifact to a local path; URLs are not inputs here."""
    uri = artifact.uri
    if uri.startswith(("http://", "https://")):
        raise ValidationError(f"text: --image must be a local file, got URL {uri!r}")
    return Path(uri.removeprefix("file://"))


@dataclass
class TextStage:
    """A Stage that runs one chat completion on the session's pod.

    Attributes:
        engine: Configured ``TextEngine`` (registry-resolved).
        instance: The booked pod; ``None`` only for in-process fakes.
        cfg: Runtime config dict (``_cfg_dict(cfg)``); ``cfg["text"]`` is read.
        store: Where ``response.txt`` / ``response.json`` land under ``run_id``.
        sink: User-facing publish seam, or ``None`` for ``--no-output-dir``.
        run_id: The run id (also written into the sidecar).
        cancel_token: Threaded into ``engine.complete``.
    """

    engine: TextEngine
    instance: Instance | None
    cfg: dict[str, Any]
    store: ArtifactStore
    sink: OutputSink | None
    run_id: str
    cancel_token: CancelToken | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Health-gate, upload, complete, store, publish; return the new state."""
        request = state.request
        block: dict[str, Any] = dict(self.cfg.get("text") or {})
        local_paths = [
            _local_path(asset.ref) for asset in request.assets
        ]  # refuses URLs first

        # 1. Health gate BEFORE any bytes move (design §4.2).
        health = self.engine.health(self.instance, self.cfg)
        if request.mode not in health.supported_modes:
            raise ValidationError(
                f"text: the pod's model {health.model!r} serves modes "
                f"{sorted(health.supported_modes)} but the request needs "
                f"{request.mode!r}; the config declared "
                f"{sorted(declared_modes(self.cfg))}. Fix "
                "capability.supported_modes to match the checkpoint, or drop --image."
            )

        # 2. Upload each image in role order; keep the pairing for the sidecar.
        uploaded: list[dict[str, Any]] = []
        for asset, local in zip(request.assets, local_paths, strict=True):
            pod_path = self.engine.upload_image(self.instance, local, self.cfg)
            uploaded.append(
                {
                    "role": asset.role,
                    "local": str(local),
                    "sha256": asset.ref.sha256,
                    "pod_path": pod_path,
                }
            )

        # 3. Complete.
        job = TextJob(
            prompt=request.prompt,
            system=block.get("system"),
            images=tuple(u["pod_path"] for u in uploaded),
            params=dict(block.get("params") or {}),
        )
        self.engine.validate_spec(job)
        result = self.engine.complete(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )

        # 4. Store + publish.
        provider = str(block.get("engine") or self.engine.name)
        sidecar = {
            "prompt": request.prompt,
            "system": job.system,
            "mode": request.mode,
            "images": uploaded,
            "model": base_model_ref(self.cfg),
            "engine": provider,
            "params": job.params,
            "text": result.text,
            "usage": result.usage,
            "finish_reason": result.finish_reason,
            "elapsed_s": result.elapsed_s,
            "run_id": self.run_id,
            "kinoforge_version": __version__,
            "instance_id": self.instance.id if self.instance is not None else None,
        }
        text_bytes = result.text.encode("utf-8")
        json_bytes = json.dumps(sidecar, indent=2, sort_keys=True).encode("utf-8")
        stored_txt = self.store.put_bytes(self.run_id, _STORE_TEXT, text_bytes)
        stored_json = self.store.put_bytes(self.run_id, _STORE_JSON, json_bytes)
        published: str | None = None
        if self.sink is not None:
            model = self.engine.model_identity(self.cfg)
            published = self.sink.publish(
                text_bytes,
                prompt=request.prompt,
                extension=".txt",
                provider=provider,
                model=model,
                kind="text",
            )
            self.sink.publish(
                json_bytes,
                prompt=request.prompt,
                extension=".json",
                provider=provider,
                model=model,
                kind="text",
            )
            _log.info("text published: %s", published)
        artifact = replace(
            stored_txt,
            meta={
                **stored_txt.meta,
                "text": result.text,
                "json_uri": stored_json.uri,
                "published": published,
            },
        )
        return replace(state, artifacts={**state.artifacts, "text": artifact})
