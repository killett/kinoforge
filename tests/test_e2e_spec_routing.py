"""End-to-end test: YAML spec/params blocks round-trip through Orchestrator.

Bug catch: a future refactor that drops the dict(cfg.spec) hand-off at
orchestrator.py would let unit tests in tests/core/test_orchestrator.py still
pass (they mutate cfg.spec at fixture time) while real CLI users with their
YAML on disk would silently see empty job.spec.  This e2e test exercises
the same path the CLI takes: file -> load_config -> generate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

# Self-registration of fake + local adapters.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import (
    GenerationJob,
    GenerationRequest,
    ModelProfile,
)
from kinoforge.core.orchestrator import generate
from kinoforge.engines.fake import FakeEngine
from kinoforge.stores.local import LocalArtifactStore

_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
params:
  fps: 24
  num_frames: 81
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
  params:
    guidance_scale: 5.0
"""


def _profile() -> ModelProfile:
    return ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def test_yaml_spec_params_round_trip_into_job_via_orchestrator(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(_YAML)

    cfg = load_config(yaml_path.read_text())

    # Sanity: load_config preserved the blocks.
    assert cfg.spec == {
        "model": "wan-ai/Wan2.2-T2V-A14B",
        "params": {"guidance_scale": 5.0},
    }
    assert cfg.params == {"fps": 24, "num_frames": 81}

    seen: dict[str, Any] = {}

    class _Spy(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            seen["spec"] = dict(job.spec)
            seen["params"] = dict(job.params)
            super().validate_spec(job)

    engine = _Spy(
        probe_profile=_profile(),
        declared_flags_map={},
        required_spec_keys={"model"},
    )

    store_root = tmp_path / "store"
    store = LocalArtifactStore(store_root)
    request = GenerationRequest(prompt="hello world", mode="t2v")

    with patch(
        "kinoforge.core.registry.get_engine", side_effect=lambda _kind: lambda: engine
    ):
        artifact, _ = generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="e2e-run",
            state_dir=tmp_path,
        )

    # uri may be either a plain path or a file:// URI depending on store impl.
    assert artifact.uri.startswith(str(store_root)) or artifact.uri.startswith(
        store_root.as_uri()
    )
    # The spy observed the real, fully-resolved job.spec (strategy injected
    # _audio_mode; everything else is user-supplied).
    assert seen["spec"]["model"] == "wan-ai/Wan2.2-T2V-A14B"
    assert seen["spec"]["params"] == {"guidance_scale": 5.0}
    assert seen["spec"]["_audio_mode"] == "separate"
    assert seen["params"] == {"fps": 24, "num_frames": 81}
