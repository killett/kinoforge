"""Tier-4 live smoke: Wan 2.2 14B dual-transformer routing matrix on RunPod.

P2 §7.1 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Seven matrix cases verify per-transformer routing end-to-end against
a real Wan-2.2 14B A100 80GB pod. All cases share a single warm pod
via a module-scope fixture to amortize the cold-boot. Spend cap: $2
(matches P1's Tier-4 budget) — enforced via
``tests/_smoke_harness/budget.py:BudgetTracker``.

Cases (per spec §7.1):
  1. Baseline (no LoRA) — reference output.
  2. Arcane high-noise only — style diff vs baseline; early-step.
  3. Arcane low-noise only — style diff vs baseline; late-step.
  4. Arcane pair canonical (h+l) — both effects.
  5. Wrong routing (h→l, l→h) — generation succeeds but perceptibly
     off; wrong-routing sha != canonical sha is the "routing matters"
     proof.
  6. MoE + auto reject — 400 ``branch_auto_disallowed_on_moe``.
  7. Same ref in two branches (composite key) — both load + generate
     succeeds.

Gated by ``KINOFORGE_LIVE_TESTS=1``.
"""

from __future__ import annotations

import hashlib
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
CFG = REPO / "examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"

# Canonical Arcane Style [WAN 2.2 T2V] v1.0 pair (CivitAI model 2197303).
# Pinned by `examples/configs/wan.yaml` (Task 13 `5004cf2`); the Tier-4
# release cfg is intentionally Base-only and drives LoRA stacks via
# /lora/set_stack so the smoke owns the refs.
ARCANE_HIGH = "civitai:2197303@2474081"
ARCANE_LOW = "civitai:2197303@2474073"

_TAG = "kinoforge-smoke-tier-4"
# Bumped from 2.0 to 4.0 for the 2026-06-23 swap-gap re-fire per
# operator $4 budget override. Revert to 2.0 after the re-fire lands
# Tier-4 7/7 GREEN — single-SXM fires run ~$0.80 each (fire #3 baseline),
# so the standing cap is intentionally tight.
_BUDGET_CAP = 4.0

# Cross-test sha cache. Tests run in file order; later cases assert
# against earlier shas via this dict.
_shas: dict[str, str] = {}


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, f"no pod id in:\n{log_text[-2000:]}"
    return m.group(1)


def _cold_boot(prompt: str, log_path: Path) -> tuple[str, Path]:
    """Run cold-boot ``kinoforge generate``; return (pod_id, mp4_path).

    The cold-boot's generated mp4 IS the baseline (no LoRAs loaded),
    so case-1 reuses it via ``_shas["baseline"]`` rather than spending
    another generate-cycle to recompute the same shape. This also
    removes back-to-back generate pressure on the warm pod immediately
    after cold-boot (case-1's redundant generate stalled
    `8h91rjnslmzwab` on 2026-06-23 at ~30 min pod uptime even with
    GPU 100% — see PROGRESS Task 16 failure history).
    """
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
                "smoke-22b-branch-cold-boot",
            ],
            cwd=str(REPO),
            stdout=f,
            stderr=subprocess.STDOUT,
            timeout=3600,
            check=False,
        )
    text = log_path.read_text()
    assert proc.returncode == 0, f"cold-boot failed:\n{text[-3000:]}"
    pod_id = _extract_pod_id(text)
    mp4_path: Path | None = None
    for line in reversed(text.splitlines()):
        if line.startswith("generated: uri="):
            uri = line.split("=", 1)[1].strip().strip("'\"")
            mp4_path = Path(uri)
            break
    assert mp4_path is not None, (
        f"cold-boot succeeded but no 'generated: uri=' line found:\n{text[-3000:]}"
    )
    assert mp4_path.exists(), f"cold-boot mp4 {mp4_path} not on disk"
    return pod_id, mp4_path


