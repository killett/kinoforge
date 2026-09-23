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
import sys
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kinoforge.engines.diffusers.servers import _lora
from tests.engines.diffusers.servers import h3_stub_pipe

_STUB = "tests.engines.diffusers.servers.h3_stub_pipe.stub_loader"
#: A pipeline holding BOTH checkpoint partitions — the case no workflow
#: actually produces, and the only one where a default target would be a guess.
_STUB_DUAL = "tests.engines.diffusers.servers.h3_stub_pipe.stub_loader_dual"


def _reload_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub: str) -> Any:
    """Reload the H3 server module wired to *stub*, with pod dirs under tmp_path.

    Args:
        tmp_path: Root for the artifact and LoRA directories.
        monkeypatch: Environment patcher.
        stub: Dotted path of the loader to install as ``KINOFORGE_H3_LOAD_STUB``.

    Returns:
        The freshly reloaded server module.
    """
    monkeypatch.setenv("KINOFORGE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("KINOFORGE_LORAS_DIR", str(tmp_path / "loras"))
    monkeypatch.setenv("KINOFORGE_H3_LOAD_STUB", stub)
    monkeypatch.setenv("WAN_MODEL_ID", "MiniMaxAI/MiniMax-H3")
    h3_stub_pipe.STATE.clear()

    from kinoforge.engines.diffusers.servers import minimax_h3_server as mod

    importlib.reload(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_lora_module_state() -> Iterator[None]:
    """``_lora``'s inventory and job table are process state, not per-app state.

    Reloading the server module builds a new ``app``; it does NOT reload
    ``_lora``, so a stack applied by one test would still be in the inventory
    the next test reads.
    """
    _lora._INVENTORY.clear()
    _lora._JOBS.clear()
    yield
    _lora._INVENTORY.clear()
    _lora._JOBS.clear()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the H3 server module wired to the stub loader.

    Args:
        tmp_path: Artifact directory for the run.
        monkeypatch: Environment patcher.

    Returns:
        The freshly reloaded server module.
    """
    yield _reload_server(tmp_path, monkeypatch, _STUB)
    h3_stub_pipe.STATE.clear()


@pytest.fixture
def dual_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The same server against a pipeline that loaded BOTH partitions.

    Args:
        tmp_path: Artifact directory for the run.
        monkeypatch: Environment patcher.

    Returns:
        The freshly reloaded server module.
    """
    yield _reload_server(tmp_path, monkeypatch, _STUB_DUAL)
    h3_stub_pipe.STATE.clear()


#: Stands in for ``torch.bfloat16``. Identity is the whole assertion: a cast to
#: float16 or float32 would be just as "a dtype" and just as wrong.
_BF16 = object()


@pytest.fixture
def fake_torch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a stand-in ``torch`` for the pod-only import inside ``after_load``.

    torch is not installed in this environment by design — the servers import it
    lazily, inside functions, so everything but the dtype restore is testable
    without it. That one line needs a ``torch.bfloat16`` to hand over.

    Args:
        monkeypatch: Patcher for ``sys.modules``.

    Returns:
        The installed stand-in module.
    """
    stub = types.ModuleType("torch")
    stub.bfloat16 = _BF16  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", stub)
    return stub


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


def test_attention_backend_failure_does_not_brick_the_boot(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attention-backend swap degrades to the default, it does not abort.

    Bug caught: `_flash_3_hub` FETCHES KERNELS FROM THE HUB when it is set. If
    that fetch fails — rate limit, network, a moved repo — an unguarded call
    raises inside the FastAPI startup handler. uvicorn then never binds the
    port, `/health` never answers, and the orchestrator waits its full
    `boot_timeout` of 45 MINUTES on an H200 at $4.54/hr: up to $3.40 to learn
    that an optional speed-up was unavailable.

    Degrading to the stock SDPA backend costs a slower run. Aborting the boot
    costs the whole pod. The log line is the deliverable — a silent fallback
    would make a 3x regression look like the model being slow.
    """

    class Boom:
        def set_attention_backend(self, name: str) -> None:
            raise RuntimeError(f"hub fetch failed for {name}")

    monkeypatch.setattr(server, "_ATTENTION_BACKEND", "_flash_3_hub")
    monkeypatch.setattr(h3_stub_pipe.FakePipe, "transformer", Boom(), raising=False)

    with TestClient(server.app) as client:
        assert client.get("/health").json()["ready"] is True
        resp = client.post("/generate", json={"prompt": "x"})
    assert resp.status_code == 200, resp.text


def test_attention_backend_is_applied_when_requested(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var actually reaches ``transformer.set_attention_backend``.

    Bug caught: the knob is documented, plumbed through the cfg, and never
    called — so an operator measures "no speed-up" and concludes the backend
    does not help, when in fact it was never enabled.
    """
    seen: list[str] = []

    class Recorder:
        def set_attention_backend(self, name: str) -> None:
            seen.append(name)

    monkeypatch.setattr(server, "_ATTENTION_BACKEND", "_flash_3_hub")
    monkeypatch.setattr(h3_stub_pipe.FakePipe, "transformer", Recorder(), raising=False)

    with TestClient(server.app):
        pass
    assert seen == ["_flash_3_hub"]


def test_no_attention_backend_leaves_the_pipeline_alone(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the env var unset, nothing is called at all.

    Bug caught: the code passes an empty string to
    ``set_attention_backend("")``, which is not a valid backend name, so every
    default run fails at startup.
    """
    seen: list[str] = []

    class Recorder:
        def set_attention_backend(self, name: str) -> None:
            seen.append(name)

    monkeypatch.setattr(server, "_ATTENTION_BACKEND", "")
    monkeypatch.setattr(h3_stub_pipe.FakePipe, "transformer", Recorder(), raising=False)

    with TestClient(server.app):
        pass
    assert seen == []


# ---------------------------------------------------------------------------
# LoRA seam: the profile is read off the pipeline, and the router rides on it
# ---------------------------------------------------------------------------


def test_health_declares_the_loaded_partition_only(server: Any) -> None:
    """A t2va pod advertises one target — the partition it actually holds.

    Bug caught: the profile is built from the ``LORA_TARGET_UNIVERSE`` constant
    instead of from the loaded pipeline, so every pod advertises both
    partitions. H3 ships ``transformer`` (t2va/fl2va) and ``transformer_ref``
    (ref2va) with IDENTICAL module names, and a workflow loads only its own — a
    LoRA aimed at the partition this pod did not load applies without error and
    silently degrades the output. Advertising it is how the controller is
    invited to do exactly that.
    """
    with TestClient(server.app) as client:
        body = client.get("/health").json()
    assert body["lora"]["supported"] is True
    assert body["lora"]["targets"] == ["transformer"]
    assert body["lora"]["default_target"] == "transformer"
    assert body["lora"]["profile"] == "minimax-h3-t2va"


def test_health_refuses_to_guess_on_a_dual_partition_pipeline(
    dual_server: Any,
) -> None:
    """Holding both partitions means there is no default — the caller must say.

    Bug caught: ``default_target`` is set to ``targets[0]`` unconditionally. On a
    pipeline holding both partitions an entry that names no target then loads
    into ``transformer`` on a coin-flip basis. The load succeeds either way, so
    the mistake never surfaces as an error — only as worse pixels.
    """
    with TestClient(dual_server.app) as client:
        body = client.get("/health").json()
    assert body["lora"]["supported"] is True
    assert body["lora"]["targets"] == ["transformer", "transformer_ref"]
    assert body["lora"]["default_target"] is None


def test_health_declares_no_lora_support_before_the_model_is_ready(
    server: Any,
) -> None:
    """Mid-load, ``/health`` says what is true: nothing can be applied yet.

    Bug caught: the block is rendered from a constant, so a pod that is still
    pulling 124 GiB of weights advertises a full LoRA capability. The
    controller's pre-flight reads ``/health`` and fires ``/lora/set_stack`` at a
    pod with no profile behind it.
    """
    server.ready.clear()
    client = TestClient(server.app)  # not a context manager: no startup
    body = client.get("/health").json()
    assert body["ready"] is False
    assert body["lora"] == {
        "supported": False,
        "targets": [],
        "default_target": None,
        "profile": None,
    }


def test_the_lora_router_is_mounted_before_the_pod_reports_ready(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ready`` is only set once ``/lora/set_stack`` can actually answer.

    Bug caught: the router is included at the END of ``_startup``, after
    ``ready.set()``. The orchestrator polls ``/health`` and fires its stack the
    instant ready flips, so it gets a 404 from a pod that just declared itself
    healthy — and a 404 is not in the client's error vocabulary, so it surfaces
    as an unmapped failure on a booked H200.

    The assertion is on the state AT the moment ready is set, not afterwards:
    checking after startup finishes cannot distinguish the two orderings.
    """
    observed: dict[str, Any] = {}
    real_set = server.ready.set

    def recording_set() -> None:
        observed["paths"] = {getattr(r, "path", None) for r in server.app.routes}
        observed["profile"] = server.lora_profile
        real_set()

    monkeypatch.setattr(server.ready, "set", recording_set)
    with TestClient(server.app):
        pass

    assert observed, "_startup never set the ready event"
    assert "/lora/set_stack" in observed["paths"]
    assert "/lora/inventory" in observed["paths"]
    assert observed["profile"] is not None, (
        "ready was set while the LoRA profile was still None"
    )


def test_lora_inventory_is_served_from_a_directory_startup_created(
    server: Any,
) -> None:
    """The LoRA directory exists by the time the first inventory read lands.

    Bug caught: ``_startup`` mkdirs the artifact directory but not
    ``LORAS_DIR``. ``disk_free_bytes`` is ``shutil.disk_usage``, which raises
    ``FileNotFoundError`` on a missing path — so the very first
    ``/lora/inventory`` (and the free-disk snapshot every completed apply job
    writes) 500s on a pod that is otherwise perfectly healthy.
    """
    with TestClient(server.app) as client:
        resp = client.get("/lora/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inventory"] == []
    assert body["free_bytes"] > 0


@pytest.mark.parametrize(
    ("target", "expected_flag"),
    [("transformer", False), ("transformer_ref", True)],
)
def test_load_sets_the_ref_partition_flag_only_for_transformer_ref(
    dual_server: Any, target: str, expected_flag: bool
) -> None:
    """``load_into_transformer_ref`` tracks the target, and nothing else.

    Bug caught: the flag is hardcoded (either way) or dropped, so every LoRA
    lands in one partition regardless of what was asked for. diffusers accepts
    a LoRA aimed at the wrong H3 partition — the module names are identical
    across the two — so there is no error anywhere; the run simply comes out
    worse than it should.
    """
    with TestClient(dual_server.app):
        pass
    profile = dual_server.lora_profile
    pipe = h3_stub_pipe.STATE["pipe"]

    profile.load(pipe, "/loras/turbo.safetensors", "lora_0", target)

    assert pipe.lora_loads == [
        {
            "path": "/loras/turbo.safetensors",
            "adapter_name": "lora_0",
            "load_into_transformer_ref": expected_flag,
        }
    ]


def test_after_load_restores_bf16_on_every_loaded_partition(
    dual_server: Any, fake_torch: Any
) -> None:
    """Both held partitions are cast back to bf16 once the stack is attached.

    Bug caught: ``after_load`` is a no-op, or touches only ``transformer``.
    DiffSynth-Studio's H3 LoRAs are fp32 throughout (518 F32 tensors, 1.26 GB),
    and without the cast the unfused LoRA path computes in fp32 for the rest of
    the pod's life — on a card already holding ~77 GB of an H200's 131 GiB,
    that is the memory budget gone. The failure is an OOM minutes into a
    generation, not at load time.
    """
    with TestClient(dual_server.app):
        pass
    profile = dual_server.lora_profile
    pipe = h3_stub_pipe.STATE["pipe"]

    profile.after_load(pipe)

    assert pipe.partitions["transformer"].dtype_calls == [fake_torch.bfloat16]
    assert pipe.partitions["transformer_ref"].dtype_calls == [fake_torch.bfloat16]


def test_after_load_touches_only_the_partition_this_workflow_loaded(
    server: Any, fake_torch: Any
) -> None:
    """On a t2va pod the restore never reaches for ``transformer_ref``.

    Bug caught: ``after_load`` iterates ``LORA_TARGET_UNIVERSE`` rather than the
    partitions the pipeline actually holds. ``getattr`` then raises
    ``AttributeError`` inside ``apply_stack``'s rollback boundary, so a
    perfectly valid single-partition stack is unloaded and refused — every
    time, on the only workflow this server ever runs.
    """
    with TestClient(server.app):
        pass
    profile = server.lora_profile
    pipe = h3_stub_pipe.STATE["pipe"]

    profile.after_load(pipe)

    assert pipe.partitions["transformer"].dtype_calls == [fake_torch.bfloat16]
    assert "transformer_ref" not in pipe.partitions
    assert not hasattr(pipe, "transformer_ref")


def test_apply_stack_weights_each_partition_through_its_own_module(
    dual_server: Any, fake_torch: Any, tmp_path: Path
) -> None:
    """Each target's adapters are weighted on that target's own submodule.

    Bug caught: ``module_for`` ignores its ``target`` argument and always
    returns ``pipe.transformer``. peft raises on an adapter name the module
    does not own, so the ref partition's adapter is either rejected outright or
    — worse, if the call is made pipe-wide — left at its default weight 1.0
    while the inventory reports the strength that was asked for.

    Drives the real shared ``apply_stack``, not a re-implementation of it: the
    profile is only correct insofar as that seam can use it.
    """
    with TestClient(dual_server.app):
        pass
    profile = dual_server.lora_profile
    pipe = h3_stub_pipe.STATE["pipe"]
    base = tmp_path / "a.safetensors"
    base.write_bytes(b"x" * 11)
    ref = tmp_path / "b.safetensors"
    ref.write_bytes(b"y" * 13)

    rows = _lora.apply_stack(
        pipe,
        profile,
        entries=[
            _lora.ResolvedEntry(
                ref="hf:base", path=base, strength=0.8, target="transformer"
            ),
            _lora.ResolvedEntry(
                ref="hf:ref", path=ref, strength=0.5, target="transformer_ref"
            ),
        ],
    )

    assert pipe.partitions["transformer"].set_adapters_calls == [(["lora_0"], [0.8])]
    assert pipe.partitions["transformer_ref"].set_adapters_calls == [
        (["lora_1"], [0.5])
    ]
    assert [(r.ref, r.target, r.size_bytes) for r in rows] == [
        ("hf:base", "transformer", 11),
        ("hf:ref", "transformer_ref", 13),
    ]


@pytest.mark.parametrize(
    "message",
    [
        "Error(s) in loading state_dict: size mismatch for blocks.0.attn.to_q",
        "shape mismatch: value tensor of shape [1536] cannot be broadcast",
        "SIZE MISMATCH in the checkpoint",
    ],
)
def test_explain_load_failure_names_the_loadable_turbo_lora(
    server: Any, message: str
) -> None:
    """A size/shape mismatch is explained as a pruned-checkpoint LoRA.

    Bug caught: the matcher keys on the exception TYPE, or on one exact
    diffusers phrase, so a real mismatch returns no hint. The router maps a
    hintless failure to 500 — retryable — and the controller re-downloads 1.96
    GB of a LoRA that can never load, instead of being told in one line which
    repo ships the loadable twin.
    """
    hint = server._explain_load_failure(RuntimeError(message))
    assert hint is not None
    assert "lightx2v/Minimax-h3-Turbo" in hint
    assert "_8step_v1.0_bf16.safetensors" in hint


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"),
        FileNotFoundError("no such file: /tmp/kf-loras/x.safetensors"),
        ValueError("invalid adapter name"),
    ],
)
def test_explain_load_failure_stays_silent_on_an_unrelated_failure(
    server: Any, exc: BaseException
) -> None:
    """Everything that is not a mismatch gets no hint — and so stays a 500.

    Bug caught: the matcher is widened until it hints on any failure. A
    transient OOM or a vanished file is then reported to the controller as
    ``lora_format_unsupported`` with a 400, which is permanent and unretryable
    — and the operator is sent chasing a checkpoint format that was never the
    problem.
    """
    assert server._explain_load_failure(exc) is None


def test_health_claims_no_support_when_no_known_partition_is_held(
    server: Any,
) -> None:
    """A pipeline holding neither partition supports no LoRA, and says so.

    Bug caught: ``supported`` is hardcoded to "the pipeline loaded", so a
    diffusers release that renames the partition attributes turns into a pod
    advertising full LoRA support with an EMPTY legal-target list. Every stack
    is then accepted by the controller's pre-flight and refused by the pod —
    the same cross-boot lie the parity test exists to prevent, arriving by a
    route the parity test cannot see.
    """
    server.lora_profile = server._build_lora_profile(object())
    server.ready.set()
    client = TestClient(server.app)  # not a context manager: no second startup

    body = client.get("/health").json()

    assert body["lora"]["targets"] == []
    assert body["lora"]["supported"] is False


def test_a_t2va_pod_refuses_the_partition_it_did_not_load(server: Any) -> None:
    """``transformer_ref`` is in the universe and still refused by THIS pod.

    Bug caught: the router is handed a profile built from
    ``LORA_TARGET_UNIVERSE``, so a ref2va LoRA is accepted by a t2va pod. H3's
    two partitions share module names — the load would SUCCEED and quietly
    degrade every frame — which is why the refusal has to come from what the
    pipeline holds, not from what the model family can hold.

    Refused on the submit path, before a byte is downloaded: the 400 arrives
    without touching the network.
    """
    with TestClient(server.app) as client:
        resp = client.post(
            "/lora/set_stack",
            json={
                "target": [{"ref": "hf:turbo", "target": "transformer_ref"}],
                "download_specs": {
                    "hf:turbo": {
                        "url": "https://example.invalid/turbo.safetensors",
                        "filename": "turbo.safetensors",
                    }
                },
            },
        )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "lora_target_unsupported"
    assert detail["legal"] == ["transformer"]


def test_a_dual_partition_pod_refuses_an_entry_that_names_no_target(
    dual_server: Any,
) -> None:
    """Holding both partitions, an unnamed target is refused, never guessed.

    Bug caught: the pod falls back to ``targets[0]``. The LoRA loads into
    ``transformer`` without raising and the run comes out subtly wrong, with
    an inventory that reports exactly what was asked for — a defect visible
    only in the pixels, on the one model where the mistake is free to make.
    """
    with TestClient(dual_server.app) as client:
        resp = client.post(
            "/lora/set_stack",
            json={
                "target": [{"ref": "hf:turbo"}],
                "download_specs": {
                    "hf:turbo": {
                        "url": "https://example.invalid/turbo.safetensors",
                        "filename": "turbo.safetensors",
                    }
                },
            },
        )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "lora_target_unsupported"
    assert detail["target"] is None
    assert detail["legal"] == ["transformer", "transformer_ref"]
