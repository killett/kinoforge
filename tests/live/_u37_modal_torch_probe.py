"""U37 probe — read a MODAL pod's torch build off /health while it lives.

U34 settled which torch wheel a RunPod pod installs, by scraping pip's
incidental chatter out of the port-8001 sidecar log. Modal has no such sidecar:
its container stdout is not streamed to the CLI, the engine's pip step runs
``-q`` so nothing is printed anyway, and ``modal app logs`` on a stopped
ephemeral app returns the image build rather than the container. A green Modal
run (app ``ap-KlBlrRvxoQ7gz992RdrB6f``) therefore answered nothing.

``/health`` now carries ``{"torch": {"version": ..., "cuda": ...}}`` on every
provider, so this probe just needs the pod's URL — which Modal prints as it
deploys — and one GET while the app is up.

Reading, against U34's RunPod result (bare ``2.6.0`` under the PyPI index,
``2.6.0+cu124`` under the cu124 index)::

    2.6.0+cu124  -> U33's cu124 default reaches Modal too, and Modal's
                    python:3.13-slim base really does download that wheel
                    (unlike RunPod, whose image already satisfied the pin).
    2.6.0 (bare) -> something on the Modal path is NOT applying the index, and
                    U33's blast radius is smaller than the 20-cfg render count
                    suggests.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

#: Modal prints the web endpoint as it creates objects, wrapped across lines and
#: sometimes truncated with an ellipsis, so the URL is reassembled from the
#: orchestrator's own artifact line instead where possible.
_URL_RE = re.compile(r"https://[a-z0-9\-.]+\.modal\.run")
_EVIDENCE_DIR = Path(__file__).parent
_POLL_S = 3.0


def _get_health(url: str) -> tuple[dict[str, Any] | None, str]:
    """GET ``<url>/health``, returning the parsed body and a reason.

    Args:
        url: Base ``https://….modal.run`` URL.

    Returns:
        ``(payload, reason)``; payload is None unless a JSON 200 came back.
    """
    req = urllib.request.Request(  # noqa: S310 — pod URL only
        f"{url.rstrip('/')}/health",
        headers={"User-Agent": "kinoforge-u37-probe/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode()), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def main(argv: list[str]) -> int:
    """Run the Modal command and capture its /health torch build.

    Args:
        argv: Command line to run, already including ``--no-reuse``.

    Returns:
        The child's exit code.
    """
    from kinoforge.core.dotenv_loader import load_env_file

    load_env_file()

    proc = subprocess.Popen(  # noqa: S603 — argv supplied by the operator
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert proc.stdout is not None

    state: dict[str, Any] = {"url": None, "health": None}
    done = threading.Event()

    def _poller() -> None:
        """Poll /health until the child exits, keeping the first good read."""
        while not done.is_set():
            url = state["url"]
            if url is None or state["health"] is not None:
                done.wait(_POLL_S)
                continue
            payload, why = _get_health(str(url))
            if payload is not None and payload.get("torch", {}).get("version"):
                state["health"] = payload
                print(f"[u37] /health torch -> {payload['torch']}", flush=True)
            else:
                print(f"[u37] /health not ready ({why})", flush=True)
            done.wait(_POLL_S)

    watcher = threading.Thread(target=_poller, name="u37-poller", daemon=True)
    watcher.start()

    # Modal WRAPS the endpoint across terminal lines as it deploys
    # ("...--loc-06fedf.moda" / "l.run"), so a per-line regex never matches it.
    # Reassembling from a rolling buffer of stripped lines recovers it at DEPLOY
    # time, ~50 s before teardown. The orchestrator's own "materializing ...
    # artifact from <url>" line carries the URL unwrapped, but it arrives about
    # 13 s before the pod is destroyed — too tight to rely on alone.
    recent: list[str] = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if state["url"] is None:
            recent.append(line.strip())
            del recent[:-4]
            m = _URL_RE.search("".join(recent))
            if m and len(m.group(0)) > 40:
                state["url"] = m.group(0)
                print(f"[u37] pod url observed: {m.group(0)}", flush=True)

    rc = proc.wait()
    done.set()
    watcher.join(timeout=15)

    health = state["health"]
    if health is None:
        print(
            "[u37] ✗ NO /health torch build captured — UNANSWERED. "
            "Do not infer a reading from the exit code.",
            flush=True,
        )
        return rc

    out = _EVIDENCE_DIR / "_u37_modal_torch_evidence.json"
    out.write_text(json.dumps(health, indent=2) + "\n", encoding="utf-8")
    print(f"[u37] ✓ torch={health['torch']} -> {out}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
