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
from pathlib import Path

import kinoforge._adapters  # noqa: F401 — triggers all self-registrations
from kinoforge.core.config import Config
from kinoforge.core.dotenv_loader import load_env_file
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import GenerationRequest
from kinoforge.core.lifecycle import Ledger, destroy_confirmed, reap
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

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
    p_provision.add_argument("--config", required=True, metavar="PATH")

    # generate
    p_generate = sub.add_parser("generate", help="run a generation job")
    p_generate.add_argument("--config", required=True, metavar="PATH")
    p_generate.add_argument("--prompt", required=True, metavar="TEXT")
    p_generate.add_argument("--mode", required=True, metavar="MODE")
    p_generate.add_argument("--run-id", default="run", metavar="ID")

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
            ledger.record(result.instance)

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
    from kinoforge.core.orchestrator import generate

    cfg = load_config(Path(args.config))
    store = _build_store(cfg, state_dir)
    request = GenerationRequest(prompt=args.prompt, mode=args.mode)
    run_id: str = args.run_id

    try:
        artifact = generate(cfg, request, store=store, run_id=run_id)
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    print(f"generated: uri={artifact.uri!r}")
    return 0


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

    parser.print_help()
    return 0
