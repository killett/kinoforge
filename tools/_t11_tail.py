"""Task-11 log summariser — keeps a noisy live-run log out of the transcript.

The Modal orchestrator log carries a full image build plus repeated validation
warnings; reading it raw costs thousands of tokens per poll. This prints only
the tail of the *interesting* lines (progress, errors, artifact paths), with
build/validation chatter collapsed to a count.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NOISE = re.compile(
    r"(WARNING kinoforge\.validation|\[WARN\]|^=> Step|^ ---> |Building image|"
    r"^\s*$|Copying|Pulling|^\.+$)"
)
KEEP = re.compile(
    r"(error|Error|ERROR|Traceback|exception|failed|FAIL|lora|LoRA|provision|"
    r"boot|ready|modal\.run|artifact|uri=|generate|step|Step \d+/|%\|)",
)


def main() -> int:
    """Print the last N interesting lines of the given log file.

    Returns:
        0 always.
    """
    path = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    raw = path.read_text(errors="replace").replace("\r", "\n")
    lines = raw.splitlines()
    kept: list[str] = []
    noise = 0
    for ln in lines:
        s = ln.rstrip()
        if NOISE.search(s) or not s:
            noise += 1
            continue
        if KEEP.search(s):
            kept.append(s[:400])
    print(f"[log] {len(lines)} lines total, {noise} noise, {len(kept)} interesting")
    for s in kept[-n:]:
        print(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
