"""FlashVSREngine: render_provision layout + HTTP dispatch shape."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.upscalers.flashvsr._engine import FlashVSREngine

_DEFAULT_BSA_WHEEL_URL = (
    "https://github.com/killett/kinoforge-artifacts/releases/download/"
    "bsa-cu128-torch2.8-v1/"
    "block_sparse_attn-0.0.1+cu128torch2.8-cp311-cp311-linux_x86_64.whl"
)


def _cfg(
    precision: str = "fp16",
    long_video: bool = False,
    bsa_wheel_url: str = _DEFAULT_BSA_WHEEL_URL,
) -> dict[str, Any]:
    return {
        "upscale": {
            "engine": "flashvsr",
            "scale": "2x",
            "flashvsr": {
                "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
                "precision": precision,
                "window_size": 24,
                "tile_size": 0,
                "long_video_mode": long_video,
                "bsa_wheel_url": bsa_wheel_url,
            },
        }
    }


def test_model_identity_shape() -> None:
    """RED: three-token slug shape (server parse contract).

    Bug caught: emitting ``flashvsr-fp16`` (two tokens) breaks the server's
    ``parts[-2], parts[-1]`` slug parser.
    """
    e = FlashVSREngine()
    assert e.model_identity(_cfg()) == "flashvsr-wan21-fp16"
    assert e.model_identity(_cfg(precision="fp32")) == "flashvsr-wan21-fp32"


def test_render_provision_step_order() -> None:
    """RED: SM80+ guard first, BSA wheel fetched + installed before FlashVSR,
    HF_HUB_OFFLINE tail.

    Bug caught: FlashVSR pip-installed before BSA — its setup.py may
    shadow-import a stub kernel and never notice BSA is missing.
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    script = rp.script

    guard_pos = script.find("torch.cuda.get_device_capability")
    curl_pos = script.find("curl -L -f")
    bsa_install_pos = script.find("pip install --no-deps /tmp/bsa.whl")
    fvsr_pos = script.find("OpenImagingLab/FlashVSR")
    fetch_pos = script.find("_fetch_weights")
    offline_pos = script.find("HF_HUB_OFFLINE=1")

    assert (
        0 <= guard_pos < curl_pos < bsa_install_pos < fvsr_pos < fetch_pos < offline_pos
    )


def test_render_provision_has_sm80_exit_87() -> None:
    """RED: guard uses exit 87 (documented UnsupportedGpuArch code).

    Bug caught: exit 1 conflates with generic pod-boot failure.
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    assert "|| exit 87" in rp.script


def test_render_provision_uses_prebuilt_wheel_not_git_source() -> None:
    """RED: BSA is installed from a prebuilt ``.whl`` fetched by curl —
    NEVER from ``git+https://.../Block-Sparse-Attention``.

    Bug caught: silent regression to ``pip install git+...@3453bbb1`` would
    reintroduce the 25-45 min nvcc compile that motivated T7.5 in the first
    place. Positive-check + explicit blacklist together — the positive-only
    check would still pass if someone left the git-install line in as a
    "fallback".
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    script = rp.script

    assert "curl -L -f -o /tmp/bsa.whl" in script
    assert "pip install --no-deps /tmp/bsa.whl" in script

    forbidden = [
        "git+https://github.com/mit-han-lab/Block-Sparse-Attention",
        "3453bbb1",
        "TORCH_EXTENSIONS_DIR",
        "MAX_JOBS",
    ]
    for token in forbidden:
        assert token not in script, (
            f"source-compile regression: {token!r} still in provision script"
        )


def test_render_provision_threads_bsa_wheel_url_from_cfg() -> None:
    """RED: engine reads ``bsa_wheel_url`` from cfg, not from a hardcoded const.

    Bug caught: engine ignores cfg override → CI or GitHub-release fallback
    URL never fires; every pod still curls the default HF Hub URL even when
    the cfg pins a mirror.
    """
    e = FlashVSREngine()
    custom = "https://internal.mirror.example/wheels/bsa-cu128-torch2.8.whl"
    rp = e.render_provision(_cfg(bsa_wheel_url=custom))
    assert custom in rp.script
    assert "killett/kinoforge-artifacts" not in rp.script, (
        "default URL leaked into script when cfg override was set"
    )


def test_render_provision_threads_include_long_video_flag() -> None:
    """RED: long_video_mode cfg → --include-long-video 1 in the fetch call."""
    e = FlashVSREngine()
    rp_lite = e.render_provision(_cfg(long_video=False))
    rp_full = e.render_provision(_cfg(long_video=True))
    assert "--include-long-video 0" in rp_lite.script
    assert "--include-long-video 1" in rp_full.script


