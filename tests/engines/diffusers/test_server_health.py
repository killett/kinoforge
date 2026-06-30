"""Tests for /health payload extension (T13).

Payload contract is additive: the legacy ``model`` field stays put so
older CLI tooling keeps working; ``models[]`` (per-pipeline state) and
``capabilities[]`` (derived from which loaders actually succeeded)
join it. Tests pin both the shape and the derivation rule so a future
edit can't silently switch ``capabilities`` from "actually loaded" to
"cfg intent" — the latter would let a half-failed provision lie about
its readiness.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def loaded_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Yield ``(srv, client)`` with a callable ``set_loaded(dict)``.

    Does NOT wrap the TestClient in ``with`` — startup is skipped so
    the ``ready`` event stays unset and ``_LOADED`` stays under test
    control. Module-level ``_LOADED`` and ``ready`` event are reset
    between tests so state never leaks.
    """
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    srv._LOADED.clear()
    srv.ready.clear()
    client = TestClient(srv.app)

    def set_loaded(entries: dict[str, dict[str, Any]]) -> None:
        srv._LOADED.clear()
        srv._LOADED.update(entries)  # type: ignore[arg-type]

    yield srv, client, set_loaded

    srv._LOADED.clear()
    srv.ready.clear()


def _entry(
    name: str,
    *,
    on_device: str = "cuda",
    vram_bytes: int = 0,
) -> dict[str, Any]:
    return {
        "name": name,
        "pipe": MagicMock(),
        "vram_bytes": vram_bytes,
        "last_used_monotonic": 0.0,
        "on_device": on_device,
    }


class TestLegacyShape:
    def test_model_field_preserved_as_hf_id(self, loaded_client: Any) -> None:
        # Bug caught: payload extension repurposes the ``model`` field
        # to a registry slug (e.g. "wan-t2v-a14b-fp8") — older CLI
        # tooling that compares it to MODEL_ID (the HF repo id) breaks
        # silently. Asserts the legacy contract literally.
        srv, client, set_loaded = loaded_client
        set_loaded({"wan-t2v-a14b-fp8": _entry("wan-t2v-a14b-fp8")})
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["model"] == srv.MODEL_ID

    def test_ready_reads_module_event_not_loaded_state(
        self, loaded_client: Any
    ) -> None:
        # Bug caught: someone derives ``ready`` from ``_LOADED`` membership
        # (e.g. "True iff Wan is on CUDA"), breaking the cold-boot wait
        # loop that drives the orchestrator's "is the server up yet"
        # poll. ``ready`` MUST track the module-level threading.Event,
        # not the registry. The test flips the event while _LOADED is
        # empty and confirms the payload follows the event.
        srv, client, set_loaded = loaded_client
        set_loaded({})  # registry empty
        assert client.get("/health").json()["ready"] is False
        srv.ready.set()
        assert client.get("/health").json()["ready"] is True


class TestModelsList:
    def test_models_is_list_one_entry_per_loaded(self, loaded_client: Any) -> None:
        # Bug caught: regression returns a single dict (the primary)
        # instead of a list → caller iteration breaks. List shape is
        # the contract the matcher's pre-flight (T14) reads.
        _srv, client, set_loaded = loaded_client
        set_loaded(
            {
                "wan-t2v-a14b-fp8": _entry("wan-t2v-a14b-fp8"),
                "seedvr2-3b-fp8": _entry("seedvr2-3b-fp8", on_device="cpu"),
            }
        )
        models = client.get("/health").json()["models"]
        assert isinstance(models, list)
        assert len(models) == 2
        for entry in models:
            assert {"name", "on_device", "ready"} <= set(entry.keys())

    def test_models_entry_reports_on_device_cpu_not_hardcoded(
        self, loaded_client: Any
    ) -> None:
        # Bug caught: a hardcoded ``"on_device": "cuda"`` everywhere
        # tricks the matcher into believing the pod is hot when the
        # LRU has actually evicted the pipeline to CPU → matcher picks
        # this pod, then the next call eats the CPU→CUDA move cost.
        _srv, client, set_loaded = loaded_client
        set_loaded({"seedvr2-3b-fp8": _entry("seedvr2-3b-fp8", on_device="cpu")})
        models = client.get("/health").json()["models"]
        assert len(models) == 1
        assert models[0]["on_device"] == "cpu"
        assert models[0]["ready"] is False  # ready iff on cuda


