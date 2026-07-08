"""Tests for SpandrelEngine — HTTP-aware UpscalerEngine implementer."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget


def _job_2x() -> UpscaleJob:
    # https:// source — passes through SpandrelEngine.upscale() without
    # triggering the file:// → PUT /upload dispatch.
    return UpscaleJob(
        source=Artifact(uri="https://example.invalid/in.mp4", sha256="0" * 64, size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
    )


def _cfg() -> dict[str, object]:
    return {
        "upscale": {
            "engine": "spandrel",
            "scale": "2x",
            "spandrel": {
                "model_url": "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth",
                "arch": "realesrgan",
                "precision": "fp16",
                "tile_size": 512,
                "batch_size": 4,
            },
        },
    }


def _instance() -> Instance:
    return Instance(
        id="pod-fake",
        provider="fake",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod.example/proxy"},
        tags={},
    )


class TestRegistration:
    def test_name_is_spandrel(self) -> None:
        # Bug caught: typo in class attr (e.g. "spandel") makes the
        # engine register under the wrong name and `cfg.upscale.engine
        # = "spandrel"` fails at registry lookup.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        assert SpandrelEngine.name == "spandrel"
        assert SpandrelEngine.requires_compute is True
        assert SpandrelEngine.requires_local_weights is True


class TestRenderProvision:
    def test_emits_pip_and_fetch_lines(self) -> None:
        # Bug caught: render_provision emits a script that pip-installs
        # the wrong package OR forgets the weights fetch step. The pod
        # boots and the `import spandrel` inside the runtime crashes.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        rp = SpandrelEngine().render_provision(_cfg())
        assert "pip install" in rp.script
        assert "spandrel" in rp.script
        assert "imageio" in rp.script
        # Weights fetch is inlined as a curl invocation rather than
        # `python -m kinoforge.upscalers.spandrel._fetch_weights` to keep
        # the on-pod bootstrap self-contained (no kinoforge.core import).
        assert "curl -L" in rp.script
        assert "RealESRGAN_x2plus.pth" in rp.script
        assert "huggingface.co/lllyasviel/realesrgan/resolve/main" in rp.script

    def test_run_cmd_empty(self) -> None:
        # Bug caught: SpandrelEngine claims a `run_cmd` and overrides
        # the wan_t2v_server entrypoint, so the pod never starts the
        # HTTP server. Composition pattern: upscaler scripts are
        # ADDITIVE; the diffusers engine owns the server process.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        rp = SpandrelEngine().render_provision(_cfg())
        assert rp.run_cmd == []


class TestUpscale:
    def test_posts_then_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug caught: upscale() either POSTs without polling, or polls
        # without POSTing. Mirror the SeedVR2 HTTP flow exactly.
        from kinoforge.upscalers.spandrel import SpandrelEngine
        from kinoforge.upscalers.spandrel import _engine as spandrel_mod

        submit_resp: dict[str, object] = {"job_id": "u-test"}
        status_resp: dict[str, object] = {
            "state": "done",
            "progress": 1.0,
            "result": {
                "filename": "out.mp4",
                "sha256": "abcd",
                "size": 4096,
                "input_resolution": [64, 48],
                "output_resolution": [128, 96],
                "engine_meta": {},
            },
            "error": None,
        }
        calls: list[tuple[str, str, object]] = []

        def fake_http(
            *, method: str, url: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            calls.append((method, url, payload))
            if method == "POST":
                return submit_resp
            return status_resp

        monkeypatch.setattr(spandrel_mod, "_http_json", fake_http)
        result = SpandrelEngine().upscale(_instance(), _job_2x(), _cfg())

        assert calls[0][0] == "POST"
        assert calls[0][1].endswith("/upscale")
        assert calls[1][0] == "GET"
        assert calls[1][1].endswith("/upscale/status/u-test")

        assert result.artifact.uri.endswith("/artifacts/out.mp4")
        assert result.artifact.sha256 == "abcd"
        assert result.output_resolution == (128, 96)

    def test_aborts_on_cancel_when_pod_dies_midjob(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel_token fired mid-poll (POD_GONE) aborts with Cancelled fast.

        Bug caught (2026-07-07 reclaim): RunPod pulls the pod mid-job; without
        the token threaded into the status retry the poll burns the full backoff
        then raises a raw HTTP 404 instead of the prompt Cancelled.
        """
        import urllib.error

        from kinoforge.core.cancel import CancelToken
        from kinoforge.core.errors import Cancelled
        from kinoforge.upscalers.spandrel import SpandrelEngine
        from kinoforge.upscalers.spandrel import _engine as spandrel_mod

        token = CancelToken()
        calls = {"get": 0}

        def fake_http(
            *, method: str, url: str, payload: dict[str, object] | None = None
        ) -> dict[str, object]:
            if method == "POST":
                return {"job_id": "u-test"}
            calls["get"] += 1
            token.set()  # pod reclaimed mid-job → POD_GONE sets the token
            raise urllib.error.HTTPError(
                url=url,
                code=404,
                msg="gone",
                hdrs=None,  # type: ignore[arg-type]
                fp=None,
            )

        monkeypatch.setattr(spandrel_mod, "_http_json", fake_http)
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

        with pytest.raises(Cancelled):
            SpandrelEngine().upscale(_instance(), _job_2x(), _cfg(), cancel_token=token)
        assert calls["get"] == 1  # aborted after first failed poll


class TestValidateSpec:
    def test_height_refused(self) -> None:
        # Bug caught: validate_spec accepts kind="height" and the pod
        # crashes at inference time. Mirror SeedVR2Engine's refusal.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        with pytest.raises(NotYetImplementedError, match="height"):
            SpandrelEngine().validate_spec(
                UpscaleJob(
                    source=Artifact(uri="file:///tmp/x.mp4", sha256="x", size=1),
                    scale=ScaleTarget(kind="height", value=1080.0),
                )
            )


class TestModelIdentity:
    def test_three_token_slug(self) -> None:
        # Bug caught: slug uses 4 tokens (spandrel-realesrgan-x2-fp16),
        # breaking the server-side `_load_model_to_gpu` 3-token parser
        # (`parts[-2], parts[-1]` would yield "x2","fp16" instead of
        # "realesrgan","fp16"). Spec §3.2 locks the 3-token shape.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        slug = SpandrelEngine().model_identity(_cfg())
        assert slug == "spandrel-realesrgan-fp16"

    def test_empty_on_missing_block(self) -> None:
        # Bug caught: missing-key handling raises instead of returning
        # empty string; sink renders "unknown" or breaks the ABC contract.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        assert SpandrelEngine().model_identity({}) == ""
