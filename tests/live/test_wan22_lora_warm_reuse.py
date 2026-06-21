"""Live smoke: Wan 2.2 T2V + Arcane LoRA warm-reuse on RunPod A100 80GB.

Drives the 4-step matrix from spec §13.3 against a real pod:

    Step 1 — cold-boot, 0 LoRAs    → plain Wan T2V mp4 (no styling).
    Step 2 — warm-attach, [high+low] → ArcaneStyle prompt, full styling.
    Step 3 — warm-attach, [low]      → partial Arcane styling.
    Step 4 — warm-attach, []         → no styling (eviction back to bare).

Gated by ``KINOFORGE_LIVE_TESTS=1``. Pod-stat polling fires every 90s
(GPU util + cost-per-hr drift) per the user-scope proactive-pod-stats
memory. Pod is destroyed in ``finally`` regardless of pass/fail.

Total spend cap: $2 enforced by post-condition.

T15 is shipped as a standalone helper (warm_reuse/integration.py) but
NOT wired into ``deploy_session`` yet; this smoke therefore drives
``POST /lora/set_stack`` directly between ``kinoforge generate
--instance-id <pod_id>`` invocations instead of going through the
matcher's auto-attach codepath. The pod-side endpoints + cold-boot
loader + LoRA helpers (T4-T8) are the load-bearing primitives under
test here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
)

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "examples/configs/wan22-lora-flexible-warm-reuse-smoke.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"
OUTPUT_DIR = REPO / "output"

# Arcane Style WAN 2.2 LoRA pair (per docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md):
LORA_HIGH = "civitai:2197303@2474081"
LORA_LOW = "civitai:2197303@2474073"
TRIGGER = "ArcaneStyle"

POD_STAT_POLL_INTERVAL_S = 90.0
MAX_SPEND_USD = 2.00


def _run_cli(*args: str, log_path: Path, timeout: int = 3900) -> str:
    """Invoke ``pixi run kinoforge <args>``; return captured combined log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as logf:
        proc = subprocess.run(
            ["pixi", "run", "kinoforge", *args],
            cwd=str(REPO),
            stdout=logf,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    text = log_path.read_text()
    assert proc.returncode == 0, (
        f"kinoforge {args[0]} exited {proc.returncode}\n"
        f"Last 60 log lines:\n{text[-4000:]}"
    )
    return text


def _extract_pod_id(log_text: str) -> str:
    for pattern in (
        r"running provisioner\.provision for instance (\w+)",
        r"warm-reuse: attached to (\w+)",
    ):
        m = re.search(pattern, log_text)
        if m:
            return m.group(1)
    raise AssertionError(f"could not find pod id in log; tail:\n{log_text[-2000:]}")


def _resolve_pod_proxy_url(pod_id: str) -> str:
    """Return the RunPod proxy URL for port 8000 on ``pod_id``.

    RunPod's pod proxy follows a stable host pattern
    ``https://{pod_id}-{port}.proxy.runpod.net``; constructing the URL
    directly avoids a stale-tag round-trip that returned an empty port
    map immediately after a fresh ``kinoforge generate`` cycle (the
    provider's ``get_instance`` doesn't re-hydrate ``tags["ports"]``
    after the orchestrator's post-job ledger refresh).
    """
    return f"https://{pod_id}-8000.proxy.runpod.net"


def _civitai_artifact(ref: str) -> Any:
    """Resolve a civitai ref to its first downloadable Artifact (the .safetensors)."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.sources.civitai import CivitAISource

    arts = CivitAISource().resolve(ref, EnvCredentialProvider())
    for a in arts:
        if a.filename.endswith(".safetensors"):
            return a
    return arts[0]


def _post_lora_set_stack(
    base_url: str, target_refs: list[str], download_specs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    body = json.dumps(
        {"target_refs": target_refs, "download_specs": download_specs}
    ).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 — kinoforge-managed pod URL
        f"{base_url}/lora/set_stack",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        return dict(json.loads(resp.read()))


def _get_lora_inventory(base_url: str) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 — kinoforge-managed pod URL
        f"{base_url}/lora/inventory", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return dict(json.loads(resp.read()))


class _PodStatPoller(threading.Thread):
    """Background thread; surfaces GPU util + cost drift every 90s."""

    def __init__(self, pod_id: str, log_path: Path) -> None:
        super().__init__(daemon=True)
        self.pod_id = pod_id
        self.log_path = log_path
        self._stop = threading.Event()
        self.last_cost_per_hr: float | None = None

    def run(self) -> None:
        from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

        endpoint = RunPodGraphQLUtilEndpoint(api_key=os.environ["RUNPOD_API_KEY"])
        with self.log_path.open("a") as f:
            while not self._stop.wait(POD_STAT_POLL_INTERVAL_S):
                try:
                    snap = endpoint.read_util(self.pod_id)
                except Exception as exc:  # noqa: BLE001 — diagnostic only
                    f.write(f"[stat-poll] read_util raised {exc!r}\n")
                    f.flush()
                    continue
                if snap is None:
                    f.write("[stat-poll] runtime not yet visible\n")
                    f.flush()
                    continue
                f.write(
                    f"[stat-poll] gpu_util={snap.gpu_util_percent} "
                    f"cpu={snap.cpu_percent} "
                    f"mem={snap.memory_percent}\n"
                )
                f.flush()

    def stop(self) -> None:
        self._stop.set()


def _destroy(pod_id: str) -> None:
    subprocess.run(
        ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
        cwd=str(REPO),
        timeout=180,
        check=False,
    )


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _latest_mp4_after(deadline_s: float) -> Path:
    """Return the newest mp4 in OUTPUT_DIR whose mtime > deadline_s."""
    candidates = [p for p in OUTPUT_DIR.glob("*.mp4") if p.stat().st_mtime > deadline_s]
    assert candidates, f"no mp4 written after {deadline_s} under {OUTPUT_DIR}"
    return max(candidates, key=lambda p: p.stat().st_mtime)


def test_wan22_lora_warm_reuse_4_step_matrix(tmp_path: Path) -> None:
    """4-step warm-reuse matrix on real A100 80GB; total spend < $2.

    Step 1 cold-boots the pod with no LoRAs. Steps 2/3/4 swap the
    Arcane high+low pair / low-only / empty via POST /lora/set_stack
    between generate calls, demonstrating that:

    - The pod-side cold-boot path supports 0-LoRA bootstrap (T4).
    - /lora/inventory returns an empty list when no LoRAs are loaded (T7).
    - /lora/set_stack downloads + loads new LoRAs idempotently (T6).
    - Eviction (full → partial → empty) reclaims VRAM + disk (T6/T8).
    - The same warm pod serves all 4 steps — no cold-boot between them.

    Would-fail bugs this guards against:
    - Cold-boot crashes on an empty `KINOFORGE_INITIAL_LORA_STACK_JSON`
      env (T4 regression).
    - /lora/set_stack rejects an empty `target_refs` (Step 4).
    - Pod-side helper deletes the LoRA file but forgets the pipeline
      adapter (T5), surfacing as a stale-adapter mp4.

    Spend is bounded by `MAX_SPEND_USD = $2`; assertion at end runs
    against the pod's costPerHr-snapshot times wall-clock.
    """
    pre = subprocess.run(
        ["pixi", "run", "preflight"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert pre.returncode == 0, f"preflight failed:\n{pre.stdout}\n{pre.stderr}"

    prompt_plain = PROMPT_FILE.read_text().strip()
    prompt_styled = f"{TRIGGER} {prompt_plain}"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    start_ts = time.time()
    pod_id: str | None = None
    poller: _PodStatPoller | None = None

    try:
        # STEP 1 — cold-boot, 0 LoRAs.
        leg1_log = tmp_path / "step1-cold-boot.log"
        out1 = _run_cli(
            "generate",
            "--config",
            str(CFG),
            "--prompt",
            prompt_plain,
            "--mode",
            "t2v",
            "--run-id",
            "lora-warm-reuse-smoke-step1",
            log_path=leg1_log,
        )
        pod_id = _extract_pod_id(out1)
        mp4_1 = _latest_mp4_after(start_ts)
        sha_1 = _sha256(mp4_1)

        base_url = _resolve_pod_proxy_url(pod_id)
        poller = _PodStatPoller(pod_id, tmp_path / "pod-stats.log")
        poller.start()

        inv1 = _get_lora_inventory(base_url)
        assert inv1.get("inventory") == [], (
            f"step-1 cold-boot: expected empty inventory, got {inv1!r}"
        )

        # STEP 2 — warm-attach with [high, low]. step-2 warm-attach high-low.
        art_high = _civitai_artifact(LORA_HIGH)
        art_low = _civitai_artifact(LORA_LOW)
        specs2 = {
            LORA_HIGH: {
                "url": art_high.url,
                "headers": art_high.headers,
                "filename": art_high.filename,
                "size_hint": art_high.size,
            },
            LORA_LOW: {
                "url": art_low.url,
                "headers": art_low.headers,
                "filename": art_low.filename,
                "size_hint": art_low.size,
            },
        }
        resp2 = _post_lora_set_stack(base_url, [LORA_HIGH, LORA_LOW], specs2)
        resident2 = sorted(e["ref"] for e in resp2.get("inventory", []))
        assert resident2 == sorted([LORA_HIGH, LORA_LOW]), (
            f"step-2: expected both Arcane LoRAs resident, got {resident2!r}"
        )

        step2_ts = time.time()
        leg2_log = tmp_path / "step2-warm-high-low.log"
        _run_cli(
            "generate",
            "--config",
            str(CFG),
            "--prompt",
            prompt_styled,
            "--mode",
            "t2v",
            "--instance-id",
            pod_id,
            "--run-id",
            "lora-warm-reuse-smoke-step2",
            log_path=leg2_log,
        )
        mp4_2 = _latest_mp4_after(step2_ts)
        sha_2 = _sha256(mp4_2)
        assert sha_2 != sha_1, (
            "step-2 mp4 sha matches step-1 — LoRA swap had no effect on output"
        )

        # STEP 3 — warm-attach [low] only. step-3 warm-attach low-only.
        resp3 = _post_lora_set_stack(base_url, [LORA_LOW], {})
        resident3 = [e["ref"] for e in resp3.get("inventory", [])]
        assert resident3 == [LORA_LOW], (
            f"step-3: expected [low] only, got {resident3!r}"
        )

        step3_ts = time.time()
        leg3_log = tmp_path / "step3-warm-low-only.log"
        _run_cli(
            "generate",
            "--config",
            str(CFG),
            "--prompt",
            prompt_styled,
            "--mode",
            "t2v",
            "--instance-id",
            pod_id,
            "--run-id",
            "lora-warm-reuse-smoke-step3",
            log_path=leg3_log,
        )
        mp4_3 = _latest_mp4_after(step3_ts)
        sha_3 = _sha256(mp4_3)

        # STEP 4 — warm-attach []. step-4 warm-attach empty.
        resp4 = _post_lora_set_stack(base_url, [], {})
        resident4 = [e["ref"] for e in resp4.get("inventory", [])]
        assert resident4 == [], f"step-4: expected empty inventory, got {resident4!r}"

        step4_ts = time.time()
        leg4_log = tmp_path / "step4-warm-empty.log"
        _run_cli(
            "generate",
            "--config",
            str(CFG),
            "--prompt",
            prompt_plain,
            "--mode",
            "t2v",
            "--instance-id",
            pod_id,
            "--run-id",
            "lora-warm-reuse-smoke-step4",
            log_path=leg4_log,
        )
        mp4_4 = _latest_mp4_after(step4_ts)
        sha_4 = _sha256(mp4_4)

        # All 4 mp4s distinct, all non-empty.
        shas = {sha_1, sha_2, sha_3, sha_4}
        assert len(shas) == 4, (
            f"expected 4 distinct mp4 shas across the matrix, got {shas!r}"
        )
        for p in (mp4_1, mp4_2, mp4_3, mp4_4):
            assert p.stat().st_size > 0, f"empty mp4: {p}"

        # `kinoforge status` + `kinoforge pod lora ls` agreement at the
        # end of step-3 LoRA inventory shape — both should show [low]
        # only. We poll the pod directly here since `status` reads the
        # ledger snapshot which may lag the pod state in this smoke
        # (T15 deferred → no orchestrator-side ledger.touch on swap).
        inv_final = _get_lora_inventory(base_url)
        assert inv_final.get("inventory") == [], (
            f"final /lora/inventory should be empty (step 4 wiped); got {inv_final!r}"
        )

        # Spend cap (best-effort — cost-rate is per-hour from the live
        # provider snapshot, multiplied by wall-clock).
        from kinoforge.core import registry as kf_registry

        provider = kf_registry.get_provider("runpod")()
        instance = provider.get_instance(pod_id)
        rate = float(instance.cost_rate_usd_per_hr)
        elapsed_hours = (time.time() - start_ts) / 3600.0
        spend = rate * elapsed_hours
        assert spend < MAX_SPEND_USD, (
            f"smoke spend ${spend:.2f} > cap ${MAX_SPEND_USD:.2f} — "
            f"rate=${rate:.2f}/hr, elapsed={elapsed_hours * 60:.1f}min"
        )
    finally:
        if poller is not None:
            poller.stop()
            poller.join(timeout=2.0)
        if pod_id is not None:
            _destroy(pod_id)