def test_render_provision_env_required_and_size() -> None:
    """RED: HF_TOKEN required; script fits in bootstrap env ceiling.

    Bug caught: script size drift busts the 64 KB RunPod env-var ceiling
    (P2 discovery); test enforces < 12 KB with generous headroom.
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    assert rp.env_required == ["HF_TOKEN"]
    assert len(rp.script.encode()) < 12 * 1024


def test_validate_spec_rejects_height() -> None:
    """RED: engine-side rejection for height target (defense-in-depth)."""
    e = FlashVSREngine()
    with pytest.raises(NotYetImplementedError):
        e.validate_spec(
            UpscaleJob(
                source=Artifact(uri="file:///tmp/in.mp4"),
                scale=ScaleTarget(kind="height", value=1080),
            )
        )


def test_upscale_uploads_local_source_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: file:// source triggers _upload_source before /upscale POST.

    Bug caught: skipping upload → pod's _download_to_local_temp reads a
    path that doesn't exist on the pod (P2 T15/T16 blocker).
    """
    from pathlib import Path

    inst = Instance(
        id="pod-abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )
    e = FlashVSREngine()

    upload_calls: list[Path] = []

    def fake_upload(instance: Instance, path: Path) -> str:
        upload_calls.append(path)
        return "file:///workspace/uploads/abc123.mp4"

    monkeypatch.setattr(e, "_upload_source", fake_upload)

    submit_body: dict[str, Any] = {}

    def fake_http(
        *, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST" and url.endswith("/upscale"):
            submit_body.update(payload or {})
            return {"job_id": "j-1"}
        return {
            "state": "done",
            "result": {
                "filename": "out.mp4",
                "sha256": "0" * 64,
                "size": 100,
                "input_resolution": [720, 480],
                "output_resolution": [1440, 960],
                "engine_meta": {},
            },
        }

    monkeypatch.setattr("kinoforge.upscalers.flashvsr._engine._http_json", fake_http)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    e.upscale(
        instance=inst,
        job=UpscaleJob(
            source=Artifact(uri="file:///workspace/output/in.mp4"),
            scale=ScaleTarget(kind="factor", value=2.0),
        ),
        cfg=_cfg(),
    )

    assert len(upload_calls) == 1
    assert submit_body["engine"] == "flashvsr"
    assert submit_body["flashvsr"]["precision"] == "fp16"
    assert submit_body["source_url"].startswith("file:///workspace/uploads/")


def test_upscale_polls_until_done_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: polls status until state == 'done', returns UpscaleResult with dims.

    Bug caught: single-shot status poll silently accepts state='running'
    as 'done' when the response schema drifts.
    """
    inst = Instance(
        id="pod-abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )
    e = FlashVSREngine()

    poll_count = {"n": 0}

    def fake_http(
        *, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            return {"job_id": "j-1"}
        poll_count["n"] += 1
        if poll_count["n"] < 3:
            return {"state": "running", "progress": 0.5}
        return {
            "state": "done",
            "result": {
                "filename": "out.mp4",
                "sha256": "0" * 64,
                "size": 200,
                "input_resolution": [1280, 720],
                "output_resolution": [2560, 1440],
                "engine_meta": {"elapsed_s_gpu": 12.5},
            },
        }

    monkeypatch.setattr("kinoforge.upscalers.flashvsr._engine._http_json", fake_http)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        e, "_upload_source", lambda *a, **k: "file:///workspace/uploads/x.mp4"
    )

    result = e.upscale(
        instance=inst,
        job=UpscaleJob(
            source=Artifact(uri="file:///tmp/in.mp4"),
            scale=ScaleTarget(kind="factor", value=2.0),
        ),
        cfg=_cfg(),
    )
    assert result.input_resolution == (1280, 720)
    assert result.output_resolution == (2560, 1440)
    assert poll_count["n"] >= 3


def test_upscale_raises_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED: state=='error' → UpscaleFailed with server_error message.

    Bug caught: silent swallow of server error → orchestrator treats
    an empty result as success and sinks a zero-byte MP4.
    """
    from kinoforge.core.errors import UpscaleFailed

    inst = Instance(
        id="pod-abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )
    e = FlashVSREngine()

    def fake_http(
        *, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            return {"job_id": "j-1"}
        return {"state": "error", "error": "CUDA OOM in stream_upscale"}

    monkeypatch.setattr("kinoforge.upscalers.flashvsr._engine._http_json", fake_http)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        e, "_upload_source", lambda *a, **k: "file:///workspace/uploads/x.mp4"
    )

    with pytest.raises(UpscaleFailed, match="CUDA OOM"):
        e.upscale(
            instance=inst,
            job=UpscaleJob(
                source=Artifact(uri="file:///tmp/in.mp4"),
                scale=ScaleTarget(kind="factor", value=2.0),
            ),
            cfg=_cfg(),
        )
