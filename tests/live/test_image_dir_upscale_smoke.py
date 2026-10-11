"""Live smoke — `kinoforge upscale --image-dir` on the spandrel x2 config (design §6.1).

RED scaffold committed BEFORE the live spend per CLAUDE.md. One pod, nine
files, every branch of the feature — including the two animated inputs
(``sub/anim.gif``, ``sub/sticker.webp``). Evidence lands under
``tests/live/evidence/2026-10-11-animated-image-dir/`` BEFORE any assertion.

Per CLAUDE.md's "Live smoke monitoring" rule, a background thread polls the
RunPod GraphQL utilisation probe for the duration of the subprocess run and
appends snapshots to ``evidence/util.txt`` — never trust ``est_spend`` alone
to prove the pod is actually working.

The cadence is :data:`_POLL_S` = 15 s, not the rule's 60-90 s floor: these are
seven small stills whose GPU-busy window is tens of seconds inside a ~3-minute
run, and the RunPod probe itself caches for ~15-30 s. A 60 s cadence sampled
``gpu=0.0`` twice on 2026-10-11 across a run that demonstrably did the work
(see ``attempt1-poll60-util.txt``) — too coarse to tell a busy pod from a dead
one, which is the one thing this poller exists to do.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_ROOT = Path(__file__).parent.parent.parent
_CFG = _ROOT / "examples" / "configs" / "runpod-diffusers-spandrel-x2-upscale.yaml"
_EVIDENCE = Path(__file__).parent / "evidence" / "2026-10-11-animated-image-dir"
_LEDGER_PATH = _ROOT / ".kinoforge" / "_lifecycle" / "ledger.json"
_POLL_S = 15
"""Seconds between utilisation probes; see the module docstring for why 15."""


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _size(p: Path) -> tuple[int, int]:
    with Image.open(p) as im:
        return im.size


def _poll_utilisation(stop: threading.Event) -> None:
    """Append a GPU/CPU/memory snapshot to ``evidence/util.txt`` every ``_POLL_S``.

    Runs as a daemon thread for the lifetime of the live subprocess. Never
    raises — any failure (missing ledger, no runpod entry, API error) is
    logged as a ``probe-error`` line and the loop keeps going, since a dead
    poller must never take down the live test it is only observing.
    """
    import os as _os

    from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

    util_path = _EVIDENCE / "util.txt"
    while not stop.is_set():
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            pod_id = None
            if _LEDGER_PATH.exists():
                data = json.loads(_LEDGER_PATH.read_text())
                for entry in data.get("entries", []):
                    if isinstance(entry, dict) and entry.get("provider") == "runpod":
                        pod_id = entry.get("id")
                        break
            if pod_id is None:
                line = f"{ts} pod=none found=False gpu=- cpu=- mem=-\n"
            else:
                endpoint = RunPodGraphQLUtilEndpoint(
                    api_key=_os.environ["RUNPOD_API_KEY"]
                )
                found, snap = endpoint.probe(pod_id)
                if found and snap is not None:
                    line = (
                        f"{ts} pod={pod_id} found={found} "
                        f"gpu={snap.gpu_util_percent} cpu={snap.cpu_percent} "
                        f"mem={snap.memory_percent}\n"
                    )
                else:
                    line = f"{ts} pod={pod_id} found={found} gpu=- cpu=- mem=-\n"
        except Exception as exc:  # noqa: BLE001 - poller must never kill the test
            line = f"{ts} probe-error: {type(exc).__name__}: {exc}\n"
        with util_path.open("a") as fh:
            fh.write(line)
        stop.wait(_POLL_S)


@pytest.mark.live
def test_directory_upscale_on_one_pod(tmp_path: Path) -> None:
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()

    from tests.live.build_image_dir_fixture import build

    src = tmp_path / "dir-smoke"
    out = tmp_path / "dir-smoke_upscaled"

    build(src)
    _EVIDENCE.mkdir(parents=True, exist_ok=True)
    existing_before = _sha(out / "existing.png")

    stop = threading.Event()
    poller = threading.Thread(target=_poll_utilisation, args=(stop,), daemon=True)
    poller.start()
    try:
        proc = subprocess.run(  # noqa: S603,S607
            [
                "pixi",
                "run",
                "kinoforge",
                "upscale",
                "-c",
                str(_CFG),
                "--image-dir",
                str(src),
                "--no-reuse",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
    finally:
        stop.set()
        poller.join(timeout=5)
    (_EVIDENCE / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE / "stderr.txt").write_text(proc.stderr)
    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    (_EVIDENCE / "list.txt").write_text(ledger.stdout + ledger.stderr)

    # One plan-time failure (huge.webp) is designed in → exit 1, not 0.
    assert proc.returncode == 1, proc.stderr
    assert (
        "upscaled 7 (2 animated, 6 frames), skipped 1 existing, failed 1, aborted 0"
        in proc.stdout
    )
    assert re.search(r"failed: huge\.webp: .*max_output_megapixels=256", proc.stdout)
    assert len(re.findall(r"^\[\d/7\] .* -> ", proc.stdout, flags=re.M)) == 7

    expected = {
        "luma.png": (5344, 3008),
        "luma-webp.png": (5344, 3008),
        "luma-avif.png": (5344, 3008),
        "rotated.png": (5344, 3008),
        "sub/anim.gif": (640, 400),
        "sub/sticker.webp": (320, 240),
        "sub/alpha.png": (600, 360),
    }
    for rel, size in expected.items():
        p = out / rel
        assert p.exists(), rel
        assert _size(p) == size, (rel, _size(p))
        shutil.copy2(p, _EVIDENCE / rel.replace("/", "__"))
    assert not (out / "huge.png").exists()
    assert _sha(out / "existing.png") == existing_before
    assert sorted(
        p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()
    ) == sorted([*expected, "existing.png"])

    # The animated branch must round-trip frame count, per-frame duration,
    # loop and alpha — not just the upscaled canvas size.
    with Image.open(out / "sub" / "sticker.webp") as im:
        assert getattr(im, "n_frames", 1) == 4
        durs = []
        for i in range(4):
            im.seek(i)
            im.load()
            durs.append(im.info["duration"])
        assert durs == [100, 200, 100, 300]
        assert im.info.get("loop", 0) == 0
        im.seek(0)
        rgba = im.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] <= 8  # type: ignore[index]  # top transparent
        assert rgba.getpixel((319, 239))[3] >= 247  # type: ignore[index]  # bottom opaque
    with Image.open(out / "sub" / "anim.gif") as im:
        assert getattr(im, "n_frames", 1) == 2
        im.seek(1)
        assert im.info["duration"] == 200
    assert "pending 7 (2 animated, 6 frames)" in proc.stdout

    assert "No running instances." in ledger.stdout
    assert "No instances recorded in ledger." in ledger.stdout