class TestCapabilities:
    def test_wan_only_yields_t2v(self, loaded_client: Any) -> None:
        # Bug caught: capabilities derives from cfg intent rather than
        # actually-loaded pipelines → a cold-boot where SeedVR2 was
        # planned but failed to download still reports "upscale" and
        # the matcher attaches an upscale job to a pod that can't run it.
        _srv, client, set_loaded = loaded_client
        set_loaded({"wan-t2v-a14b-fp8": _entry("wan-t2v-a14b-fp8")})
        assert client.get("/health").json()["capabilities"] == ["t2v", "upload"]

    def test_wan_and_seedvr2_yields_sorted_both(self, loaded_client: Any) -> None:
        # Bug caught: capabilities returned in insertion order rather
        # than sorted → the matcher's set-equality preflight (T14) sees
        # a list mismatch and re-cold-boots unnecessarily.
        _srv, client, set_loaded = loaded_client
        set_loaded(
            {
                "seedvr2-3b-fp8": _entry("seedvr2-3b-fp8"),
                "wan-t2v-a14b-fp8": _entry("wan-t2v-a14b-fp8"),
            }
        )
        assert client.get("/health").json()["capabilities"] == [
            "t2v",
            "upload",
            "upscale",
        ]

    def test_upscale_only_yields_upscale(self, loaded_client: Any) -> None:
        # Bug caught: a hardcoded ``"t2v"`` default in the capabilities
        # builder makes upscale-only pods misrepresent themselves as
        # T2V-capable. The matcher then dispatches a /generate call to
        # a pod with no Wan pipeline loaded.
        _srv, client, set_loaded = loaded_client
        set_loaded({"seedvr2-3b-fp8": _entry("seedvr2-3b-fp8")})
        assert client.get("/health").json()["capabilities"] == ["upload", "upscale"]

    def test_unknown_prefix_does_not_contribute_capability(
        self, loaded_client: Any
    ) -> None:
        # Bug caught: capabilities derives from ``_LOADED.keys()``
        # without a prefix filter, so any future model registered
        # under an unfamiliar prefix silently shows up as a stray
        # capability string — matcher logic that diff-checks against a
        # known capability vocabulary then explodes.
        _srv, client, set_loaded = loaded_client
        set_loaded(
            {
                "wan-t2v-a14b-fp8": _entry("wan-t2v-a14b-fp8"),
                "experimental-foo-2x": _entry("experimental-foo-2x"),
            }
        )
        caps = client.get("/health").json()["capabilities"]
        # unknown prefix ignored, not surfaced; "upload" is always advertised.
        assert caps == ["t2v", "upload"]


class TestUploadCapability:
    def test_health_advertises_upload_capability(self, loaded_client: Any) -> None:
        # Bug caught: /upload is always wired into the FastAPI app, but
        # the matcher reads capabilities[] to decide whether to attach
        # file:// upscale jobs to the pod. If "upload" never appears, the
        # matcher falls back to a (more expensive) cold-boot or refuses
        # the job. Tag must be present even when registry is empty.
        _srv, client, _set_loaded = loaded_client
        caps = client.get("/health").json()["capabilities"]
        assert "upload" in caps


class TestSpandrelCapability:
    def test_spandrel_prefix_yields_upscale_capability(
        self, loaded_client: Any
    ) -> None:
        # Bug caught: _capability_for_model misses the spandrel prefix
        # and a spandrel-only pod misrepresents itself as having no
        # capabilities (or as t2v-only). The matcher then refuses to
        # attach upscale jobs to it.
        _srv, client, set_loaded = loaded_client
        set_loaded({"spandrel-realesrgan-fp16": _entry("spandrel-realesrgan-fp16")})
        assert client.get("/health").json()["capabilities"] == ["upload", "upscale"]

    def test_spandrel_and_wan_yield_sorted_both(self, loaded_client: Any) -> None:
        # Bug caught: a multi-model pod (Wan T2V + spandrel upscaler) reports
        # only one capability because the loop short-circuits after the first
        # known prefix matches. Asserts the union appears, sorted.
        _srv, client, set_loaded = loaded_client
        set_loaded(
            {
                "wan-t2v-a14b-fp8": _entry("wan-t2v-a14b-fp8"),
                "spandrel-realesrgan-fp16": _entry("spandrel-realesrgan-fp16"),
            }
        )
        assert client.get("/health").json()["capabilities"] == [
            "t2v",
            "upload",
            "upscale",
        ]
