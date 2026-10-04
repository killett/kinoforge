"""kinoforge image: the --ephemeral contract (Task 7) and the CLI (Task 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

IMAGE_CFG_YAML = """\
mode: t2i
prompt: "a cat in a meadow"

image:
  engine: {engine}
  spec:
    model: "{model}"

output:
  dir: out
"""


def _write_cfg(tmp_path: Path, engine: str, model: str = "m") -> Path:
    path = tmp_path / f"{engine}-t2i.yaml"
    path.write_text(IMAGE_CFG_YAML.format(engine=engine, model=model))
    return path


@pytest.mark.parametrize("engine", ["fal", "luma_agents", "replicate"])
def test_ephemeral_is_refused_for_every_live_image_engine(
    tmp_path: Path, engine: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refusal is a DECISION, keyed on the image engine, not a key miss.

    Bug this catches: the refusal drifting back to the accidental ("", None)
    lookup miss it is today — which would silently start ALLOWING --ephemeral
    the moment anyone gave Config.engine a non-None default, with no provider
    scrub hook anywhere to honour it.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, engine)
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--ephemeral",
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "x",
            "--dry-run",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--ephemeral" in err
    assert engine in err


def test_ephemeral_refusal_names_the_image_engine_not_video_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message must not offer the video table's remedies.

    Bug this catches: reusing _preflight_error_block verbatim, which tells the
    operator to switch to `engine: replicate` or `engine: runway` — neither of
    which is an IMAGE engine, so the advice is nonsense on this path.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, "luma_agents", model="uni-1")
    main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--ephemeral",
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "x",
            "--dry-run",
        ]
    )
    err = capsys.readouterr().err
    assert "engine: runway" not in err
    assert "luma_agents" in err


def test_fake_image_engine_permits_ephemeral(tmp_path: Path) -> None:
    """The in-process fake has no provider-side state, so it is trivially safe.

    Bug this catches: a blanket refusal for every image config, which would
    make the ephemeral path untestable offline.
    """
    from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

    assert IMAGE_EPHEMERAL_CAPABILITIES["fake"] is True
    assert IMAGE_EPHEMERAL_CAPABILITIES["fal"] is False
    assert IMAGE_EPHEMERAL_CAPABILITIES["luma_agents"] is False
    assert IMAGE_EPHEMERAL_CAPABILITIES["replicate"] is False


def test_image_table_covers_every_registered_image_engine() -> None:
    """Guard the guard: a new image engine must not default to "allowed".

    Bug this catches: adding a fifth image engine and forgetting the table, so
    `.get()` returns None and the operator gets a confusing refusal — or worse,
    a permissive default if the lookup is ever written with `.get(name, True)`.
    """
    import kinoforge._adapters  # noqa: F401  — self-registration
    from kinoforge.core import registry
    from kinoforge.core.ephemeral import IMAGE_EPHEMERAL_CAPABILITIES

    registered = set(registry._image_engines)
    assert registered, "no image engines registered; the assertion below is vacuous"
    missing = registered - set(IMAGE_EPHEMERAL_CAPABILITIES)
    assert not missing, (
        f"image engines with no IMAGE_EPHEMERAL_CAPABILITIES entry: {sorted(missing)}"
    )


def test_image_inherits_every_session_global() -> None:
    """The U27 contract: every leaf subcommand accepts all five session-globals.

    Asserted against the real parser rather than by importing the matrix test
    module (which would require tests/cli/__init__.py to exist). The matrix
    enumerates _build_parser() itself, so `image` joins it automatically — this
    test states the property that matters directly.

    Bug this catches: adding the subcommand somewhere the recursive propagation
    pass does not reach, which is the U27/U29 defect — a subcommand that runs
    non-ephemerally while reporting success.
    """
    from kinoforge.cli._main import _build_parser

    for flag, dest, value in (
        ("--state-dir", "state_dir", "/probe/state"),
        ("--env-file", "env_file", "/probe/env"),
        ("--vault", "vault", "/probe/vault.yaml"),
        ("--ephemeral", "ephemeral", True),
        ("--debug-show-secrets", "debug_show_secrets", True),
    ):
        argv = ["image", "-c", "cfg.yaml", flag]
        if value is not True:
            argv.append(str(value))
        args = _build_parser().parse_args(argv)
        assert getattr(args, dest) == value, (
            f"{flag} in subcommand position did not reach args.{dest} for "
            f"`image` — the session-global propagation missed this node"
        )


def test_image_is_interruptible() -> None:
    """Without this the SIGINT handler is never installed for `image`.

    Bug this catches: shipping Task 5's cancellation fix with no way to reach
    it — a ~125 s Luma poll would still ignore Ctrl-C because main() only
    installs the handler for commands in _INTERRUPTIBLE_CMDS.
    """
    from kinoforge.cli._main import _DISPATCH, _INTERRUPTIBLE_CMDS

    assert "image" in _DISPATCH
    assert "image" in _INTERRUPTIBLE_CMDS


def test_prompt_is_optional_at_the_parser() -> None:
    """A config carrying image.prompt must be runnable with no --prompt.

    Bug this catches: copying `generate`'s required=True --prompt, which would
    make every self-contained image config un-runnable as written.
    """
    from kinoforge.cli._main import _build_parser

    args = _build_parser().parse_args(["image", "-c", "cfg.yaml"])
    assert args.prompt is None


def test_output_dir_flags_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Matches `generate`'s surface.

    Bug this catches: accepting both, leaving it ambiguous whether the operator
    wanted a custom directory or no publish at all. `pytest.raises(SystemExit)`
    alone only proves argparse bailed for SOME reason — a renamed `-c`, a
    mistyped flag, or a broken subparser name would exit non-zero too and this
    test would stay green. Assert the real failure mode instead: a non-zero
    exit AND both conflicting flag names named in stderr, mirroring
    `tests/test_cli.py`'s `generate --output-dir ... --no-output-dir` guard.
    """
    from kinoforge.cli._main import _build_parser

    with pytest.raises(SystemExit) as exc_info:
        _build_parser().parse_args(
            ["image", "-c", "c.yaml", "--output-dir", "x", "--no-output-dir"]
        )

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "--output-dir" in err and "--no-output-dir" in err


