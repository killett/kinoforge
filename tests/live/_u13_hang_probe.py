"""U13 live probe — force ``UpscaleFailed`` and watch whether the CLI exits.

U13's symptom: after the traceback prints, ``kinoforge upscale`` never
terminates. The 2026-09-10 offline pass eliminated every candidate it could
reach — the whole controller-side upscale path against the local provider
(0.8 s clean exit with ``UpscaleFailed`` injected), the upscaler engines and
``_pod_http`` (no threads at all, by inspection), a real read-only Modal
client, and a real in-process ``app.deploy()`` — so the holder only appears
against a real failing pod. This probe books that pod.

What makes it cheap, and what makes its evidence usable:

1. **A T4 spandrel pod, not an A100 FlashVSR one** (see the cfg beside this
   file). The exception, its raise site and the transport are identical.
2. **A deliberately truncated mp4.** U12's fix removed the natural way to
   reach ``UpscaleFailed``, so the failure has to be induced anyway — and an
   input the server cannot decode errors the job before any inference runs.
3. **The exception is allowed to ESCAPE ``main()``.** The child does not wrap
   it: one hypothesis for why U13 is failure-path-only is that the escaping
   traceback keeps frames (and whatever they hold) alive into interpreter
   shutdown, and catching it would quietly destroy exactly that. So the child
   reproduces the real CLI's shutdown, not a convenient approximation.
4. **The hang is detected from OUTSIDE and dumped with SIGABRT.** A
   ``faulthandler`` timer armed at process start cannot work here — it would
   fire mid-boot on a healthy run — and there is no in-process hook after an
   escaping exception. So the child calls ``faulthandler.enable()`` (which
   installs a SIGABRT handler that dumps every thread) and the parent watches:
   once the traceback has been printed and the child is STILL alive, it is
   hung by definition, and one signal produces the stack the 2026-09-06
   ``kill -9`` threw away.

Pod cost of a hang is zero: teardown happens in ``deploy_session``'s finally,
which runs BEFORE the exception reaches the top of ``main`` — T2-05b verified
0 non-stopped apps while the CLI was still hung. The only thing a long hang
spends is wall-clock, which is why the detector is measured in seconds.

Run:
    pixi run -e live-modal python tests/live/_u13_hang_probe.py
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

#: Source clip for the truncated input. 480x480/81f, the §25 fixture.
_FIXTURE = Path(
    "output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4"
)
_CFG = Path("tests/live/_u13_spandrel_modal_cfg.yaml")
_EVIDENCE = Path("tests/live/_u13_hang_probe_evidence.json")

#: Seconds the child may stay alive AFTER its traceback has been printed
#: before it is called hung. A clean interpreter exit takes well under a
#: second (measured: 0.8 s for the whole offline run); U13's process was
#: still alive 16 minutes later. Anything in between is not a near miss.
_HANG_GRACE_S = 45.0

#: Hard wall-clock cap, well above a cold boot plus a failed job.
_CHILD_TIMEOUT_S = 1500.0

#: Marker the child prints once main() has been entered, so a hang during
#: boot is distinguishable from a hang at exit.
_ENTER_MARKER = "U13PROBE entering main()"

_CHILD = textwrap.dedent(
    """
    import faulthandler, sys
    from kinoforge.cli import main

    # Installs the SIGABRT handler the parent uses to dump every thread once
    # it has decided this process is hung. NOT dump_traceback_later: a timer
    # armed here starts during the cold boot, and there is no in-process hook
    # after an escaping exception to re-arm it.
    faulthandler.enable()

    print({enter!r}, flush=True)
    # No try/except on purpose — the UpscaleFailed must escape to the top of
    # the interpreter exactly as it does in a real `kinoforge upscale`.
    sys.exit(main({argv!r}))
    """
)


def _truncated_input(dest: Path) -> Path:
    """Write a copy of the fixture truncated mid-file.

    An mp4's ``moov`` atom lives at the tail, so a truncated copy is
    structurally unreadable: the server fails to open it and errors the job,
    which is the ``UpscaleFailed`` this probe needs. The controller never
    decodes it (``_resolve_input_video_as_artifact`` only stats and hashes),
    and ``_video_arg_error`` only checks existence, so it reaches the pod.

    Args:
        dest: Where to write the truncated copy.

    Returns:
        ``dest``.
    """
    raw = _FIXTURE.read_bytes()
    dest.write_bytes(raw[: len(raw) // 3])
    return dest


def _pods_now() -> str:
    """Return ``kinoforge list`` output, for teardown verification.

    Returns:
        Combined stdout+stderr of the listing.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kinoforge", "list"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _tail(path: Path, limit: int = 8000) -> str:
    """Return the last *limit* characters of *path*, or ``""``.

    Args:
        path: File to read.
        limit: Maximum characters to return.

    Returns:
        The tail of the file's text.
    """
    try:
        return path.read_text(errors="replace")[-limit:]
    except OSError:
        return ""


