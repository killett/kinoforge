"""Snapshot tests for DiffusersEngine.render_provision."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.diffusers import DiffusersEngine


def _make_engine() -> DiffusersEngine:
    return DiffusersEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "diffusers": {
                "base_url": "http://localhost:8000",
                "pip": [],
                "server_cmd": ["python", "-m", "diffusers_server"],
            }
        }
    }


def test_render_provision_returns_rendered_provision() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert isinstance(rp, RenderedProvision)


def test_render_provision_script_starts_with_set_euo_pipefail() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.startswith("set -euo pipefail")


def test_render_provision_script_runs_pip_install_for_each_dep() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["pip"] = [
        "diffusers==0.27.0",
        "transformers",
        "accelerate",
    ]
    rp = _make_engine().render_provision(cfg)
    assert "pip install -q diffusers==0.27.0 transformers accelerate" in rp.script


def test_render_provision_script_ends_with_server_cmd_no_exec() -> None:
    """Post-trap-fix: bash must stay PID 1 to honor its EXIT trap, so the
    main server runs WITHOUT ``exec``. The script ends with the raw
    command.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith("python -m diffusers_server")
    # Belt-and-braces: assert the absence of an `exec ` prefix on the
    # final line, since `python -m diffusers_server` is a substring of
    # `exec python -m diffusers_server`.
    assert not rp.script.rstrip().endswith("exec python -m diffusers_server")


def test_render_provision_run_cmd_matches_server_cmd() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.run_cmd == ["python", "-m", "diffusers_server"]


def test_render_provision_default_image_is_stock_runpod_pytorch() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.image == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_render_provision_image_override_from_cfg() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["image"] = "myorg/diffusers-base:v1"
    rp = _make_engine().render_provision(cfg)
    assert rp.image == "myorg/diffusers-base:v1"


def test_render_provision_port_parsed_from_base_url() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = "http://localhost:9999"
    rp = _make_engine().render_provision(cfg)
    # The main server port is followed by the 8001 log-server port.
    assert rp.ports == ["9999", "8001"]


def test_render_provision_port_defaults_to_8000_when_base_url_missing() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = ""
    rp = _make_engine().render_provision(cfg)
    # The 8000 default is followed by the 8001 log-server port.
    assert rp.ports == ["8000", "8001"]


def test_render_provision_env_required_is_empty() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.env_required == []


def test_render_provision_launches_selfterm_watchdog_before_pip_install() -> None:
    """Bug it catches: Diffusers bootstrap (pip install + exec server) drops
    selfterm launch and gives leaked pods a 3h+ tail just like the ComfyUI
    pre-fix path. Lockdown pins the same write-to-tmp + nohup pattern and
    requires it to appear before any pip install line — pip can hang on slow
    mirrors and is exactly where the watchdog must already be alive.
    """
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["pip"] = ["torch", "diffusers"]
    rp = _make_engine().render_provision(cfg)
    script = rp.script
    assert "open('/tmp/selfterm.py','w')" in script
    assert "nohup python3 /tmp/selfterm.py" in script
    selfterm_idx = script.index("nohup python3 /tmp/selfterm.py")
    pip_idx = script.index("pip install -q torch")
    assert selfterm_idx < pip_idx, (
        "selfterm watchdog must launch before pip install — pip can hang"
    )


