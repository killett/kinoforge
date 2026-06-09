"""Tests for JsonProfileCache canonical redaction pattern.

Pins that any LoRA ref / label / prompt token registered by the active
vault never appears in a persisted profile JSON. Empty-registry runs are
passthrough (public-by-design path).
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kinoforge.core.interfaces import CapabilityKey, ModelProfile
from kinoforge.core.profiles import JsonProfileCache
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _key(name: str = "hf:org/base") -> CapabilityKey:
    return CapabilityKey(base_model=name, loras=(), engine="fake", precision="fp16")


def _profile(name: str = "fake") -> ModelProfile:
    return ModelProfile(
        name=name,
        max_frames=81,
        fps=16,
        supported_modes={"t2v"},
        max_resolution=(720, 480),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _persisted_text(tmp_path: Path, run_id: str, key: CapabilityKey) -> str:
    return next((tmp_path / run_id / "profiles").glob(f"{key.derive()}.*")).read_text()


def test_persist_passthrough_when_registry_empty(tmp_path: Path) -> None:
    """Public-by-design: empty registry → persisted JSON keeps original name."""
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="r1")
    key = _key()
    cache._persist(key, _profile(name="public-name"))
    text = _persisted_text(tmp_path, "r1", key)
    assert "public-name" in text


def test_persist_redacts_registered_lora_ref(tmp_path: Path) -> None:
    """A registered lora:ref token never appears in the persisted JSON.

    Would-fail-bug: persisting raw payload would leak the LoRA ref into
    profiles/<hash>.json on every cache write while a vault is loaded.
    """
    RedactionRegistry.instance().add("civitai:1234@5678", kind="lora:ref")
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="r1")
    # Profile.name carries the lora ref as a substring — represents the
    # human-readable label some engines stamp on the probe.
    key = _key()
    cache._persist(key, _profile(name="wan + civitai:1234@5678 fp16"))
    text = _persisted_text(tmp_path, "r1", key)
    assert "civitai:1234@5678" not in text
    assert "<lora:ref:" in text


def test_persist_redacts_registered_prompt_substring(tmp_path: Path) -> None:
    """Same path catches prompt:positive tokens even when accidentally
    interpolated into a probe's display name."""
    RedactionRegistry.instance().add("secret-prompt-body", kind="prompt:positive")
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="r1")
    key = _key()
    cache._persist(key, _profile(name="probe:secret-prompt-body"))
    text = _persisted_text(tmp_path, "r1", key)
    assert "secret-prompt-body" not in text


def test_persist_writes_valid_json_after_redaction(tmp_path: Path) -> None:
    """Redacted output must remain a load-able JSON dict — redact_json
    cannot break the serialised shape.

    Would-fail-bug: returning a str instead of dict from redact_json would
    make the persisted file fail to round-trip through put_json → get_json.
    """
    RedactionRegistry.instance().add("civitai:1234@5678", kind="lora:ref")
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="r1")
    key = _key()
    cache._persist(key, _profile(name="wan + civitai:1234@5678 fp16"))
    text = _persisted_text(tmp_path, "r1", key)
    payload = json.loads(text)
    assert isinstance(payload, dict)
    assert payload["max_frames"] == 81
