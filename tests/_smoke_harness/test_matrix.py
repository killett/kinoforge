"""run_matrix happy + error paths against a stubbed http module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._smoke_harness import matrix

_HTTP = "tests._smoke_harness.http"


def _make_steps() -> list[matrix.MatrixStep]:
    return [
        matrix.MatrixStep(
            name="step-1-load-a",
            target_stack=["civitai:A@1"],
            expected_inventory=["civitai:A@1"],
            expected_evict=[],
            expected_download=["civitai:A@1"],
        ),
        matrix.MatrixStep(
            name="step-2-swap-to-b",
            target_stack=["civitai:B@2"],
            expected_inventory=["civitai:B@2"],
            expected_evict=["civitai:A@1"],
            expected_download=["civitai:B@2"],
        ),
    ]


def _spec(name: str) -> dict[str, Any]:
    return {"url": "x", "headers": {}, "filename": name, "size_hint": 1}


def test_run_matrix_happy_path_inventory_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: runner forgets /lora/set_stack response → can't catch a pod
    that ack'd set_stack but didn't actually load.

    POST now returns ``{"job_id"}``; the runner polls
    ``/lora/set_stack/status/{job_id}`` to done, then reads
    ``/lora/inventory`` for the post-step state.
    """
    set_stack_calls: list[dict[str, Any]] = []
    health_calls: list[str] = []
    step_counter = {"n": 0}

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        set_stack_calls.append(body)
        job_id = f"j-{len(set_stack_calls)}"
        return {"job_id": job_id}

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        if "/health" in url:
            health_calls.append(url)
            return {"ready": True, "model": "stub"}
        if "set_stack/status" in url:
            # Return done immediately; inventory is confirmed separately.
            return {"state": "done", "inventory": [], "free_bytes": 9}
        # /lora/inventory — return refs matching the current step's target.
        idx = step_counter["n"]
        step_counter["n"] += 1
        refs_by_step = [["civitai:A@1"], ["civitai:B@2"]]
        refs = refs_by_step[idx] if idx < len(refs_by_step) else []
        return {"inventory": [{"ref": r} for r in refs], "free_bytes": 9}

    monkeypatch.setattr(f"{_HTTP}.post_json", _post)
    monkeypatch.setattr(f"{_HTTP}.get_json", _get)
    report = matrix.run_matrix(
        cfg_path=Path("/nope.yaml"),
        pod_proxy_url="http://stub",
        steps=_make_steps(),
        download_specs={
            "civitai:A@1": _spec("a.s"),
            "civitai:B@2": _spec("b.s"),
        },
        generate_per_step=False,
    )
    assert len(report.steps) == 2
    assert [r.name for r in report.steps] == [
        "step-1-load-a",
        "step-2-swap-to-b",
    ]
    assert [r.inventory_after for r in report.steps] == [
        ["civitai:A@1"],
        ["civitai:B@2"],
    ]
    assert list(set_stack_calls[0]["download_specs"].keys()) == ["civitai:A@1"]
    assert list(set_stack_calls[1]["download_specs"].keys()) == ["civitai:B@2"]
    # Warmup must have probed /health at least once before any set_stack.
    assert len(health_calls) >= 1


def test_run_matrix_raises_on_inventory_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: runner accepts wrong post-state → smoke passes against
    broken pod.

    POST returns ``{"job_id"}``, status GET returns done, but
    ``/lora/inventory`` returns the wrong refs — runner must still
    raise AssertionError naming the step.
    """

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        return {"job_id": "j-mismatch"}

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        if "set_stack/status" in url:
            return {"state": "done", "inventory": [], "free_bytes": 9}
        # /lora/inventory + /health — always "wrong"
        return {"inventory": [{"ref": "wrong"}], "free_bytes": 9}

    monkeypatch.setattr(f"{_HTTP}.post_json", _post)
    monkeypatch.setattr(f"{_HTTP}.get_json", _get)
    with pytest.raises(AssertionError, match="step-1-load-a"):
        matrix.run_matrix(
            cfg_path=Path("/x"),
            pod_proxy_url="http://stub",
            steps=_make_steps(),
            download_specs={
                "civitai:A@1": _spec("a"),
                "civitai:B@2": _spec("b"),
            },
            generate_per_step=False,
        )


def test_run_matrix_propagates_http_error_from_status_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: the retired 502-recovery swallowed any URLError (which in
    Python's urllib also catches HTTPError). A genuine HTTP error on the
    status GET (e.g. 500 Internal Server Error) must propagate, not be
    absorbed as a transient transport blip.

    The new ``_poll_swap_job`` catches HTTPError and URLError in separate
    branches; only URLError is swallowed (transient transport); HTTPError
    is re-raised immediately.
    """
    import email.message
    import urllib.error

    monkeypatch.setattr(
        f"{_HTTP}.post_json",
        lambda u, b, *, timeout: {"job_id": "j-http-err"},  # noqa: ARG005
    )

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        if "/health" in url:
            return {"ready": True}
        # Status poll raises a genuine 500.
        raise urllib.error.HTTPError(
            url, 500, "Internal Server Error", email.message.Message(), None
        )

    monkeypatch.setattr(f"{_HTTP}.get_json", _get)
    with pytest.raises(urllib.error.HTTPError) as exc:
        matrix.run_matrix(
            cfg_path=Path("/x"),
            pod_proxy_url="http://stub",
            steps=[
                matrix.MatrixStep(
                    name="step",
                    target_stack=["civitai:A@1"],
                    expected_inventory=["civitai:A@1"],
                ),
            ],
            download_specs={"civitai:A@1": _spec("a")},
            generate_per_step=False,
        )
    assert exc.value.code == 500