def main() -> int:  # noqa: PLR0915 — one linear probe; splitting hides the order
    """Run the probe and write the evidence file.

    Returns:
        ``0`` when an observation was made — a hang and a clean exit are both
        results; ``1`` when the probe could not run at all.
    """
    if not _FIXTURE.is_file():
        print(f"fixture missing: {_FIXTURE}", file=sys.stderr)
        return 1

    scratch = Path("output/_u13_probe")
    scratch.mkdir(parents=True, exist_ok=True)
    bad = _truncated_input(scratch / "truncated.mp4")
    out_path = scratch / "child.out"
    err_path = scratch / "child.err"

    argv = [
        "upscale",
        "--config",
        str(_CFG),
        "--video",
        str(bad),
        # --no-reuse so the pod dies even though the CLI may hang. T2-05b
        # proved teardown survives the failure path; this probe must never
        # depend on its own clean exit to stop the billing.
        "--no-reuse",
    ]
    child_src = _CHILD.format(enter=_ENTER_MARKER, argv=argv)

    started = time.time()
    traceback_seen_at: float | None = None
    hung = False
    with out_path.open("w") as out_f, err_path.open("w") as err_f:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", child_src], stdout=out_f, stderr=err_f
        )
        while True:
            if proc.poll() is not None:
                break
            now = time.time()
            if now - started > _CHILD_TIMEOUT_S:
                print("probe: hard timeout; aborting child", flush=True)
                proc.send_signal(signal.SIGABRT)
                hung = True
                break
            err = _tail(err_path, limit=200_000)
            if traceback_seen_at is None and "UpscaleFailed" in err:
                traceback_seen_at = now
                print(
                    f"probe: UpscaleFailed traceback printed at "
                    f"{now - started:.1f}s — watching for exit",
                    flush=True,
                )
            if (
                traceback_seen_at is not None
                and now - traceback_seen_at > _HANG_GRACE_S
            ):
                # Hung by definition: the exception has reached the top of the
                # interpreter and the process is still here. SIGABRT makes
                # faulthandler print every thread's stack — the evidence the
                # 2026-09-06 kill -9 destroyed.
                print(
                    f"probe: still alive {now - traceback_seen_at:.1f}s after the "
                    f"traceback — HUNG; dumping via SIGABRT",
                    flush=True,
                )
                proc.send_signal(signal.SIGABRT)
                hung = True
                break
            time.sleep(2.0)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()

    elapsed = time.time() - started
    stdout, stderr = _tail(out_path, 4000), _tail(err_path, 12000)
    evidence = {
        "probe": "u13-hang",
        "local_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_s": round(elapsed, 1),
        "child_returncode": proc.returncode,
        "entered_main": _ENTER_MARKER in stdout,
        "upscale_failed_raised": "UpscaleFailed" in stderr,
        "seconds_alive_after_traceback": (
            None
            if traceback_seen_at is None
            else round(elapsed - (traceback_seen_at - started), 1)
        ),
        "hung": hung,
        "verdict": (
            "HUNG — stderr_tail carries the thread dump naming the holder"
            if hung
            else "CLEAN EXIT — U13 did not reproduce on this path"
        ),
        "stdout_tail": stdout,
        "stderr_tail": stderr,
        "pods_after": _pods_now()[-2000:],
    }
    _EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"verdict: {evidence['verdict']}")
    print(f"upscale_failed_raised: {evidence['upscale_failed_raised']}")
    print(f"evidence: {_EVIDENCE}")
    # The child's FULL logs stay on disk. The first run of this probe deleted
    # them and the JSON keeps only a tail — which the base64 echo of the
    # provision script swamps — so the one thing needed to diagnose a build
    # failure was destroyed by the probe itself. Only the throwaway input goes.
    bad.unlink(missing_ok=True)
    print(f"child logs kept: {out_path} {err_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
