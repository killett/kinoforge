"""``_resolve_attach_pod`` must merge the ledger's endpoints, not only its tags.

``kinoforge upscale --attach-pod <id>`` is the explicit escape hatch for
reusing a warm GPU instead of paying a fresh boot.  The gate copied the
ledger entry's ``tags`` onto the live instance and then asked
``provider.ensure_endpoints`` for a URL — never reading the entry's
sibling ``endpoints`` field.  On a provider whose ``get_instance`` cannot
carry a URL (Modal builds the Instance from ``modal app list``, and
``ModalProvider.endpoints`` falls back to a per-process ``_deployments``
dict a fresh CLI process never populated), that left the provider with
nothing to work from, so a warm A100-80GB that had published an artifact
ninety seconds earlier was refused — with a message that offered the
*tag* keys as evidence about *endpoints*.

The tests here pin: the ledger's endpoints reach the provider, live
values still win on collision (matching the tag-merge precedence next to
them), the tag merge itself is untouched, and the refusal — when it is
genuinely earned — reports what was actually checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.fake  # noqa: F401 — registers the `fake` engine
import kinoforge.providers.local  # noqa: F401 — registers the `local` provider
import kinoforge.sources.http  # noqa: F401 — registers the `https` source
from kinoforge.cli._commands import _cfg_warm_attach_key, _resolve_attach_pod
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Instance

_CFG_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
"""

_LEDGER_URL = "https://kinoforge-build-deadbeef.modal.invalid"


class _EchoingProvider:
    """Provider whose only endpoint knowledge is what the caller hands it.

    This is Modal's shape, not a convenience stub: ``get_instance``
    rebuilds the Instance from a listing that carries no URL, and
    ``endpoints`` degrades to ``dict(instance.endpoints)`` when the
    per-process deployment cache is cold.  ``ensure_endpoints`` is the ABC
    default — the plain read.
    """

    def __init__(self, live: Instance) -> None:
        self._live = live
        self.seen_endpoints: dict[str, str] | None = None
        self.seen_tags: dict[str, str] | None = None

    def get_instance(self, instance_id: str) -> Instance:
        return self._live

    def endpoints(self, instance: Instance) -> dict[str, str]:
        self.seen_endpoints = dict(instance.endpoints)
        self.seen_tags = dict(instance.tags)
        return dict(instance.endpoints)

    def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
        return self.endpoints(instance)


