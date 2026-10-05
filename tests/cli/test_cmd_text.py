"""kinoforge text: refusal ordering, dry run, request shape, stdout contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401
from kinoforge.cli._main import main
from kinoforge.core.errors import Cancelled, TextGenerationFailed, UnknownAdapter
from kinoforge.core.interfaces import Artifact

_CFG = """\
engine:
  kind: diffusers
  precision: bf16
  diffusers:
    server_cmd: [python, -m, kinoforge.engines.diffusers.servers.text_server]
    capability:
      supported_modes: [{modes}]
models:
  - kind: base
    ref: hf:Qwen/Qwen3-0.6B
    target: checkpoints
text:
  engine: transformers
{prompt_line}compute:
  provider: fake
  image: fake:latest
"""


def _cfg(tmp_path: Path, modes: str = "t2t", prompt: str | None = None) -> Path:
    line = f"  prompt: {prompt!r}\n" if prompt else ""
    p = tmp_path / "text.yaml"
    p.write_text(_CFG.format(modes=modes, prompt_line=line))
    return p


def _png(tmp_path: Path, name: str = "in.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


@pytest.fixture
def no_pod_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any pod-adjacent call fails the test — proves refusals fire first."""

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("pod work must not start")

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", _boom)
    monkeypatch.setattr("kinoforge.cli._commands._resolve_attach_pod", _boom)
    monkeypatch.setattr("kinoforge.core.orchestrator.generate", _boom)


def _run(tmp_path: Path, *argv: str) -> int:
    return main(["--state-dir", str(tmp_path / "state"), "text", *argv])