def test_render_provision_selfterm_launch_is_guarded_for_missing_env_var() -> None:
    """Bug it catches: launch line that fails-hard when
    KINOFORGE_SELFTERM_SCRIPT isn't set (LocalProvider, non-selfterm providers).
    Parity with ComfyUI's same guard.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    script = rp.script
    assert 'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then' in script


def test_render_provision_pip_install_quotes_version_specifiers(
    tmp_path: Path,
) -> None:
    """Bug it catches: ``>=`` version constraints in pip args.

    Pre-2026-06-19 Task 8 attempt #2: ``" ".join(pip_deps)`` produced
    ``pip install -q diffusers>=0.32 transformers>=4.45 ...``. Bash with
    ``set -euo pipefail`` parsed each ``>=`` as a stdout redirect to a
    file (``=0.32``, ``=4.45``, ...), silently stripping every version
    pin from the pip argument list. Unpinned pip resolved to whatever
    diffusers/transformers were latest, often incompatible with the
    server's API expectations — wan_t2v_server.py crashed on import,
    container died, RunPod restarted it in an endless loop. Observed
    spend: $0.11 before manual destroy.

    This test runs bash on the rendered pip line with ``pip`` shimmed
    to dump its argv, then asserts the recovered arg list matches the
    pip_deps inputs verbatim. shlex.split alone would NOT catch the
    bug because it does not model bash redirect tokens.
    """
    import subprocess

    pip_deps = [
        "diffusers>=0.32",
        "transformers>=4.45",
        "accelerate>=1.0",
        "fastapi>=0.115",
        "uvicorn>=0.30",
        "imageio[ffmpeg]>=2.34",
    ]
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["pip"] = list(pip_deps)

    rp = _make_engine().render_provision(cfg)
    pip_lines = [ln for ln in rp.script.split("\n") if ln.startswith("pip install -q")]
    assert len(pip_lines) == 1, (
        f"expected exactly one pip install line, got {len(pip_lines)}: {pip_lines!r}"
    )

    # Shim ``pip`` to a function that prints one arg per line, then run
    # the rendered pip line under ``set -euo pipefail`` from an isolated
    # tmp cwd so any spurious redirect files become discoverable.
    work_dir = tmp_path / "bashrun"
    work_dir.mkdir()
    script = f"set -euo pipefail\npip() {{ printf '%s\\n' \"$@\"; }}\n{pip_lines[0]}\n"
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(work_dir),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    spurious = sorted(p.name for p in work_dir.iterdir())
    assert not spurious, (
        f"bash created spurious redirect files from the pip line: {spurious}.\n"
        f"This is the '>=' redirect bug. Raw pip line: {pip_lines[0]!r}"
    )
    assert proc.returncode == 0, (
        f"bash failed: rc={proc.returncode} stderr={proc.stderr!r}"
    )
    args = proc.stdout.strip().split("\n")
    assert args == ["install", "-q", *pip_deps], (
        f"pip args do not survive bash parsing:\n"
        f"  expected: {['install', '-q', *pip_deps]}\n"
        f"  actual:   {args}\n"
        f"  raw line: {pip_lines[0]!r}"
    )


def test_render_provision_redirects_stdout_stderr_to_bootstrap_log() -> None:
    """Bootstrap script captures all stdout+stderr to /tmp/bootstrap.log.

    Bug it catches: Task 8 attempts #2 and #3 both restart-looped on a
    crash inside wan_t2v_server.py startup. Without log capture on the
    pod, the controlling agent could not see the actual error message
    — uptime cycled, GPU/CPU stayed flat, but the *reason* required
    SSH or a separate logging surface. Bake the capture into the
    bootstrap so a sidecar HTTP server can serve it back via the
    RunPod port proxy.

    Locks in: ``exec > /tmp/bootstrap.log 2>&1`` is the second
    statement in the script (after ``set -euo pipefail``).
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    lines = [ln.strip() for ln in rp.script.split("\n") if ln.strip()]
    assert lines[0] == "set -euo pipefail"
    assert lines[1] == "exec > /tmp/bootstrap.log 2>&1", (
        f"expected log redirect as line 2; got: {lines[1]!r}"
    )


def test_render_provision_starts_log_http_server_on_port_8001() -> None:
    """A sidecar HTTP server serves /tmp/bootstrap.log over port 8001.

    The line uses python3's stdlib http.server with --directory /tmp
    so the entire /tmp dir is browseable. Backgrounded with nohup so
    it survives the main server's ``exec``; its own stdout/stderr is
    sunk to /dev/null so it never pollutes the log it serves.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    expected = "nohup python3 -m http.server 8001 --directory /tmp >/dev/null 2>&1 &"
    assert expected in rp.script, (
        f"expected log-server bind line; not found in rendered script:\n{rp.script}"
    )


def test_render_provision_log_server_launches_before_main_server() -> None:
    """The log server must be up before the main server runs.

    If the order were reversed, a crash inside the main server would
    fire the EXIT trap (which keeps bash alive) but the log server
    might not yet be bound — losing the cause-of-death log.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    script = rp.script
    log_idx = script.index("python3 -m http.server 8001")
    main_idx = script.rindex("python -m diffusers_server")
    assert log_idx < main_idx, (
        "log server must be launched before the main server; "
        f"got log_idx={log_idx} main_idx={main_idx}"
    )


