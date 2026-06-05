"""Argparse-based CLI for kinoforge.

All concrete adapters are registered via the single import of
``kinoforge._adapters`` at the top of this module.  Every other module in
kinoforge MUST NOT import concrete adapters.

Entry points
------------
* ``kinoforge deploy --config <path> [--dry-run]``
* ``kinoforge provision --config <path>``
* ``kinoforge generate --config <path> --prompt <str> --mode <str> [--run-id <id>]``
* ``kinoforge list``
* ``kinoforge status --id <id>``
* ``kinoforge stop --id <id>``
* ``kinoforge destroy --id <id>``
* ``kinoforge reap``
* ``kinoforge gc --config <path> [--run <id>] [--older-than <dur>]``

Every invocation prints an "instance overview" header (id, age, est. spend)
before running the subcommand.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import kinoforge._adapters  # noqa: F401 — triggers all self-registrations
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.config import Config
from kinoforge.core.dotenv_loader import load_env_file
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import GenerationRequest
from kinoforge.core.lifecycle import Ledger, destroy_confirmed, reap
from kinoforge.core.orchestrator import generate
from kinoforge.outputs.base import OutputSink
from kinoforge.outputs.local import LocalOutputSink
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

# CLI clock seam — overridable in tests via monkeypatch.  Used to derive
# the default --run-id and any other invocation-time stamps.
_cli_clock: Clock = RealClock()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ledger(state_dir: Path) -> Ledger:
    """Return a Ledger backed by ``state_dir``.

    Args:
        state_dir: Root directory for the local artifact store.

    Returns:
        A :class:`~kinoforge.core.lifecycle.Ledger` instance.
    """
    store = LocalArtifactStore(state_dir)
    return Ledger(store=store, run_id="_lifecycle")


def _build_store(cfg: Config, state_dir: Path) -> ArtifactStore:
    """Construct the artifact store for this run.

    Honours ``cfg.store.kind``; falls back to ``LocalArtifactStore(state_dir)``
    when ``cfg.store`` is at its defaults (``kind='local'``, ``root=None``) —
    i.e. when no ``store:`` block is present in the YAML config.

    Args:
        cfg: Loaded kinoforge ``Config``.
        state_dir: Path to the operator state directory (``--state-dir`` arg).

    Returns:
        A fresh ``ArtifactStore`` instance.

    Raises:
        UnknownAdapter: ``cfg.store.kind`` is not one of ``local | s3 | gcs``.
        ValueError: ``cfg.store.kind`` is ``"s3"`` or ``"gcs"`` and
            ``cfg.store.bucket`` is ``None``.
    """
    sc = cfg.store
    if sc.kind == "local":
        return LocalArtifactStore(sc.root or state_dir)
    if sc.kind == "s3":
        from kinoforge.stores.s3 import S3ArtifactStore  # noqa: PLC0415 — lazy

        if sc.bucket is None:  # validated by StoreConfig._check_kind_requirements
            raise ValueError("store.kind='s3' requires store.bucket")
        return S3ArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    if sc.kind == "gcs":
        from kinoforge.stores.gcs import GCSArtifactStore  # noqa: PLC0415 — lazy

        if sc.bucket is None:  # validated by StoreConfig._check_kind_requirements
            raise ValueError("store.kind='gcs' requires store.bucket")
        return GCSArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    raise UnknownAdapter(f"unknown store kind: {sc.kind!r}")


def _build_sink(cfg: Config, args: argparse.Namespace) -> OutputSink | None:
    """Return the configured OutputSink, or None when publishing is disabled.

    Precedence:
      1. ``--no-output-dir`` flag → ``None``.
      2. ``--output-dir PATH`` flag → ``LocalOutputSink(PATH)``.
      3. ``cfg.output.enabled is False`` → ``None``.
      4. Else → ``LocalOutputSink(cfg.output.dir)``.

    Args:
        cfg: Loaded kinoforge configuration.
        args: Parsed CLI arguments.

    Returns:
        A ``LocalOutputSink`` rooted at the resolved directory, or
        ``None`` when the operator opted out.
    """
    if getattr(args, "no_output_dir", False):
        return None
    explicit = getattr(args, "output_dir", None)
    if explicit is not None:
        return LocalOutputSink(dir=Path(explicit), clock=_cli_clock)
    if not cfg.output.enabled:
        return None
    return LocalOutputSink(dir=cfg.output.dir, clock=_cli_clock)


def _print_instance_overview(state_dir: Path) -> None:
    """Print a one-line overview of every ledger entry to stdout.

    Prints "No running instances." when the ledger is empty.

    Args:
        state_dir: Root directory used for the state store.
    """
    ledger = _ledger(state_dir)
    entries = ledger.entries()
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

    # stop
    p_stop = sub.add_parser("stop", help="stop an instance")
    p_stop.add_argument("--id", required=True, metavar="ID")

    # destroy
    p_destroy = sub.add_parser("destroy", help="destroy an instance")
    p_destroy.add_argument("--id", required=True, metavar="ID")

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


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_deploy(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``deploy`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    from kinoforge.core.config import load_config
    from kinoforge.core.orchestrator import deploy

    cfg = load_config(Path(args.config))

    if not args.dry_run:
        # Check for duplicate instance in ledger
        ledger = _ledger(state_dir)
        key_hash = cfg.capability_key().derive()[:12]
        for entry in ledger.entries():
            tags = entry.get("tags", {})
            if tags.get("kinoforge_key") == key_hash:
                print(
                    f"duplicate instance refused; use `kinoforge destroy --id {entry['id']}` first",
                    file=sys.stderr,
                )
                return 1

    try:
        result = deploy(cfg, dry_run=args.dry_run)
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(result.plan_text)
    else:
        print(f"deployed: instance={result.instance and result.instance.id!r}")
        # Record to ledger if an instance was created
        if result.instance is not None:
            ledger = _ledger(state_dir)
            lc = cfg.lifecycle()
            # Layer S: snapshot lifecycle policy onto the ledger entry so
            # `kinoforge status` can surface it without re-loading the YAML.
            # The persisted key `max_age_s` mirrors the spec naming; the
            # source attribute on the Lifecycle dataclass is `max_lifetime_s`.
            ledger.record(
                result.instance,
                idle_timeout_s=int(lc.idle_timeout_s),
                max_age_s=int(lc.max_lifetime_s),
            )

    return 0


def _cmd_provision(args: argparse.Namespace, state_dir: Path) -> int:  # noqa: ARG001
    """Handle ``provision`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory (unused directly here).

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    from kinoforge.core.config import load_config

    cfg = load_config(Path(args.config))
    # Resolve provider and engine, then call provisioner
    try:
        from kinoforge.core import registry

        engine = registry.get_engine(cfg.engine.kind)()
        provider = None
        if cfg.compute is not None:
            provider = registry.get_provider(cfg.compute.provider)()
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    instance = None
    if provider is not None:
        hw_reqs = cfg.hardware_requirements()
        offers = provider.find_offers(hw_reqs)
        if not offers:
            print("error: no compute offers available", file=sys.stderr)
            return 1
        from kinoforge.core.interfaces import InstanceSpec

        spec = InstanceSpec(
            image=cfg.compute.image if cfg.compute else "",
            offer=offers[0],
            lifecycle=cfg.lifecycle(),
        )
        instance = provider.create_instance(spec)
        while instance.status != "ready":
            instance = provider.get_instance(instance.id)

    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.provisioner import provision

    provision(
        engine=engine,
        cfg=cfg,  # type: ignore[arg-type]  # Config satisfies _ProvisionConfig structurally
        instance=instance,
        creds=EnvCredentialProvider(),
        download_dir=state_dir / "weights",
    )
    print(f"provisioned: instance={instance and instance.id!r}")
    return 0


def _cmd_generate(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``generate`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    from kinoforge.core.config import load_config

    cfg = load_config(Path(args.config))
    store = _build_store(cfg, state_dir)
    sink = _build_sink(cfg, args)
    request = GenerationRequest(prompt=args.prompt, mode=args.mode)

    if args.run_id is not None:
        run_id: str = args.run_id
    else:
        ts = datetime.fromtimestamp(_cli_clock.now()).strftime("%Y%m%d-%H%M%S")
        run_id = f"run-{ts}"

    try:
        artifact, _ = generate(
            cfg, request, store=store, sink=sink, run_id=run_id, state_dir=state_dir
        )
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    print(f"generated: uri={artifact.uri!r}")
    return 0


