"""CLI flag exclusion + vault loading paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.cli import main


def _write_minimal_runway_yaml(tmp_path: Path) -> Path:
    """Minimal runway config that survives Config parsing for pre-flight."""
    cfg_path = tmp_path / "runway.yaml"
    cfg_path.write_text(
        "engine:\n"
        "  kind: runway\n"
        "  precision: ''\n"
        "spec:\n"
        "  model: gen4.5\n"
        "  mode: t2v\n"
        "models:\n"
        "  - ref: 'synthetic:runway-hosted'\n"
        "    kind: base\n"
        "    target: checkpoints\n"
        "lifecycle:\n"
        "  budget: 1.5\n",
    )
    return cfg_path


def test_ephemeral_and_debug_show_secrets_excluded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both privacy flags together → exit 2 before any work, message names both.

    Would-fail-bug: a missing exclusion check would let ``--debug-show-secrets``
    bypass redaction inside a ``--ephemeral`` session, leaking the prompt
    body via log records even though the operator asked for confidentiality.
    """
    cfg = _write_minimal_runway_yaml(tmp_path)
    state = tmp_path / "state"
    rc = main(
        [
            "--state-dir",
            str(state),
            "--ephemeral",
            "--debug-show-secrets",
            "generate",
            "-c",
            str(cfg),
            "--prompt",
            "x",
            "--mode",
            "t2v",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "--debug-show-secrets" in err
    assert "--ephemeral" in err
    assert "mutually exclusive" in err


def test_vault_env_var_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``KINOFORGE_VAULT`` env var loads the vault when ``--vault`` omitted.

    Would-fail-bug: requiring the explicit flag would force every shell
    user to repeat ``--vault PATH`` on every command, defeating the
    quiet-default UX the env-var fallback exists to provide.
    """
    from kinoforge.core.redaction import RedactionRegistry

    RedactionRegistry.instance().clear_session()
    # Vault outside any git repo: place under /tmp.
    vault = Path("/tmp/_kf_test_vault.yaml")
    try:
        vault.write_text("positive_prompt: test-secret-prompt\n")
        vault.chmod(0o600)
        monkeypatch.setenv("KINOFORGE_VAULT", str(vault))
        # Drive a read-only subcommand so the run terminates early.
        # Vault loading happens at main() entry, before dispatch.
        main(["--state-dir", str(tmp_path / "state"), "list"])
        # Verify the prompt body is registered: redact() substitutes it.
        out = RedactionRegistry.instance().redact("the test-secret-prompt body")
        assert "test-secret-prompt" not in out
    finally:
        if vault.exists():
            vault.unlink()
        RedactionRegistry.instance().clear_session()


def test_vault_under_repo_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Vault path resolving under the active repo → exit 2 with VaultError.

    Would-fail-bug: silently accepting an in-repo vault path would invite
    operators to commit prompt files alongside their configs, exactly the
    leak vector vault was designed to prevent.
    """
    # The repo's own root is the active git repo at test time. Place a
    # vault inside it (under tests/ tmp dir is fine — the test asserts the
    # under-repo check fires from the load_vault implementation).
    in_repo_vault = Path(__file__).parent / "_in_repo_vault.yaml"
    try:
        in_repo_vault.write_text("positive_prompt: x\n")
        in_repo_vault.chmod(0o600)
        rc = main(
            [
                "--state-dir",
                str(tmp_path / "state"),
                "--vault",
                str(in_repo_vault),
                "list",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "vault" in err.lower()
    finally:
        if in_repo_vault.exists():
            in_repo_vault.unlink()


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["--ephemeral", "grid", "--spec", "/x/spec.yaml"], True),
        (["grid", "--spec", "/x/spec.yaml", "--ephemeral"], True),
        (["grid", "--spec", "/x/spec.yaml"], False),
    ],
)
def test_root_ephemeral_survives_the_grid_subparser(
    argv: list[str], expected: bool
) -> None:
    """``--ephemeral`` must mean the same thing on either side of ``grid``.

    ``grid`` is the ONLY subcommand that re-declares ``--ephemeral`` (every
    other one relies on the root flag). argparse parses a subcommand into a
    fresh namespace and then copies EVERY key onto the parent, so a
    subparser option with an implicit ``default=False`` silently overwrites
    a root-set ``True``. That makes ``kinoforge --ephemeral grid ...`` run
    non-ephemeral while reporting success — U11's original defect (run id +
    timestamp published to the provider) reached through the other door.

    Would-fail-bug: ``p_grid``'s ``--ephemeral`` declared without
    ``default=argparse.SUPPRESS``, so the root value is clobbered.
    """
    from kinoforge.cli._main import _build_parser

    args = _build_parser().parse_args(argv)
    assert args.ephemeral is expected, (
        f"argv {argv} → args.ephemeral={args.ephemeral!r}, expected {expected}"
    )


@pytest.mark.parametrize(
    "argv, expected",
    [
        (
            ["--env-file", "/x/creds-a", "batch", "-c", "c", "--manifest", "m"],
            "/x/creds-a",
        ),
        (
            ["batch", "-c", "c", "--manifest", "m", "--env-file", "/x/creds-a"],
            "/x/creds-a",
        ),
        (["batch", "-c", "c", "--manifest", "m"], None),
        (
            [
                "--env-file",
                "/x/creds-a",
                "generate",
                "-c",
                "c",
                "--prompt",
                "p",
                "--mode",
                "t2v",
            ],
            "/x/creds-a",
        ),
    ],
)
def test_root_env_file_survives_the_batch_subparser(
    argv: list[str], expected: str | None
) -> None:
    """U29: the ROOT ``--env-file`` must reach ``main()`` for ``batch`` too.

    ``p_batch`` re-declares ``--env-file`` with an implicit ``default=None``.
    argparse parses a subcommand into a FRESH namespace and then copies every
    key onto the parent, so that default silently overwrites the operator's
    root-set path; ``main()`` then loads the default secrets file instead
    (``env_file = Path(args.env_file) if args.env_file is not None else None``).
    Because ``batch`` books GPUs, the failure mode is a run against the wrong
    credentials or the wrong provider account, with no warning and exit 0.
    Same namespace-clobber mechanism as the ``p_grid``/``--ephemeral`` half of
    U11.

    Would-fail-bug: ``p_batch``'s ``--env-file`` declared without
    ``default=argparse.SUPPRESS``. The last case is the guard — every other
    subcommand already honours the root flag and must keep doing so.
    """
    from kinoforge.cli._main import _build_parser

    args = _build_parser().parse_args(argv)
    assert args.env_file == expected, (
        f"argv {argv} → args.env_file={args.env_file!r}, expected {expected!r}"
    )
