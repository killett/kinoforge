"""Behavior: the MiniMax-H3 t2va server's HTTP contract, loading recipe and mux.

Everything here runs offline against the in-memory pipeline stub in
``servers/h3_stub_pipe.py``, installed through ``KINOFORGE_H3_LOAD_STUB``. The
things worth testing are the ones that cost money to get wrong live:

* the ``workflow="t2va"`` kwarg — the diffusers blocks say in terms that without
  it ``load_components`` pulls **both** 61.7 GiB transformer partitions, and
  ``transformer_ref/`` is deliberately not on the Modal Volume, so the miss is a
  66 GB download on an H200 at $4.54/hr;
* the geometry gate — every violation is a ``ValueError`` raised INSIDE the
  pipeline call, i.e. minutes after a 123.8 GiB load finished on a booked card;
* the audio reaching the mux in the layout ``_av_io`` documents — a silent video
  is a "successful" run that passes every exit-code and ffprobe-dimension check,
  which is the FlashVSR lesson with sound instead of pixels.

Jobs are run by the server's real worker thread, not by a test-only drain seam,
so the threading the pod actually uses is what is under test.
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.engines.diffusers.servers import h3_stub_pipe

_STUB = "tests.engines.diffusers.servers.h3_stub_pipe.stub_loader"


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the H3 server module wired to the stub loader.

    Args:
        tmp_path: Artifact directory for the run.
        monkeypatch: Environment patcher.

    Returns:
        The freshly reloaded server module.
    """
    monkeypatch.setenv("KINOFORGE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("KINOFORGE_H3_LOAD_STUB", _STUB)
    monkeypatch.setenv("WAN_MODEL_ID", "MiniMaxAI/MiniMax-H3")
    h3_stub_pipe.STATE.clear()

    from kinoforge.engines.diffusers.servers import minimax_h3_server as mod

    importlib.reload(mod)
    yield mod
    h3_stub_pipe.STATE.clear()


def _wait_done(client: Any, job_id: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """Poll ``/status`` until the job leaves the running states.

    Args:
        client: The test client.
        job_id: The job to wait on.
        timeout_s: Deadline; the stub pipeline returns instantly, so reaching it
            means the worker thread is wedged.

    Returns:
        The terminal status payload.
    """
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get(f"/status/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished; last status {body}")


@pytest.mark.parametrize("field", ["guidance_scale", "negative_prompt"])
def test_generate_refuses_what_a_distilled_checkpoint_cannot_use(
    server: Any, field: str
) -> None:
    """The schema refuses ``guidance_scale`` and ``negative_prompt``.

    Bug caught: the request model is copied from the Wan server, so it carries
    both. Each is accepted, neither reaches the pipeline, and an operator tuning
    ``guidance_scale`` sees no effect and no error — the worst of the three
    possible outcomes. H3's transformer is guidance-distilled: there is no
    guider, and every step runs exactly one forward pass.
    """
    with TestClient(server.app) as client:
        resp = client.post("/generate", json={"prompt": "x", field: 6.0})
    assert resp.status_code == 422, f"{field} was accepted: {resp.text}"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"prompt": "x", "width": 1000}, "width is not a multiple of 32"),
        ({"prompt": "x", "height": 100}, "height is not a multiple of 32"),
        # 107 is itself 17*6+5, so it does NOT align up — 4.458 s, under the
        # window. 119 and 120 would be WRONG here: both align to 124 = 5.167 s,
        # which the model accepts.
        ({"prompt": "x", "num_frames": 107}, "aligns to 4.458 s, under the window"),
        ({"prompt": "x", "num_frames": 361}, "aligns to 15.083 s, over the window"),
        ({"prompt": "x", "num_inference_steps": 0}, "zero steps"),
        ({"prompt": "x", "fps": 30}, "fps is fixed at 24 by the checkpoint"),
    ],
)
def test_generate_rejects_geometry_the_pipeline_would_reject(
    server: Any, body: dict[str, Any], why: str
) -> None:
    """Geometry is refused at the HTTP edge, not inside the pipeline.

    Bug caught: the checks live only in diffusers, which raises ``ValueError``
    from ``before_denoise``. On Modal that surfaces as a job error some minutes
    after a 123.8 GiB load finished — the most expensive possible place to learn
    that a width was 1000 rather than 992.
    """
    with TestClient(server.app) as client:
        resp = client.post("/generate", json=body)
    assert resp.status_code == 422, f"accepted despite {why}: {resp.text}"


