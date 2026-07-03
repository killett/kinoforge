"""Subcommand handlers + build helpers for the kinoforge CLI.

Every ``_cmd_*`` handler accepts ``(args, ctx)`` where *ctx* is a
:class:`~kinoforge.cli.context.SessionContext` that bundles
``state_dir``, loaded ``cfg``, and lazy ``store()`` / ``ledger()``
accessors.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import kinoforge._adapters  # noqa: F401 — triggers self-registrations
from kinoforge.cli.context import SessionContext
from kinoforge.cli.loras_arg import LorasParseError, parse_loras_heredoc
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.config import Config
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import TeardownError, UnknownAdapter
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    Instance,
    WarmAttachKey,
)
from kinoforge.core.lifecycle import destroy_confirmed
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack
from kinoforge.core.orchestrator import generate
from kinoforge.core.reaper_actor import sweep
from kinoforge.outputs.base import OutputSink
from kinoforge.outputs.local import LocalOutputSink
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.balance_endpoints import BalanceEndpoint, ProviderBalance
    from kinoforge.core.cost import CostSnapshot
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper_actor import SweepReport

logger = logging.getLogger(__name__)

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

    # C28 A3: --diagnostic-mode is a per-invocation cfg override; rebuild the
    # Config with the flag set so the orchestrator's _build_spec sees it and
    # both wires diagnostic_env AND requests restart_policy=never. Operator
    # opts out by simply not passing the flag.
    if getattr(args, "diagnostic_mode", False):
        cfg = cfg.model_copy(update={"diagnostic_mode": True})

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
            override = getattr(args, "stall_window_override", None)
            if override is not None:
                ledger.touch(result.instance.id, stall_window_s=float(override))
            restart_loop_override = getattr(args, "restart_loop_window_override", None)
            if restart_loop_override is not None:
                ledger.touch(
                    result.instance.id,
                    restart_loop_window_s=float(restart_loop_override),
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

    # Resolve provider and engine, then call provisioner.
    # build_provider_for threads cfg.compute.cloud into SkyPilotProvider
    # (Phase 53 Stage C) so manual `kinoforge provision` honours the same
    # cloud-pin contract as `kinoforge deploy` / `generate`.
    try:
        from kinoforge._adapters import build_provider_for
        from kinoforge.core import registry

        engine = registry.get_engine(cfg.engine.kind)()
        provider = build_provider_for(cfg)
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


class _NullPodLockRegistry:
    """Stub registry for ``--dry-run-swap``: never holds state.

    ``acquire`` always returns True so the matcher reports the cheapest
    eligible candidate; ``__contains__`` always False so no pod is
    treated as busy. ``release`` is a no-op.
    """

    def acquire(
        self, pod_id: str, *, blocking: bool = False, timeout: float | None = None
    ) -> bool:
        return True

    def release(self, pod_id: str) -> None:
        return None

    def __contains__(self, pod_id: str) -> bool:
        return False


def _classify_loras_source(
    *,
    cli_loras: list[LoraEntry] | None,
    vault: Any,  # noqa: ANN401 — duck-typed Vault
    cfg: Any,  # noqa: ANN401 — duck-typed Config
) -> str:
    """Return 'cli' / 'vault' / 'cfg' / 'empty' for dry-run-swap display."""
    if cli_loras is not None:
        return "cli"
    if vault is not None and getattr(vault, "loras", None):
        return "vault"
    if getattr(cfg, "loras", None):
        return "cfg"
    return "empty"


def _dry_run_swap_preview(ctx: SessionContext) -> int:
    """Render the matcher decision for the active cfg without side effects.

    Imports the matcher lazily so the dry-run path stays fast and
    independent of provider/orchestrator init. No HTTP, no pod lock,
    no validate_for_generate — the early return upstream of those
    side-effects is part of the contract (see
    tests/cli/test_dry_run_swap.py).

    Returns:
        Always 0.
    """
    from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
    from kinoforge.core.warm_reuse.matcher import find_warm_attach_candidate

    cfg = ctx.cfg
    ledger = ctx.ledger()
    lc = cfg.lifecycle() if cfg is not None else None
    threshold = float(
        getattr(lc, "lora_swap_re_probe_after_s", 300.0) if lc is not None else 300.0
    )

    # P3 — classify which precedence branch will drive the LoRA stack.
    _session = EphemeralSession.current()
    _vault = _session.vault if _session is not None else None
    _cli_loras = getattr(_session, "cli_loras", None) if _session else None
    source = _classify_loras_source(cli_loras=_cli_loras, vault=_vault, cfg=cfg)
    print(f"loras_source: {source}")

    match = find_warm_attach_candidate(
        cfg,
        ledger,
        pod_lock_registry=_NullPodLockRegistry(),
        re_probe=None,
        re_probe_threshold_s=threshold,
        download_specs={},
        ephemeral_index=EphemeralIndex(store=ctx.store()),
    )
    if match is None:
        print("matcher: no warm candidate, would cold-boot")
        return 0
    plan = match.swap_plan
    print(f"matcher: selected pod {match.pod_id}")
    print(f"  evict:    {plan.evict}")
    print(f"  download: {plan.download}")
    print(f"  cost:     {plan.estimated_cost_seconds:.1f}s")
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

    # P3 — parse --loras heredoc and resolve eagerly so parse errors fail
    # fast (before preflight + provider work) and CLI refs hit
    # RedactionRegistry ahead of any traceback that might carry them.
    # Stashes on the active EphemeralSession so downstream resolver call
    # sites (warm-reuse set_stack swap) honour the same CLI stack.
    cli_loras: list[LoraEntry] | None = None
    _raw_loras = getattr(args, "loras", None)
    if _raw_loras is not None:
        try:
            cli_loras = parse_loras_heredoc(_raw_loras)
        except LorasParseError as err:
            sys.stderr.write(err.report.render_for_cli())
            sys.stderr.write("\n")
            return 1
        _session = EphemeralSession.current()
        _vault = _session.vault if _session is not None else None
        resolve_active_lora_stack(cfg, _vault, cli_loras=cli_loras)
        if _session is not None:
            _session.cli_loras = cli_loras

    if getattr(args, "dry_run_swap", False):
        return _dry_run_swap_preview(ctx)

    # Pre-flight gate: run NETWORK + PREFLIGHT (STATIC already ran via
    # load_config). --skip-preflight opts out for offline / pre-doctored
    # workflows. Auto-fixes already applied; this is purely advisory at
    # this point — any ERROR-severity result blocks the provider call
    # with exit 2.
    if getattr(args, "skip_preflight", False):
        logger.warning(
            "preflight skipped (--skip-preflight); cfg-time-only validation applied"
        )
    else:
        import kinoforge.providers.runpod  # noqa: F401 — self-register
        import kinoforge.providers.skypilot  # noqa: F401 — self-register
        import kinoforge.validation.checks  # noqa: F401 — self-register
        from kinoforge.core.errors import ValidationError
        from kinoforge.validation import validate_for_generate

        try:
            validate_for_generate(cfg)
        except ValidationError as exc:
            print(f"error: cfg pre-flight failed\n{exc}", file=sys.stderr)
            return 2

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

    # B3 / B4 — warm-attach precedence chain.
    if getattr(args, "no_reuse", False) and getattr(args, "force_attach", False):
        print(
            "error: --no-reuse and --force-attach are mutually exclusive "
            "(--no-reuse forces cold create; --force-attach bypasses verdicts "
            "for warm attach)",
            file=sys.stderr,
        )
        return 2

    attach_pod_id: str | None = getattr(args, "attach_pod", None)
    emit_record_path: Path | None = getattr(args, "emit_provision_record", None)
    if attach_pod_id is not None and getattr(args, "no_reuse", False):
        print(
            "error: --attach-pod and --no-reuse are mutually exclusive "
            "(--attach-pod implies pod survival; --no-reuse forces destroy)",
            file=sys.stderr,
        )
        return 1
    if attach_pod_id is not None and emit_record_path is not None:
        print(
            "error: --attach-pod and --emit-provision-record are mutually "
            "exclusive: attach does not provision",
            file=sys.stderr,
        )
        return 1

    single = bool(getattr(args, "no_reuse", False))
    auto_attach_cfg = (
        getattr(cfg.compute, "warm_reuse_auto_attach", True)
        if cfg.compute is not None
        else False
    )

    instance: Instance | None = None
    if attach_pod_id is not None:
        instance, rc = _resolve_attach_pod(ctx, cfg, attach_pod_id)
        if rc is not None:
            return rc
    elif getattr(args, "instance_id", None) is not None:
        instance, rc = _resolve_warm_instance(
            ctx,
            cfg,
            args.instance_id,
            force_attach=bool(getattr(args, "force_attach", False)),
        )
        if rc is not None:
            return rc
    elif getattr(args, "force_attach", False):
        print(
            "error: --force-attach has no effect without --instance-id",
            file=sys.stderr,
        )
        return 2
    elif single:
        logger.info(
            "--no-reuse: skipping warm-reuse scan; cold create + destroy on exit"
        )
    elif auto_attach_cfg:
        instance, report = _scan_warm_candidates(ctx, cfg)
        summary = report.summarize()
        if summary:
            logger.info(summary)

    try:
        artifact, returned_instance = _generate(
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
            instance=instance,
            single=single,
        )
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    # B3 — record cold-created instance to ledger so the next CLI invocation's
    # _scan_warm_candidates can find it. Skip when a pre-existing instance
    # was supplied (warm-attach path; entry already in ledger) or when the
    # generate path tore the pod down (--no-reuse).
    if returned_instance is not None and instance is None and not single:
        ledger = ctx.ledger()
        if ledger.read(returned_instance.id) is None:
            lc = cfg.lifecycle()
            ledger.record(
                returned_instance,
                idle_timeout_s=int(lc.idle_timeout_s),
                max_age_s=int(lc.max_lifetime_s),
            )
        # Stamp the warm_attach_key so a downstream `kinoforge generate
        # --attach-pod` invocation can validate identity without a
        # full-CapabilityKey matcher round-trip. The find_pods_by_warm_attach_key
        # index also gates on this field.
        cfg_wak = _cfg_warm_attach_key(cfg)
        ledger.touch(
            returned_instance.id,
            warm_attach_key=cfg_wak,
            kinoforge_stages=",".join(_cfg_want_stages(cfg)),
            kinoforge_upscaler=cfg.upscale.engine if cfg.upscale else "",
            kinoforge_upscaler_precision=_upscaler_precision_tag(cfg),
        )
        # 2026-06-27 — ephemeral warm-reuse discovery (Option B disk index).
        # Under STRICT_POLICY the ledger entry above lives only in
        # session.in_memory_ledger; this index row is what lets the next
        # CLI process discover the surviving pod.
        from kinoforge.core.warm_reuse.ephemeral_index import (
            EphemeralIndex,
            EphemeralIndexRow,
        )

        if EphemeralSession.current() is not None:
            EphemeralIndex(store=ctx.store()).add(
                EphemeralIndexRow(
                    id=returned_instance.id,
                    warm_attach_key=cfg_wak,
                    kinoforge_key=cfg.capability_key().derive()[:12],
                    endpoints=dict(returned_instance.endpoints),
                    provider=returned_instance.provider,
                    created_at_local=datetime.now().isoformat(),
                )
            )
        if emit_record_path is not None:
            _write_provision_record(
                emit_record_path,
                instance=returned_instance,
                warm_attach_key=cfg_wak,
            )

    print(f"generated: uri={artifact.uri!r}")
    return 0


def _cmd_upscale(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``upscale`` subcommand — standalone video upscale.

    The CLI surface validates scale + flag conflicts BEFORE any cfg
    load or pod provisioning so a malformed invocation costs zero
    cold-boot time. Full warm-reuse / attach / cold-create wiring
    routes through the orchestrator (see T16); for v1 the handler
    only owns the dry-run path + early refusal gates.
    """
    from kinoforge.core.scale_target import ScaleTarget

    # Mutual exclusion FIRST — does not require cfg load.
    if getattr(args, "no_reuse", False) and getattr(args, "attach_pod", None):
        print(
            "error: --no-reuse and --attach-pod are mutually exclusive "
            "(--no-reuse forces cold create + destroy; --attach-pod "
            "implies pod survival)",
            file=sys.stderr,
        )
        return 2

    # CLI --scale parses BEFORE cfg load. Catches height-target and
    # malformed tokens at startup so the operator does not pay for a
    # cold-boot whose inference call was always going to crash.
    scale: ScaleTarget | None = None
    raw_scale = getattr(args, "scale", None)
    if raw_scale is not None:
        try:
            scale = ScaleTarget.parse(raw_scale)
        except ValueError as exc:
            print(f"error: invalid --scale {raw_scale!r}: {exc}", file=sys.stderr)
            return 2
        if scale.kind == "height":
            print(
                f"error: --scale {raw_scale} deferred to a later session; "
                f"use --scale Nx for v1",
                file=sys.stderr,
            )
            return 2

    if ctx.cfg is None:
        print("error: --config required for upscale", file=sys.stderr)
        return 2
    cfg = ctx.cfg
    if cfg.upscale is None:
        print(
            "error: --config must contain an `upscale:` block; "
            "see examples/configs/upscale-spandrel-x2.yaml",
            file=sys.stderr,
        )
        return 2

    # CLI override takes precedence over cfg.upscale.scale.
    if scale is None:
        try:
            scale = ScaleTarget.parse(cfg.upscale.scale)
        except ValueError as exc:
            print(f"error: invalid cfg.upscale.scale: {exc}", file=sys.stderr)
            return 2

    if getattr(args, "dry_run", False):
        print("upscale plan:")
        print(f"  source: {args.video}")
        print(f"  scale: {raw_scale or cfg.upscale.scale}")
        print(f"  engine: {cfg.upscale.engine}")
        if cfg.upscale.seedvr2 is not None:
            print(
                f"  seedvr2: variant={cfg.upscale.seedvr2.variant} "
                f"precision={cfg.upscale.seedvr2.precision}"
            )
        print(f"  no_reuse: {bool(getattr(args, 'no_reuse', False))}")
        print(f"  attach_pod: {getattr(args, 'attach_pod', None)}")
        return 0

    # T11 — non-dry-run wiring. Reuses generate()'s machinery via the
    # skip_clip_stage flag (T10). Mirrors _cmd_generate's warm-reuse /
    # attach / cold-create precedence chain.
    from kinoforge.core import orchestrator as _orchestrator

    del scale  # ScaleTarget recomputed inside UpscaleStage via cfg.upscale.scale

    input_artifact = _resolve_input_video_as_artifact(args.video)
    store = ctx.store()
    sink_local = _build_sink(cfg, args)
    run_id = (
        args.run_id
        if getattr(args, "run_id", None) is not None
        else f"upscale-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )

    instance: Instance | None = None
    attach_pod_id = getattr(args, "attach_pod", None)
    if attach_pod_id:
        instance, rc = _resolve_attach_pod(ctx, cfg, attach_pod_id)
        if rc is not None:
            return rc
    elif not args.no_reuse:
        instance, report = _scan_warm_candidates(ctx, cfg)
        summary = report.summarize()
        if summary:
            logger.info(summary)

    artifact, returned_instance = _orchestrator.generate(
        cfg,
        request=None,
        store=store,
        sink=sink_local,
        run_id=run_id,
        state_dir=ctx.state_dir,
        cancel_token=ctx.cancel_token,
        instance=instance,
        single=bool(args.no_reuse),
        skip_clip_stage=True,
        initial_clip=input_artifact,
    )

    # T11 — symmetric ledger stamp with _cmd_generate (resolves T7 deferral).
    if returned_instance is not None and instance is None and not args.no_reuse:
        ledger = ctx.ledger()
        if ledger.read(returned_instance.id) is None:
            lc = cfg.lifecycle()
            ledger.record(
                returned_instance,
                idle_timeout_s=int(lc.idle_timeout_s),
                max_age_s=int(lc.max_lifetime_s),
            )
        cfg_wak = _cfg_warm_attach_key(cfg)
        ledger.touch(
            returned_instance.id,
            warm_attach_key=cfg_wak,
            kinoforge_stages=",".join(_cfg_want_stages(cfg)),
            kinoforge_upscaler=cfg.upscale.engine,
            kinoforge_upscaler_precision=_upscaler_precision_tag(cfg),
        )

    print(f"upscaled: uri={artifact.uri!r}")
    return 0


def _resolve_input_video_as_artifact(video_path_or_url: str) -> Artifact:
    """Materialise the ``--video`` arg as a kinoforge Artifact.

    Local file path → ``file://`` URL + sha256 from disk + size from stat.
    ``http(s)://`` URL → passthrough; sha256/size deferred to server-side
    fetch (the upscaler engine's ``source_url`` consumer handles the
    integrity check after download).
    """
    import hashlib as _hashlib

    if video_path_or_url.startswith(("http://", "https://")):
        return Artifact(uri=video_path_or_url, sha256="", size=0)
    p = Path(video_path_or_url).resolve()
    h = _hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return Artifact(uri=f"file://{p}", sha256=h.hexdigest(), size=p.stat().st_size)


def _upscaler_precision_tag(cfg: Config) -> str:
    """Derive the ``kinoforge_upscaler_precision`` ledger tag from cfg.upscale.

    Format is engine-specific: spandrel uses bare precision (``"fp16"``),
    seedvr2 uses ``"{variant_lower}-{precision}"`` (``"3b-fp8"``).
    Returns ``""`` when no upscale block is present or its engine-specific
    sub-block is unset.
    """
    if cfg.upscale is None:
        return ""
    if cfg.upscale.spandrel is not None:
        return cfg.upscale.spandrel.precision
    if cfg.upscale.seedvr2 is not None:
        return f"{cfg.upscale.seedvr2.variant.lower()}-{cfg.upscale.seedvr2.precision}"
    return ""


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

    if getattr(args, "dry_run_swap", False):
        return _dry_run_swap_preview(ctx)

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

    # B3 / B4 — warm-attach precedence chain.
    if getattr(args, "no_reuse", False) and getattr(args, "force_attach", False):
        print(
            "error: --no-reuse and --force-attach are mutually exclusive "
            "(--no-reuse forces cold create; --force-attach bypasses verdicts "
            "for warm attach)",
            file=sys.stderr,
        )
        return 2
    single = bool(getattr(args, "no_reuse", False))
    auto_attach_cfg = (
        getattr(cfg.compute, "warm_reuse_auto_attach", True)
        if cfg.compute is not None
        else False
    )

    instance: Instance | None = None
    if getattr(args, "instance_id", None) is not None:
        instance, rc = _resolve_warm_instance(
            ctx,
            cfg,
            args.instance_id,
            force_attach=bool(getattr(args, "force_attach", False)),
        )
        if rc is not None:
            return rc
    elif getattr(args, "force_attach", False):
        print(
            "error: --force-attach has no effect without --instance-id",
            file=sys.stderr,
        )
        return 2
    elif single:
        logger.info(
            "--no-reuse: skipping warm-reuse scan; cold create + destroy on exit"
        )
    elif auto_attach_cfg:
        instance, report = _scan_warm_candidates(ctx, cfg)
        summary = report.summarize()
        if summary:
            logger.info(summary)

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
            instance=instance,
            single=single,
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
            cap_key = str(entry.get("tags", {}).get("kinoforge_key", "<unknown>"))
            print(
                f"  {entry.get('id', '?')}  "
                f"provider={entry.get('provider', '?')}  "
                f"capability_key={cap_key}"
            )
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
    lora_section: str | None = None,
) -> None:
    """Print a merged + alphabetically sorted ``key=value`` block to stdout.

    Args:
        ledger_block: Output of :func:`_build_ledger_block`.
        provider_block: Provider-derived fields (``provider_status`` and
            optionally ``endpoints``).
        advisory: Optional advisory line; printed AFTER the sorted block.
        lora_section: Optional pre-rendered LoRA inventory block (from
            :func:`_render_lora_inventory_section`); printed after the
            sorted block but before the advisory when set.
    """
    merged = {**ledger_block, **provider_block}
    for key in sorted(merged):
        print(f"{key}={merged[key]}")
    if lora_section is not None:
        print(lora_section)
    if advisory is not None:
        print(advisory)


