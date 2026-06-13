"""B4 — end-to-end smoke for `kinoforge generate --instance-id`.

Verifies the warm-attach path skips create_instance and reuses the
operator-supplied pod. Pure offline; no cloud spend. Lives under
tests/live/ because it exercises the full CLI through cli.main([...])
not just one handler. No KINOFORGE_LIVE_TESTS gate because LocalProvider
keeps state in-process only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_FAKE_YAML = (
    "engine:\n  kind: fake\n  precision: fp16\n"
    "models:\n  - kind: base\n    name: m\n    ref: fake://m\n"
    "    target: checkpoints\n"
    "compute:\n  provider: local\n  image: kinoforge/local:latest\n"
    "  lifecycle:\n    idle_timeout: 1h\n    job_timeout: 30m\n"
    "    time_buffer: 30m\n    max_lifetime: 3h\n    budget: 10.0\n"
    "    heartbeat_interval_s: 30\n"
)


def test_full_cli_warm_attach_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Warm-attach path skips create_instance; reuses operator-supplied pod."""
    from kinoforge.cli import main
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.local import LocalProvider

    cfg_path = tmp_path / "fake.yaml"
    cfg_path.write_text(_FAKE_YAML)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)

    # Seed ledger with an entry whose cap_key matches the cfg's hash.
    sctx = SessionContext.from_args(state_dir=state, cfg_path=cfg_path)
    assert sctx.cfg is not None
    cap_hash = sctx.cfg.capability_key().derive()[:12]
    now = time.time()
    entry = {
        "id": "i-warm-smoke",
        "provider": "local",
        "created_at": now - 60.0,
        "cost_rate_usd_per_hr": 0.0,
        "last_heartbeat": now - 5.0,
        "heartbeat_thread_tick": now - 5.0,
        "tags": {"kinoforge_key": cap_hash},
    }
    ledger_path = state / "_lifecycle" / "ledger.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({"entries": [entry]}))

    fake = Instance(
        id="i-warm-smoke",
        provider="local",
        status="ready",
        created_at=now - 60.0,
        endpoints={},
        tags={"kinoforge_key": cap_hash},
    )

    create_spy = MagicMock(wraps=LocalProvider.create_instance)
    monkeypatch.setattr(LocalProvider, "create_instance", create_spy)
    # Make list/get report our seeded pod (LocalProvider keeps state in-
    # process only; fresh constructions otherwise see an empty roster).
    monkeypatch.setattr(LocalProvider, "list_instances", lambda self: [fake])
    monkeypatch.setattr(LocalProvider, "get_instance", lambda self, iid: fake)

    rc = main(
        [
            "--state-dir",
            str(state),
            "generate",
            "-c",
            str(cfg_path),
            "--prompt",
            "smoke test prompt",
            "--mode",
            "t2v",
            "--instance-id",
            "i-warm-smoke",
        ]
    )

    assert rc == 0, "warm-attach smoke should exit 0"
    assert create_spy.call_count == 0, "warm-attach must NOT call create_instance"
    out = capsys.readouterr().out
    assert "generated: uri=" in out