def test_generate_accepts_the_documented_defaults(server: Any) -> None:
    """A bare prompt is a valid request.

    Bug caught: the validators are written as required fields, so the simplest
    possible call — the one in every usage block — fails with 422 and the
    operator has to discover five numbers before the first run.
    """
    with TestClient(server.app) as client:
        resp = client.post("/generate", json={"prompt": "a fox in snow"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"]


def test_loader_passes_the_t2va_workflow_and_enables_auto_offload(
    server: Any,
) -> None:
    """The $66 kwarg and the offload recipe are both on the load path.

    Bug caught: ``workflow="t2va"`` is omitted, so ``load_components`` pulls BOTH
    61.7 GiB transformer partitions. ``transformer_ref/`` is not on the Volume,
    so that is a 66 GB download on an H200 at $4.54/hr.

    Second bug caught: no offload is enabled, so 123.8 GiB of weights are asked
    to be resident on a 131.3 GiB card and the denoise OOMs with ~7 GiB of
    headroom. Note that ``enable_model_cpu_offload`` — what the design doc
    originally specified — does not exist on ``ModularPipeline`` at all;
    ``ComponentsManager.enable_auto_cpu_offload`` is the mechanism.
    """
    with TestClient(server.app):
        pass
    pipe = h3_stub_pipe.STATE["pipe"]
    stub_manager = h3_stub_pipe.STATE["manager"]
    assert pipe.from_pretrained_kwargs.get("workflow") == "t2va"
    assert pipe.from_pretrained_kwargs.get("components_manager") is stub_manager
    assert stub_manager.offload_calls, "auto CPU offload was never enabled"
    assert stub_manager.offload_calls[0]["device"] == "cuda"
    # The manager must be reachable from the module afterwards: dropping the
    # reference is how accelerate's offload hooks get garbage-collected.
    assert server.manager is stub_manager


def test_worker_muxes_audio_into_the_mp4(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The soundtrack reaches ``_av_io.write_mp4_with_audio`` in its layout.

    Bug caught: the writer is ``_video_io.write_mp4``, which takes frames and
    nothing else, so the output is a silent video that passes every exit-code and
    ffprobe-dimension check.

    Second bug caught: ``audio`` is handed over as the raw ``(1, 2, samples)``
    the pipeline returns instead of the ``(samples, channels)`` ``_av_io``
    documents — which would label 2 samples as 165k channels and either explode
    in ``wave`` or write garbage.
    """
    seen: dict[str, Any] = {}

    def fake_write(
        frames: Any, audio: Any, fps: Any, sample_rate: Any, path: Any
    ) -> None:
        seen.update(
            frames=frames, audio=audio, fps=fps, sample_rate=sample_rate, path=path
        )
        Path(path).write_bytes(b"mp4")

    monkeypatch.setattr(server, "write_mp4_with_audio", fake_write)

    with TestClient(server.app) as client:
        job_id = client.post("/generate", json={"prompt": "x"}).json()["job_id"]
        status = _wait_done(client, job_id)

    assert status["status"] == "done", status
    assert seen["frames"].dtype == np.uint8
    # Shape comes from the stub, which is deliberately tiny; what is under
    # test is the rank, the channel-last layout and the audio transpose. The
    # geometry the server FORWARDS is pinned on the call kwargs instead.
    assert seen["frames"].shape == (8, 64, 96, 3)
    assert seen["audio"].shape == (1_000, 2), seen["audio"].shape
    assert seen["fps"] == 24
    assert seen["sample_rate"] == 32000
    # Channel order preserved: left was +0.25, right -0.25. A reshape instead of
    # a permute would scramble this while keeping the shape correct.
    assert seen["audio"][0, 0] > 0 > seen["audio"][0, 1]


def test_sample_rate_comes_from_the_pipeline_not_a_constant(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkpoint reporting a different rate is muxed at that rate.

    Bug caught: 32000 is hardcoded in the writer call. The audio VAE reports its
    own ``sampling_rate``, and the moment a checkpoint ships a different one the
    soundtrack plays at the wrong speed — audible, but perfectly in sync with
    itself, so neither a duration check nor a silence check would catch it.
    """
    h3_stub_pipe.STATE["pipe_kwargs"] = {"sampling_rate": 48000}
    seen: dict[str, Any] = {}

    def fake_write(
        frames: Any, audio: Any, fps: Any, sample_rate: Any, path: Any
    ) -> None:
        seen["sample_rate"] = sample_rate
        Path(path).write_bytes(b"mp4")

    monkeypatch.setattr(server, "write_mp4_with_audio", fake_write)

    with TestClient(server.app) as client:
        job_id = client.post("/generate", json={"prompt": "x"}).json()["job_id"]
        assert _wait_done(client, job_id)["status"] == "done"

    assert seen["sample_rate"] == 48000


def test_pipeline_is_asked_for_all_three_outputs(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``videos``, ``audio`` and ``sampling_rate`` are all requested.

    Bug caught: ``output="videos"`` is passed (the single-string form
    ``ModularPipeline.__call__`` also accepts), the call returns the frames
    alone, and the audio is dropped one layer above the mux — with ``_av_io``
    then never reached at all.
    """
    monkeypatch.setattr(server, "write_mp4_with_audio", lambda *a, **k: None)
    with TestClient(server.app) as client:
        job_id = client.post("/generate", json={"prompt": "x"}).json()["job_id"]
        _wait_done(client, job_id)
    call = h3_stub_pipe.STATE["pipe"].calls[0]
    assert call["output"] == ["videos", "audio", "sampling_rate"]
    assert "guidance_scale" not in call
    assert "negative_prompt" not in call
    assert call["num_frames"] == 124
    assert call["num_inference_steps"] == 50
    assert call["height"] == 768
    assert call["width"] == 1344
    assert call["prompt"] == "x"


def test_bookkeeping_spec_keys_do_not_reach_the_pipeline(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``model`` / ``pipeline`` / ``scheduler`` are accepted but not forwarded.

    ``DiffusersBackend.submit`` posts the cfg's whole ``spec`` block as the
    request body, and that block carries those three for bookkeeping plus the
    ``_audio_mode`` marker the strategy writes.

    Bug caught: they are splatted into the pipeline call, which warns
    "Unexpected input" and — for a name that happens to collide with a real
    block input — silently changes the request.
    """
    monkeypatch.setattr(server, "write_mp4_with_audio", lambda *a, **k: None)
    body = {
        "prompt": "x",
        "model": "MiniMax-H3",
        "pipeline": "MiniMaxH3ModularPipeline",
        "scheduler": "MiniMaxH3Scheduler",
        "fps": 24,
    }
    with TestClient(server.app) as client:
        resp = client.post("/generate", json=body)
        assert resp.status_code == 200, resp.text
        _wait_done(client, resp.json()["job_id"])
    call = h3_stub_pipe.STATE["pipe"].calls[0]
    for key in ("model", "pipeline", "scheduler", "fps"):
        assert key not in call, f"{key} was forwarded to the pipeline"


def test_a_pipeline_error_is_reported_not_swallowed(server: Any) -> None:
    """A raising pipeline marks the job errored and keeps the worker alive.

    Bug caught: the worker's try/except is missing or too narrow, the thread
    dies on the first bad job, and every subsequent poll hangs until the
    orchestrator's wall-clock timeout — on a pod that is still billing.
    """
    with TestClient(server.app) as client:
        h3_stub_pipe.STATE["pipe"].raises = ValueError(
            "MiniMax-H3 generates between 5.0 and 15.0 seconds"
        )
        first = client.post("/generate", json={"prompt": "x"}).json()["job_id"]
        body = _wait_done(client, first)
        assert body["status"] == "error"
        assert "5.0 and 15.0 seconds" in body["error"]

        # The worker must still be draining: a dead thread is the real cost.
        h3_stub_pipe.STATE["pipe"].raises = None
        second = client.post("/generate", json={"prompt": "y"}).json()["job_id"]
        assert _wait_done(client, second)["status"] == "done"


def test_health_and_util_are_served(server: Any) -> None:
    """The two routes the orchestrator and the polling rule depend on.

    Bug caught: ``/util`` is omitted because the server "does not need it", and
    the mandatory utilisation polling has nothing to read — which is how the
    2026-07-05 RIFE smoke burned ~12 minutes on a pod sitting at 0% GPU.
    """
    with TestClient(server.app) as client:
        health = client.get("/health").json()
        assert health["ready"] is True
        assert health["model"] == "MiniMaxAI/MiniMax-H3"
        assert health["capabilities"] == ["t2va"]
        util = client.get("/util").json()
    assert set(util) == {
        "gpu_util_percent",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "uptime_seconds",
    }


def test_generate_is_refused_before_the_model_is_ready(server: Any) -> None:
    """A request arriving mid-load gets 503, not a half-loaded generation.

    Bug caught: the readiness gate is missing, so a poll that races the ~10
    minute load calls a ``None`` pipeline and the job errors for a reason that
    looks like a model failure rather than a race.
    """
    server.ready.clear()
    client = TestClient(server.app)  # not a context manager: no startup
    assert client.post("/generate", json={"prompt": "x"}).status_code == 503


def test_artifact_route_refuses_traversal(server: Any) -> None:
    """``../`` cannot escape the artifact directory.

    Bug caught: the guard is copied without the ``..`` check and the pod serves
    arbitrary container files — including its own boot script, which carries
    HF_TOKEN.
    """
    with TestClient(server.app) as client:
        for name in ("../../etc/passwd", "..%2Fsecret", "sub/dir.mp4"):
            assert client.get(f"/artifacts/{name}").status_code in (400, 404), name


def test_status_404s_an_unknown_job(server: Any) -> None:
    """An unknown job id is a 404, not an empty success.

    Bug caught: ``jobs.get`` returning None falls through to a payload with no
    ``status`` key, and ``DiffusersBackend.result`` polls forever on a job that
    does not exist.
    """
    with TestClient(server.app) as client:
        assert client.get("/status/deadbeef").status_code == 404


def test_the_shipped_cfg_spec_block_is_accepted_verbatim(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact body the orchestrator posts for the shipped cfg is accepted.

    Bug caught: THE 2026-09-18 live failure, HTTP 422 on /generate after the
    H200 had booted and loaded. `DiffusersBackend.submit` posts
    ``dict(job.spec)`` — the cfg's whole `spec` block — and `strategy.decide`
    injects an ``_audio_mode`` marker into that spec on the way. So the body
    carries a key no cfg author wrote, `extra="forbid"` refused it, and the run
    died at the one point where everything expensive had already happened.

    The per-field tests above all passed because each was written from the
    server's own schema. This one is written from the CONFIG and the pipeline
    that feeds it, which is the only direction that can catch a key the server
    never knew about. It is the cheap version of the live run.
    """
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import ModelProfile, Segment
    from kinoforge.core.strategy import decide

    cfg = load_config("examples/configs/modal-diffusers-minimax-h3-t2va.yaml")
    profile = ModelProfile(
        name="minimax-h3",
        max_frames=360,
        fps=24,
        supported_modes={"t2va"},
        max_resolution=(1344, 768),
        supports_native_extension=False,
        supports_joint_audio=True,  # the flag that makes _audio_mode "joint"
    )
    jobs = decide(profile, [Segment(prompt=cfg.prompt or "x")], {}, cfg.spec)
    body = dict(jobs[0].spec)
    body.setdefault("prompt", cfg.prompt or "x")
    assert "_audio_mode" in body, (
        "the strategy no longer injects _audio_mode; this test is now guarding "
        "nothing and should be re-pointed at whatever it injects instead"
    )

    monkeypatch.setattr(server, "write_mp4_with_audio", lambda *a, **k: None)
    with TestClient(server.app) as client:
        resp = client.post("/generate", json=body)
        assert resp.status_code == 200, (
            f"the shipped cfg's own spec block was refused: {resp.text}"
        )
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"

    # ... and the marker must not reach the pipeline, which would warn
    # "Unexpected input" on every generation.
    call = h3_stub_pipe.STATE["pipe"].calls[0]
    assert "_audio_mode" not in call


@pytest.mark.parametrize("frames", [346, 352, 360])
def test_generate_rejects_frame_counts_that_align_past_the_window(
    server: Any, frames: int
) -> None:
    """A count that is in range but ALIGNS out of range is refused here.

    Bug caught: the schema bounds num_frames by the duration window's raw edges
    (120..360 = 5..15 s at 24 fps), but the pipeline snaps the count up to the
    next `17 * n + 5` and checks the duration of the ALIGNED value. The largest
    aligned count inside 15.0 s is 345 (14.375 s); the next is 362 (15.083 s).
    So every request in 346..360 passes a naive bound and then raises ValueError
    from `before_denoise` — on a booked H200, minutes after a 124 GiB load.

    The diffusers source calls out this exact trap in a comment: "346 frames
    would otherwise pass the check and then be rounded up to 362, i.e. 15.083
    seconds."
    """
    with TestClient(server.app) as client:
        resp = client.post("/generate", json={"prompt": "x", "num_frames": frames})
    assert resp.status_code == 422, (
        f"num_frames={frames} accepted, but it aligns to "
        f"{frames + (5 - frames % 17) % 17} and the pipeline rejects it: {resp.text}"
    )


def test_generate_accepts_the_longest_legal_clip(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """345 frames — 14.375 s — is the maximum and must be accepted.

    Bug caught: the bound is tightened to the wrong side (e.g. 344, or the raw
    14 s) and the model's longest clip becomes unreachable through kinoforge.
    """
    monkeypatch.setattr(server, "write_mp4_with_audio", lambda *a, **k: None)
    with TestClient(server.app) as client:
        resp = client.post("/generate", json={"prompt": "x", "num_frames": 345})
        assert resp.status_code == 200, resp.text
        assert _wait_done(client, resp.json()["job_id"])["status"] == "done"
    assert h3_stub_pipe.STATE["pipe"].calls[0]["num_frames"] == 345