def _generate_and_sha(pod_id: str, prompt: str, label: str) -> str:
    """Run `kinoforge generate --instance-id <pod>`; return mp4 sha256."""
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
            "--instance-id",
            pod_id,
            "--run-id",
            f"smoke-22b-branch-{label}",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, (
        f"generate({label}) failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    mp4_path: Path | None = None
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("generated: uri="):
            uri = line.split("=", 1)[1].strip().strip("'\"")
            mp4_path = Path(uri)
            break
    assert mp4_path is not None, (
        f"no 'generated:' line for {label}:\n{proc.stdout[-2000:]}"
    )
    assert mp4_path.exists(), f"mp4 path {mp4_path} not on disk for {label}"
    return hashlib.sha256(mp4_path.read_bytes()).hexdigest()


def _parse_http_error_body(exc: urllib.error.HTTPError) -> dict[str, object]:
    body_match = re.search(r"body=(['\"])(\{.*\})\1", exc.msg or "")
    assert body_match is not None, f"no JSON body in HTTPError.msg: {exc.msg!r}"
    body = json.loads(body_match.group(2))
    return body.get("detail", body)  # FastAPI nests under 'detail'


@pytest.fixture(scope="module")
def _warm_wan22_pod(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, str]]:
    """Cold-boot once, share across all 7 cases, destroy on teardown."""
    pre = subprocess.run(  # noqa: S603
        ["pixi", "run", "preflight"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert pre.returncode == 0, f"preflight failed:\n{pre.stdout}\n{pre.stderr}"

    tmp_dir = tmp_path_factory.mktemp("dual_transformer_routing_tier4")
    prompt = PROMPT_FILE.read_text().strip()
    pod_id, cold_boot_mp4 = _cold_boot(prompt, tmp_dir / "cold-boot.log")
    base_url = runpod_lifecycle.resolve_proxy_url(pod_id)
    poller = runpod_lifecycle.PodStatPoller(pod_id, tmp_dir / "pod-stats.log")
    poller.start()
    tracker = budget.BudgetTracker(cap_usd=_BUDGET_CAP, pod_id=pod_id)
    # Cold-boot mp4 IS the baseline (no LoRAs); cache its sha so case-1
    # only validates set_stack(empty) shape without burning another
    # generate cycle.
    _shas["baseline"] = hashlib.sha256(cold_boot_mp4.read_bytes()).hexdigest()
    try:
        from tests._smoke_harness.matrix import _warmup_proxy

        _warmup_proxy(base_url)
        yield {"pod_id": pod_id, "base_url": base_url, "prompt": prompt}
    finally:
        poller.stop()
        poller.join(timeout=2.0)
        try:
            tracker.assert_under_cap()
        finally:
            # Closes the 2026-06-23 money leak: sweep + targeted
            # subprocess fallback + post-condition probe in one call,
            # raises AssertionError if the pod is still alive after
            # both layers run.
            runpod_lifecycle.teardown_pod_or_raise(pod_id, repo_root=REPO)


def test_case_1_baseline_no_lora(_warm_wan22_pod: dict[str, str]) -> None:
    """Bug catch: cold-boot of empty LoRA stack regresses on Wan 2.2 —
    the routing path adds spurious arity validation that rejects an
    empty stack. The baseline sha is captured directly from the
    fixture's cold-boot mp4; this case only proves the empty-target
    HTTP path returns 200 + empty inventory on the warm pod."""
    base_url = _warm_wan22_pod["base_url"]
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {"target": [], "download_specs": {}},
        timeout=900,
    )
    assert resp["inventory"] == [], f"expected empty inventory, got {resp['inventory']}"
    assert "baseline" in _shas, "fixture failed to seed cold-boot baseline sha"


def test_case_2_arcane_high_noise_only(_warm_wan22_pod: dict[str, str]) -> None:
    """Bug catch: ``branch=high_noise`` silently lands into
    ``transformer_2`` (or both), defeating per-stage routing.
    Verification: generated sha differs from baseline (LoRA actually
    applied)."""
    base_url = _warm_wan22_pod["base_url"]
    spec_h = civitai.resolve(ARCANE_HIGH)
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {
            "target": [
                {"ref": ARCANE_HIGH, "strength": 1.0, "branch": "high_noise"},
            ],
            "download_specs": {ARCANE_HIGH: spec_h},
        },
        timeout=900,
    )
    inv = resp["inventory"]
    assert len(inv) == 1, f"expected exactly 1 inventory row, got {inv}"
    assert inv[0]["ref"] == ARCANE_HIGH
    assert inv[0]["branch"] == "high_noise"
    sha = _generate_and_sha(
        _warm_wan22_pod["pod_id"], _warm_wan22_pod["prompt"], "h-only"
    )
    _shas["h_only"] = sha
    assert "baseline" in _shas, "case_1 did not run; cannot compare h-only vs baseline"
    assert sha != _shas["baseline"], (
        f"high-noise-only sha={sha} matches baseline={_shas['baseline']} — "
        f"LoRA not actually applied"
    )