def _cmd_batch(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``batch`` subcommand.

    Args:
        args: Parsed CLI arguments. Required: ``config``, ``manifest``.
            Optional: ``batch_id``, ``concurrent``, ``env_file``.
        state_dir: Path to the operator state directory.

    Returns:
        Exit code:
          * ``0`` — every entry succeeded.
          * ``1`` — one+ per-entry failure, setup-fatal exception
            (any other ``KinoforgeError`` from ``deploy_session.__enter__``
            such as ``CapacityError`` / ``AuthError`` / ``UnknownAdapter``),
            batch-id collision, or invalid ``--concurrent`` flag.
          * ``2`` — batch-fatal exception mid-run
            (``BudgetExceeded`` / ``CapabilityMismatch`` / ``TeardownError``).
    """
    from datetime import datetime

    from pydantic import ValidationError as PydanticValidationError

    from kinoforge.core.batch import batch_generate, load_manifest
    from kinoforge.core.config import load_config
    from kinoforge.core.errors import (
        BudgetExceeded,
        CapabilityMismatch,
        ConfigError,
        KinoforgeError,
        TeardownError,
    )

    if args.env_file is not None:
        load_env_file(Path(args.env_file))

    # Early flag validation -- fail before touching compute.
    if args.concurrent is not None and args.concurrent < 1:
        print(
            f"error: --concurrent must be a positive integer (got {args.concurrent})",
            file=sys.stderr,
        )
        return 1

    try:
        cfg = load_config(Path(args.config))
    except (ConfigError, PydanticValidationError) as exc:
        print(f"error: config: {exc}", file=sys.stderr)
        return 1

    try:
        manifest = load_manifest(Path(args.manifest))
    except (ConfigError, PydanticValidationError) as exc:
        print(f"error: manifest: {exc}", file=sys.stderr)
        return 1

    store = _build_store(cfg, state_dir)
    sink = _build_sink(cfg, args)

    batch_id: str = (
        args.batch_id
        if args.batch_id is not None
        else datetime.now().strftime("batch-%Y%m%d-%H%M%S")
    )

    # Collision check via the existing store API; pre-compute on purpose.
    # ``LocalArtifactStore.list`` returns ``[]`` for an unknown ``batch_id``;
    # real S3/GCS adapter errors (auth, permission denied) must NOT be
    # swallowed — they propagate and are caught by the outer
    # ``KinoforgeError`` handler below.
    existing = store.list(batch_id)
    if existing:
        print(
            f"error: batch_id collision: {batch_id} already has artifacts "
            f"(pass --batch-id to override)",
            file=sys.stderr,
        )
        return 1

    print(
        f"[{batch_id}] manifest loaded: {len(manifest.entries)} entries, "
        f"concurrency={args.concurrent or cfg.lifecycle().max_in_flight}"
    )

    try:
        result = batch_generate(
            cfg,
            manifest,
            store=store,
            sink=sink,
            batch_id=batch_id,
            concurrent=args.concurrent,
            state_dir=state_dir,
        )
    except (BudgetExceeded, CapabilityMismatch, TeardownError) as exc:
        print(
            f"[{batch_id}] batch-fatal: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    except KinoforgeError as exc:
        # Setup-fatal: spec §7 "Setup fatal" row -- CapacityError, AuthError,
        # UnknownAdapter, hosted-preflight KinoforgeError, provider create
        # timeout. All originate inside deploy_session.__enter__ and would
        # otherwise escape as raw tracebacks, breaking the "every CLI failure
        # path produces a clean stderr line + non-zero exit" contract.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # Final per-entry summary table. Auto-size the run_id column to the
    # widest entry (plus one space) so realistic run_ids like
    # ``dawn-flight-attempt-3`` (21 chars) don't spill past a hard 20-char
    # cap and break alignment. Status column stays fixed-width (max label
    # = "interrupted" = 11 chars, so 12 is enough).
    print("\nsummary:")
    rid_width = max((len(o.run_id) for o in result.outcomes), default=1) + 1
    for o in result.outcomes:
        status_label = o.status.upper()
        duration = f"{o.duration_s:.1f}s" if o.duration_s is not None else "—"
        detail = o.uri if o.uri else (o.error or "")
        print(f"  {o.run_id:<{rid_width}s} {status_label:<12s} {duration:<8s} {detail}")
    print(f"batch-id: {batch_id}")
    n_ok = sum(1 for o in result.outcomes if o.status == "ok")
    n_fail = len(result.outcomes) - n_ok
    print(f"results:  {n_ok}/{len(result.outcomes)} ok, {n_fail} failed")
    return 0 if n_fail == 0 else 1


def _cmd_list(state_dir: Path) -> int:
    """Handle ``list`` subcommand — prints ledger entries.

    Args:
        state_dir: Path to the state directory.

    Returns:
        Exit code (always 0).
    """
    ledger = _ledger(state_dir)
    entries = ledger.entries()
    if not entries:
        print("No instances recorded in ledger.")
    else:
        for entry in entries:
            print(f"  {entry.get('id', '?')}  provider={entry.get('provider', '?')}")
    return 0


def _cmd_status(args: argparse.Namespace, state_dir: Path) -> int:  # noqa: ARG001
    """Handle ``status`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    try:
        # Try all registered providers to find the instance
        # For the local provider path:
        from kinoforge.providers.local import LocalProvider

        provider = LocalProvider()
        try:
            instance = provider.get_instance(args.id)
            print(
                f"id={instance.id}  status={instance.status}  provider={instance.provider}"
            )
            return 0
        except KeyError:
            pass

        print(f"instance {args.id!r} not found", file=sys.stderr)
        return 1
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1


def _cmd_stop(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``stop`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    ledger = _ledger(state_dir)
    entries = ledger.entries()
    entry = next((e for e in entries if e.get("id") == args.id), None)
    if entry is None:
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1

    provider_name = entry.get("provider", "local")
    try:
        from kinoforge.core import registry

        provider = registry.get_provider(str(provider_name))()
        provider.stop_instance(args.id)
        print(f"stopped: {args.id}")
        return 0
    except (UnknownAdapter, KeyError) as exc:
        print(f"error stopping {args.id!r}: {exc}", file=sys.stderr)
        return 1


def _cmd_destroy(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``destroy`` subcommand.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    ledger = _ledger(state_dir)
    entries = ledger.entries()
    entry = next((e for e in entries if e.get("id") == args.id), None)
    if entry is None:
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1

    provider_name = entry.get("provider", "local")
    try:
        from kinoforge.core import registry

        provider = registry.get_provider(str(provider_name))()
        destroy_confirmed(provider, args.id, sleep=lambda _: None)
        ledger.forget(args.id)
        print(f"destroyed: {args.id}")
        return 0
    except (UnknownAdapter, KeyError) as exc:
        print(f"error destroying {args.id!r}: {exc}", file=sys.stderr)
        return 1


def _cmd_reap(state_dir: Path) -> int:
    """Handle ``reap`` subcommand.

    Args:
        state_dir: Path to the state directory.

    Returns:
        Exit code (always 0).
    """
    from kinoforge.core.clock import RealClock
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.lifecycle import LifecycleManager
    from kinoforge.providers.local import LocalProvider

    ledger = _ledger(state_dir)
    clock = RealClock()
    lifecycle = Lifecycle()
    provider = LocalProvider(clock=clock)
    manager = LifecycleManager(
        provider=provider,
        clock=clock,
        lifecycle=lifecycle,
        run_id="_lifecycle",
    )

    destroyed = reap(provider=provider, lifecycle_manager=manager, ledger=ledger)
    if destroyed:
        print(f"reaped: {', '.join(destroyed)}")
    else:
        print("reap: no instances destroyed")
    return 0


def _cmd_gc(args: argparse.Namespace, state_dir: Path) -> int:
    """Handle ``gc`` subcommand — remove store entries matching criteria.

    Args:
        args: Parsed CLI arguments.
        state_dir: Path to the state directory.

    Returns:
        Exit code (always 0).
    """
    from kinoforge.core.config import load_config

    cfg = load_config(Path(args.config))
    store = _build_store(cfg, state_dir)
    run_id: str | None = args.run
    removed = 0

    if run_id is not None:
        items = store.list(run_id)
        for name in items:
            uri = store.uri_for(run_id, name)
            try:
                store.delete(uri)
                removed += 1
            except FileNotFoundError:
                pass
    else:
        print("gc: nothing to do (specify --run <id>)")
        return 0

    print(f"gc: removed {removed} artifact(s)")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse *argv* and dispatch to the appropriate subcommand.

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

    # Print instance overview on every invocation (before subcommand work).
    _print_instance_overview(state_dir)

    if args.cmd is None:
        parser.print_help()
        return 0

    if args.cmd == "deploy":
        return _cmd_deploy(args, state_dir)
    if args.cmd == "provision":
        return _cmd_provision(args, state_dir)
    if args.cmd == "generate":
        return _cmd_generate(args, state_dir)
    if args.cmd == "list":
        return _cmd_list(state_dir)
    if args.cmd == "status":
        return _cmd_status(args, state_dir)
    if args.cmd == "stop":
        return _cmd_stop(args, state_dir)
    if args.cmd == "destroy":
        return _cmd_destroy(args, state_dir)
    if args.cmd == "reap":
        return _cmd_reap(state_dir)
    if args.cmd == "gc":
        return _cmd_gc(args, state_dir)
    if args.cmd == "batch":
        return _cmd_batch(args, state_dir)

    parser.print_help()
    return 0
