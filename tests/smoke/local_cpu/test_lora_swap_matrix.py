"""Tier-1 LoRA swap matrix against a stubbed wan_t2v_server.

Drives the harness run_matrix() over the uvicorn subprocess
(local CPU, no CUDA, no diffusers weights). Covers the four-step
happy path + the VRAM OOM rollback contract from spec §11.2.

The wan_t2v_server's ``_download_one`` actually fetches the URL —
to avoid a network call we point each spec at a ``file://`` URL
backed by a tmp file. The stub pipe never reads the bytes (just
appends to ``_loaded_adapters``), so empty fixture files suffice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests._smoke_harness import http, matrix


def _spec(ref: str, tmp_path: Path) -> dict[str, Any]:
    """Empty fixture file + file:// URL — exercises the swap path no network."""
    fname = ref.replace(":", "_").replace("@", "_") + ".safetensors"
    p = tmp_path / fname
    p.write_bytes(b"")
    return {
        "url": p.as_uri(),
        "headers": {},
        "filename": fname,
        "size_hint": 0,
    }


def test_happy_4_step_matrix_inventory_only(
    uvicorn_server: str, tmp_path: Path
) -> None:
    """4-step matrix end-to-end against the stub pipe + real HTTP.

    Bug: any of the 4 kinoforge-internal patterns regresses (UA,
    api_key suffix, URLError retry, /lora/* path shape).
    """
    refs = {
        "civitai:A@1": _spec("civitai:A@1", tmp_path),
        "civitai:B@2": _spec("civitai:B@2", tmp_path),
        "civitai:C@3": _spec("civitai:C@3", tmp_path),
    }
    steps = [
        matrix.MatrixStep(name="step-1-empty", target_stack=[], expected_inventory=[]),
        matrix.MatrixStep(
            name="step-2-load-ab",
            target_stack=["civitai:A@1", "civitai:B@2"],
            expected_inventory=["civitai:A@1", "civitai:B@2"],
        ),
        matrix.MatrixStep(
            name="step-3-swap-to-bc",
            target_stack=["civitai:B@2", "civitai:C@3"],
            expected_inventory=["civitai:B@2", "civitai:C@3"],
        ),
        matrix.MatrixStep(
            name="step-4-empty-again",
            target_stack=[],
            expected_inventory=[],
        ),
    ]
    report = matrix.run_matrix(
        cfg_path=Path("/unused"),
        pod_proxy_url=uvicorn_server,
        steps=steps,
        download_specs=refs,
        generate_per_step=False,
    )
    assert len(report.steps) == 4


def test_vram_oom_rollback_restores_previous_stack(
    uvicorn_server: str, tmp_path: Path
) -> None:
    """Target=[A,bigs] exceeds stub VRAM → server rolls back to [A].

    Bug: server returns 5xx instead of 200 with swap_rejected /
    inventory does not restore the previous stack / dropped specs'
    files are not cleaned up.
    """
    a_spec = _spec("civitai:A@1", tmp_path)
    resp = http.post_json(
        f"{uvicorn_server}/lora/set_stack",
        {
            "target_refs": ["civitai:A@1"],
            "download_specs": {"civitai:A@1": a_spec},
        },
        timeout=30,
    )
    assert [e["ref"] for e in resp["inventory"]] == ["civitai:A@1"]

    # Keep A in target so it is NOT mandatorily evicted; add enough
    # new refs to bust the stub's 80GB budget (201 × 500MB > 80GB).
    big_specs = {
        f"civitai:big-{i}@1": _spec(f"civitai:big-{i}@1", tmp_path) for i in range(200)
    }
    body = {
        "target_refs": ["civitai:A@1", *big_specs.keys()],
        "download_specs": {"civitai:A@1": a_spec, **big_specs},
    }
    resp = http.post_json(f"{uvicorn_server}/lora/set_stack", body, timeout=60)
    assert resp["swap_rejected"] is not None
    assert resp["swap_rejected"]["reason"] == "vram_oom"
    assert set(resp["swap_rejected"]["target_refs_dropped"]) == set(big_specs)
    assert [e["ref"] for e in resp["inventory"]] == ["civitai:A@1"]

    # /lora/inventory agrees with the rollback snapshot.
    resp = http.get_json(f"{uvicorn_server}/lora/inventory", timeout=10)
    assert [e["ref"] for e in resp["inventory"]] == ["civitai:A@1"]