def _human_bytes(n: int | float) -> str:
    """Format ``n`` as B/KB/MB/GB with one decimal place above KB."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _render_lora_inventory_section(
    inventory: list[dict] | None,  # type: ignore[type-arg]
    *,
    free_bytes: int | None,
) -> str | None:
    """Render the LoRA inventory block for ``kinoforge status`` / ``pod lora ls``.

    Sorts rows newest-last-used first so the operator sees the LoRAs the
    matcher is most likely to keep at the top of the list. Refs flow
    through :class:`RedactionRegistry` so vault-registered + observed
    refs appear as their placeholder token.

    Args:
        inventory: List of inventory entry dicts as written by
            :meth:`Ledger.touch` (or returned from
            ``/lora/inventory`` / ``/lora/set_stack``). Falsy → no section.
        free_bytes: Pod-side ``shutil.disk_usage(LORAS_DIR).free`` snapshot.
            ``None`` → free disk is omitted from the header.

    Returns:
        Multi-line string ready for ``print``, or ``None`` when the
        inventory is empty.
    """
    if not inventory:
        return None
    from kinoforge.core.redaction import RedactionRegistry
    from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs

    _register_observed_lora_refs({"inventory": inventory})
    registry = RedactionRegistry.instance()
    total_bytes = sum(int(e.get("size_bytes", 0) or 0) for e in inventory)
    header = f"  loras ({len(inventory)} resident, {_human_bytes(total_bytes)} used"
    if free_bytes is not None:
        header += f", {_human_bytes(free_bytes)} free):"
    else:
        header += "):"
    rows: list[str] = [header]
    ordered = sorted(
        inventory, key=lambda e: e.get("last_used_at_local") or "", reverse=True
    )
    for e in ordered:
        ref = registry.redact(str(e.get("ref", "?")))
        size = _human_bytes(int(e.get("size_bytes", 0) or 0))
        last_used = str(e.get("last_used_at_local", "?"))
        adapter = str(e.get("adapter_name", "?"))
        rows.append(f"    {ref}  {size}  last_used {last_used}  adapter {adapter}")
    return "\n".join(rows)


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
        stall_window_s=lifecycle.stall_window_s,
        stall_gpu_threshold=lifecycle.stall_gpu_threshold,
        stall_cpu_threshold=lifecycle.stall_cpu_threshold,
        restart_loop_window_s=lifecycle.restart_loop_window_s,
        restart_loop_uptime_threshold_s=lifecycle.restart_loop_uptime_threshold_s,
    ).value


# ---------------------------------------------------------------------------
# B4 — `--instance-id` warm-attach helper
# ---------------------------------------------------------------------------


_FORCE_BYPASSABLE_VERDICTS: frozenset[str] = frozenset(
    {"HEARTBEAT_UNKNOWN", "IDLE_REAP", "ORPHAN_REAP"}
)


# ---------------------------------------------------------------------------
# B3 — auto-discovery warm-attach scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScanReport:
    """Outcome of a single ``_scan_warm_candidates`` call.

    Attributes:
        attached: Instance id of the candidate the scan attached to, or
            ``None`` when no valid candidate was found.
        skipped: List of ``(instance_id, reason_code)`` tuples per
            per-candidate validation failure. Coarse-filter rejects
            (provider mismatch, cap_key mismatch, busy) are NOT
            recorded here — they short-circuit before validation.
    """

    attached: str | None = None
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def summarize(self) -> str:
        """Single-line INFO summary per D6 lock.

        Returns:
            On hit:   ``"warm-reuse: attached to <id> (skipped N: ...)"``
            On miss:  ``"warm-reuse: scanned N, 0 attachable (reasons: ...) — cold create"``
            On empty: ``""``  (silent — happy first-generate path)
        """
        if self.attached is not None:
            if self.skipped:
                reasons = ", ".join(f"{rid}={r}" for rid, r in self.skipped)
                return (
                    f"warm-reuse: attached to {self.attached} "
                    f"(skipped {len(self.skipped)}: {reasons})"
                )
            return f"warm-reuse: attached to {self.attached}"
        if not self.skipped:
            return ""
        reason_counts: dict[str, int] = {}
        for _, r in self.skipped:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        formatted = ", ".join(f"{n} {r}" for r, n in sorted(reason_counts.items()))
        return (
            f"warm-reuse: scanned {len(self.skipped)}, 0 attachable "
            f"(reasons: {formatted}) — cold create"
        )


def _probe_lock_held(store: ArtifactStore, key: str) -> bool:
    """Non-blocking probe: is *key* currently held by another process?

    ``ttl_s=0.0`` reflects "we are not claiming this lock for any
    duration" — the probe acquires + immediately releases. Mirrors B7's
    reaper-side probe pattern.

    Args:
        store: ArtifactStore exposing :meth:`acquire_lock`.
        key: Lock key to probe (e.g. ``"reaper/pod-1"``).

    Returns:
        True iff the lock is currently held by another process; False
        iff free.
    """
    from kinoforge.core.errors import LockTimeout

    try:
        lock = store.acquire_lock(key, ttl_s=0.0)
    except LockTimeout:
        return True
    token = lock.acquire(blocking=False)
    if token is None:
        return True
    lock.release(token)
    return False


def _rc_to_reason(rc: int | None, entry: Mapping[str, Any]) -> str:
    """Map ``_resolve_warm_instance`` return code to scan-report reason code.

    rc=1 → ledger-absent (impossible in scan path; entry already from ledger).
    rc=2 → catch-all precondition refused; use ``classify-not-live`` as
    the umbrella since B3 auto-discovery's most common rc=2 path is
    verdict-gate refusal.
    """
    del entry  # reserved for future finer-grained dispatch
    if rc == 1:
        return "cap-key-drift"  # defensive — should never fire in scan
    return "classify-not-live"


def _scan_warm_candidates(
    ctx: SessionContext,
    cfg: Config,
    *,
    clock: Clock | None = None,
) -> tuple[Instance | None, _ScanReport]:
    """Auto-discover a warm pod for cfg's capability_key.

    B3 entry point. Walks the ledger for non-busy LIVE candidates
    matching cfg's provider + capability_key. Validates each via B4's
    cheap-first chain plus reaper:<id> + provision:<id> non-blocking
    probes. Returns ``(Instance, report)`` on first valid candidate;
    ``(None, report)`` when all candidates exhausted or none exist.

    Args:
        ctx: Per-invocation session context.
        cfg: Loaded kinoforge config.
        clock: Optional clock for is_session_busy; defaults to RealClock.

    Returns:
        ``(Instance, _ScanReport)`` — instance is non-None iff scan
        attached to a candidate; report carries skip detail for
        observability + B2 dashboard ingestion.
    """
    from kinoforge.core.lifecycle import is_session_busy

    _clock: Clock = clock if clock is not None else RealClock()
    now = _clock.now()
    hb_interval = cfg.lifecycle().heartbeat_interval_s
    cap_key = cfg.capability_key().derive()[:12]
    provider_kind = cfg.compute.provider if cfg.compute is not None else ""

    from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex

    entries = ctx.ledger().entries()
    # 2026-06-27 — union ephemeral-index rows so --ephemeral process #2
    # can discover pods provisioned by --ephemeral process #1.
    index_entries = [
        r.to_entry_dict() for r in EphemeralIndex(store=ctx.store()).rows()
    ]
    ledger_ids = {e["id"] for e in entries}
    index_only_ids: set[str] = set()
    for ie in index_entries:
        if ie["id"] not in ledger_ids:  # ledger wins on overlap
            entries.append(ie)
            index_only_ids.add(ie["id"])

    matches = [
        e
        for e in entries
        if e.get("provider") == provider_kind
        and e.get("tags", {}).get("kinoforge_key") == cap_key
        and not is_session_busy(e, now=now, heartbeat_interval_s=hb_interval)
    ]
    matches.sort(
        key=lambda e: float(e.get("heartbeat_thread_tick") or 0.0),
        reverse=True,
    )

    store = ctx.store()
    skipped: list[tuple[str, str]] = []

    for entry in matches:
        instance_id = str(entry["id"])

        # D5 — reaper:<id> non-blocking probe (acquire order matches B1).
        if _probe_lock_held(store, f"reaper/{instance_id}"):
            skipped.append((instance_id, "reaper-held"))
            continue

        # D2 — provision:<id> non-blocking probe.
        if _probe_lock_held(store, f"provision/{instance_id}"):
            skipped.append((instance_id, "provision-held"))
            continue

        # B4 cheap-first chain — force_attach=False (D3
        # conservative-on-ignorance). Index-only entries (ephemeral
        # process #1 wrote them; no ledger row) carry no heartbeat
        # snapshot, so classify would return HEARTBEAT_UNKNOWN; bypass
        # via force_attach + supply the entry so _resolve_warm_instance
        # does not re-fetch from the empty disk ledger.
        is_index_only = instance_id in index_only_ids
        instance, rc = _resolve_warm_instance(
            ctx,
            cfg,
            instance_id,
            force_attach=is_index_only,
            entry=entry if is_index_only else None,
        )
        if rc is not None:
            skipped.append((instance_id, _rc_to_reason(rc, entry)))
            continue

        # T14: /health-driven refinement — even with matching cap_key,
        # refuse a half-failed pod whose loaders did not actually bring
        # up every stage the cfg needs. Unreachable /health → None →
        # fall through (the legacy verdict machinery above already
        # passed; do not synthesise a STAGE_MISMATCH from missing data).
        want_stages = _cfg_want_stages(cfg)
        if want_stages and instance is not None:
            proxy_url = instance.endpoints.get("http") or next(
                iter(instance.endpoints.values()), ""
            )
            if proxy_url:
                verdict = _health_preflight_ok(
                    proxy_url=proxy_url, want_stages=want_stages
                )
                if verdict is False:
                    skipped.append((instance_id, "stage-mismatch"))
                    continue

        return (instance, _ScanReport(attached=instance_id, skipped=skipped))

    return (None, _ScanReport(attached=None, skipped=skipped))


def _cfg_warm_attach_key(cfg: Config) -> str:
    """Derive ``WarmAttachKey`` hex from a cfg's (base, engine, precision)."""
    base_models = [m for m in cfg.models if m.kind == "base"]
    base_ref = base_models[0].ref if base_models else ""
    engine = cfg.engine
    return WarmAttachKey(
        base_model=base_ref,
        engine=engine.kind if engine is not None else "",
        precision=engine.precision if engine is not None else "",
    ).derive()


