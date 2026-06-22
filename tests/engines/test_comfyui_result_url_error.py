"""Cover the new URLError absorption in ComfyUIBackend.result().

Task 25: new dedicated test for the latent-bug closure introduced in
Task 24. Before Task 24, a TLS reset or socket error during /history
polling would propagate as URLError to the caller. After Task 24,
result() absorbs RUNPOD_PROXY_POLICY.catch_classes errors (URLError,
OSError) per the shared helper contract and continues polling.
"""

from __future__ import annotations

import urllib.error
from typing import Any

from kinoforge.core.interfaces import Artifact, ModelProfile
from kinoforge.engines.comfyui import ComfyUIBackend
from tests.engines.conftest import _load_comfy_fixture

# ---------------------------------------------------------------------------
# Fixture helpers (mirror test_comfyui.py conventions)
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="comfyui-test",
    max_frames=24,
    fps=8,
    supported_modes={"t2v"},
    max_resolution=(1024, 576),
    supports_native_extension=False,
    supports_joint_audio=False,
)

_EMPTY_QUEUE: dict[str, Any] = {"queue_running": [], "queue_pending": []}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_comfyui_result_absorbs_url_error() -> None:
    """Catches: comfyui result() crashes on TLS reset (the latent bug).

    Before Task 24 the URLError propagated immediately to the caller.
    After Task 24 the shared retry helper absorbs it and polling continues.

    Arrange: http_get raises URLError("Connection reset") on the first
    two /history calls, then returns a done-shaped response.
    Act: call backend.result(prompt_id).
    Assert: no exception; the returned value is an Artifact; http_get was
    called more than twice (proves the URLError did not terminate the poll).

    Bug it catches: regression where RUNPOD_PROXY_POLICY.catch_classes
    branch is removed from result(), causing URLError to escape.
    """
    _DONE_FIXTURE = _load_comfy_fixture("history_done.json")
    _PROMPT_ID = next(iter(_DONE_FIXTURE))

    history_calls: list[str] = []

    def get_spy(url: str) -> dict[str, Any]:
        if url.endswith("/queue"):
            return _EMPTY_QUEUE
        history_calls.append(url)
        n = len(history_calls)
        if n <= 2:
            raise urllib.error.URLError(
                ConnectionResetError(104, "Connection reset by peer")
            )
        return _DONE_FIXTURE

    backend = ComfyUIBackend(
        http_post=lambda u, b: {},
        http_get=get_spy,
        base_url="http://test-pod-8188.proxy.example.net",
        probe=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result(_PROMPT_ID)

    assert isinstance(artifact, Artifact), (
        "result() should return an Artifact after absorbing transport errors"
    )
    assert len(history_calls) > 2, (
        f"expected >2 /history calls (URLError must be absorbed and polling must "
        f"continue), got {len(history_calls)}"
    )
