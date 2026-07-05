"""FlashVSR corruption debug-matrix driver (2026-07-04 root-cause session).

POSTs a sequence of ``/upscale`` variants against a WARM pod running
wan_t2v_server (skips orchestration entirely), downloading each artifact
for local visual QA. Variants exercise the debug knobs shipped in
``FlashVSRParams`` (pipe_overrides / attention_impl / debug_stats).

Usage:
    pixi run python -m tools.flashvsr_debug_matrix \
        --pod-id <id> --video output/clip.mp4 \
        --out-dir /tmp/matrix --variants nofix,dense
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.dotenv_loader import load_env_file
from kinoforge.providers.runpod import RunPodProvider

_UA = "kinoforge-flashvsr/0.1"

#: variant name -> extra keys merged into the request's flashvsr block.
VARIANTS: dict[str, dict[str, Any]] = {
    # Production baseline (BSA + color_fix) — same as the orchestrated path.
    "baseline": {},
    # Raw VAE decode, no adain color transfer: is the decode itself garbage?
    "nofix": {"pipe_overrides": {"color_fix": False}},
    # BSA kernel bypassed with the dense fp32 reference; raw decode.
    "dense": {
        "attention_impl": "dense",
        "pipe_overrides": {"color_fix": False},
    },
    # Dense + color_fix — the "what production would look like" variant.
    "dense_fix": {"attention_impl": "dense"},
}


def _log(msg: str) -> None:
    """Print a timestamped line to stderr."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> Any:  # noqa: ANN401 — JSON payloads are dynamically shaped
    """Small JSON-over-HTTP helper with the kinoforge UA."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": _UA}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return json.loads(resp.read())


def _upload(base: str, video: Path) -> str:
    """PUT /upload the source clip; return the pod-local file:// URL."""
    body = video.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    req = urllib.request.Request(  # noqa: S310
        f"{base}/upload",
        data=body,
        method="PUT",
        headers={
            "User-Agent": _UA,
            "Content-Type": "video/mp4",
            "X-Filename": f"{sha[:8]}.mp4",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        payload = json.loads(resp.read())
    if payload.get("sha256") != sha:
        raise RuntimeError(f"upload sha mismatch: {payload}")
    return f"file://{payload['path']}"


def _run_variant(
    base: str, video: Path, name: str, extra: dict[str, Any], out_dir: Path
) -> Path:
    """Upload + submit + poll + download one variant; return the local path."""
    src_url = _upload(base, video)  # server unlinks the upload per job
    flashvsr_block: dict[str, Any] = {
        "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
        "precision": "bfloat16",
        "debug_stats": True,
        **extra,
    }
    submit = _http_json(
        "POST",
        f"{base}/upscale",
        {
            "source_url": src_url,
            "source_filename": src_url.rsplit("/", 1)[-1],
            "scale": "4x",
            "engine": "flashvsr",
            "flashvsr": flashvsr_block,
        },
    )
    job_id = submit["job_id"]
    _log(f"{name}: job_id={job_id}")
    t0 = time.monotonic()
    while True:
        status = _http_json("GET", f"{base}/upscale/status/{job_id}")
        state = status["state"]
        if state == "done":
            break
        if state == "error":
            raise RuntimeError(f"{name}: server error: {status.get('error')}")
        if time.monotonic() - t0 > 45 * 60:
            raise TimeoutError(f"{name}: no result after 45min")
        time.sleep(5)
    filename = status["result"]["filename"]
    out = out_dir / f"{name}.mp4"
    req = urllib.request.Request(  # noqa: S310
        f"{base}/artifacts/{filename}", headers={"User-Agent": _UA}
    )
    with urllib.request.urlopen(req, timeout=600) as resp, out.open("wb") as fh:  # noqa: S310
        fh.write(resp.read())
    _log(
        f"{name}: done in {time.monotonic() - t0:.0f}s -> {out} "
        f"({out.stat().st_size} bytes)"
    )
    return out


def main() -> int:
    """Run the requested variants sequentially against the warm pod."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod-id", required=True)
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument(
        "--variants",
        default="nofix,dense",
        help=f"comma list from {sorted(VARIANTS)}",
    )
    args = ap.parse_args()

    load_env_file()  # pixi does not auto-source .env (activation quirk)
    provider = RunPodProvider(creds=EnvCredentialProvider())
    inst = provider.get_instance(args.pod_id)
    endpoints = inst.endpoints or {}
    base = (endpoints.get("8000") or next(iter(endpoints.values()))).rstrip("/")
    _log(f"pod={args.pod_id} base={base}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name in args.variants.split(","):
        name = name.strip()
        if name not in VARIANTS:
            raise SystemExit(f"unknown variant {name!r}; pick from {sorted(VARIANTS)}")
        _run_variant(base, args.video, name, VARIANTS[name], args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
