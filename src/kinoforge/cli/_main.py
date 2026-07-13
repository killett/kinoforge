"""argparse entry point + dispatch for the kinoforge CLI.

``main(argv)`` resolves to the ``kinoforge.cli.main`` import surface via
the back-compat shim in ``cli/__init__.py``.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError as PydanticValidationError

from kinoforge import __version__
from kinoforge.cli._commands import (
    _build_sink,  # noqa: F401 — re-exported via package shim
    _build_store,  # noqa: F401 — re-exported via package shim
    _cli_clock,  # noqa: F401 — re-exported via package shim
    _cmd_batch,
    _cmd_cost,
    _cmd_deploy,
    _cmd_destroy,
    _cmd_doctor,
    _cmd_forget,
    _cmd_gc,
    _cmd_generate,
    _cmd_grid,
    _cmd_interpolate,
    _cmd_list,
    _cmd_logs,
    _cmd_pod_lora_ls,
    _cmd_provision,
    _cmd_reap,
    _cmd_status,
    _cmd_stop,
    _cmd_sweeper_metrics,
    _cmd_sweeper_start,
    _cmd_sweeper_status,
    _cmd_sweeper_stop,
    _cmd_upscale,
)
from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries
from kinoforge.cli.context import SessionContext
from kinoforge.cli.sidecar import SIDECAR_NAME
from kinoforge.core.cancel import CancelToken
from kinoforge.core.dotenv_loader import load_env_file
from kinoforge.core.ephemeral import EPHEMERAL_CAPABILITIES, EphemeralSession
from kinoforge.core.errors import (
    ConfigError,
    SidecarMigrationBlocked,
    SidecarMismatch,
    VaultError,
)
from kinoforge.core.redaction import RedactingLogFilter, RedactionRegistry
from kinoforge.core.vault import Vault, load_vault, register_vault_tokens

_log = logging.getLogger(__name__)


class _LorasOnceAction(argparse.Action):
    """Reject a second ``--loras`` on the same invocation (P3-D1)."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        """Set ``namespace.loras`` once; error on repeat."""
        if getattr(namespace, self.dest) is not None:
            parser.error("--loras may be specified at most once")
        setattr(namespace, self.dest, values)


# Subcommands that run a long orchestration loop and therefore need the
# graceful-interrupt SIGINT handler installed before dispatch. Every
# other subcommand keeps the default Ctrl-C behavior (read-only
# operations should exit immediately on the first press).
_INTERRUPTIBLE_CMDS: frozenset[str] = frozenset({"generate", "batch"})

# Subcommands that never trigger orchestration. ``--ephemeral`` is a no-op
# for them; emit a one-line stderr note rather than running pre-flight.
_READ_ONLY_CMDS: frozenset[str] = frozenset(
    {
        "list",
        "status",
        "stop",
        "destroy",
        "forget",
        "reap",
        "gc",
        "cost",
        "pod",
        "logs",
    }
)


def _dispatch_pod(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Route ``kinoforge pod <subcmd>`` to its handler."""
    sub = getattr(args, "pod_cmd", None)
    if sub == "lora":
        lora_sub = getattr(args, "pod_lora_cmd", None)
        if lora_sub == "ls":
            return _cmd_pod_lora_ls(args, ctx)
        sys.stderr.write("kinoforge pod lora: missing subcommand (ls)\n")
        return 2
    sys.stderr.write("kinoforge pod: missing subcommand (lora)\n")
    return 2


