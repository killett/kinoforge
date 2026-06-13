"""C25 acceptance smoke: Wan + ComfyUI + B3 cross-CLI warm-reuse + B5a heartbeat.

Closes the entry-#6 "Production limitation (C25)" paragraph. Two
identical ``kinoforge generate`` subprocess CLIs 60s apart on a real
Wan 2.1 14B T2V workload; gen2 auto-attaches to gen1's pod via B3's
``_scan_warm_candidates`` with no operator id-juggling.

Post-smoke a direct GraphQL inspection asserts the heartbeat carrier
slot (env var or dockerArgs trailer, depending on Task 2's branch)
contains the kinoforge marker AND the Phase 24 selfterm injection
survives.

Gated by ``KINOFORGE_LIVE_RUNPOD=1`` AND ``KINOFORGE_LIVE_TESTS=1``.
Live spend ceiling: $0.30 across both gens.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_TESTS_GATE_ENV = "KINOFORGE_LIVE_TESTS"
_CFG_PATH = Path("tests/live/cfg_c25_wan_comfyui.yaml")
_PROMPT_PATH = Path("prompt-field-realistic.txt")
_SEMANTICS_SIDECAR = Path("tests/live/_runpod_env_semantics.json")
_SPEND_CAP_USD = 0.30


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(f"set {_LIVE_GATE_ENV}=1 to run the C25 acceptance smoke")
    if os.environ.get(_TESTS_GATE_ENV) != "1":
        pytest.skip(f"set {_TESTS_GATE_ENV}=1 to opt into live spend")


def _kinoforge_generate(
    *,
    cfg_path: Path,
    state_dir: Path,
    prompt: str,
    run_id: str,
    timeout_s: float = 3600.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
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


def _extract_pod_id_from_ledger(state_dir: Path) -> str | None:
    ledger_path = state_dir / "ledger.json"
    if not ledger_path.exists():
        return None
    data = json.loads(ledger_path.read_text())
    for entry in data.get("entries", []):
        if entry.get("provider_kind") == "runpod" and entry.get("instance_id"):
            return entry["instance_id"]
    return None


def _inspect_pod_via_graphql(pod_id: str, semantics_branch: str) -> dict[str, Any]:
    """Direct GraphQL inspection of the C25 carrier slot.

    Branch B (preserve-and-merge) — the shipped branch — queries
    ``dockerArgs`` only. The pre-C25 ``env { key value }`` selection
    set 400s on RunPod's GraphQL because ``pod.env`` is typed
    ``[String]`` (no subfields); see the env-semantics probe outcome
    at ``tests/live/_runpod_env_semantics.json``.
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    provider = RunPodProvider(creds=creds)
    if semantics_branch == "additive":
        # Branch A path — unused in shipped configuration; kept for the
        # case where a future probe re-runs and produces ``additive``.
        query = """
        query GetPod($podId: String!) {
          pod(input: {podId: $podId}) { id }
        }
        """.strip()
    else:
        query = """
        query GetPod($podId: String!) {
          pod(input: {podId: $podId}) { id dockerArgs }
        }
        """.strip()
    resp = provider._http_post(  # noqa: SLF001
        provider._base_url,
        {"query": query, "variables": {"podId": pod_id}},
    )
    pod = (resp.get("data") or {}).get("pod") or {}
    return pod


def test_c25_warm_reuse_comfyui_wan() -> None:
    assert _PROMPT_PATH.exists(), "standard test prompt missing"
    assert _CFG_PATH.exists(), f"cfg missing: {_CFG_PATH}"
    assert _SEMANTICS_SIDECAR.exists(), (
        "env-semantics sidecar missing — run Task 1 probe first"
    )
    semantics = json.loads(_SEMANTICS_SIDECAR.read_text())["semantics"]
    branch = "additive" if semantics == "additive" else "preserve-merge"

    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    state_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"c25-smoke-{int(time.time())}"
    state_dir.mkdir(parents=True, exist_ok=True)

    pod_id_1: str | None = None
    try:
        # Gen 1: cold create.
        t0 = time.time()
        r1 = _kinoforge_generate(
            cfg_path=_CFG_PATH,
            state_dir=state_dir,
            prompt=prompt,
            run_id="c25-smoke-1",
        )
        gen1_elapsed = time.time() - t0
        assert r1.returncode == 0, (
            f"gen 1 failed: stderr={r1.stderr!r}\nstdout={r1.stdout!r}"
        )
        pod_id_1 = _extract_pod_id_from_ledger(state_dir)
        assert pod_id_1 is not None, "no pod id in ledger after gen 1"
        print(f"gen 1 elapsed: {gen1_elapsed:.1f}s pod={pod_id_1}")

        # Sleep 60s — well under idle_timeout (25m).
        time.sleep(60)

        # Gen 2: warm reuse via B3 auto-discovery.
        t0 = time.time()
        r2 = _kinoforge_generate(
            cfg_path=_CFG_PATH,
            state_dir=state_dir,
            prompt=prompt,
            run_id="c25-smoke-2",
        )
        gen2_elapsed = time.time() - t0
        assert r2.returncode == 0, (
            f"gen 2 failed: stderr={r2.stderr!r}\nstdout={r2.stdout!r}"
        )
        pod_id_2 = _extract_pod_id_from_ledger(state_dir)
        assert pod_id_2 == pod_id_1, (
            f"warm reuse failed: pod_id_2={pod_id_2!r} != pod_id_1={pod_id_1!r}"
        )

        combined = r2.stdout + r2.stderr
        assert "warm-reuse: attached to" in combined, (
            f"missing warm-reuse INFO; combined log:\n{combined}"
        )

        print(f"gen 2 elapsed: {gen2_elapsed:.1f}s")
        assert gen2_elapsed < gen1_elapsed * 0.7, (
            f"cold-skip ratio failed: gen1={gen1_elapsed:.1f}s gen2={gen2_elapsed:.1f}s"
        )

        # Post-smoke GraphQL inspection.
        pod = _inspect_pod_via_graphql(pod_id_1, semantics)
        if branch == "additive":
            # Unused arm under the shipped Branch B configuration.
            pytest.fail(
                "Branch A inspection path not implemented for read-unavailable "
                "semantics; re-run env probe before exercising this arm."
            )
        else:
            docker_args = pod.get("dockerArgs") or ""
            assert "bash /tmp/p.sh" in docker_args, (
                f"Phase 24 bash decoder missing from dockerArgs (C25 collision!): "
                f"{docker_args!r}"
            )
            markers = re.findall(r"#\s*_kinoforge_hb:", docker_args)
            assert len(markers) == 1, (
                f"expected exactly one heartbeat marker; got {len(markers)} "
                f"in dockerArgs={docker_args!r}"
            )
    finally:
        # Teardown.
        if pod_id_1 is not None:
            subprocess.run(  # noqa: S603
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