def _make_ctx(tmp_path: Path) -> SessionContext:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_CFG_YAML)
    cfg = load_config(cfg_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return SessionContext(state_dir=state_dir, cfg=cfg, sidecar=None)


def _instance(
    *,
    iid: str = "pod-warm",
    endpoints: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
    status: str = "ready",
) -> Instance:
    return Instance(
        id=iid,
        provider="local",
        status=status,
        created_at=0.0,
        endpoints=dict(endpoints or {}),
        tags=dict(tags or {}),
        cost_rate_usd_per_hr=0.0,
    )


def _seed(
    ctx: SessionContext,
    *,
    recorded: Instance,
) -> None:
    """Record ``recorded`` and stamp the cfg's warm-attach key onto it."""
    ledger = ctx.ledger()
    ledger.record(recorded)
    cfg = ctx.cfg
    assert cfg is not None
    ledger.touch(recorded.id, warm_attach_key=_cfg_warm_attach_key(cfg))


def _install(
    monkeypatch: pytest.MonkeyPatch, provider: _EchoingProvider
) -> _EchoingProvider:
    from kinoforge.core import registry

    def _factory() -> Any:
        return provider

    monkeypatch.setattr(registry, "get_provider", lambda _name: _factory)
    return provider


def test_attach_pod_uses_the_endpoints_the_ledger_is_holding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger's endpoints must survive the merge.

    Bug caught: the merge copied ``tags`` and never ``endpoints``, so on a
    provider whose ``get_instance`` returns no URL (Modal), ``--attach-pod``
    refused a warm A100 that was answering ``/util`` at that moment, with a
    message blaming the tag keys.
    """
    ctx = _make_ctx(tmp_path)
    _seed(ctx, recorded=_instance(endpoints={"8000": _LEDGER_URL}))
    provider = _install(monkeypatch, _EchoingProvider(_instance(endpoints={}, tags={})))

    cfg = ctx.cfg
    assert cfg is not None
    resolved, rc = _resolve_attach_pod(ctx, cfg, "pod-warm")

    assert rc is None
    assert resolved is not None
    assert resolved.endpoints == {"8000": _LEDGER_URL}
    # The provider must be handed the recorded URL — it is the thing it
    # is being asked to repair.  Asserting only on the return value would
    # pass for an implementation that patched the endpoints back on
    # *after* ensure_endpoints, which is the wrong seam: a provider that
    # can rebuild a fresher URL would never see the seed.
    assert provider.seen_endpoints == {"8000": _LEDGER_URL}


def test_live_endpoints_win_over_the_ledgers_on_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledger values are a hint; the live probe is the newer truth.

    Bug caught: merging ledger-over-live (or replacing the live map
    wholesale with the recorded one) would hand the engine a URL from a
    previous boot while the provider was reporting the current one — a
    stale endpoint that fails at request time rather than at attach time.
    """
    ctx = _make_ctx(tmp_path)
    _seed(
        ctx,
        recorded=_instance(
            endpoints={"8000": "https://stale.invalid", "8001": "https://logs.invalid"}
        ),
    )
    _install(
        monkeypatch,
        _EchoingProvider(_instance(endpoints={"8000": "https://fresh.invalid"})),
    )

    cfg = ctx.cfg
    assert cfg is not None
    resolved, rc = _resolve_attach_pod(ctx, cfg, "pod-warm")

    assert rc is None
    assert resolved is not None
    assert resolved.endpoints["8000"] == "https://fresh.invalid"
    # A ledger-only port survives: this is a merge, not a pick-one.
    assert resolved.endpoints["8001"] == "https://logs.invalid"


def test_the_ledger_tag_merge_still_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoints merge must not displace the tag merge beside it.

    Bug caught: RunPod's sparse pod query drops ``tags["ports"]``, and
    ``RunPodProvider.endpoints`` rebuilds its proxy hostname from that
    tag.  Losing the tag merge while adding the endpoints merge would
    trade one silent endpoint failure for another.
    """
    ctx = _make_ctx(tmp_path)
    _seed(
        ctx,
        recorded=_instance(
            endpoints={"8000": _LEDGER_URL},
            tags={"ports": "8000/http", "mode": "stale"},
        ),
    )
    provider = _install(
        monkeypatch,
        _EchoingProvider(_instance(endpoints={}, tags={"mode": "pod"})),
    )

    cfg = ctx.cfg
    assert cfg is not None
    resolved, rc = _resolve_attach_pod(ctx, cfg, "pod-warm")

    assert rc is None
    assert resolved is not None
    assert resolved.tags["ports"] == "8000/http"
    assert resolved.tags["mode"] == "pod"  # live still wins
    assert provider.seen_tags == {"ports": "8000/http", "mode": "pod"}


def test_refusal_reports_the_endpoints_it_checked_not_the_tag_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An earned refusal must cite endpoints, never tag keys.

    Bug caught: the message printed ``ledger tag keys=[...]`` as evidence
    about a missing *endpoint*.  That is a different field, and reading it
    as endpoint evidence is what forced the live diagnosis to be filed as
    a hypothesis instead of a cause.
    """
    ctx = _make_ctx(tmp_path)
    _seed(
        ctx,
        recorded=_instance(endpoints={}, tags={"kinoforge_key": "abc123deadbeef"}),
    )
    _install(monkeypatch, _EchoingProvider(_instance(endpoints={})))

    cfg = ctx.cfg
    assert cfg is not None
    resolved, rc = _resolve_attach_pod(ctx, cfg, "pod-warm")

    assert resolved is None
    assert rc == 1
    err = capsys.readouterr().err
    assert "pod-warm" in err
    assert "endpoint" in err
    # The tag payload is not evidence about endpoints and must not be
    # offered as such.
    assert "tag" not in err
    assert "kinoforge_key" not in err
