"""Behavior: a warm attach uses a LIVE endpoint, not a recorded one.

Finding F11: the ledger branch replayed a dead process's 127.0.0.1:<port> and
the fallback handed an ``ssh://`` URL to an HTTP client — both branches wrong
for skypilot. Both are cured by asking the provider.
"""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import Instance


def _instance(provider: str, endpoints: dict[str, str]) -> Instance:
    return Instance(
        id="kf-warm-1",
        provider=provider,
        status="ready",
        created_at=0.0,
        endpoints=endpoints,
        tags={"ports": "8000"},
        cost_rate_usd_per_hr=0.0,
    )


def test_provider_answer_beats_the_recorded_row() -> None:
    """A live port replaces a recorded dead one.

    Bug caught: the exact F11 ledger branch — engine connects to a port whose
    ssh process died with the CLI that opened it.
    """
    from kinoforge.cli._commands import _resolve_warm_endpoints

    class _Sky:
        def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
            return {"8000": "http://127.0.0.1:60123"}

    resolved = _resolve_warm_endpoints(
        _Sky(),
        _instance("skypilot", {}),
        entry={"endpoints": {"8000": "http://127.0.0.1:1"}},
    )
    assert resolved == {"8000": "http://127.0.0.1:60123"}


def test_modal_recorded_url_survives_because_it_is_seeded_first() -> None:
    """Modal's non-rebuildable URL still replays (commit 1cb4299).

    Bug caught: calling the provider before seeding the recorded endpoints
    makes ModalProvider.endpoints fall back to an EMPTY map, and the warm
    attach dies with "has no endpoints".
    """
    from kinoforge.cli._commands import _resolve_warm_endpoints

    class _Modal:
        def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
            # Mirrors ModalProvider: no local deployment record in a fresh
            # process, so it echoes whatever the instance carries.
            return dict(instance.endpoints)

    url = "https://kinoforge-eph-8afe5ec6--srv.modal.run"
    resolved = _resolve_warm_endpoints(
        _Modal(), _instance("modal", {}), entry={"endpoints": {"8000": url}}
    )
    assert resolved == {"8000": url}


def test_provider_failure_falls_back_to_the_recording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A provider fault degrades to the old behaviour, loudly.

    Bug caught: an ensure_endpoints raising (ssh binary missing, creds
    expired) aborting a warm attach that the recorded URL could have served.
    """
    from kinoforge.cli._commands import _resolve_warm_endpoints

    class _Broken:
        def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
            raise RuntimeError("ssh: command not found")

    resolved = _resolve_warm_endpoints(
        _Broken(), _instance("skypilot", {}), entry={"endpoints": {"8000": "http://x"}}
    )
    assert resolved == {"8000": "http://x"}
    assert "ssh: command not found" in capsys.readouterr().err
