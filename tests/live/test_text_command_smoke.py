"""Live smokes — `kinoforge text` on an ultrasmall text-only model and an
ultrasmall vision-language model (design §11.2).

RED scaffold committed BEFORE the live spend per CLAUDE.md. Both runs go through
the real CLI as subprocesses with `--no-reuse` and `--output-dir tmp_path`, so
the repo output/ guard stays clean. Evidence lands under
``tests/live/evidence/<local date>-text-command/`` BEFORE any assertion.
Prompts are read VERBATIM from examples/configs/prompts/text-smoke-*.txt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_ROOT = Path(__file__).parent.parent.parent
_CFG_T2T = _ROOT / "examples" / "configs" / "runpod-diffusers-qwen3-0_6b-t2t.yaml"
_CFG_IT2T = _ROOT / "examples" / "configs" / "runpod-diffusers-smolvlm-256m-it2t.yaml"
_PROMPT_T2T = _ROOT / "examples" / "configs" / "prompts" / "text-smoke-t2t.txt"
_PROMPT_IT2T = _ROOT / "examples" / "configs" / "prompts" / "text-smoke-it2t.txt"
_INPUT_PNG = (
    _ROOT
    / "output"
    / "20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png"
)
_EVIDENCE_DIR = (
    Path(__file__).parent / "evidence" / f"{datetime.now():%Y-%m-%d}-text-command"
)


def _run_text(
    cfg: Path, prompt_file: Path, out_dir: Path, tag: str, *extra: str
) -> tuple[subprocess.CompletedProcess[str], Path, dict]:  # type: ignore[type-arg]
    """Run the CLI, write evidence FIRST, return (proc, txt_path, sidecar)."""
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603,S607
        [
            "pixi",
            "run",
            "kinoforge",
            "text",
            "--config",
            str(cfg),
            "--prompt",
            prompt_file.read_text(),
            "--output-dir",
            str(out_dir),
            "--no-reuse",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=2400,
        check=False,
    )
    (_EVIDENCE_DIR / f"{tag}-stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / f"{tag}-stderr.txt").write_text(proc.stderr)
    txts = sorted(out_dir.glob("*_text_*.txt"))
    jsons = sorted(out_dir.glob("*_text_*.json"))
    for p in [*txts, *jsons]:
        shutil.copy2(p, _EVIDENCE_DIR / f"{tag}-{p.name}")
    assert proc.returncode == 0, proc.stderr
    assert len(txts) == 1 and len(jsons) == 1, (txts, jsons)
    return proc, txts[0], json.loads(jsons[0].read_text())


def _assert_torn_down() -> None:
    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "No running instances." in ledger.stdout, ledger.stdout
    assert "No instances recorded in ledger." in ledger.stdout, ledger.stdout


@pytest.mark.live
def test_t2t_on_qwen3_0_6b(tmp_path: Path) -> None:
    assert _CFG_T2T.exists() and _PROMPT_T2T.exists()
    proc, txt, sidecar = _run_text(_CFG_T2T, _PROMPT_T2T, tmp_path, "t2t")
    text = txt.read_text()
    assert text.strip(), "empty completion"
    assert proc.stdout == (text if text.endswith("\n") else text + "\n")
    assert sidecar["mode"] == "t2t"
    assert sidecar["images"] == []
    assert sidecar["usage"]["completion_tokens"] > 0
    assert sidecar["finish_reason"] in {"stop", "length"}
    assert sidecar["model"] == "hf:Qwen/Qwen3-0.6B"
    _assert_torn_down()


@pytest.mark.live
def test_it2t_on_smolvlm_256m(tmp_path: Path) -> None:
    if not _INPUT_PNG.exists():
        pytest.skip(
            f"input PNG missing ({_INPUT_PNG.name}); regenerate with "
            "`kinoforge image -c examples/configs/luma-uni1-t2i.yaml`"
        )
    assert _CFG_IT2T.exists() and _PROMPT_IT2T.exists()
    _, txt, sidecar = _run_text(
        _CFG_IT2T, _PROMPT_IT2T, tmp_path, "it2t", "--image", str(_INPUT_PNG)
    )
    assert txt.read_text().strip(), "empty completion"
    assert sidecar["mode"] == "it2t"
    assert len(sidecar["images"]) == 1
    assert (
        sidecar["images"][0]["sha256"]
        == hashlib.sha256(_INPUT_PNG.read_bytes()).hexdigest()
    )
    assert sidecar["usage"]["completion_tokens"] > 0
    _assert_torn_down()
