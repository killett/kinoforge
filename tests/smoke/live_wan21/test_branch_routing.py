"""Tier-3 live smoke: branch routing against real Wan 2.1 (1.3B) on RunPod.

P2 §7.1 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Two pinned invariants on a single warm pod:

  - ``branch="auto"`` on a Wan-2.1 (single-transformer) pipeline →
    /lora/set_stack returns HTTP 200; inventory carries the entry with
    ``branch="auto"``.  ``auto`` IS the single-transformer-only value
    (Q5 strict-reject contract).
  - ``branch="high_noise"`` on the same Wan-2.1 pod → /lora/set_stack
    returns HTTP 400 with structured ``branch_routing`` /
    ``branch_unsupported_single_transformer`` body.

Gated by ``KINOFORGE_LIVE_TESTS=1``.  Spend cap: $0.30 (Wan 2.1 1.3B
on RunPod A5000 — typical $0.10-0.15/fire).

The two test functions share a session-scoped pod via the
``_warm_wan21_pod`` fixture so the cold-boot tax is paid once.  Both
tests assert against the same pod via direct ``/lora/set_stack`` POSTs
on the existing smoke harness http client.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._smoke_harness import budget, civitai, http, runpod_lifecycle

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
        reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
    ),
]


REPO = Path(__file__).resolve().parents[3]
CFG = (
    REPO
    / "examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml"
)
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

_TAG = "kinoforge-smoke-tier-3-branch"
_BUDGET_CAP = 0.30


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, f"no pod id in:\n{log_text[-2000:]}"
    return m.group(1)


def _cold_boot(prompt: str, log_path: Path) -> str:
    """Run cold-boot ``kinoforge generate``; return pod_id."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as f:
        proc = subprocess.run(  # noqa: S603
            [
                "pixi",
                "run",
                "kinoforge",
                "generate",
                "--config",
                str(CFG),
                "--prompt",
                prompt,
                "--mode",
                "t2v",
                "--run-id",
                "smoke-21b-branch-cold-boot",
            ],
            cwd=str(REPO),
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=1500,
            check=False,
        )
    text = log_path.read_text()
    assert proc.returncode == 0, f"cold-boot failed:\n{text[-3000:]}"
    return _extract_pod_id(text)


@pytest.fixture(scope="module")
def _warm_wan21_pod(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, str]]:
    """Cold-boot once, share across both branch tests, destroy on teardown."""
    pre = subprocess.run(  # noqa: S603
        ["pixi", "run", "preflight"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert pre.returncode == 0, f"preflight failed:\n{pre.stdout}\n{pre.stderr}"

    tmp_dir = tmp_path_factory.mktemp("branch_routing_tier3")
    prompt = PROMPT_FILE.read_text().strip()
    pod_id = _cold_boot(prompt, tmp_dir / "cold-boot.log")
    base_url = runpod_lifecycle.resolve_proxy_url(pod_id)
    poller = runpod_lifecycle.PodStatPoller(pod_id, tmp_dir / "pod-stats.log")
    poller.start()
    try:
        # matrix.run_matrix's _warmup_proxy semantics — keep behavior
        # consistent with the existing Wan 2.1 smoke.
        from tests._smoke_harness.matrix import _warmup_proxy

        _warmup_proxy(base_url)
        yield {"pod_id": pod_id, "base_url": base_url}
    finally:
        poller.stop()
        poller.join(timeout=2.0)
        budget.BudgetTracker(cap_usd=_BUDGET_CAP, pod_id=pod_id).assert_under_cap()
        # See test_dual_transformer_routing for the post-condition rationale.
        runpod_lifecycle.teardown_pod_or_raise(pod_id)


def test_auto_branch_succeeds_on_wan21(
    _warm_wan21_pod: dict[str, str], tmp_path: Path
) -> None:
    """Wan 2.1 (single transformer) accepts ``branch="auto"`` and the
    pod-side inventory mirrors the field.

    Bug catch: a future server edit hard-requires explicit h/l on every
    pipeline, regressing Wan 2.1 deployments that legitimately use
    ``auto``.  The /lora/inventory branch field must echo the value
    the orchestrator shipped on the wire.
    """
    base_url = _warm_wan21_pod["base_url"]
    cfg = __import__("yaml").safe_load(CFG.read_text())
    lora_a: str = cfg["smoke"]["lora_a"]
    spec = civitai.resolve(lora_a)
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {
            "target": [{"ref": lora_a, "strength": 1.0, "branch": "auto"}],
            "download_specs": {lora_a: spec},
        },
        timeout=900,
    )
    inv = resp["inventory"]
    assert len(inv) == 1, f"expected exactly 1 inventory row, got {inv}"
    assert inv[0]["ref"] == lora_a
    assert inv[0]["branch"] == "auto"


def test_explicit_high_noise_branch_rejected_on_wan21(
    _warm_wan21_pod: dict[str, str], tmp_path: Path
) -> None:
    """Wan 2.1 rejects an explicit ``branch="high_noise"`` request with
    HTTP 400 + structured ``branch_routing`` body.

    Bug catch: server silently collapses the explicit branch to the
    bare ``transformer`` (Q5 lenient-collapse violation) and returns
    200 with a successful-looking response.
    """
    base_url = _warm_wan21_pod["base_url"]
    cfg = __import__("yaml").safe_load(CFG.read_text())
    lora_b: str = cfg["smoke"]["lora_b"]
    spec = civitai.resolve(lora_b)
    with pytest.raises(urllib.error.HTTPError) as ei:
        http.post_json(
            f"{base_url.rstrip('/')}/lora/set_stack",
            {
                "target": [{"ref": lora_b, "strength": 1.0, "branch": "high_noise"}],
                "download_specs": {lora_b: spec},
            },
            timeout=900,
        )
    assert ei.value.code == 400, (
        f"expected HTTP 400, got {ei.value.code}: {ei.value.msg!r}"
    )
    body_match = re.search(r"body=(['\"])(\{.*\})\1", ei.value.msg or "")
    assert body_match is not None, f"no JSON body in HTTPError.msg: {ei.value.msg!r}"
    body = json.loads(body_match.group(2))
    detail = body.get("detail", body)  # FastAPI nests under 'detail'
    assert detail["error"] == "branch_routing"
    assert detail["reason"] == "branch_unsupported_single_transformer"
    assert detail["branch"] == "high_noise"
    assert detail["arity"] == 1
