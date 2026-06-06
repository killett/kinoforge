"""argparse entry point + dispatch for the kinoforge CLI.

``main(argv)`` resolves to the ``kinoforge.cli.main`` import surface via
the back-compat shim in ``cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from kinoforge.cli._commands import (
    _build_sink,  # noqa: F401 — re-exported via package shim
    _build_store,  # noqa: F401 — re-exported via package shim
    _cli_clock,  # noqa: F401 — re-exported via package shim
    _cmd_batch,
    _cmd_deploy,
    _cmd_destroy,
    _cmd_forget,
    _cmd_gc,
    _cmd_generate,
    _cmd_list,
    _cmd_provision,
    _cmd_reap,
    _cmd_status,
    _cmd_stop,
)
from kinoforge.cli.context import SessionContext
from kinoforge.cli.sidecar import SIDECAR_NAME
from kinoforge.core.dotenv_loader import load_env_file
from kinoforge.core.errors import (
    ConfigError,
    SidecarMigrationBlocked,
    SidecarMismatch,
)

_DISPATCH: dict[str, Callable[[argparse.Namespace, SessionContext], int]] = {
    "deploy": _cmd_deploy,
    "provision": _cmd_provision,
    "generate": _cmd_generate,
    "batch": _cmd_batch,
    "list": _cmd_list,
    "status": _cmd_status,
    "stop": _cmd_stop,
    "destroy": _cmd_destroy,
    "forget": _cmd_forget,
    "reap": _cmd_reap,
    "gc": _cmd_gc,
}


def _build_parser(state_dir_default: str = ".kinoforge") -> argparse.ArgumentParser:
    """Build and return the top-level ArgumentParser.

    Args:
        state_dir_default: Default value for ``--state-dir``.

    Returns:
        A fully configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="kinoforge",
        description="kinoforge — GPU video generation orchestrator",
    )
    parser.add_argument(
        "--state-dir",
        default=state_dir_default,
        metavar="DIR",
        help="directory for state/ledger (default: .kinoforge)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help=(
            "path to a .env file containing kinoforge credentials "
            "(default: ./.env if it exists; absent default is silent)"
        ),
    )

    sub = parser.add_subparsers(dest="cmd", metavar="SUBCOMMAND")

    # deploy
    p_deploy = sub.add_parser("deploy", help="provision compute and deploy")
    p_deploy.add_argument("--config", required=True, metavar="PATH")
    p_deploy.add_argument("--dry-run", action="store_true")

    # provision
    p_provision = sub.add_parser("provision", help="provision an existing instance")
    p_provision.add_argument("-c", "--config", required=True, metavar="PATH")

    # generate
    p_generate = sub.add_parser("generate", help="run a generation job")
    p_generate.add_argument("-c", "--config", required=True, metavar="PATH")
    p_generate.add_argument("--prompt", required=True, metavar="TEXT")
    p_generate.add_argument("--mode", required=True, metavar="MODE")
    p_generate.add_argument("--run-id", default=None, metavar="ID")
    p_generate_output = p_generate.add_mutually_exclusive_group()
    p_generate_output.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="user-facing output directory (overrides cfg.output.dir)",
    )
    p_generate_output.add_argument(
        "--no-output-dir",
        action="store_true",
        help="disable user-facing publish; clips remain only in the store",
    )

    # list
    sub.add_parser("list", help="list running instances from ledger")

    # status
    p_status = sub.add_parser("status", help="show status of one instance")
    p_status.add_argument("--id", required=True, metavar="ID")
    p_status.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        metavar="PATH",
        help="optional config; fills missing legacy ledger fields",
    )

    # stop
    p_stop = sub.add_parser("stop", help="stop an instance")
    p_stop.add_argument("--id", required=True, metavar="ID")

    # destroy
    p_destroy = sub.add_parser("destroy", help="destroy an instance")
    p_destroy.add_argument("--id", required=True, metavar="ID")

    # forget
    p_forget = sub.add_parser(
        "forget", help="remove an instance entry from the local ledger"
    )
    p_forget.add_argument("--id", required=True, metavar="ID")

    # reap
    sub.add_parser("reap", help="sweep and destroy stale instances")

    # gc
    p_gc = sub.add_parser("gc", help="garbage-collect stored artifacts")
    p_gc.add_argument("--config", required=True, metavar="PATH")
    p_gc.add_argument("--run", default=None, metavar="RUN_ID")
    p_gc.add_argument("--older-than", default=None, metavar="DUR")

    # batch
    p_batch = sub.add_parser("batch", help="run a batch of generation jobs")
    p_batch.add_argument("-c", "--config", required=True, metavar="PATH")
    p_batch.add_argument("--manifest", required=True, metavar="PATH")
    p_batch.add_argument("--batch-id", default=None, metavar="ID")
    p_batch.add_argument("--concurrent", type=int, default=None, metavar="N")
    p_batch.add_argument("--env-file", default=None, metavar="PATH")
    p_batch_output = p_batch.add_mutually_exclusive_group()
    p_batch_output.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="user-facing output directory (overrides cfg.output.dir)",
    )
    p_batch_output.add_argument(
        "--no-output-dir",
        action="store_true",
        help="disable user-facing publish; clips remain only in the store",
    )

    return parser


def _print_instance_overview(ctx: SessionContext) -> None:
    """Print one-line overview per ledger entry; degrade gracefully on failure.

    Args:
        ctx: Per-invocation session context.
    """
    ledger, warn = ctx.ledger_safe()
    if ledger is None:
        print(f"[instance overview] unavailable: {warn}")
        return
    try:
        entries = ledger.entries()
    except Exception as exc:  # noqa: BLE001 — best-effort surface
        print(f"[instance overview] unavailable: {type(exc).__name__}: {exc}")
        return
    now = time.time()
    if not entries:
        print("[instance overview] No running instances.")
        return
    print("[instance overview]")
    for entry in entries:
        iid = entry.get("id", "?")
        created_at = float(entry.get("created_at", now))
        age_s = now - created_at
        age_h = age_s / 3600.0
        rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        spend = age_h * rate
        print(f"  {iid}  age={age_h:.1f}h  est_spend=${spend:.4f}")


def main(argv: list[str] | None = None) -> int:
    """Parse argv, build SessionContext, dispatch to subcommand.

    Args:
        argv: Explicit argument list; uses ``sys.argv[1:]`` when ``None``.

    Returns:
        Integer exit code (0 on success, non-zero on error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)

    # Load .env secrets before dispatch; shell-set values always win.
    env_file = Path(args.env_file) if args.env_file is not None else None
    load_env_file(env_file)

    cfg_path = Path(args.config) if getattr(args, "config", None) else None
    try:
        ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)
    except (SidecarMismatch, SidecarMigrationBlocked) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PydanticValidationError as exc:
        print(
            f"error: sidecar at {state_dir / SIDECAR_NAME} is unreadable: "
            f"{exc}; rm to reset",
            file=sys.stderr,
        )
        return 1
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: config: {exc}", file=sys.stderr)
        return 1

    _print_instance_overview(ctx)

    if args.cmd is None:
        parser.print_help()
        return 0

    return _DISPATCH[args.cmd](args, ctx)


__all__ = [
    "_DISPATCH",
    "_build_parser",
    "_build_sink",
    "_build_store",
    "_cli_clock",
    "_print_instance_overview",
    "main",
]