def test_dry_run_exits_zero_and_makes_no_http_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run must not touch the network, and must never call generate_image.

    Bug this catches: deleting the entire --dry-run branch from _cmd_image.
    The `fake` engine's backend is in-process (no HTTP) and its artifact
    carries no `url`, so with the branch gone `rc == 0` and zero urlopen calls
    would ALSO hold — this config would fall straight through to
    generate_image and still look "clean" by those two assertions alone. That
    made the one requirement in this task that costs real money have no
    regression guard. Pin two more facts that only the dry-run branch
    produces: the plan is printed, and generate_image is never invoked.
    """
    import types
    import urllib.request

    from kinoforge.cli._main import main

    calls: list[str] = []

    def _boom_urlopen(*a: object, **k: object) -> object:
        calls.append("urlopen")
        raise AssertionError("dry-run made an HTTP call")

    def _fake_generate_image(*a: object, **k: object) -> object:
        # Recorded, not raised: a clean `calls == []` failure is more useful
        # than an uncaught exception bubbling out of main() when the branch
        # under test has been removed.
        calls.append("generate_image")
        return types.SimpleNamespace(uri="fake://should-not-have-run")

    monkeypatch.setattr(urllib.request, "urlopen", _boom_urlopen)
    monkeypatch.setattr("kinoforge.core.image_run.generate_image", _fake_generate_image)

    cfg = _write_cfg(tmp_path, "fake")
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "a cat",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "image plan:" in out
    assert "engine: fake" in out


def test_missing_image_block_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A video config handed to `image` is a precondition error, not a crash.

    Bug this catches: an AttributeError traceback instead of a message naming
    the missing block.
    """
    from kinoforge.cli._main import main

    cfg = tmp_path / "video.yaml"
    cfg.write_text(
        "engine:\n  kind: fake\n  precision: ''\n"
        "models:\n  - kind: base\n    ref: 'hf:a/b'\n    target: checkpoints\n"
    )
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "x",
        ]
    )
    assert rc == 2
    assert "image:" in capsys.readouterr().err