def test_case_3_arcane_low_noise_only(_warm_wan22_pod: dict[str, str]) -> None:
    """Bug catch: ``branch=low_noise`` silently lands into the bare
    ``transformer`` because ``load_into_transformer_2`` kwarg was dropped
    from the wire payload."""
    base_url = _warm_wan22_pod["base_url"]
    spec_l = civitai.resolve(ARCANE_LOW)
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {
            "target": [
                {"ref": ARCANE_LOW, "strength": 1.0, "branch": "low_noise"},
            ],
            "download_specs": {ARCANE_LOW: spec_l},
        },
        timeout=900,
    )
    inv = resp["inventory"]
    assert len(inv) == 1, f"expected exactly 1 inventory row, got {inv}"
    assert inv[0]["ref"] == ARCANE_LOW
    assert inv[0]["branch"] == "low_noise"
    sha = _generate_and_sha(
        _warm_wan22_pod["pod_id"], _warm_wan22_pod["prompt"], "l-only"
    )
    _shas["l_only"] = sha
    assert "baseline" in _shas and "h_only" in _shas, (
        "cases 1+2 did not run; cannot compare l-only"
    )
    assert sha != _shas["baseline"], (
        f"low-noise-only sha={sha} matches baseline — LoRA not applied"
    )
    assert sha != _shas["h_only"], (
        f"low-noise-only sha={sha} matches h-only — routing not distinguishing "
        f"high/low transformer"
    )


def test_case_4_arcane_pair_canonical_high_plus_low(
    _warm_wan22_pod: dict[str, str],
) -> None:
    """Bug catch: routing regression silently degrades the canonical
    pair output. Verifies the (high_noise, low_noise) pair generates
    AND produces a sha distinct from each single-branch case."""
    base_url = _warm_wan22_pod["base_url"]
    spec_h = civitai.resolve(ARCANE_HIGH)
    spec_l = civitai.resolve(ARCANE_LOW)
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {
            "target": [
                {"ref": ARCANE_HIGH, "strength": 1.0, "branch": "high_noise"},
                {"ref": ARCANE_LOW, "strength": 1.0, "branch": "low_noise"},
            ],
            "download_specs": {ARCANE_HIGH: spec_h, ARCANE_LOW: spec_l},
        },
        timeout=900,
    )
    inv = resp["inventory"]
    assert len(inv) == 2, f"expected 2 inventory rows, got {inv}"
    branch_to_ref = {row["branch"]: row["ref"] for row in inv}
    assert branch_to_ref == {
        "high_noise": ARCANE_HIGH,
        "low_noise": ARCANE_LOW,
    }, f"canonical-pair inventory wrong: {inv}"
    sha = _generate_and_sha(
        _warm_wan22_pod["pod_id"], _warm_wan22_pod["prompt"], "canonical-pair"
    )
    _shas["canonical_pair"] = sha
    assert sha != _shas.get("h_only"), (
        "canonical-pair sha matches h-only — low_noise LoRA had no effect"
    )
    assert sha != _shas.get("l_only"), (
        "canonical-pair sha matches l-only — high_noise LoRA had no effect"
    )


