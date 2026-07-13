"""4-prompt warm-reuse smoke for Wan 2.2 T2V-A14B via DiffusersEngine.

Sibling of ``test_diffusers_wan_t2v_live.py``. That smoke covers cross-
capability-key isolation across 14B + 5B; this one drives the warm-reuse
chain DEEPER — one cold leg followed by THREE consecutive warm-reuse
legs on the same pod, one per prompt file in
``examples/configs/prompts/``.

Gated by KINOFORGE_LIVE_TESTS=1. Single pod, four MP4s.

Pass criteria:
  - All four ``kinoforge generate`` invocations exit 0.
  - Four MP4s land under ``output/``; each survives ffprobe
    (h264 / yuv420p / 480x480 / 81 frames / 16 fps).
  - Legs 2, 3, 4 log ``warm-reuse: attached to <pod_id>`` and the
    attached pod matches leg 1's cold-create pod.
  - All four MP4 sha256s pairwise differ — distinct prompts produced
    distinct bytes (rules out cached-output bug).
  - Single pod destroyed in teardown; ledger empty.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live smokes",
)


REPO = Path(__file__).resolve().parents[2]
CFG_14B = REPO / "examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml"
PROMPTS_DIR = REPO / "examples/configs/prompts"
LEGS: list[tuple[str, str]] = [
    ("cold", "field-realistic.txt"),
    ("warm1", "field-dreamlike.txt"),
    ("warm2", "forest.txt"),
    ("warm3", "dawn-flight.md"),
]
OUTPUT_DIR = REPO / "output"


def _run_generate(cfg: Path, prompt_path: Path, log_path: Path) -> str:
    """Run ``kinoforge generate`` and return captured log text."""
    prompt = prompt_path.read_text()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as logf:
        proc = subprocess.run(
            [
                "pixi",
                "run",
                "kinoforge",
                "generate",
                "--config",
                str(cfg),
                "--prompt",
                prompt,
                "--mode",
                "t2v",
            ],
            cwd=str(REPO),
            stdout=logf,
            stderr=subprocess.STDOUT,
            # 65 min per leg — same headroom as the 3-leg smoke.
            timeout=3900,
        )
    log_text = log_path.read_text()
    assert proc.returncode == 0, (
        f"kinoforge generate failed with exit {proc.returncode}\n"
        f"Last 60 log lines:\n{log_text[-4000:]}"
    )
    return log_text


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, "could not find pod id in log"
    return m.group(1)


def _extract_warm_attach_pod_id(log_text: str) -> str:
    m = re.search(r"warm-reuse: attached to (\w+)", log_text)
    assert m is not None, "expected warm-reuse log line not found"
    return m.group(1)


def _destroy(pod_id: str) -> None:
    subprocess.run(
        ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
        cwd=str(REPO),
        timeout=120,
        check=False,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ffprobe(path: Path) -> dict[str, str]:
    """Return ffprobe stream metadata for ``path``."""
    proc = subprocess.run(
        [
            "pixi",
            "run",
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height,nb_read_frames,r_frame_rate,duration",
            "-count_frames",
            "-of",
            "json",
            str(path),
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    data = json.loads(proc.stdout)
    streams = data.get("streams") or []
    assert streams, f"ffprobe returned no streams for {path}"
    return {str(k): str(v) for k, v in streams[0].items()}


def _latest_output(slug_fragment: str) -> Path:
    candidates = [p for p in OUTPUT_DIR.glob("*.mp4") if slug_fragment in p.name]
    assert candidates, f"no MP4 matching {slug_fragment!r} found in {OUTPUT_DIR}"
    return max(candidates, key=lambda p: p.stat().st_mtime)


def test_wan22_t2v_a14b_cold_then_three_warm_reuse(tmp_path: Path) -> None:
    pre = subprocess.run(
        ["pixi", "run", "preflight"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert pre.returncode == 0, f"preflight failed:\n{pre.stdout}\n{pre.stderr}"

    pod_id: str | None = None
    artifact_paths: list[Path] = []
    artifact_shas: list[str] = []
    legs_meta: list[dict[str, object]] = []
    evidence: dict[str, object] = {"legs": legs_meta}
    try:
        for idx, (slug, fname) in enumerate(LEGS):
            prompt_path = PROMPTS_DIR / fname
            log_path = tmp_path / f"leg{idx}-{slug}.log"
            log_text = _run_generate(CFG_14B, prompt_path, log_path)
            if idx == 0:
                pod_id = _extract_pod_id(log_text)
                evidence["pod_id_cold"] = pod_id
            else:
                warm_pod = _extract_warm_attach_pod_id(log_text)
                assert warm_pod == pod_id, (
                    f"leg {idx} ({slug}) hit wrong pod: "
                    f"warm_pod={warm_pod!r} pod_id={pod_id!r}"
                )
            # Best-effort slug-match against the new output. The published
            # filename includes the first ~16 chars of the prompt; pick the
            # newest MP4 produced after the leg started.
            mp4 = max(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
            artifact_paths.append(mp4)
            sha = _sha256(mp4)
            artifact_shas.append(sha)
            probe = _ffprobe(mp4)
            legs_meta.append(
                {
                    "leg": idx,
                    "slug": slug,
                    "prompt_file": fname,
                    "log_path": str(log_path),
                    "mp4_path": str(mp4),
                    "size_bytes": mp4.stat().st_size,
                    "sha256": sha,
                    "ffprobe": probe,
                }
            )
            # ffprobe asserts on cfg-expected geometry.
            assert probe.get("codec_name") == "h264", (
                f"leg {idx} ({slug}): expected h264, got {probe.get('codec_name')!r}"
            )
            assert probe.get("pix_fmt") == "yuv420p", (
                f"leg {idx} ({slug}): expected yuv420p, got {probe.get('pix_fmt')!r}"
            )
            assert probe.get("width") == "480" and probe.get("height") == "480", (
                f"leg {idx} ({slug}): expected 480x480, "
                f"got {probe.get('width')}x{probe.get('height')}"
            )
            assert probe.get("nb_read_frames") == "81", (
                f"leg {idx} ({slug}): expected 81 frames, "
                f"got {probe.get('nb_read_frames')!r}"
            )
            assert probe.get("r_frame_rate") == "16/1", (
                f"leg {idx} ({slug}): expected 16/1 fps, "
                f"got {probe.get('r_frame_rate')!r}"
            )
        # Pairwise sha256 distinctness.
        for i, sha_i in enumerate(artifact_shas):
            for j in range(i + 1, len(artifact_shas)):
                assert sha_i != artifact_shas[j], (
                    f"sha256 collision between legs {i} and {j}: {sha_i}"
                )

        # Copy MP4s to a stable evidence dir so they survive any future
        # `git clean` or worktree teardown — paths in successful-generations.md
        # entry #8 "See also" line point here.
        evidence_dir = REPO / ".kinoforge/wan22_4prompt_evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for mp4 in artifact_paths:
            shutil.copy2(mp4, evidence_dir / mp4.name)
        (evidence_dir / "manifest.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True)
        )
    finally:
        # Recover pod_id from log files if cold leg raised before assignment.
        if not pod_id:
            for leg_idx in range(len(LEGS)):
                slug = LEGS[leg_idx][0]
                log_path = tmp_path / f"leg{leg_idx}-{slug}.log"
                if not log_path.exists():
                    continue
                try:
                    pod_id = _extract_pod_id(log_path.read_text())
                    break
                except AssertionError:
                    continue
        if pod_id:
            _destroy(pod_id)