def test_no_prompt_anywhere_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preflight refusal surfaces as exit 2, not a traceback.

    Bug this catches: letting ValidationError escape main() as an unhandled
    exception, which exits 1 with a stack trace instead of 2 with the fix.
    """
    from kinoforge.cli._main import main

    cfg = tmp_path / "noprompt.yaml"
    cfg.write_text(
        "image:\n  engine: fake\n  spec:\n    model: 'm'\noutput:\n  dir: out\n"
    )
    rc = main(["--state-dir", str(tmp_path / "state"), "image", "-c", str(cfg)])
    assert rc == 2
    assert "--prompt" in capsys.readouterr().err


def test_resolve_run_id_prefixes_and_honours_the_flag() -> None:
    """The extracted helper replaces three copies of the same derivation.

    Bug this catches: the four copies drifting — e.g. one of them forgetting to
    honour --run-id, which silently ignores an operator's explicit id.
    """
    import argparse

    from kinoforge.cli._commands import _resolve_run_id

    explicit = argparse.Namespace(run_id="my-run")
    assert _resolve_run_id(explicit, "image") == "my-run"

    derived = _resolve_run_id(argparse.Namespace(run_id=None), "image")
    assert derived.startswith("image-")
    assert len(derived) == len("image-") + 15  # YYYYmmdd-HHMMSS


def test_resolve_run_id_reads_the_cli_clock_seam_and_pins_the_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins format, prefix, AND the ``_cli_clock`` seam in one assertion.

    Bug this catches: the two assertions above (``startswith`` + a bare
    length check) admit a transposed ``%H%M%S-%Y%m%d`` of the same length,
    and neither one exercises the ``kinoforge.cli._cli_clock`` seam that
    ``_cmd_generate``'s existing
    ``test_cli_default_run_id_uniquifies_per_invocation`` depends on — a
    regression to a bare ``datetime.now()`` in ``_resolve_run_id`` (this
    task's brief literally specified one) is caught there only as a side
    effect, not directly. A FakeClock monkeypatch pins the exact string.
    """
    import argparse
    from datetime import datetime

    from kinoforge.cli._commands import _resolve_run_id
    from kinoforge.core.clock import FakeClock

    fake_clock = FakeClock(start=1_000_000.0)
    monkeypatch.setattr("kinoforge.cli._cli_clock", fake_clock)

    expected_ts = datetime.fromtimestamp(1_000_000.0).strftime("%Y%m%d-%H%M%S")
    derived = _resolve_run_id(argparse.Namespace(run_id=None), "image")
    assert derived == f"image-{expected_ts}"


def test_cancelled_generation_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI maps a cancelled generation to exit 1, not a stack trace.

    Bug this catches: an except-clause ordering mistake — ``Cancelled`` is a
    ``KinoforgeError`` subclass, so a handler that checks ``KinoforgeError``
    before ``Cancelled`` would swallow it with the wrong message, and a
    handler that omits the ``Cancelled`` clause entirely would let it escape
    ``main()`` as an unhandled exception instead of the documented exit 1.
    This is also the clause the whole widened ``ImageBackend.result``
    cancellation chain exists to serve, and until now it had no CLI-level
    test anywhere (only the backend itself was covered, by
    ``tests/image_engines/test_cancellation.py``).
    """
    from kinoforge.cli._main import main
    from kinoforge.core.errors import Cancelled

    def _cancel(*a: object, **k: object) -> object:
        raise Cancelled("operator pressed ctrl-c")

    monkeypatch.setattr("kinoforge.core.image_run.generate_image", _cancel)

    cfg = _write_cfg(tmp_path, "fake")
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "a cat",
        ]
    )
    assert rc == 1
    assert "cancelled" in capsys.readouterr().err


def test_unknown_image_engine_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd ``image.engine`` is a config/precondition problem, not exit 1.

    Bug this catches: ``UnknownAdapter`` subclasses ``KinoforgeError``, so a
    handler that checks the broad ``except KinoforgeError`` clause before (or
    without) a dedicated ``except UnknownAdapter`` clause swallows it as a
    generic run-time failure and returns 1. Design §2.4 lists an unknown
    image engine under exit 2 (config/precondition) — the same exit code
    ``docs/configuration.md`` tells operators to act on, because only a real
    run resolves the engine name. No monkeypatch needed: a genuinely
    unregistered engine name drives the real ``registry.get_image_engine``
    lookup inside ``resolve_image_stack``.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, "lmua_agents")  # typo'd engine name, never registered
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "a cat",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "lmua_agents" in err


def test_dry_run_prompt_resolution_matches_the_real_path(tmp_path: Path) -> None:
    """Dry-run prompt resolution must not drift from generate_image's.

    Bug this catches: a hand-rolled ``getattr(...) or ... or ...`` precedence
    chain in the dry-run branch, which LATCHES a whitespace-only ``--prompt``
    instead of treating it as absent the way
    ``kinoforge.core.image_run._resolve_prompt`` does (it skips a
    whitespace-only candidate and falls through to the next source). Before
    the fix, ``--prompt "   " --dry-run`` against this config (top-level
    ``prompt: "a cat in a meadow"``, no ``image.prompt``) exited 2, while the
    same invocation without ``--dry-run`` succeeded by falling through to the
    top-level prompt — a dry run disagreeing with the run it previews is the
    exact defect class this task eliminated for ``run_id``.
    """
    from kinoforge.cli._main import main

    cfg = _write_cfg(tmp_path, "fake")  # top-level prompt: "a cat in a meadow"
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "image",
            "-c",
            str(cfg),
            "--prompt",
            "   ",
            "--dry-run",
        ]
    )
    assert rc == 0
