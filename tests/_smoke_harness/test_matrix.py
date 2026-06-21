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
    that ack'd set_stack but didn't actually load."""
    set_stack_calls: list[dict[str, Any]] = []

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        set_stack_calls.append(body)
        return {
            "inventory": [{"ref": r} for r in body["target_refs"]],
            "free_bytes": 9,
        }

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        return {"inventory": [], "free_bytes": 9}

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


def test_run_matrix_raises_on_inventory_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: runner accepts wrong post-state → smoke passes against
    broken pod."""

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        return {"inventory": [{"ref": "wrong"}], "free_bytes": 9}

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
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


def test_run_matrix_recovers_from_proxy_502_via_inventory_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a proxy 502 on /lora/set_stack aborts the matrix even when
    the upstream wan server is still processing the download. Runner
    must catch 502 + poll /lora/inventory until it matches the target.
    """
    import email.message
    import urllib.error

    set_stack_calls: list[int] = []
    inventory_calls: list[int] = []
    inventory_response_idx = iter(
        [
            {"inventory": [], "free_bytes": 9},  # 1st poll: still downloading
            {
                "inventory": [{"ref": "civitai:A@1"}],
                "free_bytes": 9,
            },  # 2nd: converged
        ]
    )

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        set_stack_calls.append(1)
        raise urllib.error.HTTPError(
            url, 502, "Bad Gateway", email.message.Message(), None
        )

    def _get(url: str, *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        inventory_calls.append(1)
        return next(inventory_response_idx)

    monkeypatch.setattr(f"{_HTTP}.post_json", _post)
    monkeypatch.setattr(f"{_HTTP}.get_json", _get)
    # Compress the poll interval so the test stays fast.
    monkeypatch.setattr("tests._smoke_harness.matrix.time.sleep", lambda s: None)
    report = matrix.run_matrix(
        cfg_path=Path("/x"),
        pod_proxy_url="http://stub",
        steps=[
            matrix.MatrixStep(
                name="step-1-load-a",
                target_stack=["civitai:A@1"],
                expected_inventory=["civitai:A@1"],
            ),
        ],
        download_specs={"civitai:A@1": _spec("a")},
        generate_per_step=False,
    )
    assert len(report.steps) == 1
    assert report.steps[0].inventory_after == ["civitai:A@1"]
    assert len(set_stack_calls) == 1
    assert len(inventory_calls) == 2


def test_run_matrix_propagates_non_502_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: 502-recovery accidentally swallows 4xx (e.g. 403 auth failure)
    that should fail loud immediately."""
    import email.message
    import urllib.error

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        raise urllib.error.HTTPError(
            url, 403, "Forbidden", email.message.Message(), None
        )

    monkeypatch.setattr(f"{_HTTP}.post_json", _post)
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
    assert exc.value.code == 403


def test_run_matrix_distinct_sha_assertion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: runner accepts identical mp4 shas → LoRA swap had no
    measurable effect, false positive."""
    fixed_mp4 = tmp_path / "fixed.mp4"
    fixed_mp4.write_bytes(b"identical")

    def _post(url: str, body: dict[str, Any], *, timeout: int) -> dict[str, Any]:  # noqa: ARG001
        return {
            "inventory": [{"ref": r} for r in body["target_refs"]],
            "free_bytes": 9,
        }

    def _generate(cfg: Path, pod_id: str, prompt: str) -> Path:  # noqa: ARG001
        return fixed_mp4

    monkeypatch.setattr(f"{_HTTP}.post_json", _post)
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