def _resolve_attach_pod(
    ctx: SessionContext, cfg: Config, pod_id: str
) -> tuple[Instance | None, int | None]:
    """Validate operator-supplied --attach-pod; return Instance or exit code.

    Distinct from :func:`_resolve_warm_instance` (which uses full
    ``CapabilityKey`` + matcher classify): this gate is the simpler
    "I just provisioned this pod and want to attach by id; skip the
    matcher, gate on ``WarmAttachKey`` only" surface used by
    ``kinoforge grid`` swap-mode cells 2..N and any operator scripting
    a deliberate hand-off from a cold-boot to a follow-up generation.

    Order:
      1. ``Ledger.read(pod_id)`` — missing → exit 1.
      2. ``warm_attach_key`` field on entry matches cfg's derived WAK.
      3. ``provider.get_instance(pod_id).status == "ready"`` (live probe).
    """
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    ledger = ctx.ledger()
    entry = ledger.read(pod_id)
    if entry is None:
        print(
            f"pod {pod_id} not in ledger; cannot --attach-pod. Run "
            f"'kinoforge list' to see ledger ids.",
            file=sys.stderr,
        )
        return (None, 1)

    cfg_wak = _cfg_warm_attach_key(cfg)
    entry_wak = entry.get("warm_attach_key")
    if entry_wak != cfg_wak:
        print(
            f"pod {pod_id} warm_attach_key mismatch: ledger entry has "
            f"warm_attach_key={entry_wak!r}; cfg derives {cfg_wak!r}. "
            f"Use a cfg whose (base_model, engine, precision) match the "
            f"pod's, or 'kinoforge destroy --id {pod_id}' first.",
            file=sys.stderr,
        )
        return (None, 1)

    provider_kind = str(entry.get("provider", ""))
    try:
        provider = registry.get_provider(provider_kind)()
    except UnknownAdapter as exc:
        print(
            f"provider {provider_kind!r} unconstructable: {type(exc).__name__}: {exc}.",
            file=sys.stderr,
        )
        return (None, 1)

    # RunPod's GraphQL pod-query is eventually consistent — a pod that
    # was just provisioned + recorded in the ledger by an immediately-
    # preceding `kinoforge generate --emit-provision-record` call can
    # still surface `KeyError: no RunPod pod found` for ~10s post-boot
    # OR briefly during pod state transitions (observed 2026-06-26
    # Tier-4 fire `db787cbb`: cell-0 cold-boot + generation completed
    # at 23:12:17, cell-1 attach at 23:12:18 saw KeyError, the pod was
    # in fact alive). Retry KeyError up to 3 times at 5s backoff before
    # giving up; non-KeyError failures (auth, transport) surface fast.
    live: Instance | None = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            live = provider.get_instance(pod_id)
            break
        except KeyError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(5.0)
                continue
        except Exception as exc:  # noqa: BLE001
            print(
                f"pod {pod_id} get_instance failed: {type(exc).__name__}: {exc}.",
                file=sys.stderr,
            )
            return (None, 1)
    if live is None:
        print(
            f"pod {pod_id} get_instance returned no pod after 3 retries "
            f"({type(last_exc).__name__ if last_exc else '?'}: {last_exc}).",
            file=sys.stderr,
        )
        return (None, 1)

    if live.status != "ready":
        print(
            f"pod {pod_id} has status={live.status!r}; --attach-pod "
            f"requires status=ready.",
            file=sys.stderr,
        )
        return (None, 1)

    # RunPod's `get_instance` returns an Instance whose `tags` LACK the
    # original port list (the pod-query GraphQL selection set doesn't
    # include the port spec); `provider.endpoints` reads `tags["ports"]`
    # and would return an empty dict, which makes
    # `deploy_session → wait_for_ready` raise
    # `ProvisionFailed: pod has no endpoints`. The ledger entry captured
    # the original `instance.tags` at `ledger.record` time, so merge
    # those back in to recover the port list.
    ledger_tags = entry.get("tags", {}) or {}
    if isinstance(ledger_tags, dict):
        merged_tags = dict(ledger_tags)
        merged_tags.update(live.tags or {})  # live values still win on collision
        live.tags = merged_tags
    try:
        live.endpoints = provider.endpoints(live)
    except Exception as exc:  # noqa: BLE001
        print(
            f"pod {pod_id} endpoints query failed: {type(exc).__name__}: {exc}.",
            file=sys.stderr,
        )
        return (None, 1)
    if not live.endpoints:
        print(
            f"pod {pod_id} has no endpoints after ledger tag merge "
            f"(ledger tag keys={list(ledger_tags.keys())}); cannot --attach-pod.",
            file=sys.stderr,
        )
        return (None, 1)

    return (live, None)


