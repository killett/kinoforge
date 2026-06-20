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


def test_render_provision_script_ends_with_exec_server_cmd() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith("exec python -m diffusers_server")


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
    assert rp.ports == ["9999"]


def test_render_provision_port_defaults_to_8000_when_base_url_missing() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = ""
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["8000"]


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
