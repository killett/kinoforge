"""Preflight tool — walk every configured AuthStrategy + probe health.

Mirrors tools/preflight.py (Phase 39) but for hosted-engine auth. Used by
Layer 2 / 3 live smokes as a fail-fast gate before any cloud call.

Usage::

    pixi run probe-hosted -- --config examples/configs/veo.yaml

Exit 0 == every configured strategy's credentials_present() AND
health_check() pass. Non-zero == at least one strategy failed; the
checklist on stdout names every gap.

All I/O is injectable through the public API (probe_strategies,
write_snapshot, run); the CLI entry point is a thin wrapper.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kinoforge.core.auth import AuthStrategy, AWSSigV4

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    """One strategy's probe outcome.

    Attributes:
        name: Engine / service name this strategy authenticates.
        ok: True if credentials_present and health_check both pass.
        identity: Authenticated principal string when ok is True; None otherwise.
        reason: Short human-readable failure reason when ok is False; None otherwise.
    """

    name: str
    ok: bool
    identity: str | None
    reason: str | None


def _git_sha() -> str:
    """Return the current HEAD SHA, or 'unknown' on failure.

    Returns:
        Short HEAD SHA string.
    """
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()  # noqa: S607
    except Exception:  # noqa: BLE001
        return "unknown"


def probe_strategies(
    strategies: Sequence[tuple[str, AuthStrategy]],
) -> list[ProbeResult]:
    """Run credentials_present + health_check on each strategy.

    Args:
        strategies: Sequence of ``(name, strategy)`` pairs; ``name`` is the
            engine / service the strategy authenticates.

    Returns:
        List of :class:`ProbeResult` in input order.
    """
    results: list[ProbeResult] = []
    for name, strat in strategies:
        if not strat.credentials_present():
            results.append(
                ProbeResult(
                    name=name, ok=False, identity=None, reason="credentials missing"
                )
            )
            continue
        outcome = strat.health_check()
        results.append(
            ProbeResult(
                name=name,
                ok=outcome.ok,
                identity=outcome.identity,
                reason=outcome.reason,
            )
        )
    return results


def write_snapshot(path: Path, results: Sequence[ProbeResult]) -> None:
    """Atomic snapshot write: ``path.tmp`` then ``os.replace`` to ``path``.

    Args:
        path: Destination path for the JSON snapshot.
        results: Sequence of :class:`ProbeResult` to serialise.
    """
    body = {
        "git_sha": _git_sha(),
        "captured_at": datetime.now().isoformat(),
        "strategies": [
            {
                "name": r.name,
                "ok": r.ok,
                "identity": r.identity,
                "reason": r.reason,
            }
            for r in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n")
    os.replace(tmp, path)


def check_bedrock_model_access(client: Any, model_id: str) -> ProbeResult:  # noqa: ANN401
    """Probe whether ``model_id`` is accessible to the caller.

    Args:
        client: A boto3 bedrock control-plane client (NOT bedrock-runtime).
        model_id: Bedrock foundation-model identifier
            (e.g. ``"amazon.nova-reel-v1:1"``).

    Returns:
        :class:`ProbeResult` with ``ok=True`` and ``identity=<model_id>``
        when the model appears in :py:meth:`list_foundation_models`,
        otherwise ``ok=False`` with a descriptive reason.
    """
    name = f"bedrock:{model_id}"
    try:
        resp = client.list_foundation_models()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            ok=False,
            identity=None,
            reason=f"list_foundation_models failed: {exc}",
        )
    listed = [m for m in resp.get("modelSummaries", []) if m.get("modelId") == model_id]
    if not listed:
        return ProbeResult(
            name=name,
            ok=False,
            identity=None,
            reason=f"model {model_id!r} not in list_foundation_models response",
        )
    return ProbeResult(name=name, ok=True, identity=model_id, reason=None)


def run(
    strategies: Sequence[tuple[str, AuthStrategy]],
    *,
    snapshot_path: Path | None = None,
    extra_checks: Sequence[tuple[str, Callable[[], ProbeResult]]] = (),
) -> int:
    """Probe all strategies and optionally write a snapshot.

    Args:
        strategies: Sequence of ``(name, strategy)`` pairs.
        snapshot_path: Optional path to write the JSON snapshot.
        extra_checks: Optional sequence of ``(label, callable)`` pairs for
            provider-specific checks (e.g. Bedrock model-access verification).
            Each callable returns a :class:`ProbeResult`.

    Returns:
        0 if all strategies pass; 1 if any fail.
    """
    results = probe_strategies(strategies)
    for _label, check in extra_checks:
        results.append(check())
    for r in results:
        if r.ok:
            print(f"PASS strategy={r.name} identity={r.identity}")
        else:
            print(f"FAIL strategy={r.name} reason={r.reason}")
    if snapshot_path is not None:
        write_snapshot(snapshot_path, results)
    return 0 if all(r.ok for r in results) else 1


def _load_strategies_from_config(config_path: Path) -> list[tuple[str, AuthStrategy]]:
    """Parse a kinoforge YAML config and instantiate every configured auth strategy.

    Args:
        config_path: Path to a kinoforge YAML config file.

    Returns:
        List of ``(engine_name, strategy)`` pairs for each engine block with an
        ``auth:`` sub-key containing a ``strategy:`` discriminator.
    """
    import yaml

    from kinoforge.core.auth import build_auth_strategy

    cfg = yaml.safe_load(config_path.read_text())
    strategies: list[tuple[str, AuthStrategy]] = []
    engine_block = cfg.get("engine", {})
    for engine_name, engine_cfg in engine_block.items():
        if not isinstance(engine_cfg, dict):
            continue
        auth_spec = engine_cfg.get("auth")
        if isinstance(auth_spec, dict):
            strategies.append((engine_name, build_auth_strategy(auth_spec)))
    return strategies


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:] when None).

    Returns:
        Exit code: 0 on all-pass, non-zero on any failure.
    """
    parser = argparse.ArgumentParser(
        description="kinoforge hosted-auth preflight probe"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="kinoforge YAML config"
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Where to write the JSON snapshot (default: tools/_snapshots/probe-<config-stem>.json)",
    )
    parser.add_argument(
        "--check-bedrock-model-access",
        default=None,
        metavar="MODEL_ID",
        help="In addition to AuthStrategy health, verify the given Bedrock model is listed",
    )
    args = parser.parse_args(argv)

    snapshot_path = (
        args.snapshot or Path("tools/_snapshots") / f"probe-{args.config.stem}.json"
    )
    strategies = _load_strategies_from_config(args.config)

    extra_checks: list[tuple[str, Callable[[], ProbeResult]]] = []
    if args.check_bedrock_model_access:
        import boto3  # noqa: PLC0415 — lazy

        # Use the first AWSSigV4 strategy's region if any was configured;
        # otherwise default to us-east-1.
        region = "us-east-1"
        for _, strat in strategies:
            if isinstance(strat, AWSSigV4):
                region = strat._region_name
                break
        client = boto3.Session().client("bedrock", region_name=region)
        model_id = args.check_bedrock_model_access
        extra_checks.append(
            (
                f"bedrock:{model_id}",
                lambda: check_bedrock_model_access(client, model_id),
            )
        )

    return run(strategies, snapshot_path=snapshot_path, extra_checks=extra_checks)


if __name__ == "__main__":
    sys.exit(main())
