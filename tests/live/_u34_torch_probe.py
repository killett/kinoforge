"""U34 discriminator harness — capture the pod's torch version WHILE it lives.

The question U34 asks cannot be answered after the fact. `kinoforge upscale
--no-reuse` destroys the pod the moment the artifact is published, and the only
place the installed torch version is written down is the pod's own
``/tmp/bootstrap.log``, served on port 8001 by the bootstrap's static file
server. The 2026-09-11 run (pod ``yu8rkgfzmcq873``) was green end to end and
still answered nothing, because by the time the log was requested the pod was
already gone — a ~100 s window, missed.

So this harness runs the probe and races it: it spawns ``kinoforge upscale``,
reads the pod id out of the orchestrator's own stdout, then polls port 8001
every few seconds and keeps the LAST non-empty copy of ``bootstrap.log``. It
also probes GPU/CPU/memory utilisation on the cadence CLAUDE.md requires of a
live smoke, so a dead pod surfaces here rather than at a timeout.

Teardown is NOT this script's job and is deliberately not weakened: the child
keeps ``--no-reuse``, so the pod still self-destroys. This only widens the
window in which its log is readable.

What to read out of the captured log::

    torch 2.6.0+cu124  -> the base IMAGE supplies the +cu124 build. U33 is a
                          no-op on this RunPod image, and the real exposure is
                          Modal's different base plus any future image bump.
    torch 2.6.0 (bare) -> the cu124 INDEX supplies it. U33 really does change
                          the installed wheel on every RunPod diffusers cfg,
                          and U34 stands as filed.

The probe cfg pins ``pytorch_extra_index_url`` to PyPI itself, which makes the
rendered ``--extra-index-url`` a no-op and reproduces pre-U33 behaviour without
touching engine code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

_POD_ID_RE = re.compile(r"for instance ([a-z0-9]{10,})\b")
_EVIDENCE = Path(__file__).with_name("_u34_torch_probe_evidence.log")
_POLL_S = 3.0
_UTIL_EVERY = 10  # every 10th poll -> ~30 s, inside the 60-90 s cadence


def _fetch(url: str) -> str | None:
    """GET *url*, returning its body or None when it is not (yet) servable.

    Args:
        url: The absolute URL to fetch.

    Returns:
        The decoded response body, or None on any transport error or empty body.
    """
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None
    return body or None


def _probe_util(pod_id: str) -> str:
    """Return a one-line utilisation summary for *pod_id*.

    Args:
        pod_id: The RunPod pod id.

    Returns:
        A human-readable ``gpu=..% cpu=..% mem=..%`` line, or a reason it could
        not be read. Never raises — a dead probe must not kill the capture.
    """
    try:
        from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

        key = os.environ.get("RUNPOD_API_KEY", "")
        if not key:
            return "util=UNREADABLE (no RUNPOD_API_KEY)"
        ok, snap = RunPodGraphQLUtilEndpoint(api_key=key).probe(pod_id)
        if not ok or snap is None:
            return "util=UNREADABLE (probe returned nothing)"
        return (
            f"gpu={snap.gpu_util_percent}% cpu={snap.cpu_percent}% "
            f"mem={snap.memory_percent}%"
        )
    except Exception as exc:  # noqa: BLE001 — a probe failure is not a capture failure
        return f"util=UNREADABLE ({exc!r})"


def main(argv: list[str]) -> int:
    """Run the probe and capture the pod's bootstrap log while it is alive.

    Args:
        argv: Command line to run, already including ``--no-reuse``.

    Returns:
        The child's exit code.
    """
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()

    proc = subprocess.Popen(  # noqa: S603 — argv is supplied by the operator
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None

    # The capture MUST NOT be driven by the child's output. The orchestrator
    # goes silent between "running provisioner.provision" and "materializing
    # upscaled artifact" — on the 2026-09-11 run that was 98 of the pod's ~100
    # seconds of life. A poll loop stepped by incoming lines would sit blocked
    # in readline for exactly the window it exists to sample.
    state: dict[str, str | None] = {"pod_id": None, "captured": None}
    done = threading.Event()

    def _poller() -> None:
        """Poll port 8001 for bootstrap.log until the child exits."""
        tick = 0
        while not done.is_set():
            pod_id = state["pod_id"]
            if pod_id is None:
                done.wait(_POLL_S)
                continue
            tick += 1
            body = _fetch(f"https://{pod_id}-8001.proxy.runpod.net/bootstrap.log")
            if body:
                # Keep the LATEST copy: the log grows, and the last read before
                # teardown is the most complete one.
                state["captured"] = body
            if tick % _UTIL_EVERY == 1:
                got = f"bootstrap.log {len(body)} B" if body else "not servable yet"
                print(f"[u34] poll {tick}: {got}, {_probe_util(pod_id)}", flush=True)
            done.wait(_POLL_S)

    watcher = threading.Thread(target=_poller, name="u34-poller", daemon=True)
    watcher.start()

    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if state["pod_id"] is None:
            m = _POD_ID_RE.search(line)
            if m:
                state["pod_id"] = m.group(1)
                print(f"[u34] pod id observed: {m.group(1)}", flush=True)

    rc = proc.wait()
    done.set()
    watcher.join(timeout=15)
    captured = state["captured"]

    if captured is None:
        print(
            "[u34] ✗ NO bootstrap.log captured — the question is UNANSWERED. "
            "Do not infer a reading from the exit code.",
            flush=True,
        )
        return rc

    _EVIDENCE.write_text(captured, encoding="utf-8")
    print(f"[u34] ✓ captured {len(captured)} B -> {_EVIDENCE}", flush=True)
    for ln in captured.splitlines():
        low = ln.lower()
        if "torch" in low and (
            "satisf" in low or "download" in low or "install" in low
        ):
            print(f"[u34]   {ln}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