def _write_provision_record(
    path: Path,
    *,
    instance: Instance,
    warm_attach_key: str,
) -> None:
    """Write the post-provision JSON record for grid swap-mode handoff.

    Schema (6 keys; ``cost_per_hr_usd`` lets the grid swap-mode
    executor budget cap-trip without a follow-up provider RPC):

    - pod_id, endpoint_url, provider, warm_attach_key
    - provision_ts (local TZ ISO-8601)
    - cost_per_hr_usd (from instance.cost_rate_usd_per_hr)
    """
    from datetime import datetime

    endpoint_url = instance.endpoints.get("http") or next(
        iter(instance.endpoints.values()), ""
    )
    record = {
        "pod_id": instance.id,
        "endpoint_url": endpoint_url,
        "provider": instance.provider,
        "warm_attach_key": warm_attach_key,
        "provision_ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cost_per_hr_usd": instance.cost_rate_usd_per_hr,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(  # kinoforge:public-write
        json.dumps(record, indent=2) + "\n"
    )


def _resolve_warm_instance(
    ctx: SessionContext,
    cfg: Config,
    instance_id: str,
    *,
    force_attach: bool,
    entry: Mapping[str, Any] | None = None,
) -> tuple[Instance | None, int | None]:
    """Validate operator-supplied --instance-id; return Instance or exit code.

    Order (D1 cheap-first):
      1. Ledger.read(instance_id) — missing → (None, 1).
      2. Provider-kind: entry["provider"] vs cfg.compute.provider → (None, 2).
      3. capability_key: cfg.capability_key().derive()[:12] vs
         entry["tags"]["kinoforge_key"] → (None, 2).
      4. Provider construction → (None, 2) on UnknownAdapter / other.
      5. list_instances() → (None, 2) on raise.
      6. classify(entry, live_ids, now, ...) verdict gate per D3:
           LIVE: pass.
           HEARTBEAT_UNKNOWN / IDLE_REAP / ORPHAN_REAP: pass IFF force_attach.
           STALE_LEDGER / OVERAGE_REAP / UNROUTABLE: refuse always.
      7. provider.get_instance(instance_id) → (None, 2) on KeyError.
    """
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.reaper import classify

    now = time.time()

    # 1. Ledger lookup. Skipped when caller supplies an entry directly
    # — e.g. the ephemeral-warm-reuse scan path, where the entry comes
    # from the on-disk ephemeral-index rather than the in-memory ledger
    # (process #2 has a fresh strict-policy session; the ledger.read
    # would return None even though the pod is alive).
    if entry is None:
        ledger = ctx.ledger()
        entry = ledger.read(instance_id)
        if entry is None:
            print(
                f"instance not found in ledger: {instance_id}. "
                f"Run 'kinoforge list' to see available ids.",
                file=sys.stderr,
            )
            return (None, 1)

    # 2. Provider-kind.
    entry_provider = str(entry.get("provider", ""))
    cfg_provider = cfg.compute.provider if cfg.compute is not None else ""
    if entry_provider != cfg_provider:
        print(
            f"provider mismatch: cfg={cfg_provider}, ledger says "
            f"provider={entry_provider} for {instance_id}. "
            f"Use a cfg matching the pod's provider.",
            file=sys.stderr,
        )
        return (None, 2)

    # 3. capability_key.
    cfg_hash = cfg.capability_key().derive()[:12]
    entry_hash_raw = entry.get("tags", {}).get("kinoforge_key")
    entry_hash = str(entry_hash_raw) if entry_hash_raw is not None else "<unknown>"
    if cfg_hash != entry_hash:
        print(
            f"capability_key mismatch: cfg={cfg_hash}, ledger entry "
            f"{instance_id}={entry_hash}. Either use a cfg matching this pod "
            f"or 'kinoforge destroy --id {instance_id}' first.",
            file=sys.stderr,
        )
        return (None, 2)

    # 4. Provider construction.
    try:
        provider = registry.get_provider(entry_provider)()
    except UnknownAdapter as exc:
        print(
            f"provider {entry_provider} unconstructable: "
            f"{type(exc).__name__}: {exc}. Check provider credentials.",
            file=sys.stderr,
        )
        return (None, 2)
    except Exception as exc:  # noqa: BLE001
        print(
            f"provider {entry_provider} unconstructable: {type(exc).__name__}: {exc}.",
            file=sys.stderr,
        )
        return (None, 2)

    # 5. list_instances RPC for classify's live_pod_ids.
    try:
        live_ids = {i.id for i in provider.list_instances()}
    except Exception as exc:  # noqa: BLE001
        print(
            f"provider {entry_provider} list_instances failed: "
            f"{type(exc).__name__}: {exc}.",
            file=sys.stderr,
        )
        return (None, 2)

    # 6. classify verdict gate.
    lifecycle = cfg.lifecycle() if cfg is not None else Lifecycle()
    verdict = classify(
        entry,
        live_ids,
        now,
        idle_timeout_s=lifecycle.idle_timeout_s,
        max_lifetime_s=lifecycle.max_lifetime_s,
        heartbeat_interval_s=lifecycle.heartbeat_interval_s,
        grace_after_session_s=lifecycle.grace_after_session_s,
        stall_window_s=lifecycle.stall_window_s,
        stall_gpu_threshold=lifecycle.stall_gpu_threshold,
        stall_cpu_threshold=lifecycle.stall_cpu_threshold,
        restart_loop_window_s=lifecycle.restart_loop_window_s,
        restart_loop_uptime_threshold_s=lifecycle.restart_loop_uptime_threshold_s,
    )
    v_name = verdict.value
    if v_name == "LIVE":
        pass
    elif v_name in _FORCE_BYPASSABLE_VERDICTS:
        if not force_attach:
            reason = _refuse_reason_for_verdict(v_name, dict(entry), lifecycle, now)
            print(
                f"classify verdict {v_name} blocks attach for {instance_id}: "
                f"{reason}. Pass --force-attach to override, or "
                f"'kinoforge reap --apply' to clean up.",
                file=sys.stderr,
            )
            return (None, 2)
    elif v_name == "STALE_LEDGER":
        print(
            f"instance {instance_id} is stale: provider no longer has this "
            f"pod. Run 'kinoforge forget --id {instance_id}' and provision "
            f"a fresh one.",
            file=sys.stderr,
        )
        return (None, 2)
    elif v_name == "OVERAGE_REAP":
        print(
            f"OVERAGE_REAP: instance {instance_id} exceeded max_lifetime_s "
            f"(cfg policy). Destroy it with "
            f"'kinoforge destroy --id {instance_id}' before reusing the slot.",
            file=sys.stderr,
        )
        return (None, 2)
    else:  # UNROUTABLE / HEARTBEAT_SUBSTRATE_MISSING / unknown
        print(
            f"classify verdict {v_name} blocks attach for {instance_id}.",
            file=sys.stderr,
        )
        return (None, 2)

    # 7. Provider get_instance.
    try:
        instance = provider.get_instance(instance_id)
    except KeyError:
        print(
            f"instance {instance_id} disappeared between classify and "
            f"lookup; a concurrent reaper may have destroyed it. "
            f"Re-run after 'kinoforge list'.",
            file=sys.stderr,
        )
        return (None, 2)

    # 8. Re-hydrate endpoints + tags from the ledger entry.
    #
    # ``provider.get_instance`` impoverishes the Instance because the
    # underlying list/status APIs strip the create-time fields (RunPod's
    # ``Pod`` GraphQL query only returns id/desiredStatus/imageName; same
    # gap latent in SkyPilot's ``_cluster_record_to_instance``). Without
    # rehydration, ``Instance.endpoints`` stays empty and the next engine
    # call (e.g. ``ComfyUIEngine.wait_for_ready`` at
    # ``engines/comfyui/__init__.py:1472``) raises
    # ``ProvisionFailed("pod ... has no endpoints — cannot construct
    # ready URL")``.
    #
    # The local ledger is authoritative for create-time fields under the
    # same-host scope (see the B5b deferral spec). Merge ledger tags
    # under the provider's tags so e.g. ``"mode": "pod"`` from the live
    # query takes precedence over any stale tag-side state, then ask
    # the provider to build the endpoints dict — providers compute
    # endpoints deterministically from instance fields (e.g. RunPod's
    # ``{pod_id}-{port}.proxy.runpod.net`` pattern) so the call is
    # cheap, network-free, and idempotent.
    #
    # Empirically caught 2026-06-18 against Wan 1.3B on RunPod, pod
    # ``di506yuuczuhht``: warm-reuse classify cleared LIVE, attach
    # succeeded, generation aborted immediately on the empty endpoints
    # field.
    entry_tags = entry.get("tags", {}) or {}
    merged_tags: dict[str, str] = {**entry_tags, **instance.tags}
    instance = dataclasses.replace(instance, tags=merged_tags)
    endpoints_dict: dict[str, str] = {}
    # Prefer endpoints recorded at cold-create time (e.g. ephemeral-index
    # row) over the provider-deterministic reconstruction: the recorded
    # dict already names the engine's port (RunPod sparse tags do not).
    entry_endpoints = entry.get("endpoints")
    if isinstance(entry_endpoints, dict) and entry_endpoints:
        endpoints_dict = {str(k): str(v) for k, v in entry_endpoints.items()}
    elif hasattr(provider, "endpoints"):
        try:
            endpoints_dict = provider.endpoints(instance)
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment
            print(
                f"warning: warm-attach endpoint reconstruction failed for "
                f"{instance_id}: {type(exc).__name__}: {exc}. Downstream "
                f"engine may not be able to construct the ready URL.",
                file=sys.stderr,
            )
    if endpoints_dict:
        instance = dataclasses.replace(instance, endpoints=endpoints_dict)

    return (instance, None)


def _refuse_reason_for_verdict(
    verdict: str,
    entry: dict,  # type: ignore[type-arg]
    lifecycle: Lifecycle,
    now: float,
) -> str:
    """One-line human-readable reason for a refused verdict."""
    if verdict == "IDLE_REAP":
        hb = float(entry.get("last_heartbeat", now))
        return f"hb_age={now - hb:.0f}s > idle_timeout={lifecycle.idle_timeout_s:.0f}s"
    if verdict == "ORPHAN_REAP":
        # Mirror reaper.classify's decision basis: time since last detach
        # (session_end), with pod_age fallback when session_end never written.
        # Reporting sentinel_age here is a footgun — the sentinel is the
        # trigger for entering the stale branch, not the threshold compared
        # against grace_after_session_s.
        session_end = entry.get("session_end")
        if session_end is not None:
            elapsed_label = f"time_since_session_end={now - float(session_end):.0f}s"
        else:
            created_at = float(entry.get("created_at", now))
            elapsed_label = f"pod_age={now - created_at:.0f}s"
        return (
            f"{elapsed_label} past "
            f"grace_after_session_s={lifecycle.grace_after_session_s:.0f}s"
        )
    if verdict == "HEARTBEAT_UNKNOWN":
        return "no sentinel data in ledger entry"
    return verdict


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

    # Refresh the ledger rate from the live provider value so accrued
    # spend, the cost dashboard, and the budget-ceiling guard use the
    # rate the provider is actually billing — not the catalog rate
    # snapshotted at provision time.  A 0.0 reading (partial response,
    # early boot, providers that don't surface a per-instance rate) is
    # treated as "no fresh signal" and the stored rate is preserved.
    live_rate = instance.cost_rate_usd_per_hr
    if live_rate > 0 and live_rate != float(entry.get("cost_rate_usd_per_hr", 0.0)):
        ledger.touch(args.id, cost_rate_usd_per_hr=live_rate)
        refreshed = ledger.read(args.id)
        if refreshed is not None:
            entry = refreshed
            ledger_block = _build_ledger_block(entry, cfg=cfg, now=now)

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

    lora_section = _render_lora_inventory_section(
        entry.get("lora_inventory"),
        free_bytes=entry.get("loras_dir_free_bytes"),
    )
    _print_status_block(
        ledger_block,
        provider_block,
        advisory=heartbeat_advisory,
        lora_section=lora_section,
    )
    return 0


def _cfg_want_stages(cfg: Config) -> tuple[str, ...]:
    """Return pipeline stages this cfg will exercise on the pod.

    Mirrors :meth:`Config.capability_key` stages derivation: pure-t2v
    cfgs return ``()`` (no preflight refinement needed; the cap_key
    pre-filter already gates on pipeline identity); upscale-attached
    cfgs return ``("t2v", "upscale")``. Drives the matcher's
    /health-aware refusal of half-failed pods.
    """
    if getattr(cfg, "upscale", None) is not None:
        return ("t2v", "upscale")
    return ()


def _health_preflight_ok(
    *,
    proxy_url: str,
    want_stages: tuple[str, ...],
) -> bool | None:
    """Pre-flight gate via ``/health`` before claiming a warm-attach candidate.

    Args:
        proxy_url: Base URL of the pod's HTTP endpoint (no trailing slash
            required; ``/health`` is appended).
        want_stages: Stage tags this cfg requires. Empty tuple
            short-circuits to True without hitting the network.

    Returns:
        True — pod's ``capabilities`` is a superset of ``want_stages``.
        False — pod reachable AND capabilities does NOT cover want_stages
                (caller should refuse with ``stage-mismatch`` reason).
                Also returned when the pod is on an older server build
                whose /health payload lacks the ``capabilities`` key:
                conservative-on-ignorance — refuse rather than gamble.
        None — pod /health unreachable; caller falls through to the
               legacy verdict machinery instead of inventing a refusal.
    """
    if not want_stages:
        return True
    try:
        payload = _http_get_json(f"{proxy_url.rstrip('/')}/health")
    except (ConnectionError, TimeoutError, OSError):
        return None
    caps_raw = payload.get("capabilities")
    if caps_raw is None:
        return False
    caps = set(caps_raw)
    return set(want_stages).issubset(caps)


def _http_get_json(url: str) -> dict[str, Any]:
    """GET ``url`` and return the parsed JSON body.

    Module-level seam for tests to monkey-patch — keeps the network call
    out of the unit-test pass path without dragging an httpx dependency
    into the CLI handler signature.

    Args:
        url: The URL to fetch.

    Returns:
        Parsed JSON dict on HTTP 2xx.

    Raises:
        ConnectionError / urllib.error.URLError: Transport-level failure.
        json.JSONDecodeError: Response body not valid JSON.
    """
    import json
    import urllib.request

    req = urllib.request.Request(  # noqa: S310 — operator-supplied pod URL
        url, headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — operator-supplied
        raw = resp.read()
    return dict(json.loads(raw))


def _cmd_pod_lora_ls(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``kinoforge pod lora ls <pod_id>``.

    Queries the pod's ``GET /lora/inventory`` endpoint directly and
    renders the same section as ``kinoforge status``. Ledger is consulted
    only to resolve the pod's provider; the inventory snapshot is taken
    live from the pod (never from ledger cache) so the operator sees the
    current resident set even under ``--ephemeral``.

    Args:
        args: Parsed CLI arguments; uses ``args.pod_id``.
        ctx: Per-invocation session context.

    Returns:
        Exit code: 0 on success, 1 when the pod is absent from the
        ledger, 2 on pod unreachable / transport error.
    """
    from kinoforge.core import registry

    ledger = ctx.ledger()
    entry = next((e for e in ledger.entries() if e.get("id") == args.pod_id), None)
    if entry is None:
        print(f"pod {args.pod_id!r} not found in ledger", file=sys.stderr)
        return 1

    provider_name = str(entry.get("provider", "local"))
    try:
        provider = registry.get_provider(provider_name)()
    except UnknownAdapter:
        print(f"pod lora ls: unknown provider {provider_name!r}", file=sys.stderr)
        return 2

    try:
        instance = provider.get_instance(args.pod_id)
    except Exception as exc:  # noqa: BLE001 — surface as unreachable
        print(
            f"pod lora ls: pod {args.pod_id} unreachable "
            f"({exc.__class__.__name__}: {exc})",
            file=sys.stderr,
        )
        return 2

    try:
        endpoints_map = provider.endpoints(instance)
    except Exception as exc:  # noqa: BLE001
        print(
            f"pod lora ls: endpoint resolution failed "
            f"({exc.__class__.__name__}: {exc})",
            file=sys.stderr,
        )
        return 2

    base_url = endpoints_map.get("8000") or next(iter(endpoints_map.values()), None)
    if base_url is None:
        print(
            f"pod lora ls: no endpoint URL for pod {args.pod_id}",
            file=sys.stderr,
        )
        return 2

    try:
        payload = _http_get_json(f"{base_url.rstrip('/')}/lora/inventory")
    except Exception as exc:  # noqa: BLE001 — clean exit code on any I/O error
        print(
            f"pod lora ls: pod unreachable ({exc.__class__.__name__}: {exc})",
            file=sys.stderr,
        )
        return 2

    section = _render_lora_inventory_section(
        payload.get("inventory"), free_bytes=payload.get("free_bytes")
    )
    if section is None:
        print(f"pod {args.pod_id}: no LoRAs loaded")
    else:
        print(section)
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


_LOG_SIDECAR_PORT = "8001"


def _cmd_logs(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Fetch a file served by the pod's port-8001 sidecar http.server.

    Contract (wan_t2v_server bootstrap): ``/tmp/bootstrap.log`` is the
    server's redirected stdout/stderr; the sidecar serves ``--directory
    /tmp`` on port 8001 via the RunPod proxy at
    ``https://{pod}-8001.proxy.runpod.net``. Pass ``--file selfterm.log``
    (or any other name under /tmp) to fetch alternate captures.

    Writes bytes to stdout by default; ``--out PATH`` writes to disk
    without decoding so binary artifacts (frame samples, tensor dumps)
    pass through unchanged.
    """
    del ctx  # ledger not consulted — proxy URL is deterministic from id
    filename = getattr(args, "file", None) or "bootstrap.log"
    url = f"https://{args.id}-{_LOG_SIDECAR_PORT}.proxy.runpod.net/{filename}"
    req = urllib.request.Request(  # noqa: S310 — pod proxy URL only
        url, headers={"User-Agent": "kinoforge-logs/0.1"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            body: bytes = resp.read()
    except urllib.error.HTTPError as exc:
        print(f"error fetching {url}: HTTP {exc.code} {exc.reason}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error fetching {url}: {exc}", file=sys.stderr)
        return 1

    out_path = getattr(args, "out", None)
    if out_path:
        # kinoforge:public-write — user explicitly requested this destination
        # via `kinoforge logs --out <path>`; the write IS the command's contract.
        Path(out_path).write_bytes(body)  # kinoforge:public-write
    else:
        sys.stdout.write(body.decode("utf-8", errors="replace"))
    return 0


def _cmd_destroy(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``destroy`` subcommand.

    Args:
        args: Parsed CLI arguments.
        ctx: Per-invocation session context.

    Returns:
        Exit code (0 on success, 1 on error).
    """
    from kinoforge.core import registry

    ledger = ctx.ledger()
    entries = ledger.entries()
    entry = next((e for e in entries if e.get("id") == args.id), None)
    if entry is not None:
        provider_name = entry.get("provider", "local")
        try:
            provider = registry.get_provider(str(provider_name))()
            from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex

            destroy_confirmed(
                provider,
                args.id,
                sleep=lambda _: None,
                ephemeral_index=EphemeralIndex(store=ctx.store()),
            )
            ledger.forget(args.id)
            print(f"destroyed: {args.id}")
            return 0
        except (UnknownAdapter, KeyError) as exc:
            print(f"error destroying {args.id!r}: {exc}", file=sys.stderr)
            return 1

    # No ledger entry — probe every registered provider for the orphan.
    # This is the 2026-06-23 destroy-on-teardown fallback path: the
    # smoke harness's subprocess `kinoforge destroy --id` had no way
    # to reach a pod whose ledger entry the orchestrator had already
    # forgotten (e.g. after a partial cleanup or a cross-process
    # workspace).  KeyError on a probe means "this provider doesn't
    # know that id"; any other exception means we couldn't tell, so
    # skip and try the next provider — the probe is exploratory.
    orphan_provider = None
    orphan_name: str | None = None
    for name in registry.provider_names():
        try:
            provider = registry.get_provider(name)()
        except UnknownAdapter:
            continue
        try:
            provider.get_instance(args.id)
        except KeyError:
            continue
        except Exception as exc:  # noqa: BLE001, S112
            # Probe failed for network / auth / GraphQL reasons.  Be
            # conservative and try the next provider; don't claim
            # ownership of an id we couldn't actually confirm.  Log
            # to stderr so the operator sees why a provider was
            # skipped — silent skip masked the 2026-06-23 bug.
            print(
                f"_cmd_destroy: skipping orphan probe via {name!r}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        orphan_provider = provider
        orphan_name = name
        break

    if orphan_provider is None:
        print(f"instance {args.id!r} not found in ledger", file=sys.stderr)
        return 1

    try:
        from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex

        destroy_confirmed(
            orphan_provider,
            args.id,
            sleep=lambda _: None,
            ephemeral_index=EphemeralIndex(store=ctx.store()),
        )
    except (UnknownAdapter, KeyError, TeardownError) as exc:
        print(
            f"error destroying orphan {args.id!r} via {orphan_name}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"destroyed orphan: {args.id} (no ledger entry, provider={orphan_name})")
    return 0


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
        "stall_window_s": lifecycle.stall_window_s,
        "stall_gpu_threshold": lifecycle.stall_gpu_threshold,
        "stall_cpu_threshold": lifecycle.stall_cpu_threshold,
        "restart_loop_window_s": lifecycle.restart_loop_window_s,
        "restart_loop_uptime_threshold_s": lifecycle.restart_loop_uptime_threshold_s,
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
        deferred = sum(
            1 for a in report.actions if a.action == "deferred-session-claim"
        )
        print(
            f"acted on {len(report.actions)}: {destroyed} destroyed · "
            f"{forgot} forgotten · {skipped} drift-skipped · "
            f"{deferred} deferred · {failed} failed"
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


# --------------------------------------------------------------------
# B2 / Layer X — `kinoforge cost` subcommand.
# --------------------------------------------------------------------

_COST_CACHE_RUN_ID = "_cost_cache"


def cached_balance_read(
    *,
    store: ArtifactStore,
    provider: str,
    endpoint: BalanceEndpoint,
    cache_ttl_s: float,
    no_cache: bool,
    now: datetime,
) -> tuple[ProviderBalance | None, str | None]:
    """TTL-gated balance read with stale-fallback.

    Reads ``<store>/_cost_cache/cost/balance_<provider>.json`` first; if
    fresh (``now - cached_at < cache_ttl_s``) returns the cached value.
    Otherwise hits ``endpoint.read()``; on success persists the result
    and returns it. On :class:`TransportError` falls back to the cached
    value when one exists (annotates the error string so the renderer
    can flag it as stale).

    Args:
        store: ArtifactStore to read/write cache entries.
        provider: Provider kind string (cache key axis).
        endpoint: BalanceEndpoint to read fresh values from.
        cache_ttl_s: Cache freshness window in seconds.
        no_cache: When True, bypasses read AND write — used by ``--no-cache``.
        now: Wall-clock for staleness math.

    Returns:
        ``(balance, error_message)``. Either may be ``None``. When the
        cached entry is returned because the fresh fetch failed, both
        are non-``None``: balance carries the cached value, error
        explains why the fresh fetch fell back.
    """
    from kinoforge.core.balance_endpoints import TransportError

    name = f"cost/balance_{provider}.json"
    cached: dict[str, Any] | None = None
    if not no_cache:
        try:
            uri = store.uri_for(_COST_CACHE_RUN_ID, name)
            cached = store.get_json(uri)
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            cached = None
        if cached is not None:
            try:
                cached_at = datetime.fromisoformat(cached["cached_at"])
            except (KeyError, TypeError, ValueError):
                cached = None
            else:
                age_s = (now - cached_at).total_seconds()
                if age_s < cache_ttl_s:
                    return _balance_from_cache(cached), None

    try:
        fresh = endpoint.read()
    except TransportError as exc:
        if cached is not None:
            return _balance_from_cache(cached), f"transport (using cache): {exc}"
        return None, f"transport: {exc}"

    if fresh is None:
        return None, None
    if not no_cache:
        try:
            store.put_json(  # kinoforge:public-write — balance JSON has no secret fields
                _COST_CACHE_RUN_ID,
                name,
                {
                    "usd": fresh.usd,
                    "as_of": fresh.as_of.isoformat(),
                    "source": fresh.source,
                    "currency": fresh.currency,
                    "cached_at": now.isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cost: cache write failed for %s: %s; "
                "returning fresh value without cache",
                provider,
                exc,
            )
    return fresh, None


def _balance_from_cache(cached: dict[str, Any]) -> ProviderBalance:
    """Reconstruct ProviderBalance from a cached JSON dict."""
    from kinoforge.core.balance_endpoints import ProviderBalance

    return ProviderBalance(
        usd=float(cached["usd"]),
        as_of=datetime.fromisoformat(cached["as_of"]),
        source=str(cached["source"]),
        currency=str(cached.get("currency", "USD")),
    )


def _cmd_cost(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``cost`` subcommand: ledger walk + classify + balance dispatch + render.

    Three output modes (mutually exclusive): default human table, ``--json``,
    ``--prom``. Never raises from the render path — every failure
    degrades the affected column per spec §12.

    Args:
        args: Parsed CLI arguments with fields ``json``, ``prom``,
            ``no_cache``, ``cache_ttl``.
        ctx: Per-invocation session context.

    Returns:
        Exit code 0 on success (including degraded balance / partial truth).
    """
    from kinoforge._adapters import build_balance_endpoint_for
    from kinoforge.core import registry
    from kinoforge.core.balance_endpoints import (
        provider_balance_supported,
    )
    from kinoforge.core.cost import aggregate
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.heartbeat_endpoints import provider_heartbeat_supported
    from kinoforge.core.reaper import Verdict, classify

    cfg = ctx.cfg
    ledger = ctx.ledger()
    entries = list(ledger.entries())

    now_dt = datetime.now()
    now_ts = now_dt.timestamp()

    providers_in_ledger = {str(e.get("provider", "unknown")) for e in entries}
    live_pod_ids_by_provider: dict[str, frozenset[str]] = {}
    for provider_kind in providers_in_ledger:
        try:
            prov_inst = registry.get_provider(provider_kind)()
            ids = frozenset(str(i.id) for i in prov_inst.list_instances())
            live_pod_ids_by_provider[provider_kind] = ids
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cost: provider %s list_instances failed (%s); "
                "assuming all ledger ids are up",
                provider_kind,
                exc.__class__.__name__,
            )
            fallback = frozenset(
                str(e["id"])
                for e in entries
                if e.get("provider") == provider_kind and e.get("id") is not None
            )
            live_pod_ids_by_provider[provider_kind] = fallback

    stall_window_s: float | None
    restart_loop_window_s: float | None
    if cfg is not None:
        lc = cfg.lifecycle()
        idle_timeout_s = float(lc.idle_timeout_s)
        max_lifetime_s = float(lc.max_lifetime_s)
        heartbeat_interval_s = lc.heartbeat_interval_s
        grace_after_session_s = float(lc.grace_after_session_s)
        stall_window_s = lc.stall_window_s
        stall_gpu_threshold = lc.stall_gpu_threshold
        stall_cpu_threshold = lc.stall_cpu_threshold
        restart_loop_window_s = lc.restart_loop_window_s
        restart_loop_uptime_threshold_s = lc.restart_loop_uptime_threshold_s
    else:
        idle_timeout_s = 600.0
        max_lifetime_s = 3600.0
        heartbeat_interval_s = None
        grace_after_session_s = 300.0
        stall_window_s = None
        stall_gpu_threshold = 5.0
        stall_cpu_threshold = 20.0
        restart_loop_window_s = None
        restart_loop_uptime_threshold_s = 90.0

    verdicts_by_id: dict[str, Verdict] = {}
    for entry in entries:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        provider_kind = str(entry.get("provider", "unknown"))
        live_ids = live_pod_ids_by_provider.get(provider_kind, frozenset())
        try:
            verdicts_by_id[str(entry_id)] = classify(
                entry,
                live_ids,
                now_ts,
                idle_timeout_s=idle_timeout_s,
                max_lifetime_s=max_lifetime_s,
                heartbeat_interval_s=heartbeat_interval_s,
                grace_after_session_s=grace_after_session_s,
                stall_window_s=stall_window_s,
                stall_gpu_threshold=stall_gpu_threshold,
                stall_cpu_threshold=stall_cpu_threshold,
                restart_loop_window_s=restart_loop_window_s,
                restart_loop_uptime_threshold_s=restart_loop_uptime_threshold_s,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cost: classify failed on entry %s (%s); skipping",
                entry_id,
                exc.__class__.__name__,
            )

    balances: dict[str, ProviderBalance | None] = {}
    balance_errors: dict[str, str] = {}
    creds = EnvCredentialProvider()
    store = ctx.store()
    for provider_kind in providers_in_ledger:
        if not provider_balance_supported(provider_kind):
            balances[provider_kind] = None
            continue
        if cfg is None:
            balances[provider_kind] = None
            continue
        endpoint = build_balance_endpoint_for(cfg, creds)
        bal, err = cached_balance_read(
            store=store,
            provider=provider_kind,
            endpoint=endpoint,
            cache_ttl_s=args.cache_ttl,
            no_cache=args.no_cache,
            now=now_dt,
        )
        balances[provider_kind] = bal
        if err is not None:
            balance_errors[provider_kind] = err

    heartbeat_partial_truth = tuple(
        sorted(p for p in providers_in_ledger if not provider_heartbeat_supported(p))
    )

    try:
        threshold = float(os.environ.get("KINOFORGE_REPLICATE_THROTTLE_AT_USD", "4.50"))
    except ValueError:
        threshold = 4.50
    throttle_warnings: tuple[str, ...] = ()

    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts_by_id,
        now=now_dt,
        balances=balances,
        balance_errors=balance_errors,
        heartbeat_partial_truth=heartbeat_partial_truth,
        throttle_warnings=throttle_warnings,
    )

    if args.json:
        sys.stdout.write(_render_cost_json(snap))
    elif args.prom:
        sys.stdout.write(_render_cost_prom(snap, balance_errors))
    else:
        sys.stdout.write(_render_cost_human(snap, threshold_set=(threshold > 0)))
    return 0


def _render_cost_human(snap: CostSnapshot, *, threshold_set: bool) -> str:
    """Human-readable cost table per spec §5."""
    from kinoforge.core.reaper import Verdict

    lines: list[str] = []
    lines.append(f"As of {snap.as_of.isoformat(timespec='seconds')}")
    lines.append(f"Burn rate: ${snap.burn_rate_usd_per_hr:.2f}/hr")
    if not snap.per_provider:
        lines.append("(no entries in ledger)")
    else:
        lines.append("")
        lines.append("Per-provider:")
        for p in snap.per_provider:
            counts_str = " ".join(
                f"{v.value}={p.pod_counts_by_verdict.get(v, 0)}"
                for v in Verdict
                if p.pod_counts_by_verdict.get(v, 0) > 0
            )
            bal = snap.balances.get(p.provider)
            bal_err = snap.balance_errors.get(p.provider)
            if bal is not None:
                bal_str = f"balance ${bal.usd:.2f}"
            elif bal_err is not None:
                bal_str = f"balance ? ({bal_err})"
            else:
                bal_str = "balance N/A"
            lines.append(
                f"  {p.provider}: ${p.burn_rate_usd_per_hr:.2f}/hr  "
                f"spend ${p.spend_usd_total:.2f}  {bal_str}  [{counts_str}]"
            )
    if snap.heartbeat_partial_truth:
        lines.append("")
        lines.append(
            "WARNING: heartbeat substrate not yet shipped for "
            f"{','.join(snap.heartbeat_partial_truth)} (B5b pending); "
            "LIVE counts are upper-bound estimates."
        )
    if snap.hosted_spend_pending:
        lines.append("compute spend only (hosted spend deferred to B10)")
    if threshold_set:
        lines.append("replicate spend tracking pending B10")
    for w in snap.throttle_warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines) + "\n"


def _render_cost_json(snap: CostSnapshot) -> str:
    """Render the stable §10 JSON schema."""
    from kinoforge.core.reaper import Verdict

    out: dict[str, Any] = {
        "as_of": snap.as_of.isoformat(),
        "burn_rate_usd_per_hr": snap.burn_rate_usd_per_hr,
        "per_provider": [
            {
                "provider": p.provider,
                "burn_rate_usd_per_hr": p.burn_rate_usd_per_hr,
                "spend_usd_total": p.spend_usd_total,
                "pod_counts_by_verdict": {
                    v.value: p.pod_counts_by_verdict.get(v, 0) for v in Verdict
                },
            }
            for p in snap.per_provider
        ],
        "balance": {
            provider: (
                None
                if bal is None
                else {
                    "usd": bal.usd,
                    "as_of": bal.as_of.isoformat(),
                    "source": bal.source,
                    "currency": bal.currency,
                    "cached_age_s": 0,
                }
            )
            for provider, bal in snap.balances.items()
        },
        "balance_errors": dict(snap.balance_errors),
        "heartbeat_partial_truth": list(snap.heartbeat_partial_truth),
        "hosted_spend_pending": snap.hosted_spend_pending,
        "throttle_warnings": list(snap.throttle_warnings),
    }
    return json.dumps(out, indent=2) + "\n"


def _render_cost_prom(snap: CostSnapshot, balance_errors: dict[str, str]) -> str:
    """Render Prometheus text exposition per spec §9. LF-only."""
    from kinoforge.core.reaper import Verdict

    lines: list[str] = []

    def emit_help(metric: str, help_text: str, type_: str) -> None:
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} {type_}")

    emit_help(
        "kinoforge_burn_rate_usd_per_hr",
        "Sum of cost_rate_usd_per_hr across pod-up verdicts.",
        "gauge",
    )
    for p in snap.per_provider:
        lines.append(
            f'kinoforge_burn_rate_usd_per_hr{{provider="{p.provider}"}} '
            f"{p.burn_rate_usd_per_hr}"
        )

    emit_help(
        "kinoforge_balance_usd",
        "Provider-account balance, when a balance endpoint ships.",
        "gauge",
    )
    for provider, bal in snap.balances.items():
        if bal is not None:
            lines.append(f'kinoforge_balance_usd{{provider="{provider}"}} {bal.usd}')

    emit_help(
        "kinoforge_balance_as_of_seconds",
        "Unix timestamp the balance was read (or cached).",
        "gauge",
    )
    for provider, bal in snap.balances.items():
        if bal is not None:
            lines.append(
                f'kinoforge_balance_as_of_seconds{{provider="{provider}"}} '
                f"{int(bal.as_of.timestamp())}"
            )

    emit_help(
        "kinoforge_pod_count",
        "Pod count per provider per verdict.",
        "gauge",
    )
    for p in snap.per_provider:
        for v in Verdict:
            count = p.pod_counts_by_verdict.get(v, 0)
            lines.append(
                f'kinoforge_pod_count{{provider="{p.provider}",'
                f'verdict="{v.value}"}} {count}'
            )

    emit_help(
        "kinoforge_spend_usd_total",
        "Lifetime $ spent on currently-up pods this provider.",
        "gauge",
    )
    for p in snap.per_provider:
        lines.append(
            f'kinoforge_spend_usd_total{{provider="{p.provider}"}} {p.spend_usd_total}'
        )

    emit_help(
        "kinoforge_cost_scrape_errors_total",
        "Failed balance reads since process start.",
        "counter",
    )
    for p in snap.per_provider:
        err = balance_errors.get(p.provider, "")
        err_lower = err.lower()
        for reason in ("transport", "schema", "cred"):
            value = 1 if (reason in err_lower) else 0
            lines.append(
                f'kinoforge_cost_scrape_errors_total{{provider="{p.provider}",'
                f'reason="{reason}"}} {value}'
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Layer W — kinoforge sweeper start | stop | status | metrics
# ---------------------------------------------------------------------------


def _cmd_sweeper_start(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Layer W: foreground sweeper daemon supervisor.

    Blocks until SIGTERM. Operator wraps under systemd / nohup / docker
    PID 1 / tmux. Materialises the synthetic ``sweeper:<host>`` ledger
    entry (§4.4 init), prints the §4.7 banner, installs SIGTERM /
    SIGHUP / SIGUSR1 handlers, then starts the SweeperLoop.
    """
    import signal
    import socket
    import threading

    from kinoforge.core import registry
    from kinoforge.core.config import load_config, sweeper_policy_from_cfg
    from kinoforge.core.sweeper import SweeperLoop, _SweeperStats

    cfg = ctx.cfg
    if cfg is None:
        sys.stderr.write("kinoforge sweeper start: --config is required\n")
        return 2
    cfg_path = args.config
    host = cfg.sweeper.host or socket.gethostname()
    interval_s = float(args.interval_s) if args.interval_s else cfg.sweeper.interval_s
    if interval_s <= 0:
        logger.error("invalid interval_s=%s", interval_s)
        return 2
    policy = sweeper_policy_from_cfg(cfg)
    lc = cfg.lifecycle()
    thresholds = {
        "idle_timeout_s": float(lc.idle_timeout_s),
        "max_lifetime_s": float(lc.max_lifetime_s),
        "heartbeat_interval_s": (
            float(lc.heartbeat_interval_s) if lc.heartbeat_interval_s else None
        ),
        "grace_after_session_s": float(lc.grace_after_session_s),
    }
    ledger = ctx.ledger()
    store = ctx.store()

    pid = os.getpid()
    logger.info(
        "kinoforge sweeper starting host=%s interval_s=%s policy=%s "
        "include_orphans=%s force_forget=%s pid=%s",
        host,
        interval_s,
        sorted(v.value for v in policy.act_verdicts),
        cfg.sweeper.include_orphans,
        cfg.sweeper.force_forget,
        pid,
    )
    logger.info(
        "B5a heartbeat-substrate gate is ACTIVE: providers with no "
        "shipped HeartbeatEndpoint satisfier emit HEARTBEAT_SUBSTRATE_MISSING "
        "and are NEVER reaped. SkyPilot is the only such provider today; "
        "B5b ships the satisfier when GPU quota lands. WARN-once-per-"
        "(provider,instance_id) deduped."
    )
    logger.info(
        "B7 cooperative session-claim probe is ACTIVE: entries whose "
        "orchestrator holds provision:<id> emit "
        'action="deferred-session-claim" and are skipped this pass; '
        "the next sweep re-evaluates."
    )

    synthetic = Instance(
        id=f"sweeper:{host}",
        provider="_sweeper",
        status="ready",
        created_at=datetime.now().timestamp(),
        cost_rate_usd_per_hr=0.0,
    )
    ledger.record(synthetic)
    ledger.touch(f"sweeper:{host}", pid=pid)

    stats = _SweeperStats()
    loop = SweeperLoop(
        store=store,
        ledger=ledger,
        registry_get_provider=registry.get_provider,
        thresholds=thresholds,
        interval_s=interval_s,
        host=host,
        policy=policy,
        stats=stats,
    )

    exit_event = threading.Event()

    def _handle_sigterm(_signum: int, _frame: FrameType | None) -> None:
        exit_event.set()

    def _handle_sighup(_signum: int, _frame: FrameType | None) -> None:
        try:
            new_cfg = load_config(cfg_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SIGHUP: cfg reload failed: %s", exc)
            return
        new_policy = sweeper_policy_from_cfg(new_cfg)
        new_lc = new_cfg.lifecycle()
        new_thresholds = {
            "idle_timeout_s": float(new_lc.idle_timeout_s),
            "max_lifetime_s": float(new_lc.max_lifetime_s),
            "heartbeat_interval_s": (
                float(new_lc.heartbeat_interval_s)
                if new_lc.heartbeat_interval_s
                else None
            ),
            "grace_after_session_s": float(new_lc.grace_after_session_s),
        }
        loop.reload(
            policy=new_policy,
            thresholds=new_thresholds,
            interval_s=new_cfg.sweeper.interval_s,
        )
        logger.info("SIGHUP: cfg reloaded from %s", cfg_path)

    def _handle_sigusr1(_signum: int, _frame: FrameType | None) -> None:
        logger.info("sweeper stats: %s", stats.snapshot_for_log())

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGHUP, _handle_sighup)
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    loop.start()
    exit_event.wait()
    loop.stop()
    return 0


def _cmd_sweeper_stop(args: argparse.Namespace, ctx: SessionContext) -> int:  # noqa: ARG001
    """Layer W: send SIGTERM to the daemon owning this host's sweeper entry."""
    import signal
    import socket

    cfg = ctx.cfg
    if cfg is None:
        sys.stderr.write("kinoforge sweeper stop: --config is required\n")
        return 2
    host = cfg.sweeper.host or socket.gethostname()
    ledger = ctx.ledger()
    entry = ledger.read(f"sweeper:{host}")
    if entry is None:
        sys.stderr.write(f"no sweeper running on host={host}\n")
        return 1
    pid = entry.get("pid")
    try:
        pid_int = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        pid_int = 0
    if not pid_int:
        sys.stderr.write(f"daemon liveness entry has no pid on host={host} (stale?)\n")
        return 1
    try:
        os.kill(pid_int, signal.SIGTERM)
    except ProcessLookupError:
        sys.stderr.write(f"pid {pid_int} no longer alive on host={host}\n")
        return 1
    deadline = time.monotonic() + 30.0
    last_tick = entry.get("heartbeat_thread_tick", 0.0)
    stable_polls = 0
    while time.monotonic() < deadline:
        time.sleep(1.0)
        entry = ledger.read(f"sweeper:{host}")
        if entry is None:
            return 0
        tick = entry.get("heartbeat_thread_tick", 0.0)
        if tick == last_tick:
            stable_polls += 1
            if stable_polls >= 2:
                return 0
        else:
            stable_polls = 0
            last_tick = tick
    sys.stderr.write(f"sweeper on host={host} did not stop within 30s\n")
    return 2


def _cmd_sweeper_status(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Layer W: render sweeper liveness — human (default) or --json."""
    import socket

    from kinoforge.core.sweeper_metrics import (
        render_status_human,
        render_status_json,
    )

    cfg = ctx.cfg
    if cfg is None:
        sys.stderr.write("kinoforge sweeper status: --config is required\n")
        return 2
    host = cfg.sweeper.host or socket.gethostname()
    ledger = ctx.ledger()
    entry = ledger.read(f"sweeper:{host}")
    now = datetime.now().timestamp()
    if args.json:
        sys.stdout.write(
            render_status_json(
                entry, host=host, interval_s=cfg.sweeper.interval_s, now=now
            )
            + "\n"
        )
    else:
        sys.stdout.write(
            render_status_human(
                entry, host=host, interval_s=cfg.sweeper.interval_s, now=now
            )
        )
    return 0


def _cmd_doctor(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Run the full cfg validation Check Registry and print a report.

    Bypasses ``load_config``'s STATIC pass via ``_parse_cfg_raw`` so the
    report surfaces every failing row instead of aborting at parse
    time. Exit code = number of ERRORs (0 on a clean cfg). NETWORK and
    PREFLIGHT categories DO run here.
    """
    import kinoforge.providers.runpod  # noqa: F401 — self-register RunPod check
    import kinoforge.providers.skypilot  # noqa: F401 — self-register SkyPilot check
    import kinoforge.validation.checks  # noqa: F401 — self-register built-ins
    from kinoforge.core.config import _parse_cfg_raw
    from kinoforge.core.errors import ConfigError
    from kinoforge.validation import validate_for_doctor

    cfg_path = Path(args.config).resolve()
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        print(f"error: cfg not found: {exc}", file=sys.stderr)
        return 1
    try:
        cfg = _parse_cfg_raw(text, yaml_path=cfg_path)
    except ConfigError as exc:
        # Pydantic parse error itself — surface as a single failing row.
        print("doctor — cfg failed Pydantic parse, cannot run checks:")
        print(f"  ✗ pydantic_parse: {exc}")
        return 1

    report = validate_for_doctor(cfg)
    for r in report.results:
        glyph = "✓" if r.passed else ("✗" if r.severity.value == "error" else "⚠")
        print(f"{glyph} {r.name:35s} {r.message}")
        if not r.passed and r.fix_suggestion:
            label = "fix" if r.severity.value == "error" else "suggested"
            print(f"  {label}: {r.fix_suggestion}")
    if report.auto_fixes:
        print()
        print("auto-fixed:")
        for af in report.auto_fixes:
            print(f"  - {af.name}: {af.message}")
    return len(report.errors)


def _cmd_sweeper_metrics(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Layer W: render Prom textfile-collector target."""
    import socket

    from kinoforge.core.sweeper_metrics import render_metrics_prom

    if not args.prom:
        sys.stderr.write("kinoforge sweeper metrics requires --prom\n")
        return 2
    cfg = ctx.cfg
    if cfg is None:
        sys.stderr.write("kinoforge sweeper metrics: --config is required\n")
        return 2
    host = cfg.sweeper.host or socket.gethostname()
    ledger = ctx.ledger()
    entry = ledger.read(f"sweeper:{host}")
    sys.stdout.write(
        render_metrics_prom(entry, host=host, interval_s=cfg.sweeper.interval_s)
    )
    return 0


def _cmd_grid(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``grid`` subcommand.

    Maps :class:`GridResult.status` → exit code:
    full → 0, partial → 2, budget → 3, ffmpeg → 4, teardown → 5,
    spec error → 1.
    """
    import asyncio

    from kinoforge.core.grid.errors import (
        GridSpecParseError,
        GridSpecPathError,
        GridSpecUnderRepoError,
    )
    from kinoforge.core.grid.executor import run_grid
    from kinoforge.core.grid.spec import GridSpec

    del ctx  # grid spec is self-contained — no SessionContext-derived state needed

    try:
        spec = GridSpec.load(args.spec)
    except (GridSpecUnderRepoError, GridSpecPathError, GridSpecParseError) as exc:
        print(f"error: grid spec load failed\n{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"[grid dry-run] {len(spec.cells)} cells, layout={spec.layout}, "
            f"budget_cap=${spec.budget_cap_usd:.2f}"
        )
        return 0

    output_dir = Path("output")
    out_path = Path(args.out) if args.out else None

    result = asyncio.run(
        run_grid(
            spec=spec,
            output_dir=output_dir,
            max_parallel_groups=args.max_parallel_groups,
            out_path=out_path,
        )
    )
    status_to_exit = {
        "full": 0,
        "partial": 2,
        "budget": 3,
        "ffmpeg": 4,
        "teardown": 5,
    }
    code = status_to_exit[result.status]
    if result.status == "full" and result.composed_mp4_path is not None:
        print(f"[grid summary] composed mp4 → {result.composed_mp4_path}")
    else:
        print(
            f"[grid summary] status={result.status}; "
            f"partial mp4s → {result.partial_dir}",
            file=sys.stderr,
        )
        if result.teardown_breadcrumb:
            print(
                f"[grid summary] residual pod breadcrumb:\n{result.teardown_breadcrumb}",
                file=sys.stderr,
            )
    return code
