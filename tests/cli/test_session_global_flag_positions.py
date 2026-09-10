"""Session-global flags parse in BOTH positions, on every subcommand (U27).

``--state-dir``, ``--env-file``, ``--vault``, ``--ephemeral`` and
``--debug-show-secrets`` are session-globals: :func:`main` reads them after
parse and before dispatch. They were declared on the ROOT parser only, so the
position an operator naturally reaches for — after the subcommand — was an
argparse usage dump (**U27**: ``kinoforge batch --ephemeral`` exits 2).

Two defects in this family have already cost real money, and both had the same
mechanism: argparse parses a subcommand into a FRESH namespace and then copies
every key of it onto the parent, so a subparser that re-declares a root flag
with an implicit default silently CLOBBERS the root value.

* **U11** — ``p_grid`` re-declared ``--ephemeral``, so
  ``kinoforge --ephemeral grid …`` parsed to ``ephemeral=False`` and ran
  non-ephemerally while reporting success.
* **U29** — ``p_batch`` re-declared ``--env-file``, so
  ``kinoforge --env-file X batch …`` loaded the DEFAULT secrets file and booked
  GPUs against the wrong credentials, with no warning and exit 0.

Both were fixed with ``default=argparse.SUPPRESS``. This module pins the
general rule so the next subcommand cannot reintroduce the class.

Every assertion here is on a PARSED VALUE (``args.<dest>``) obtained from the
real :func:`_build_parser`, never on argv membership. That distinction is not
stylistic: U11's regression shipped precisely because the tests guarding it
asserted the flag was PRESENT in argv, which is blind to it being in a position
the parser rejects, or in a position whose value is later overwritten.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from kinoforge.cli._main import _build_parser, main

#: The five session-globals, with a sentinel value for each and the attribute
#: the parsed namespace must carry it on. Sentinels are arbitrary literals
#: chosen here, NOT read back out of the parser — so a propagation that quietly
#: substituted a default could not make these assertions pass.
_SESSION_GLOBALS: tuple[tuple[str, str, str | bool], ...] = (
    ("--state-dir", "state_dir", "/probe/state-dir"),
    ("--env-file", "env_file", "/probe/env-file"),
    ("--vault", "vault", "/probe/vault.yaml"),
    ("--ephemeral", "ephemeral", True),
    ("--debug-show-secrets", "debug_show_secrets", True),
)


def _leaf_paths() -> list[tuple[str, ...]]:
    """Enumerate every terminal subcommand path in the real parser.

    Derived from the parser itself rather than hand-listed, so a subcommand
    added later is covered automatically instead of quietly escaping the
    matrix — which is how U27 survived: ``p_batch`` was simply never on
    anybody's list.

    Returns:
        One tuple of subcommand tokens per leaf, e.g. ``("pod", "lora", "ls")``.
    """
    out: list[tuple[str, ...]] = []

    def walk(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            out.append(path)
            return
        for action in subs:
            for name, sub in action.choices.items():
                walk(sub, (*path, name))

    walk(_build_parser(), ())
    return out


def _required_argv(path: tuple[str, ...]) -> list[str]:
    """Synthesize the minimum required arguments for one leaf subcommand.

    Introspected rather than tabulated for the same reason as
    :func:`_leaf_paths`: a new required argument on an existing subcommand
    must not silently drop that subcommand out of the matrix.

    Args:
        path: Subcommand tokens identifying the leaf.

    Returns:
        Argv fragment satisfying the leaf's required arguments.
    """
    parser: argparse.ArgumentParser = _build_parser()
    for token in path:
        action = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        parser = action.choices[token]

    argv: list[str] = []
    for act in parser._actions:
        if isinstance(act, argparse._HelpAction):
            continue
        if not act.option_strings:  # a positional
            argv.append(f"probe-{act.dest}")
            continue
        if not act.required:
            continue
        argv.append(act.option_strings[-1])  # the long form
        if act.nargs != 0:
            argv.append(f"probe-{act.dest}")
    return argv


@pytest.mark.parametrize("path", _leaf_paths(), ids=lambda p: "_".join(p))
@pytest.mark.parametrize(
    ("flag", "dest", "value"), _SESSION_GLOBALS, ids=lambda v: str(v)
)
def test_session_global_parses_in_root_position(
    path: tuple[str, ...], flag: str, dest: str, value: str | bool
) -> None:
    """A root-position session-global survives every subcommand's parse.

    Bug caught: a subparser re-declaring the flag without
    ``argparse.SUPPRESS``, so the fresh-namespace copy overwrites the
    operator's root value with a default. That is U11 (ran non-ephemerally
    while reporting success) and U29 (booked GPUs against the wrong
    credentials) — both silent, both money.
    """
    argv = [flag] if value is True else [flag, str(value)]
    argv += [*path, *_required_argv(path)]

    args = _build_parser().parse_args(argv)

    assert getattr(args, dest) == value


@pytest.mark.parametrize("path", _leaf_paths(), ids=lambda p: "_".join(p))
@pytest.mark.parametrize(
    ("flag", "dest", "value"), _SESSION_GLOBALS, ids=lambda v: str(v)
)
def test_session_global_parses_in_subcommand_position(
    path: tuple[str, ...], flag: str, dest: str, value: str | bool
) -> None:
    """The natural position — after the subcommand — parses too.

    Bug caught: U27 itself. ``kinoforge batch --ephemeral -c … --manifest …``
    exits 2 with a usage dump, so an operator who reaches for the flag where
    every other option of that command lives gets either no run at all or,
    worse, retries without it and books a non-confidential one.
    """
    argv = [*path, *_required_argv(path)]
    argv += [flag] if value is True else [flag, str(value)]

    args = _build_parser().parse_args(argv)

    assert getattr(args, dest) == value


def test_nearer_position_wins_when_a_session_global_is_given_twice() -> None:
    """Subcommand position beats root position for the same flag.

    Bug caught: a propagation written with ``default=None`` rather than
    ``argparse.SUPPRESS``. With ``None`` the sub-position value is still
    copied over the root one, so this direction passes — but its sibling
    (root position, flag absent after the subcommand) breaks, which is
    exactly U29. Pinning the precedence makes the intended semantics
    explicit rather than incidental: the value nearest the work wins.
    """
    args = _build_parser().parse_args(
        [
            "--state-dir",
            "/probe/root",
            "generate",
            "-c",
            "probe-cfg",
            "--prompt",
            "probe",
            "--mode",
            "t2v",
            "--state-dir",
            "/probe/sub",
        ]
    )

    assert args.state_dir == "/probe/sub"


def test_intermediate_node_accepts_a_session_global_before_its_nested_subcommand() -> (
    None
):
    """``kinoforge pod --ephemeral lora ls X`` parses.

    Bug caught: a propagation pass that walks only LEAF subparsers, leaving
    the intermediate ``pod`` and ``sweeper`` nodes rejecting the flag. That
    fix would look complete — the leaf matrix above would be fully green —
    while two real positions still exit 2.
    """
    args = _build_parser().parse_args(
        ["pod", "--ephemeral", "lora", "ls", "probe-pod-id"]
    )

    assert args.ephemeral is True
    assert args.pod_id == "probe-pod-id"


def test_read_only_subcommand_still_reports_that_ephemeral_does_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accepting the token in the new position keeps the no-effect note.

    Bug caught: widening where argparse accepts ``--ephemeral`` while losing
    the ``_READ_ONLY_CMDS`` branch, so ``kinoforge list --ephemeral`` exits 0
    in silence and the operator believes the listing was confidential.
    """
    rc = main(["--state-dir", str(tmp_path / "state"), "list", "--ephemeral"])

    assert rc == 0
    assert (
        "--ephemeral has no effect on read-only subcommands" in capsys.readouterr().err
    )


def test_debug_show_secrets_mutex_fires_from_the_subcommand_position(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The redaction-bypass mutex holds in the newly-accepted position.

    ``main()`` refuses ``--ephemeral`` together with ``--debug-show-secrets``
    because ephemeral runs require redaction. That check reads both parsed
    values, so it is only as reliable as the propagation feeding it.

    Bug caught: a propagation that let one of the pair arrive as its default
    from the subcommand position, so the mutex stops firing and a run that
    the operator asked to be confidential writes unredacted credentials to
    the transcript. Credential safety, not ergonomics — hence its own test
    rather than a row in the matrix.
    """
    rc = main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "list",
            "--ephemeral",
            "--debug-show-secrets",
        ]
    )

    assert rc == 2
    assert (
        "--ephemeral and --debug-show-secrets are mutually" in capsys.readouterr().err
    )