def test_case_5_wrong_routing_h_into_low_and_l_into_high(
    _warm_wan22_pod: dict[str, str],
) -> None:
    """Bug catch: routing is a no-op — wrong-routing sha equals canonical
    sha, meaning the per-transformer dispatch doesn't actually reach the
    transformers. Invariant: wrong_routing_sha != canonical_sha."""
    base_url = _warm_wan22_pod["base_url"]
    spec_h = civitai.resolve(ARCANE_HIGH)
    spec_l = civitai.resolve(ARCANE_LOW)
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {
            # SWAPPED: high-noise ref tagged low_noise, low-noise ref tagged high_noise.
            "target": [
                {"ref": ARCANE_HIGH, "strength": 1.0, "branch": "low_noise"},
                {"ref": ARCANE_LOW, "strength": 1.0, "branch": "high_noise"},
            ],
            "download_specs": {ARCANE_HIGH: spec_h, ARCANE_LOW: spec_l},
        },
        timeout=900,
    )
    inv = resp["inventory"]
    assert len(inv) == 2, f"expected 2 inventory rows, got {inv}"
    branch_to_ref = {row["branch"]: row["ref"] for row in inv}
    assert branch_to_ref == {
        "low_noise": ARCANE_HIGH,
        "high_noise": ARCANE_LOW,
    }, f"wrong-routing inventory wrong: {inv}"
    sha = _generate_and_sha(
        _warm_wan22_pod["pod_id"], _warm_wan22_pod["prompt"], "wrong-routing"
    )
    _shas["wrong_routing"] = sha
    assert "canonical_pair" in _shas, (
        "case_4 did not run; cannot verify routing-matters invariant"
    )
    assert sha != _shas["canonical_pair"], (
        f"wrong-routing sha={sha} matches canonical-pair sha — "
        f"per-transformer routing is not actually routing"
    )


def test_case_6_moe_with_auto_branch_returns_400(
    _warm_wan22_pod: dict[str, str],
) -> None:
    """Bug catch: server accepts ``auto`` on Wan 2.2 and routes the
    LoRA into ``pipe.transformer`` only — silently half-applies the
    stack. Pre-load validation gate must reject before any unload/load
    fires."""
    base_url = _warm_wan22_pod["base_url"]
    spec_h = civitai.resolve(ARCANE_HIGH)
    with pytest.raises(urllib.error.HTTPError) as ei:
        http.post_json(
            f"{base_url.rstrip('/')}/lora/set_stack",
            {
                "target": [
                    {"ref": ARCANE_HIGH, "strength": 1.0, "branch": "auto"},
                ],
                "download_specs": {ARCANE_HIGH: spec_h},
            },
            timeout=900,
        )
    assert ei.value.code == 400, (
        f"expected HTTP 400, got {ei.value.code}: {ei.value.msg!r}"
    )
    detail = _parse_http_error_body(ei.value)
    assert detail["error"] == "branch_routing"
    assert detail["reason"] == "branch_auto_disallowed_on_moe"
    assert detail["arity"] == 2


def test_case_7_same_ref_in_both_branches_composite_key(
    _warm_wan22_pod: dict[str, str],
) -> None:
    """Bug catch: composite key collapse — inventory keys (ref, branch)
    accidentally reduce to ref-only, so the second-branch entry
    overwrites the first. Verification: /lora/inventory shows TWO rows
    for the same ref with different branches; generate succeeds."""
    base_url = _warm_wan22_pod["base_url"]
    spec_h = civitai.resolve(ARCANE_HIGH)
    resp = http.post_json(
        f"{base_url.rstrip('/')}/lora/set_stack",
        {
            "target": [
                {"ref": ARCANE_HIGH, "strength": 1.0, "branch": "high_noise"},
                {"ref": ARCANE_HIGH, "strength": 0.8, "branch": "low_noise"},
            ],
            "download_specs": {ARCANE_HIGH: spec_h},
        },
        timeout=900,
    )
    inv = resp["inventory"]
    assert len(inv) == 2, (
        f"expected 2 inventory rows for composite key (ref, branch), got {inv}"
    )
    keys = sorted((row["ref"], row["branch"]) for row in inv)
    assert keys == [
        (ARCANE_HIGH, "high_noise"),
        (ARCANE_HIGH, "low_noise"),
    ], f"composite-key inventory wrong: {inv}"
    # Strength echoed per-row.
    strength_by_branch = {row["branch"]: row.get("last_strength") for row in inv}
    assert strength_by_branch["high_noise"] == pytest.approx(1.0)
    assert strength_by_branch["low_noise"] == pytest.approx(0.8)
    sha = _generate_and_sha(
        _warm_wan22_pod["pod_id"], _warm_wan22_pod["prompt"], "same-ref-two-branches"
    )
    _shas["composite"] = sha
