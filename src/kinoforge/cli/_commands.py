"""Subcommand handlers + build helpers for the kinoforge CLI.

Every ``_cmd_*`` handler accepts ``(args, ctx)`` where *ctx* is a
:class:`~kinoforge.cli.context.SessionContext` that bundles
``state_dir``, loaded ``cfg``, and lazy ``store()`` / ``ledger()``
accessors.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import kinoforge._adapters  # noqa: F401 — triggers self-registrations
from kinoforge.cli.context import SessionContext
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.config import Config
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import GenerationRequest
from kinoforge.core.lifecycle import destroy_confirmed
from kinoforge.core.orchestrator import generate
from kinoforge.core.reaper_actor import sweep
from kinoforge.outputs.base import OutputSink
from kinoforge.outputs.local import LocalOutputSink
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.reaper_actor import SweepReport

# Module-level clock seam preserved for test monkeypatching.
_cli_clock: Clock = RealClock()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
    # Read _cli_clock through the kinoforge.cli namespace so that test
    # monkeypatches on ``kinoforge.cli._cli_clock`` are honoured.
    clock = getattr(sys.modules.get("kinoforge.cli"), "_cli_clock", _cli_clock)
    if getattr(args, "no_output_dir", False):
        return None
    explicit = getattr(args, "output_dir", None)
    if explicit is not None:
        return LocalOutputSink(dir=Path(explicit), clock=clock)
    if not cfg.output.enabled:
        return None
    return LocalOutputSink(dir=cfg.output.dir, clock=clock)


@runtime_checkable
class _LedgerProto(Protocol):
    """Structural protocol for the subset of Ledger used by _SingleIdLedgerView."""

    def entries(self) -> list[dict]:  # type: ignore[type-arg]
        """Return all ledger entries."""
        ...

    def forget(self, instance_id: str) -> None:
        """Remove a ledger entry by instance id."""
        ...

    def touch(self, instance_id: str) -> bool:
        """Update the last-heartbeat timestamp for an instance."""
        ...


class _SingleIdLedgerView:
    """Read+mutate proxy that surfaces only the entry matching one id.

    Acts as a thin filter over the underlying Ledger: ``entries()``
    returns at most one entry; mutating calls (``forget``, ``touch`` if
    used) pass through to the underlying ledger. Used by
    ``kinoforge reap --id X`` so the wrapping does not leave the real
    ledger object in a patched state.

    Args:
        base: The underlying ledger object.
        instance_id: The instance id to restrict entries to.
    """

    def __init__(self, base: _LedgerProto, instance_id: str) -> None:
        self._base = base
        self._id = instance_id

    def entries(self) -> list[dict]:  # type: ignore[type-arg]
        """Return only the entry matching the configured instance id."""
        return [e for e in self._base.entries() if e.get("id") == self._id]

    def forget(self, instance_id: str) -> None:
        """Delegate forget to the underlying ledger."""
        self._base.forget(instance_id)

    def touch(self, instance_id: str) -> bool:
        """Delegate touch to the underlying ledger (HeartbeatLoop compat)."""
        return self._base.touch(instance_id)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_deploy(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``deploy`` subcommand.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    from kinoforge.core.orchestrator import deploy

    if ctx.cfg is None:
        raise RuntimeError("_cmd_deploy requires --config")
    cfg = ctx.cfg

    if not args.dry_run:
        # Check for duplicate instance in ledger
        ledger = ctx.ledger()
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
            ledger = ctx.ledger()
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


def _cmd_provision(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``provision`` subcommand.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    if ctx.cfg is None:
        raise RuntimeError("_cmd_provision requires --config")
    cfg = ctx.cfg

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
        download_dir=ctx.state_dir / "weights",
    )
    print(f"provisioned: instance={instance and instance.id!r}")
    return 0


def _cmd_generate(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``generate`` subcommand.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    if ctx.cfg is None:
        raise RuntimeError("_cmd_generate requires --config")
    cfg = ctx.cfg
    store = ctx.store()
    sink = _build_sink(cfg, args)
    request = GenerationRequest(prompt=args.prompt, mode=args.mode)

    # Read _cli_clock and generate through the kinoforge.cli namespace so that
    # test monkeypatches on ``kinoforge.cli._cli_clock`` /
    # ``kinoforge.cli.generate`` are honoured.
    _cli_mod = sys.modules.get("kinoforge.cli")
    _clock = getattr(_cli_mod, "_cli_clock", _cli_clock)
    _generate = getattr(_cli_mod, "generate", generate)

    if args.run_id is not None:
        run_id: str = args.run_id
    else:
        ts = datetime.fromtimestamp(_clock.now()).strftime("%Y%m%d-%H%M%S")
        run_id = f"run-{ts}"

    try:
        artifact, _ = _generate(
            cfg,
            request,
            store=store,
            sink=sink,
            run_id=run_id,
            state_dir=ctx.state_dir,
            # Phase 50 — thread the per-invocation cancel token into the
            # orchestrator so a CLI SIGINT (set by _install_sigint_handler
            # in cli._main) propagates through every backend poll loop.
            cancel_token=ctx.cancel_token,
        )
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    print(f"generated: uri={artifact.uri!r}")
    return 0


def _cmd_batch(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``batch`` subcommand.

    Args:
        args: Parsed CLI arguments. Required: ``config``, ``manifest``.
            Optional: ``batch_id``, ``concurrent``, ``env_file``,
            ``stream_format`` (``human`` / ``jsonl`` / ``none``;
            default ``human``).  In ``jsonl`` mode, the manifest-loaded
            header is routed to stderr so stdout stays pure JSONL for
            piping (``kinoforge batch ... | jq .``).
        ctx: Per-invocation session context.

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

    from kinoforge.cli.batch_formatters import build_formatter
    from kinoforge.core.batch import batch_generate, load_manifest
    from kinoforge.core.errors import (
        BudgetExceeded,
        CapabilityMismatch,
        ConfigError,
        KinoforgeError,
        TeardownError,
    )

    if ctx.cfg is None:
        raise RuntimeError("_cmd_batch requires --config")
    cfg = ctx.cfg

    if args.env_file is not None:
        from kinoforge.core.dotenv_loader import load_env_file

        load_env_file(Path(args.env_file))

    # Early flag validation -- fail before touching compute.
    if args.concurrent is not None and args.concurrent < 1:
        print(
            f"error: --concurrent must be a positive integer (got {args.concurrent})",
            file=sys.stderr,
        )
        return 1

    try:
        manifest = load_manifest(Path(args.manifest))
    except (ConfigError, PydanticValidationError) as exc:
        print(f"error: manifest: {exc}", file=sys.stderr)
        return 1

    store = ctx.store()
    sink = _build_sink(cfg, args)

    batch_id: str = (
        args.batch_id
        if args.batch_id is not None
        else datetime.now().strftime("batch-%Y%m%d-%H%M%S")
    )

    formatter = build_formatter(args.stream_format)

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

    header = (
        f"[{batch_id}] manifest loaded: {len(manifest.entries)} entries, "
        f"concurrency={args.concurrent or cfg.lifecycle().max_in_flight}"
    )
    if args.stream_format == "jsonl":
        # Keep stdout pure JSONL; operator info goes to stderr.
        print(header, file=sys.stderr)
    else:
        print(header)

    try:
        result = batch_generate(
            cfg,
            manifest,
            store=store,
            sink=sink,
            batch_id=batch_id,
            concurrent=args.concurrent,
            state_dir=ctx.state_dir,
            on_event=formatter.emit,
            # Phase 50 — thread the per-invocation cancel token in so a
            # CLI SIGINT propagates through the inner deploy_session +
            # every per-entry GenerateClipStage.run() poll loop.
            cancel_token=ctx.cancel_token,
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

    formatter.render_summary(result)
    n_ok = sum(1 for o in result.outcomes if o.status == "ok")
    n_fail = len(result.outcomes) - n_ok
    return 0 if n_fail == 0 else 1


def _cmd_list(args: argparse.Namespace, ctx: SessionContext) -> int:  # noqa: ARG001
    """Handle ``list`` subcommand — prints ledger entries.

    Args:
        args: Parsed CLI arguments (unused).
        ctx: Per-invocation session context.

    Returns:
        Exit code (always 0).
    """
    ledger = ctx.ledger()
    entries = ledger.entries()
    if not entries:
        print("No instances recorded in ledger.")
    else:
        for entry in entries:
            print(f"  {entry.get('id', '?')}  provider={entry.get('provider', '?')}")
    return 0


# ---------------------------------------------------------------------------
# Layer S — `kinoforge status` helpers
# ---------------------------------------------------------------------------


# Map ledger key -> Lifecycle attribute. The ledger persists the generic
# name `max_age_s`; the Lifecycle dataclass attribute is `max_lifetime_s`.
# T1 commit acdc8e1 introduced the same mapping at the _cmd_deploy call
# site; this map mirrors it on read.
_CFG_LIFECYCLE_ATTR: dict[str, str] = {
    "idle_timeout_s": "idle_timeout_s",
    "max_age_s": "max_lifetime_s",
}


def _ledger_field_or_cfg(
    entry: dict,  # type: ignore[type-arg]
    key: str,
    cfg: Config | None,
) -> str:
    """Return entry-supplied value, else ``cfg.lifecycle()`` value, else sentinel.

    Args:
        entry: Ledger entry dict (may be a legacy entry missing newer keys).
        key: One of ``"idle_timeout_s"`` or ``"max_age_s"``.
        cfg: Optional Config for fallback when entry lacks the key.

    Returns:
        Stringified value, or ``"<not in ledger>"`` when neither source has it.
    """
    value = entry.get(key)
    if value is not None:
        return str(value)
    if cfg is not None:
        lc = cfg.lifecycle()
        return str(getattr(lc, _CFG_LIFECYCLE_ATTR[key]))
    return "<not in ledger>"


def _build_ledger_block(
    entry: dict,  # type: ignore[type-arg]
    *,
    cfg: Config | None,
    now: float,
) -> dict[str, str]:
    """Build the ledger-derived portion of ``kinoforge status`` output.

    Pure: no I/O, no clock reads. All time inputs flow through ``now``.

    Args:
        entry: A ledger entry dict (possibly legacy-shaped).
        cfg: Optional config used as fallback for lifecycle policy fields.
        now: Wall-clock seconds-since-epoch used for age / spend calculations.

    Returns:
        An ordered dict of ``{field: stringified_value}``. The ``last_heartbeat``
        key is included only when the entry has it.
    """
    out: dict[str, str] = {}
    out["id"] = str(entry.get("id", "?"))
    out["provider"] = str(entry.get("provider", "?"))
    created_at_raw = float(entry.get("created_at", now))
    age_h = max(0.0, (now - created_at_raw) / 3600.0)
    out["created_at"] = (
        datetime.fromtimestamp(created_at_raw)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    out["age_h"] = f"{age_h:.1f}"
    rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
    out["cost_rate_usd_per_hr"] = f"{rate:.4f}"
    out["accrued_spend_usd"] = f"{age_h * rate:.4f}"
    out["idle_timeout_s"] = _ledger_field_or_cfg(entry, "idle_timeout_s", cfg)
    out["max_age_s"] = _ledger_field_or_cfg(entry, "max_age_s", cfg)
    hb = entry.get("last_heartbeat")
    if hb is not None:
        out["last_heartbeat"] = (
            datetime.fromtimestamp(float(hb)).astimezone().isoformat(timespec="seconds")
        )
    return out


def _print_status_block(
    ledger_block: dict[str, str],
    provider_block: dict[str, str],
    *,
    advisory: str | None = None,
) -> None:
    """Print a merged + alphabetically sorted ``key=value`` block to stdout.

    Args:
        ledger_block: Output of :func:`_build_ledger_block`.
        provider_block: Provider-derived fields (``provider_status`` and
            optionally ``endpoints``).
        advisory: Optional advisory line; printed AFTER the sorted block when set.
    """
    merged = {**ledger_block, **provider_block}
    for key in sorted(merged):
        print(f"{key}={merged[key]}")
    if advisory is not None:
        print(advisory)


def _classify_for_status(
    entry: dict,  # type: ignore[type-arg]
    live_ids: set[str],
    cfg: Config | None,
    now: float,
) -> str:
    """Compute a verdict string for ``kinoforge status``.

    Uses the same Layer V ``classify`` call as ``kinoforge reap`` so a
    status-line ``verdict=...`` always agrees with what reap would
    decide for the same entry. When cfg is None, ``Lifecycle()``
    defaults are used.

    Args:
        entry: A ledger entry dict (possibly legacy-shaped).
        live_ids: Set of instance IDs currently known to the provider.
        cfg: Optional Config used for lifecycle policy fields.
        now: Wall-clock seconds-since-epoch.

    Returns:
        The string value of the :class:`~kinoforge.core.reaper.Verdict` enum.
    """
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper import classify

    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
    return classify(
        entry,
        live_ids,
        now,
        idle_timeout_s=lifecycle.idle_timeout_s,
        max_lifetime_s=lifecycle.max_lifetime_s,
        heartbeat_interval_s=lifecycle.heartbeat_interval_s,
        grace_after_session_s=lifecycle.grace_after_session_s,
    ).value


def _cmd_status(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``status`` subcommand: read ledger, dispatch to recorded provider.

    Args:
        args: Parsed CLI arguments. Uses ``args.id`` and optionally
            ``args.config`` (``--config``/``-c``) for legacy-entry fallback.
        ctx: Per-invocation session context.

    Returns:
        Exit code per the Layer S design contract:
            * 0 — provider success OR stale ledger (``KeyError``) OR
              endpoints-only failure.
            * 1 — ledger entry absent.
            * 2 — unknown provider in entry OR non-``KeyError`` exception
              from the provider lookup.
    """
    from kinoforge.core import registry

    ledger = ctx.ledger()
    entry = next((e for e in ledger.entries() if e.get("id") == args.id), None)
    if entry is None:
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1

    cfg = ctx.cfg
    now = time.time()
    ledger_block = _build_ledger_block(entry, cfg=cfg, now=now)

    # Layer U — sentinel-staleness advisory. When the ledger entry carries
    # both `last_heartbeat` and the writer's `heartbeat_thread_tick`
    # sentinel, surface an advisory if the sentinel is older than
    # 3 * heartbeat_interval_s. This is the user-visible side of the
    # forward-compat gate documented on Ledger.touch — it tells the
    # operator the loop has stopped writing (e.g. silent thread crash)
    # without claiming the pod is dead.
    heartbeat_advisory: str | None = None
    hb_tick = entry.get("heartbeat_thread_tick")
    hb = entry.get("last_heartbeat")
    if hb_tick is not None and hb is not None:
        interval = 30.0
        if cfg is not None and cfg.lifecycle().heartbeat_interval_s is not None:
            interval = float(cfg.lifecycle().heartbeat_interval_s or interval)
        age = now - float(hb_tick)
        if age > 3 * interval:
            heartbeat_advisory = (
                f"advisory: heartbeat thread stale ({age:.0f}s since last tick)"
            )

    provider_name = str(entry.get("provider", "local"))
    try:
        provider = registry.get_provider(provider_name)()
    except UnknownAdapter:
        provider_block = {
            "provider_status": f"unknown (unknown provider: {provider_name})",
            "verdict": "UNROUTABLE",
        }
        _print_status_block(ledger_block, provider_block, advisory=heartbeat_advisory)
        return 2

    try:
        instance = provider.get_instance(args.id)
    except KeyError:
        provider_block = {
            "provider_status": "unknown (stale ledger — provider has no record)",
            "verdict": "STALE_LEDGER",
        }
        # Stale-ledger advisory wins over heartbeat advisory: the entry
        # is gone from the provider, so heartbeat freshness is moot.
        _print_status_block(
            ledger_block,
            provider_block,
            advisory=(
                f"advisory: ledger entry is stale — "
                f"run 'kinoforge forget --id {args.id}'"
            ),
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — explicit transient-error surface
        provider_block = {
            "provider_status": (
                f"unknown (provider lookup failed: {exc.__class__.__name__})"
            ),
            "verdict": "HEARTBEAT_UNKNOWN",
        }
        _print_status_block(ledger_block, provider_block, advisory=heartbeat_advisory)
        return 2

    provider_block = {"provider_status": instance.status}
    try:
        provider_block["endpoints"] = json.dumps(provider.endpoints(args.id))
    except Exception as exc:  # noqa: BLE001
        provider_block["endpoints"] = f"unknown ({exc.__class__.__name__})"

    # Layer V — verdict line, same source of truth as `kinoforge reap`.
    # When list_instances raises, we cannot trust pod presence to
    # compute classify; surface HEARTBEAT_UNKNOWN rather than silently
    # bias toward LIVE.
    try:
        live_ids = {i.id for i in provider.list_instances()}
        provider_block["verdict"] = _classify_for_status(entry, live_ids, cfg, now)
    except Exception:  # noqa: BLE001 — honest "I can't tell" verdict
        provider_block["verdict"] = "HEARTBEAT_UNKNOWN"

    _print_status_block(ledger_block, provider_block, advisory=heartbeat_advisory)
    return 0


def _cmd_stop(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``stop`` subcommand.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    ledger = ctx.ledger()
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


def _cmd_destroy(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``destroy`` subcommand.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    ledger = ctx.ledger()
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


def _cmd_forget(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``forget`` subcommand: remove one ledger entry.

    Layer S recovery command — clears the stale entries that
    ``kinoforge status`` advises about (when the provider has no record
    of the id). Touches the local ledger only; does not contact the
    upstream provider. Non-idempotent by design (sibling parity with
    ``stop`` and ``destroy``): a second call on the same id, after the
    first removes it, returns exit 1.

    Args:
        args: Parsed CLI arguments (uses ``args.id``).
        ctx: Per-invocation session context.

    Returns:
        Exit code:
            * 0 — entry was present and has been removed.
            * 1 — no ledger entry matched ``args.id``.
    """
    ledger = ctx.ledger()
    if not any(e.get("id") == args.id for e in ledger.entries()):
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1
    ledger.forget(args.id)
    print(f"forgot: {args.id}")
    return 0


def _cmd_reap(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``reap`` subcommand (Layer V — heartbeat-aware).

    Dry-run by default. ``--apply`` activates DEFAULT_APPLY_POLICY
    (IDLE_REAP, OVERAGE_REAP, STALE_LEDGER). Opt-in flags
    ``--include-orphans`` and ``--force-forget`` add ORPHAN_REAP and
    UNROUTABLE respectively. ``--strict`` exits non-zero when uncertain
    verdicts are surfaced.

    Args:
        args: Parsed CLI arguments — apply, include_orphans, force_forget,
            strict, id, format, config (all optional).
        ctx: Per-invocation session context.

    Returns:
        Exit code per spec §3.7:
            * 0 — normal (dry-run or --apply with no failures)
            * 2 — at least one action="failed" under --apply
            * 3 — --strict tripped by UNROUTABLE / HEARTBEAT_UNKNOWN
            * 4 — invalid flag combo
    """
    from kinoforge.core import registry
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper import (
        DEFAULT_STRICT_VERDICTS,
        policy_from_cli_flags,
    )

    apply_flag = bool(getattr(args, "apply", False))
    include_orphans = bool(getattr(args, "include_orphans", False))
    force_forget = bool(getattr(args, "force_forget", False))
    strict = bool(getattr(args, "strict", False))
    single_id: str | None = getattr(args, "id", None)
    fmt: str = getattr(args, "format", "human") or "human"

    if include_orphans and not apply_flag:
        print(
            "error: --include-orphans requires --apply (Layer V opt-in safety)",
            file=sys.stderr,
        )
        return 4
    if force_forget and not apply_flag:
        print(
            "error: --force-forget requires --apply (Layer V opt-in safety)",
            file=sys.stderr,
        )
        return 4

    ledger: _LedgerProto = ctx.ledger()
    if single_id is not None:
        ledger = _SingleIdLedgerView(ledger, single_id)
    if not ledger.entries():
        print("reap: ledger empty (nothing to do)")
        return 0

    cfg = ctx.cfg
    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
    thresholds = {
        "idle_timeout_s": lifecycle.idle_timeout_s,
        "max_lifetime_s": lifecycle.max_lifetime_s,
        "heartbeat_interval_s": lifecycle.heartbeat_interval_s,
        "grace_after_session_s": lifecycle.grace_after_session_s,
    }

    policy = policy_from_cli_flags(
        apply=apply_flag,
        include_orphans=include_orphans,
        force_forget=force_forget,
    )

    store = ctx.store()
    _cli_mod = sys.modules.get("kinoforge.cli")
    clock = getattr(_cli_mod, "_cli_clock", _cli_clock)

    report = sweep(
        store=store,
        ledger=ledger,  # type: ignore[arg-type]  # _SingleIdLedgerView is structurally compatible
        registry_get_provider=registry.get_provider,
        thresholds=thresholds,
        clock=clock,
        policy=policy if apply_flag else None,
    )

    if fmt == "json":
        _emit_reap_jsonl(report)
    else:
        _emit_reap_human(report, apply_flag, include_orphans)

    # Exit code priority: failed actions > strict > 0
    if any(a.action == "failed" for a in report.actions):
        return 2
    if strict:
        verdicts = {v for _, v in report.snapshot.values()}
        if verdicts & DEFAULT_STRICT_VERDICTS:
            return 3
    return 0


def _emit_reap_human(report: SweepReport, applied: bool, include_orphans: bool) -> None:
    """Pretty-print the verdict table + summary (Layer V T6).

    Args:
        report: SweepReport returned by sweep().
        applied: True when --apply was set.
        include_orphans: True when --include-orphans was set.
    """
    if not report.snapshot:
        print("reap: no entries to classify")
        return
    print(
        f"{'verdict':<18}{'id':<22}{'provider':<10}{'age_h':>7}"
        f"{'hb_age_s':>10}{'sent_age_s':>12}"
    )
    now = time.time()
    for eid, (entry, verdict) in report.snapshot.items():
        provider = entry.get("provider", "?")
        created_at = entry.get("created_at", now)
        try:
            age_h = max(0.0, (now - float(created_at)) / 3600.0)
            age_str = f"{age_h:.1f}"
        except (TypeError, ValueError):
            age_str = "-"
        hb = entry.get("last_heartbeat")
        hb_str = f"{(now - float(hb)):.0f}" if hb is not None else "-"
        tick = entry.get("heartbeat_thread_tick")
        sent_str = f"{(now - float(tick)):.0f}" if tick is not None else "-"
        print(
            f"{verdict.value:<18}{eid:<22}{str(provider):<10}"
            f"{age_str:>7}{hb_str:>10}{sent_str:>12}"
        )
    print()
    if not applied:
        print(
            f"{len(report.snapshot)} entries classified — pass --apply "
            "to act on default policy"
        )
        if not include_orphans:
            orphans = sum(
                1 for _, v in report.snapshot.values() if v.value == "ORPHAN_REAP"
            )
            if orphans:
                print(f"add --include-orphans to also act on {orphans} orphan(s)")
    else:
        destroyed = sum(1 for a in report.actions if a.action == "destroyed_and_forgot")
        forgot = sum(
            1 for a in report.actions if a.action in {"forgot", "forgot_unroutable"}
        )
        skipped = sum(1 for a in report.actions if a.action == "skipped")
        failed = sum(1 for a in report.actions if a.action == "failed")
        print(
            f"acted on {len(report.actions)}: {destroyed} destroyed · "
            f"{forgot} forgotten · {skipped} drift-skipped · {failed} failed"
        )


def _emit_reap_jsonl(report: SweepReport) -> None:
    """Emit JSONL: one record per snapshot entry plus one per action.

    Args:
        report: SweepReport returned by sweep().
    """
    print(json.dumps({"type": "header", "entries": len(report.snapshot)}))
    for eid, (entry, verdict) in report.snapshot.items():
        print(
            json.dumps(
                {
                    "type": "verdict",
                    "id": eid,
                    "provider": str(entry.get("provider", "?")),
                    "verdict": verdict.value,
                }
            )
        )
    for action in report.actions:
        print(
            json.dumps(
                {
                    "type": "action",
                    "id": action.instance_id,
                    "snapshot_verdict": action.snapshot_verdict.value,
                    "applied_verdict": action.applied_verdict.value,
                    "action": action.action,
                    "reason": action.reason,
                }
            )
        )


def _cmd_gc(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``gc`` subcommand — remove store entries matching criteria.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (always 0).
    """
    if ctx.cfg is None:
        raise RuntimeError("_cmd_gc requires --config")
    store = ctx.store()
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