def _dispatch_sweeper(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Route ``kinoforge sweeper <subcmd>`` to its handler."""
    sub = getattr(args, "sweeper_cmd", None)
    if sub == "start":
        return _cmd_sweeper_start(args, ctx)
    if sub == "stop":
        return _cmd_sweeper_stop(args, ctx)
    if sub == "status":
        return _cmd_sweeper_status(args, ctx)
    if sub == "metrics":
        return _cmd_sweeper_metrics(args, ctx)
    sys.stderr.write(
        "kinoforge sweeper: missing subcommand (start|stop|status|metrics)\n"
    )
    return 2


_DISPATCH: dict[str, Callable[[argparse.Namespace, SessionContext], int]] = {
    "deploy": _cmd_deploy,
    "provision": _cmd_provision,
    "generate": _cmd_generate,
    "upscale": _cmd_upscale,
    "interpolate": _cmd_interpolate,
    "batch": _cmd_batch,
    "list": _cmd_list,
    "status": _cmd_status,
    "stop": _cmd_stop,
    "destroy": _cmd_destroy,
    "logs": _cmd_logs,
    "forget": _cmd_forget,
    "reap": _cmd_reap,
    "gc": _cmd_gc,
    "cost": _cmd_cost,
    "doctor": _cmd_doctor,
    "sweeper": _dispatch_sweeper,
    "pod": _dispatch_pod,
    "grid": _cmd_grid,
}


def _install_redacting_filter(*, bypass: bool) -> None:
    """Install ``RedactingLogFilter`` AND a record-factory redactor.

    Two complementary layers:

    1. ``RedactingLogFilter`` on root + ``kinoforge`` loggers — runs for
       any record emitted DIRECTLY to those loggers (the logger-filter
       chain is consulted by ``Logger.handle()``).
    2. ``setLogRecordFactory`` override — Python's logger-filters do NOT
       run during child-logger propagation; only handler-filters do.
       Hooking the factory redacts every record AT BIRTH, so propagated
       records (e.g. ``kinoforge.<submodule>`` logs flowing up to root's
       handler) are caught regardless of where filters live downstream.

    Both layers are idempotent — repeated ``main()`` calls within one
    Python process (as the test suite does) install at most one filter
    per logger and one factory override.
    """
    import logging

    flt = RedactingLogFilter(RedactionRegistry.instance(), bypass=bypass)
    for logger in (logging.getLogger("kinoforge"), logging.getLogger()):
        existing = [f for f in logger.filters if isinstance(f, RedactingLogFilter)]
        for f in existing:
            logger.removeFilter(f)
        logger.addFilter(flt)

    # Record-factory hook: catches propagated child-logger records that
    # the logger-filter chain misses. Uses the BASE factory (the original
    # default), not the previously installed redactor — so repeated calls
    # don't double-wrap.
    registry = RedactionRegistry.instance()
    base_factory = getattr(
        _install_redacting_filter, "_base_factory", logging.getLogRecordFactory()
    )
    _install_redacting_filter._base_factory = base_factory  # type: ignore[attr-defined]

    def _redacting_factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        if bypass:
            return record
        if isinstance(record.msg, str):
            record.msg = registry.redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    registry.redact(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (registry.redact(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return record

    logging.setLogRecordFactory(_redacting_factory)


def _load_vault_or_none(vault_arg: str | None) -> Vault | None:
    """Load vault from ``--vault`` flag, ``KINOFORGE_VAULT`` env, or ``None``.

    On success, also registers the vault's prompt-derived tokens with the
    ``RedactionRegistry`` so the logging filter substitutes them.
    """
    import os

    path = vault_arg or os.environ.get("KINOFORGE_VAULT")
    if path is None:
        return None
    vault = load_vault(Path(path))
    register_vault_tokens(vault)
    return vault


def _preflight_error_block(engine: str, provider: str | None) -> str:
    return (
        "ERROR: --ephemeral is not supported for this configuration.\n"
        f"  engine:    {engine}\n"
        f"  provider:  {provider or '(none — hosted API)'}\n"
        f"  reason:    {engine} has no public prediction-delete endpoint.\n"
        "\n"
        "  Use one of these instead:\n"
        "    engine: replicate     (DELETE /v1/predictions/{id})\n"
        "    engine: runway        (DELETE /v1/tasks/{id})\n"
        "    engine: comfyui       (any pod-based provider: runpod, skypilot, modal, local)\n"
        "    engine: diffusers     (any pod-based provider: runpod, skypilot, modal, local)\n"
        "\n"
        "  Or drop --ephemeral to allow provider-side record retention."
    )


def _preflight_ephemeral(ctx: SessionContext) -> str | None:
    """Look up ``(engine, provider)`` in ``EPHEMERAL_CAPABILITIES``.

    Returns ``None`` when the combination is supported (or no config is
    bound to the session) and an error-block string when refused.
    """
    cfg = ctx.cfg
    if cfg is None:
        return None
    engine_kind = cfg.engine.kind if cfg.engine else ""
    provider = cfg.compute.provider if cfg.compute else None
    # Provider-less hosted engines (replicate, runway) carry None in the
    # capability table.
    key = (engine_kind, provider) if cfg.compute else (engine_kind, None)
    supported = EPHEMERAL_CAPABILITIES.get(key)
    if not supported:
        return _preflight_error_block(engine_kind, provider)
    return None


def _install_sigint_handler(token: CancelToken) -> None:
    """Install a two-press SIGINT handler that flips *token* on first press.

    Behavior:

    1. **First press:** logs a WARN line and sets ``token``. The
       orchestrator + every backend poll loop observe ``token.is_set()``
       and unwind cooperatively (Phase 50 cancel cascade). No
       ``KeyboardInterrupt`` is raised, so a graceful drain has time to
       complete.
    2. **Second press:** restores ``signal.SIG_DFL`` and re-raises
       ``KeyboardInterrupt`` so the operator can always force-exit, even
       if a backend is wedged in non-interruptible I/O.

    Idempotent at the level of the second press — once we restore
    ``SIG_DFL``, any subsequent ``SIGINT`` runs the default handler
    (process termination) and never re-enters this closure.

    Args:
        token: The shared :class:`CancelToken` from
            :attr:`SessionContext.cancel_token`. Backends that received
            this same token via the orchestrator's plumbing observe the
            flip on their next poll tick.
    """

    def _handler(signum: int, frame: object) -> None:  # noqa: ARG001
        if token.is_set():
            # Second press — restore default first so KeyboardInterrupt
            # raised below cannot recurse into this handler.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise KeyboardInterrupt
        _log.warning(
            "interrupt received; finishing in-flight work + draining pool. "
            "Press Ctrl-C again to force-exit."
        )
        token.set()

    signal.signal(signal.SIGINT, _handler)


def _nonnegative_float(value: str) -> float:
    """Argparse type converter — accept any non-negative float, else exit rc=1.

    Args:
        value: Raw string from the command line.

    Returns:
        Parsed float value.

    Raises:
        argparse.ArgumentTypeError: When ``value`` is not a parseable
            non-negative float (argparse converts this to ``SystemExit(2)``
            with an "invalid value" message).
    """
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a float") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be >= 0")
    return parsed


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
    parser.add_argument(
        "--vault",
        default=None,
        metavar="PATH",
        help=(
            "path to a vault YAML file (outside the repo) holding the "
            "positive prompt and optional LoRA references. Or set "
            "KINOFORGE_VAULT."
        ),
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help=(
            "ephemeral run: skip local writes, delete provider records "
            "on completion, in-memory run id."
        ),
    )
    parser.add_argument(
        "--debug-show-secrets",
        action="store_true",
        help=(
            "bypass logging redaction (forbidden under --ephemeral; for "
            "local debugging only)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kinoforge {__version__}",
        help="print kinoforge version and exit",
    )

    sub = parser.add_subparsers(dest="cmd", metavar="SUBCOMMAND")

    # deploy
    p_deploy = sub.add_parser("deploy", help="provision compute and deploy")
    p_deploy.add_argument("--config", required=True, metavar="PATH")
    p_deploy.add_argument("--dry-run", action="store_true")
    p_deploy.add_argument(
        "--stall-window-override",
        type=_nonnegative_float,
        default=None,
        metavar="SECONDS",
        help=(
            "C26: persist a per-entry stall_window_s override into the "
            "ledger entry (≥ 0). Useful for known-slow-boot workloads."
        ),
    )
    p_deploy.add_argument(
        "--restart-loop-window-override",
        type=_nonnegative_float,
        default=None,
        metavar="SECONDS",
        help=(
            "C27: persist a per-entry restart_loop_window_s override "
            "into the ledger entry (≥ 0). Useful for workloads with a "
            "known-long first boot."
        ),
    )
    p_deploy.add_argument(
        "--diagnostic-mode",
        action="store_true",
        help=(
            "C28: enable in-pod EXIT trap + S3 boot-log capture and request "
            "restart_policy=never so a failed boot leaves the snapshot intact "
            "(skipped silently if RunPod does not expose restartPolicy)."
        ),
    )

    # provision
    p_provision = sub.add_parser("provision", help="provision an existing instance")
    p_provision.add_argument("-c", "--config", required=True, metavar="PATH")

    # doctor — run the full cfg validation Check Registry against a cfg
    # and print a per-check report. Exit code = number of ERRORs. Bypasses
    # SessionContext's auto-load so the report can include load-time
    # validation errors instead of aborting at parse time (see main()).
    p_doctor = sub.add_parser(
        "doctor",
        help="run the cfg validation registry against a cfg and print a report",
    )
    p_doctor.add_argument("-c", "--config", required=True, metavar="PATH")

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
    p_generate.add_argument(
        "--instance-id",
        default=None,
        metavar="ID",
        help=(
            "reuse an existing pod from the local ledger instead of cold-"
            "creating (skip ComfyUI + Wan spin-up). Use `kinoforge list` to "
            "find candidate ids."
        ),
    )
    p_generate.add_argument(
        "--force-attach",
        action="store_true",
        help=(
            "override classify verdicts HEARTBEAT_UNKNOWN, IDLE_REAP, "
            "ORPHAN_REAP for the supplied --instance-id. Has no effect "
            "without --instance-id. Never bypasses STALE_LEDGER, "
            "OVERAGE_REAP, UNROUTABLE, or capability_key mismatch."
        ),
    )
    p_generate.add_argument(
        "--no-reuse",
        action="store_true",
        dest="no_reuse",
        help=(
            "force cold create_instance (skip warm-reuse auto-discovery) AND "
            "destroy the pod immediately when generation finishes. Use for "
            "one-shot jobs, benchmarking cold-boot, or forcing a fresh pod "
            "after suspected engine-state drift. Mutex with --force-attach. "
            "Composes with --instance-id (attach to that pod, then destroy at end)."
        ),
    )
    p_generate.add_argument(
        "--skip-preflight",
        action="store_true",
        dest="skip_preflight",
        help=(
            "skip the cfg validation pre-flight (NETWORK + PREFLIGHT "
            "categories). STATIC validation always runs via load_config. "
            "Use only when you have already run `kinoforge doctor` and "
            "confirmed cleanliness, or when running offline."
        ),
    )
    p_generate.add_argument(
        "--dry-run-swap",
        action="store_true",
        dest="dry_run_swap",
        help=(
            "preview the warm-attach matcher decision without acquiring "
            "the pod lock, issuing HTTP, or running validate_for_generate. "
            "Prints the chosen pod + swap plan (evict/download) or the "
            "cold-boot fall-through reason. Exits 0."
        ),
    )
    p_generate.add_argument(
        "--loras",
        action=_LorasOnceAction,
        default=None,
        metavar="HEREDOC",
        help=(
            "override cfg.loras AND vault.loras with a CLI-supplied LoRA "
            "stack. Heredoc body: one LoRA per line, whitespace-separated "
            "columns `ref [strength] [branch]`. `#` line comments + blank "
            "lines ignored. Numeric shorthand `<modelId>:<versionId>` "
            "expands to `civitai:<modelId>@<versionId>`; other refs "
            "(civitai:..., civarchive:..., hf:..., file:..., https://...) "
            "pass through verbatim; unknown schemes rejected. Strength "
            "defaults to 1.0; branch defaults to `auto`. Empty heredoc "
            "clears the stack for this run. Vault.loras bypass logged to "
            "stderr. URLs from civitai.com, civarchive.com, huggingface.co "
            "are accepted and normalized to the canonical form; "
            "civitai/civarchive URLs MUST include `?modelVersionId=...` "
            "(canonical refs are version-pinned). civarchive refs are "
            "parse-accepted but their downstream resolver is pending."
        ),
    )
    p_generate.add_argument(
        "--attach-pod",
        type=str,
        default=None,
        metavar="POD_ID",
        help=(
            "attach to an existing warm pod from the ledger; skip provision; "
            "pod survives at end. Pod must be ledger-recorded AND match cfg's "
            "WarmAttachKey(base, engine, precision). Distinct from "
            "--instance-id, which uses full CapabilityKey (and would reject "
            "a different LoRA stack). Primarily for `kinoforge grid` "
            "swap-mode cell 2..N. Mutex with --no-reuse and "
            "--emit-provision-record."
        ),
    )
    p_generate.add_argument(
        "--emit-provision-record",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "on successful cold-boot provision, write a JSON record "
            "{pod_id, endpoint_url, provider, warm_attach_key, provision_ts} "
            "to PATH. Used by `kinoforge grid` swap-mode + operator scripting "
            "to hand a fresh pod off to a follow-up --attach-pod call. Not "
            "written on provision failure. Mutex with --attach-pod."
        ),
    )

    # upscale (T15) — standalone video upscale subcommand
    p_upscale = sub.add_parser("upscale", help="upscale a video clip")
    p_upscale.add_argument("-c", "--config", required=True, metavar="PATH")
    p_upscale.add_argument(
        "--video",
        required=True,
        metavar="PATH_OR_URL",
        help="source mp4 (file path or http(s)://... URL)",
    )
    p_upscale.add_argument(
        "--scale",
        default=None,
        metavar="TARGET",
        help="scale target (e.g. '2x', '4x'); overrides cfg.upscale.scale",
    )
    p_upscale.add_argument(
        "--no-reuse",
        action="store_true",
        dest="no_reuse",
        help="force cold create + destroy on completion. Mutex with --attach-pod.",
    )
    p_upscale.add_argument(
        "--attach-pod",
        type=str,
        default=None,
        metavar="POD_ID",
        help="attach to an existing pod; skip provision. Mutex with --no-reuse.",
    )
    p_upscale.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="emit the resolved plan to stdout and exit 0; no pod work",
    )

    # interpolate
    p_interp = sub.add_parser("interpolate", help="raise a video's frame rate (RIFE)")
    p_interp.add_argument("-c", "--config", required=True, metavar="PATH")
    p_interp.add_argument(
        "--video",
        required=True,
        metavar="PATH_OR_URL",
        help="source mp4 (file path or http(s)://... URL)",
    )
    p_interp.add_argument(
        "--fps",
        type=float,
        default=None,
        metavar="FPS",
        help="target output fps (overrides cfg.interpolate.fps)",
    )
    p_interp.add_argument(
        "--no-reuse",
        action="store_true",
        dest="no_reuse",
        help="force cold create + destroy on completion. Mutex with --attach-pod.",
    )
    p_interp.add_argument(
        "--attach-pod",
        type=str,
        default=None,
        metavar="POD_ID",
        help="attach to an existing pod; skip provision. Mutex with --no-reuse.",
    )
    p_interp.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="emit the resolved plan to stdout and exit 0; no pod work",
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

    # logs — fetch a file served by the pod's port-8001 sidecar
    p_logs = sub.add_parser(
        "logs", help="fetch a file from a running pod's sidecar http.server"
    )
    p_logs.add_argument("--id", required=True, metavar="ID")
    p_logs.add_argument(
        "--file",
        default="bootstrap.log",
        metavar="NAME",
        help="filename under /tmp on the pod (default: bootstrap.log)",
    )
    p_logs.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="write bytes to this local path (default: stdout)",
    )

    # forget
    p_forget = sub.add_parser(
        "forget", help="remove an instance entry from the local ledger"
    )
    p_forget.add_argument("--id", required=True, metavar="ID")

    # reap (Layer V — heartbeat-aware sweeper)
    p_reap = sub.add_parser(
        "reap", help="classify ledger; optionally destroy stale instances"
    )
    p_reap.add_argument(
        "--apply",
        action="store_true",
        help="actually destroy / forget (default: dry-run)",
    )
    p_reap.add_argument(
        "--include-orphans",
        action="store_true",
        help="extend --apply to ORPHAN_REAP entries",
    )
    p_reap.add_argument(
        "--force-forget",
        action="store_true",
        help="also forget ledger entries whose provider can no longer be reached (implies --apply)",
    )
    p_reap.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on UNROUTABLE / HEARTBEAT_UNKNOWN",
    )
    p_reap.add_argument(
        "--id", default=None, metavar="ID", help="restrict sweep to one ledger entry"
    )
    p_reap.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="output format (default: human)",
    )
    p_reap.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        metavar="PATH",
        help="cfg for thresholds; defaults to Lifecycle() defaults",
    )

    # gc
    p_gc = sub.add_parser("gc", help="garbage-collect stored artifacts")
    p_gc.add_argument("--config", required=True, metavar="PATH")
    p_gc.add_argument("--run", default=None, metavar="RUN_ID")
    p_gc.add_argument("--older-than", default=None, metavar="DUR")

    p_cost = sub.add_parser(
        "cost",
        help="show cost dashboard: burn rate + per-provider breakdown + balance",
    )
    p_cost.add_argument("-c", "--config", required=True, metavar="PATH")
    cost_mode = p_cost.add_mutually_exclusive_group()
    cost_mode.add_argument(
        "--json",
        action="store_true",
        help="emit stable JSON schema for piping (Grafana, jq, etc.)",
    )
    cost_mode.add_argument(
        "--prom",
        action="store_true",
        help="emit Prometheus text exposition format",
    )
    p_cost.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass disk cache for balance reads (force fresh)",
    )
    p_cost.add_argument(
        "--cache-ttl",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="balance cache TTL (default 15s)",
    )

    # pod (LoRA-flexible warm-reuse — direct pod-side queries)
    p_pod = sub.add_parser("pod", help="direct pod-side queries (no orchestration)")
    pod_sub = p_pod.add_subparsers(dest="pod_cmd", metavar="SUBCOMMAND")
    p_pod_lora = pod_sub.add_parser("lora", help="LoRA inventory queries")
    pod_lora_sub = p_pod_lora.add_subparsers(dest="pod_lora_cmd", metavar="SUBCOMMAND")
    p_pod_lora_ls = pod_lora_sub.add_parser(
        "ls", help="list the pod's resident LoRAs (GET /lora/inventory)"
    )
    p_pod_lora_ls.add_argument("pod_id", metavar="POD_ID")

    # sweeper (Layer W)
    p_sweeper = sub.add_parser("sweeper", help="Layer W: long-running reap daemon")
    sw_sub = p_sweeper.add_subparsers(dest="sweeper_cmd", metavar="SUBCOMMAND")

    p_sweeper_start = sw_sub.add_parser(
        "start", help="run the sweeper daemon in the foreground"
    )
    p_sweeper_start.add_argument("-c", "--config", required=True, metavar="PATH")
    p_sweeper_start.add_argument(
        "--interval-s",
        type=float,
        default=None,
        metavar="N",
        help="override cfg.sweeper.interval_s for this run",
    )

    p_sweeper_stop = sw_sub.add_parser(
        "stop", help="SIGTERM the daemon owning sweeper:<host>"
    )
    p_sweeper_stop.add_argument("-c", "--config", required=True, metavar="PATH")

    p_sweeper_status = sw_sub.add_parser("status", help="read sweeper liveness")
    p_sweeper_status.add_argument("-c", "--config", required=True, metavar="PATH")
    p_sweeper_status.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )

    p_sweeper_metrics = sw_sub.add_parser(
        "metrics", help="Prometheus textfile-collector target"
    )
    p_sweeper_metrics.add_argument("-c", "--config", required=True, metavar="PATH")
    p_sweeper_metrics.add_argument(
        "--prom",
        action="store_true",
        required=True,
        help="emit Prom text exposition",
    )

    # batch
    p_batch = sub.add_parser("batch", help="run a batch of generation jobs")
    p_batch.add_argument("-c", "--config", required=True, metavar="PATH")
    p_batch.add_argument("--manifest", required=True, metavar="PATH")
    p_batch.add_argument("--batch-id", default=None, metavar="ID")
    p_batch.add_argument("--concurrent", type=int, default=None, metavar="N")
    p_batch.add_argument("--env-file", default=None, metavar="PATH")
    p_batch.add_argument(
        "--stream-format",
        choices=("human", "jsonl", "none"),
        default="human",
        help="streaming output format (default: human)",
    )
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
    p_batch.add_argument(
        "--instance-id",
        default=None,
        metavar="ID",
        help=(
            "reuse an existing pod across every manifest row instead of "
            "cold-creating. Use `kinoforge list` to find candidate ids."
        ),
    )
    p_batch.add_argument(
        "--force-attach",
        action="store_true",
        help=(
            "override classify verdicts HEARTBEAT_UNKNOWN, IDLE_REAP, "
            "ORPHAN_REAP for the supplied --instance-id."
        ),
    )
    p_batch.add_argument(
        "--no-reuse",
        action="store_true",
        dest="no_reuse",
        help=(
            "force cold create_instance + destroy after the whole batch "
            "completes. Mutex with --force-attach."
        ),
    )
    p_batch.add_argument(
        "--dry-run-swap",
        action="store_true",
        dest="dry_run_swap",
        help=(
            "preview the warm-attach matcher decision without acquiring "
            "the pod lock, issuing HTTP, or loading the manifest. Prints "
            "the chosen pod + swap plan (evict/download) or the cold-boot "
            "fall-through reason. Exits 0."
        ),
    )

    # grid
    p_grid = sub.add_parser(
        "grid", help="compose N generations into a side-by-side grid mp4"
    )
    p_grid.add_argument(
        "--spec",
        required=True,
        metavar="PATH",
        help="grid spec yaml (outside repo)",
    )
    p_grid.add_argument(
        "--out", default=None, metavar="PATH", help="composed grid mp4 destination"
    )
    p_grid.add_argument(
        "--max-parallel-groups",
        type=int,
        default=2,
        dest="max_parallel_groups",
        help="concurrent groups (default 2)",
    )
    p_grid.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="resolve + plan, no compute",
    )
    p_grid.add_argument(
        "--ephemeral",
        action="store_true",
        help="pass-through to each underlying generate",
    )

    return parser


#: Fallback suspect-age threshold when a ledger row lacks ``max_age_s``
#: (legacy rows). A row older than this is probed against the provider before
#: its est_spend is printed. Probing a still-live long-lived pod merely
#: confirms it alive (get_instance succeeds → kept), so this is a
#: performance knob, never a correctness one.
_OVERVIEW_STALE_AFTER_S: float = 6 * 3600.0


def _overview_get_provider(name: str) -> Callable[[], Any]:
    """Resolve a provider factory by name (test seam; patched in unit tests).

    Ensures the runpod provider is registered before resolving, so the
    top-of-command overview reconcile works even when no subcommand has
    imported the provider yet.
    """
    import kinoforge.providers.runpod  # noqa: F401 — self-registers
    from kinoforge.core import registry

    return registry.get_provider(name)


def _print_instance_overview(
    ctx: SessionContext, *, file: TextIO | None = None
) -> None:
    """Print one-line overview per ledger entry; degrade gracefully on failure.

    Args:
        ctx: Per-invocation session context.
        file: Output stream. ``None`` resolves to ``sys.stdout`` at call
            time (capsys-safe).  Callers running in machine-readable
            streaming modes (e.g. ``--stream-format=jsonl``) pass
            ``sys.stderr`` so stdout stays pure JSONL for piping.
    """
    out = file if file is not None else sys.stdout
    ledger, warn = ctx.ledger_safe()
    if ledger is None:
        print(f"[instance overview] unavailable: {warn}", file=out)
        return
    try:
        entries = ledger.entries()
    except Exception as exc:  # noqa: BLE001 — best-effort surface
        print(f"[instance overview] unavailable: {type(exc).__name__}: {exc}", file=out)
        return
    now = time.time()
    # Read-side self-heal: reconcile only SUSPECT rows (older than their own
    # max_age_s reap deadline, or a default for legacy rows). Young/live rows
    # are never probed, so the warm-reuse hot path stays zero-network. Any
    # failure degrades gracefully — the overview must never raise.
    suspect = [
        e
        for e in entries
        if now - float(e.get("created_at", now))
        > float(e.get("max_age_s", _OVERVIEW_STALE_AFTER_S))
    ]
    if suspect:
        try:
            gone = _reconcile_dead_ledger_entries(
                ledger, suspect, get_provider=_overview_get_provider
            )
            if gone:
                gone_set = set(gone)
                entries = [e for e in entries if str(e.get("id") or "") not in gone_set]
        except Exception as exc:  # noqa: BLE001 — best-effort, never fatal
            print(
                f"[instance overview] reconcile skipped: {type(exc).__name__}: {exc}",
                file=out,
            )
    if not entries:
        print("[instance overview] No running instances.", file=out)
        return
    print("[instance overview]", file=out)
    for entry in entries:
        iid = entry.get("id", "?")
        created_at = float(entry.get("created_at", now))
        age_s = now - created_at
        age_h = age_s / 3600.0
        rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        spend = age_h * rate
        max_age_s = float(entry.get("max_age_s", _OVERVIEW_STALE_AFTER_S))
        # A suspect row still present here survived reconcile — the provider
        # could not confirm it gone (unreachable/uncertain), so its est_spend
        # may be entirely fictional. Flag it rather than present it as fact.
        row_suspect = age_s > max_age_s
        marker = "  ⚠ unverified — run 'kinoforge list'" if row_suspect else ""
        print(
            f"  {iid}  age={age_h:.1f}h  "
            f"est≤${spend:.4f} (age×rate; $0 if pod already dead)"
            f"{marker}",
            file=out,
        )


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

    # Confidentiality flags — validate exclusion + load vault before any work.
    if args.ephemeral and args.debug_show_secrets:
        print(
            "error: --ephemeral and --debug-show-secrets are mutually "
            "exclusive (the debug flag bypasses log redaction, which "
            "ephemeral requires).",
            file=sys.stderr,
        )
        return 2
    try:
        _loaded_vault = _load_vault_or_none(args.vault)
    except VaultError as exc:
        print(f"error: vault: {exc}", file=sys.stderr)
        return 2
    _install_redacting_filter(bypass=args.debug_show_secrets)

    # Load .env secrets before dispatch; shell-set values always win.
    env_file = Path(args.env_file) if args.env_file is not None else None
    load_env_file(env_file)

    # doctor reads + validates its own cfg via _parse_cfg_raw (bypassing
    # load_config's STATIC check pass) so the report can surface errors
    # instead of aborting at parse time.
    if args.cmd == "doctor":
        cfg_path = None
    else:
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

    # In JSONL streaming mode, route the instance-overview header to stderr
    # so stdout stays pure JSONL for piping (`kinoforge batch ... | jq .`).
    _print_instance_overview(
        ctx,
        file=sys.stderr if getattr(args, "stream_format", None) == "jsonl" else None,
    )

    if args.cmd is None:
        parser.print_help()
        return 0

    # Pre-flight: refuse --ephemeral on engine/provider combos that cannot
    # honour delete-on-completion. Read-only subcommands emit a stderr
    # note and skip the gate (they never trigger orchestration).
    if args.ephemeral:
        if args.cmd in _READ_ONLY_CMDS:
            print(
                "note: --ephemeral has no effect on read-only subcommands",
                file=sys.stderr,
            )
        else:
            err_block = _preflight_ephemeral(ctx)
            if err_block is not None:
                print(err_block, file=sys.stderr)
                return 2

    # Phase 50 — install the graceful-interrupt SIGINT handler ONLY for
    # the long-running orchestration subcommands. Read-only operations
    # (list / status / stop / destroy / forget / reap / gc) keep the
    # default Ctrl-C semantics so the operator doesn't have to press
    # twice to escape a hung ledger read.
    if args.cmd in _INTERRUPTIBLE_CMDS:
        _install_sigint_handler(ctx.cancel_token)

    with EphemeralSession(enabled=args.ephemeral, vault=_loaded_vault):
        return _DISPATCH[args.cmd](args, ctx)


__all__ = [
    "_DISPATCH",
    "_build_parser",
    "_build_sink",
    "_build_store",
    "_cli_clock",
    "_install_sigint_handler",
    "_print_instance_overview",
    "main",
]