def test_render_provision_ports_include_8001_log_server_port() -> None:
    """``RenderedProvision.ports`` advertises 8001 so the provider exposes
    it as a proxy. Without this, the orchestrator's log-fetch CLI would
    have no proxy URL to hit.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "8001" in rp.ports, (
        f"expected port '8001' in RenderedProvision.ports; got {rp.ports!r}"
    )


def test_render_provision_installs_keep_alive_trap_on_exit() -> None:
    """A trap on EXIT keeps the container alive after the bootstrap dies.

    Bug it catches: Task 8 attempt #4 (after the log-fetch surface
    shipped). The container restart loop was so tight that the sidecar
    http.server on port 8001 never bound before PID 1 died, so logs
    were unfetchable. Without a way to see the actual crash, every
    further fix is a guess.

    The trap fires on any bash exit (success, ``set -e`` abort,
    SIGTERM, SIGINT) and runs ``sleep infinity`` so PID 1 — and the
    backgrounded log server with it — stays alive long enough for the
    orchestrator to fetch ``/tmp/bootstrap.log``. Selfterm's
    ``KINOFORGE_SELFTERM_DEAD_MAN_S`` / ``max_lifetime`` caps still
    fire, so this is not a runaway-cost risk.

    Locks in: the trap line exists, fires on EXIT, runs sleep
    infinity, and lands before any line that can fail (i.e. before
    pip install and before the main server).
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    script = rp.script
    expected_trap_substring = "sleep infinity"
    assert "trap" in script, "no trap line in rendered script"
    trap_lines = [ln for ln in script.split("\n") if ln.lstrip().startswith("trap ")]
    assert len(trap_lines) >= 1, f"no trap line found; script:\n{script}"
    trap_line = trap_lines[0]
    assert expected_trap_substring in trap_line, (
        f"trap line does not invoke sleep infinity: {trap_line!r}"
    )
    assert " EXIT" in trap_line, f"trap line does not trigger on EXIT: {trap_line!r}"


def test_render_provision_main_server_is_not_exec_for_trap_to_fire() -> None:
    """The main server line must NOT begin with ``exec``.

    With ``exec``, the python process replaces bash and bash's EXIT
    trap is lost — so a python crash terminates PID 1 immediately and
    the log server with it. Dropping the ``exec`` keeps bash as PID 1;
    when python exits (clean or crash), the trap runs and sleeps
    forever so the log surface stays up.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    last_python_line = next(
        ln
        for ln in reversed(rp.script.split("\n"))
        if ln.strip().endswith("diffusers_server")
    )
    assert not last_python_line.lstrip().startswith("exec "), (
        f"main server invocation must not be exec'd or the EXIT trap is "
        f"lost on python crash. Line: {last_python_line!r}"
    )


def test_render_provision_trap_registered_before_log_server_bind() -> None:
    """Trap must be registered before any failable line, including the
    log-server bind. If the http.server bind itself failed (port
    collision, missing python3, etc.), bash would exit without ever
    reaching the trap declaration; the container would terminate and
    the (un-bound) log server with it.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    script = rp.script
    trap_idx = script.index("trap ")
    http_idx = script.index("python3 -m http.server 8001")
    assert trap_idx < http_idx, (
        f"trap (idx={trap_idx}) must come before log-server bind "
        f"(idx={http_idx}); script:\n{script}"
    )


def test_render_provision_pip_install_no_naked_redirect_tokens() -> None:
    """Belt-and-braces: scan the pip line for naked ``>=`` outside quotes.

    A bare ``>=`` between word tokens is the canonical bash redirect
    misparse that bit us in Task 8 attempt #2. This guard fires even if
    the round-trip test above somehow agrees with a clever
    counter-example.
    """
    pip_deps = ["diffusers>=0.32", "transformers>=4.45"]
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["pip"] = list(pip_deps)
    rp = _make_engine().render_provision(cfg)
    pip_lines = [ln for ln in rp.script.split("\n") if ln.startswith("pip install -q")]
    assert len(pip_lines) == 1
    line = pip_lines[0]
    # Each `>=` must be inside a single-quoted segment, e.g.
    # `'diffusers>=0.32'`. A simple proxy: every `>=` occurrence must
    # have an unmatched `'` opening before it on the same line.
    for idx in range(len(line)):
        if line[idx : idx + 2] == ">=":
            preceding = line[:idx]
            # An odd number of `'` chars before this `>=` means we are
            # inside a single-quoted segment.
            assert preceding.count("'") % 2 == 1, (
                f"naked '>=' at index {idx} outside single-quote: {line!r}"
            )