def test_run_matrix_distinct_sha_assertion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: runner accepts identical mp4 shas → LoRA swap had no
    measurable effect, false positive."""
    fixed_mp4 = tmp_path / "fixed.mp4"
    fixed_mp4.write_bytes(b"identical")

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        return {"job_id": "j-sha-test"}

    def _generate(cfg: Path, pod_id: str, prompt: str) -> Path:  # noqa: ARG001
        return fixed_mp4

    step_refs = [["civitai:A@1"], ["civitai:B@2"]]
    status_call_n = {"n": 0}
    inv_call_n = {"n": 0}

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        if "set_stack/status" in url:
            idx = status_call_n["n"]
            status_call_n["n"] += 1
            refs = step_refs[idx] if idx < len(step_refs) else []
            return {
                "state": "done",
                "inventory": [{"ref": r} for r in refs],
                "free_bytes": 9,
            }
        if "lora/inventory" in url:
            idx = inv_call_n["n"]
            inv_call_n["n"] += 1
            refs = step_refs[idx] if idx < len(step_refs) else []
            return {"inventory": [{"ref": r} for r in refs], "free_bytes": 9}
        return {"ready": True}

    monkeypatch.setattr(f"{_HTTP}.post_json", _post)
    monkeypatch.setattr(f"{_HTTP}.get_json", _get)
    monkeypatch.setattr(matrix, "_run_generate", _generate)
    with pytest.raises(AssertionError, match="sha"):
        matrix.run_matrix(
            cfg_path=tmp_path / "x.yaml",
            pod_proxy_url="http://stub",
            steps=_make_steps(),
            download_specs={
                "civitai:A@1": _spec("a"),
                "civitai:B@2": _spec("b"),
            },
            generate_per_step=True,
            sha_distinct_required=True,
            pod_id="pod-x",
        )


def test_run_matrix_polls_swap_job_to_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug caught: harness never learns the swap finished because it polls
    the wrong signal — POST response inventory vs job status endpoint."""
    posts: list[dict[str, Any]] = []

    def fake_post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        posts.append(body)
        return {"job_id": "s-1"}

    swap_status: list[dict[str, Any]] = [
        {"state": "running"},
        {"state": "done", "inventory": [{"ref": "civitai:A@1"}], "free_bytes": 9},
    ]

    def fake_get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        if "set_stack/status" in url:
            return swap_status.pop(0)
        if "/health" in url:
            return {"ready": True}
        return {"inventory": [{"ref": "civitai:A@1"}], "free_bytes": 9}

    monkeypatch.setattr(f"{_HTTP}.post_json", fake_post)
    monkeypatch.setattr(f"{_HTTP}.get_json", fake_get)
    monkeypatch.setattr("tests._smoke_harness.matrix.time.sleep", lambda s: None)
    report = matrix.run_matrix(
        cfg_path=Path("x"),
        pod_proxy_url="http://pod:8000",
        steps=[
            matrix.MatrixStep(
                name="s",
                target_stack=["civitai:A@1"],
                expected_inventory=["civitai:A@1"],
            )
        ],
        download_specs={"civitai:A@1": _spec("a")},
        generate_per_step=False,
    )
    assert report.steps[0].inventory_after == ["civitai:A@1"]


def test_run_matrix_surfaces_real_error_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug caught: the 3-week misdiagnosis — a real failure logged as
    `last observed []` instead of the server's error string.  Now the
    job error payload must surface verbatim in the AssertionError."""
    monkeypatch.setattr(
        f"{_HTTP}.post_json",
        lambda u, b, *, timeout: {"job_id": "s-9"},  # noqa: ARG005
    )

    def fake_get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        if "set_stack/status" in url:
            return {
                "state": "error",
                "error": {
                    "error": "lora_download_failed",
                    "status": 502,
                    "underlying": "connection reset",
                },
            }
        return {"ready": True}

    monkeypatch.setattr(f"{_HTTP}.get_json", fake_get)
    with pytest.raises(AssertionError, match="connection reset"):
        matrix.run_matrix(
            cfg_path=Path("x"),
            pod_proxy_url="http://pod:8000",
            steps=[
                matrix.MatrixStep(
                    name="s",
                    target_stack=["civitai:A@1"],
                    expected_inventory=["civitai:A@1"],
                )
            ],
            download_specs={"civitai:A@1": _spec("a")},
            generate_per_step=False,
        )
