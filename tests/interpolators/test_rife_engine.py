"""RifeEngine: HTTP dispatch shape + registration + capability.

Mirrors tests/upscalers/flashvsr/test_engine.py: mocks the ``_http_json`` seam
and ``_upload_source`` so no pod or network is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.errors import InterpolationError
from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import Artifact, Instance, InterpolateJob
from kinoforge.interpolators.rife._engine import RifeEngine


def _cfg(model: str = "rife49", precision: str = "fp16") -> dict[str, Any]:
    return {
        "interpolate": {
            "engine": "rife",
            "fps": 60.0,
            "rife": {
                "weights_ref": "hf:kinoforge/rife",
                "model": model,
                "precision": precision,
            },
        }
    }


def _inst() -> Instance:
    return Instance(
        id="pod-abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )


def test_capability_is_arbitrary_timestep() -> None:
    # Bug caught: declaring RECURSIVE_2X would make the resolver overshoot to a
    # power of two + decimate instead of hitting the target in one pass.
    assert RifeEngine().capability is InterpCapability.ARBITRARY_TIMESTEP


def test_model_identity_shape_and_missing_field_safety() -> None:
    e = RifeEngine()
    assert e.model_identity(_cfg(model="rife49")) == "rife-rife49"
    # MUST NOT raise on a malformed / partial cfg (sink-filename slug contract).
    assert e.model_identity({}) == ""


def test_validate_spec_rejects_nonpositive_fps() -> None:
    e = RifeEngine()
    with pytest.raises(ValueError):
        e.validate_spec(
            InterpolateJob(source=Artifact(uri="file:///x.mp4"), target_fps=0.0)
        )


def test_interpolate_uploads_local_source_then_polls_to_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: a file:// source not uploaded before POST -> the pod fetches a
    # path that only exists on the client; also, a single-shot poll that accepts
    # state='running' as done would return an unfinished job.
    e = RifeEngine()
    uploaded: dict[str, Any] = {}

    def fake_upload(instance: Instance, path: Path) -> str:
        uploaded["path"] = path
        return "file:///workspace/uploads/x.mp4"

    monkeypatch.setattr(e, "_upload_source", fake_upload)

    poll = {"n": 0}
    posted: dict[str, Any] = {}

    def fake_http(
        *, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            posted["payload"] = payload
            return {"job_id": "j-1"}
        poll["n"] += 1
        if poll["n"] < 3:
            return {"state": "running", "progress": 0.5}
        return {
            "state": "done",
            "result": {
                "filename": "out.mp4",
                "sha256": "0" * 64,
                "size": 4096,
                "input_fps": 16.0,
                "output_fps": 60.0,
                "input_frame_count": 16,
                "output_frame_count": 60,
                "engine_meta": {"gpu_s": 3.0},
            },
        }

    monkeypatch.setattr("kinoforge.interpolators.rife._engine._http_json", fake_http)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = e.interpolate(
        instance=_inst(),
        job=InterpolateJob(source=Artifact(uri="file:///tmp/in.mp4"), target_fps=60.0),
        cfg=_cfg(),
    )

    assert uploaded["path"] == Path("/tmp/in.mp4")
    assert posted["payload"]["target_fps"] == 60.0
    assert posted["payload"]["engine"] == "rife"
    assert posted["payload"]["rife"]["model"] == "rife49"
    assert poll["n"] >= 3
    assert result.output_fps == 60.0
    assert result.input_fps == 16.0
    assert result.input_frame_count == 16
    assert result.output_frame_count == 60
    assert result.artifact.uri == "http://pod-abc.runpod.io/artifacts/out.mp4"
    assert result.artifact.sha256 == "0" * 64


def test_interpolate_raises_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bug caught: server state='error' swallowed -> caller treats a failed job
    # as success and ships a broken/absent artifact.
    e = RifeEngine()

    def fake_http(
        *, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            return {"job_id": "j-err"}
        return {"state": "error", "error": "cuda oom on interp"}

    monkeypatch.setattr("kinoforge.interpolators.rife._engine._http_json", fake_http)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(e, "_upload_source", lambda *a, **k: "file:///up/x.mp4")

    with pytest.raises(InterpolationError, match="cuda oom on interp"):
        e.interpolate(
            instance=_inst(),
            job=InterpolateJob(
                source=Artifact(uri="file:///tmp/in.mp4"), target_fps=60.0
            ),
            cfg=_cfg(),
        )


def test_interpolate_aborts_on_cancel_when_pod_dies_midjob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel_token fired mid-poll (POD_GONE) aborts with Cancelled fast.

    Bug caught (2026-07-07 reclaim): RunPod pulls the pod mid-job; without the
    token threaded into the status retry the poll burns the full backoff then
    raises a raw HTTP 404 instead of the prompt Cancelled POD_GONE signalled.
    """
    import urllib.error

    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.errors import Cancelled

    e = RifeEngine()
    token = CancelToken()
    calls = {"get": 0}

    def fake_http(
        *, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            return {"job_id": "j-1"}
        calls["get"] += 1
        token.set()  # pod reclaimed mid-job → POD_GONE sets the token
        raise urllib.error.HTTPError(url=url, code=404, msg="gone", hdrs=None, fp=None)  # type: ignore[arg-type]

    monkeypatch.setattr("kinoforge.interpolators.rife._engine._http_json", fake_http)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(e, "_upload_source", lambda *a, **k: "file:///up/x.mp4")

    with pytest.raises(Cancelled):
        e.interpolate(
            instance=_inst(),
            job=InterpolateJob(
                source=Artifact(uri="file:///tmp/in.mp4"), target_fps=60.0
            ),
            cfg=_cfg(),
            cancel_token=token,
        )
    assert calls["get"] == 1  # aborted after first failed poll, not full backoff


def test_rife_is_registered() -> None:
    # Bug caught: importing the package doesn't self-register -> the orchestrator's
    # registry.get_interpolator("rife") raises UnknownAdapter at runtime.
    import kinoforge.interpolators.rife  # noqa: F401
    from kinoforge.core import registry

    assert "rife" in registry.interpolator_names()
    assert isinstance(registry.get_interpolator("rife")(), RifeEngine)
