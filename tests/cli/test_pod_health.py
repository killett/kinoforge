"""``kinoforge.cli.pod_health.probe_pod_health`` — the CLI-side concrete
``/health`` probe injected into ``core.upscale_dir``.

Lives under ``tests/cli`` (not ``tests/core``) because the probe itself
lives under ``src/kinoforge/cli`` — ``core`` may never import an adapter
namespace (``kinoforge.engines``), so the concrete RunPod-proxy probe had
to move here (see ``tests/test_core_invariant.py::test_no_adapter_imports_in_core``).
"""

from __future__ import annotations

import time
import urllib.error
from email.message import Message
from typing import Any

import pytest

from kinoforge.cli import pod_health
from kinoforge.cli.pod_health import probe_pod_health


def test_transient_502_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bug caught: a routine RunPod-proxy startup-window 502 (the same race
    # kinoforge/engines/_pod_http.py documents, tolerated by every other
    # engine call via retry_proxy_call) raised straight through a bare
    # http_json call and was indistinguishable from a dead pod.
    calls = {"n": 0}

    def flaky_http_json(
        *, method: str, url: str, payload: Any, user_agent: str
    ) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 502, "bad gateway", Message(), None)
        return {"ok": True}

    monkeypatch.setattr(pod_health, "http_json", flaky_http_json)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    result = probe_pod_health("https://pod-1-8000.proxy.runpod.net/health", None)

    assert result == {"ok": True}
    assert calls["n"] == 2


def test_non_transient_http_error_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bug caught: a genuinely dead/misconfigured pod (e.g. a bare 500) gets
    # silently retried into oblivion instead of surfacing immediately —
    # retry_proxy_call only retries codes in its transient set
    # (404/502/503/504); a 500 must raise on the first attempt.
    def always_500(
        *, method: str, url: str, payload: Any, user_agent: str
    ) -> dict[str, Any]:
        raise urllib.error.HTTPError(url, 500, "internal error", Message(), None)

    monkeypatch.setattr(pod_health, "http_json", always_500)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        probe_pod_health("https://pod-1-8000.proxy.runpod.net/health", None)
    assert exc_info.value.code == 500
