"""B3 Task j — live RunPod smoke: two-generation warm-reuse round-trip.

Gated by KINOFORGE_LIVE_RUNPOD=1 (plus the standard
KINOFORGE_LIVE_TESTS + RUNPOD_API_KEY + RUNPOD_TERMINATE_KEY + HF_TOKEN
creds bundle the Layer P live suite already requires). Live spend
≤$2.50 per spec §1.1.

Per feedback_standard_test_prompt, the prompt body is read VERBATIM
from /workspace/prompt-field-realistic.txt — no paraphrase, no
per-test override.

Per CLAUDE.md durability rule, this file is committed in RED state
(skipped by default) BEFORE the live invocation. Mid-spend crash
leaves the scaffold in git; the next session re-fires with no
catch-up work.

Smoke contract:
  1. Run ``kinoforge generate`` cold → capture pod_id_1.
  2. Sleep 30s (well under default 7200s idle_timeout).
  3. Run ``kinoforge generate`` again → capture pod_id_2.
  4. Assert pod_id_2 == pod_id_1 (B3 auto-discovery fired).
  5. Assert second invocation logged ``warm-reuse: attached to``.
  6. Assert gen 2 wall-elapsed < 70% of gen 1 (warm-skip evidence).
  7. Cleanup: ``kinoforge destroy --id <pod_id>``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

PROMPT_PATH = Path("/workspace/prompt-field-realistic.txt")
CFG_TEMPLATE = Path("/workspace/tests/live/cfg_b3_warm_attach.yaml")

if not (
    os.getenv("KINOFORGE_LIVE_RUNPOD") == "1"
    and os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
    and os.getenv("HF_TOKEN")
):
    pytest.skip(
        "live smoke requires KINOFORGE_LIVE_RUNPOD=1 + "
        "KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY + "
        "RUNPOD_TERMINATE_KEY + HF_TOKEN",
        allow_module_level=True,
    )


def _extract_pod_id_from_ledger(state_dir: Path) -> str | None:
    """Read the most recent pod id from the local-store ledger.

    The CLI writes the ledger at ``<state_dir>/_lifecycle/ledger.json``
    via :class:`LocalArtifactStore`.

    Args:
        state_dir: The kinoforge ``--state-dir`` path passed to both
            invocations.

    Returns:
        The pod id of the most recently created ledger entry, or
        ``None`` when the ledger is absent or empty.
    """
    ledger_path = state_dir / "_lifecycle" / "ledger.json"
    if not ledger_path.exists():
        return None
    data = json.loads(ledger_path.read_text())
    entries = data.get("entries", [])
    if not entries:
        return None
    return str(max(entries, key=lambda e: e.get("created_at", 0)).get("id"))


def _kinoforge_generate(
    *,
    cfg_path: Path,
    state_dir: Path,
    prompt: str,
    run_id: str,
    timeout_s: float = 2400.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``pixi run -e live-comfyui kinoforge generate ...`` with the args
    the smoke needs."""
    return subprocess.run(
        [
            "pixi",
            "run",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "generate",
            "-c",
            str(cfg_path),
            "--prompt",
            prompt,
            "--mode",
            "t2v",
            "--run-id",
            run_id,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def test_two_generations_share_warm_pod_via_b3_auto_discovery(
    tmp_path: Path,
) -> None:
    """Two `kinoforge generate` invocations 30s apart attach to same pod.

    Bug catch: if scan dispatch doesn't fire on the second invocation
    (precedence chain regression, scan-summary log missing, or the
    auto_attach config field defaulting wrong), gen 2 cold-creates a
    new pod and pod_id_2 != pod_id_1.
    """
    assert PROMPT_PATH.exists(), "Standard test prompt missing"
    assert CFG_TEMPLATE.exists(), f"Live cfg template missing: {CFG_TEMPLATE}"
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Gen 1: cold create.
    t0 = time.time()
    r1 = _kinoforge_generate(
        cfg_path=CFG_TEMPLATE,
        state_dir=state_dir,
        prompt=prompt,
        run_id="b3-smoke-1",
    )
    gen1_elapsed = time.time() - t0
    assert r1.returncode == 0, f"gen 1 failed: stderr={r1.stderr!r}"
    pod_id_1 = _extract_pod_id_from_ledger(state_dir)
    assert pod_id_1 is not None, "no pod id in ledger after gen 1"

    # Sleep 30s — well under idle_timeout_s default (7200s).
    time.sleep(30)

    # Gen 2: warm reuse.
    t0 = time.time()
    r2 = _kinoforge_generate(
        cfg_path=CFG_TEMPLATE,
        state_dir=state_dir,
        prompt=prompt,
        run_id="b3-smoke-2",
    )
    gen2_elapsed = time.time() - t0
    assert r2.returncode == 0, f"gen 2 failed: stderr={r2.stderr!r}"
    pod_id_2 = _extract_pod_id_from_ledger(state_dir)
    assert pod_id_2 == pod_id_1, (
        f"warm reuse failed: pod_id_2={pod_id_2!r} != pod_id_1={pod_id_1!r}"
    )

    combined = r2.stdout + r2.stderr
    assert "warm-reuse: attached to" in combined, (
        f"missing warm-reuse INFO; combined log:\n{combined}"
    )

    # Wan cold = 1-5 min model load. Warm reuse should skip ~2 min.
    # Loose threshold accounts for actual generation time (~30-60s).
    print(f"gen 1 elapsed: {gen1_elapsed:.1f}s")
    print(f"gen 2 elapsed: {gen2_elapsed:.1f}s")
    assert gen2_elapsed < gen1_elapsed * 0.7, (
        f"expected gen 2 < 70% of gen 1: "
        f"gen1={gen1_elapsed:.1f}s gen2={gen2_elapsed:.1f}s"
    )

    # Cleanup — destroy the pod so we don't pay idle_timeout.
    subprocess.run(
        [
            "pixi",
            "run",
            "kinoforge",
            "--state-dir",
            str(state_dir),
            "destroy",
            "--id",
            pod_id_1,
        ],
        check=False,
        timeout=120,
    )