@pytest.mark.usefixtures("no_pod_work")
def test_images_to_a_text_only_model_exit_2_before_pod_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Design §4.1. Bug caught: booking the pod and refusing after (the weak
    assert-raises version passes against that); a message naming neither model nor mode."""
    rc = _run(
        tmp_path,
        "-c",
        str(_cfg(tmp_path, "t2t")),
        "--prompt",
        "describe",
        "--image",
        str(_png(tmp_path)),
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "hf:Qwen/Qwen3-0.6B" in err and "it2t" in err and "['t2t']" in err


@pytest.mark.usefixtures("no_pod_work")
@pytest.mark.parametrize(
    ("argv_extra", "needle"),
    [
        (["--image", "/nonexistent/x.png"], "not found"),
        (["--image", "__GIF__"], ".png/.jpg/.jpeg"),
        (["--no-reuse", "--attach-pod", "p1"], "mutually exclusive"),
    ],
    ids=["missing-image", "bad-suffix", "reuse-vs-attach"],
)
def test_precondition_faults_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv_extra: list[str],
    needle: str,
) -> None:
    if "__GIF__" in argv_extra:
        gif = tmp_path / "x.gif"
        gif.write_bytes(b"GIF89a")
        argv_extra = [a if a != "__GIF__" else str(gif) for a in argv_extra]
    rc = _run(
        tmp_path, "-c", str(_cfg(tmp_path, "t2t, it2t")), "--prompt", "p", *argv_extra
    )
    assert rc == 2
    assert needle in capsys.readouterr().err


@pytest.mark.usefixtures("no_pod_work")
def test_no_prompt_anywhere_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path)))
    assert rc == 2
    assert "--prompt" in capsys.readouterr().err


@pytest.mark.usefixtures("no_pod_work")
def test_config_without_text_block_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "video.yaml"
    cfg.write_text(
        "engine:\n  kind: diffusers\n  precision: fp8\nmodels:\n  - kind: base\n    ref: hf:Wan-AI/Wan2.2-T2V\n"
        "    target: diffusion_models\ncompute:\n  provider: fake\n  image: fake:latest\n"
    )
    rc = _run(tmp_path, "-c", str(cfg), "--prompt", "p")
    assert rc == 2
    assert "`text:` block" in capsys.readouterr().err


@pytest.mark.usefixtures("no_pod_work")
def test_dry_run_reports_the_derived_mode_and_makes_no_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug caught: a dry run that scans warm pods, or reports t2t with an image."""
    rc = _run(
        tmp_path,
        "-c",
        str(_cfg(tmp_path, "t2t, it2t", prompt="from config")),
        "--image",
        str(_png(tmp_path)),
        "--dry-run",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode: it2t" in out and "images: 1" in out
    assert "hf:Qwen/Qwen3-0.6B" in out and "['it2t', 't2t']" in out
    assert "'from config'" in out


def test_happy_path_request_shape_and_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug caught: stdout polluted with a `text: uri=...` line (breaks piping);
    request=None (the upscale placeholder) so the stage has no prompt."""
    captured: dict[str, Any] = {}

    def fake_generate(cfg: Any, request: Any, **kw: Any) -> Any:
        captured["request"] = request
        captured.update(kw)
        return (
            Artifact(
                uri="/store/text-x/response.txt",
                meta={"text": "hello world", "published": None},
            ),
            None,
        )

    monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
    rc = _run(
        tmp_path,
        "-c",
        str(_cfg(tmp_path, "t2t, it2t")),
        "--prompt",
        "describe",
        "--image",
        str(_png(tmp_path)),
        "--no-reuse",
    )
    assert rc == 0
    assert capsys.readouterr().out == "hello world\n"
    req = captured["request"]
    assert req.prompt == "describe" and req.mode == "it2t"
    assert [a.role for a in req.assets] == ["image_1"]
    assert req.assets[0].ref.uri.startswith("file://")
    assert captured["skip_clip_stage"] is True
    assert captured["single"] is True
    assert "initial_clip" not in captured or captured["initial_clip"] is None


@pytest.mark.parametrize(
    ("exc", "needle"),
    # needle is matched against err.lower() (repo convention, e.g.
    # test_resolve_warm_instance.py:328) — "oom" lowercase, not "OOM" as the
    # brief's literal parametrize list had it, which is unsatisfiable since
    # TextGenerationFailed preserves the server_error's case verbatim.
    [(TextGenerationFailed("j1", "OOM"), "oom"), (Cancelled(), "cancelled")],
)
def test_pod_side_failures_exit_1_with_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: Exception,
    needle: str,
) -> None:
    """Bug caught: a traceback on a pod-side error (upscale's current shape),
    or exit 2 for an operational failure. Also (ruling C1, review finding):
    a failed run must NOT settle the launch row — settling releases
    launch.upgraded_id, the row on_instance_created re-keyed to the real pod
    id, deleting the only durable handle to a pod that may still be alive
    and billing. Only the return path settles."""

    def fake_generate(*a: Any, **k: Any) -> Any:
        raise exc

    settled: list[Any] = []
    monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
    monkeypatch.setattr(
        "kinoforge.cli._commands._settle_unused_launch_row",
        lambda *a, **k: settled.append(a),
    )
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path)), "--prompt", "p", "--no-reuse")
    assert rc == 1
    assert needle in capsys.readouterr().err.lower()
    assert not settled, (
        "a raise must keep the launch row (ruling C1) — settling it here "
        "would release the handle to a pod that may still be alive and "
        "billing"
    )


def test_unknown_text_engine_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Controller ruling 1 / spec §2.6: an unknown ``text.engine`` is a
    config/precondition fault (exit 2), not an operational failure (exit 1).
    Bug caught: UnknownAdapter falling through the broad `except
    KinoforgeError` rung below it and exiting 1. Also (ruling C1, review
    finding): this is still a raise from the orchestrator call, so it must
    NOT settle the launch row either — same "pod may still be alive"
    reasoning as the KinoforgeError/Cancelled rungs."""

    def fake_generate(*a: Any, **k: Any) -> Any:
        raise UnknownAdapter(
            "no text engine registered as 'nope'; known: ['transformers']"
        )

    settled: list[Any] = []
    monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
    monkeypatch.setattr(
        "kinoforge.cli._commands._settle_unused_launch_row",
        lambda *a, **k: settled.append(a),
    )
    rc = _run(tmp_path, "-c", str(_cfg(tmp_path)), "--prompt", "p", "--no-reuse")
    assert rc == 2
    assert "error:" in capsys.readouterr().err
    assert not settled, (
        "a raise must keep the launch row (ruling C1) even for an "
        "unknown-engine fault — settling it here would release the handle "
        "to a pod that may still be alive and billing"
    )


@pytest.mark.usefixtures("no_pod_work")
@pytest.mark.parametrize("extra", [["--dry-run"], []], ids=["dry-run", "real-run"])
def test_unknown_text_engine_is_refused_before_dry_run_and_pod_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    """Behaviour: an unregistered ``text.engine`` is a config fact (spec §2.5),
    so it exits 2 from the refusal block — before the ``--dry-run`` report,
    before the warm scan, before ``_resolve_attach_pod`` and before the launch
    row — with a message naming the bad name and the registered ones.

    Bug caught: the refusal happening only incidentally, deep inside
    ``DiffusersEngine.render_provision``. Exit 2 was right, but it arrived
    after a warm-candidate scan and a ledger launch row, and a ``--dry-run``
    (the one command whose whole job is to validate a config without spending)
    reported a happy plan for a config that can never run. A typo'd engine name
    then surfaces only once the operator has committed to a 10-minute boot.
    The ``no_pod_work`` fixture is what makes "before any pod work" an
    assertion rather than a claim.
    """
    cfg = _cfg(tmp_path)
    cfg.write_text(cfg.read_text().replace("engine: transformers", "engine: nope"))

    rc = _run(tmp_path, "-c", str(cfg), "--prompt", "p", *extra)

    assert rc == 2
    captured = capsys.readouterr()
    assert "nope" in captured.err and "transformers" in captured.err
    assert "text plan:" not in captured.out


def test_text_is_dispatched_and_interruptible() -> None:
    """Bug caught: a handler nobody can reach, or a 2-minute boot that ignores Ctrl-C."""
    from kinoforge.cli._main import _DISPATCH, _INTERRUPTIBLE_CMDS

    assert "text" in _DISPATCH
    assert "text" in _INTERRUPTIBLE_CMDS


def test_prompt_is_optional_and_images_repeat_at_the_parser() -> None:
    from kinoforge.cli._main import _build_parser

    args = _build_parser().parse_args(
        ["text", "-c", "cfg.yaml", "--image", "a.png", "--image", "b.jpg"]
    )
    assert args.prompt is None
    assert args.images == ["a.png", "b.jpg"]
    assert _build_parser().parse_args(["text", "-c", "cfg.yaml"]).images == []
