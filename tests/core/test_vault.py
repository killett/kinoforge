"""Tests for core.vault — vault file load, validation, alias derivation."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import (
    VaultEmptyError,
    VaultParseError,
    VaultPathError,
    VaultUnderRepoError,
)
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.core.vault import (
    Vault,
    VaultLoRA,
    VaultSegment,
    compute_profile_alias,
    load_vault,
    register_vault_tokens,
)

_MINIMAL_FAKE_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
"""


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _write_vault(tmp_path: Path, content: dict[str, Any]) -> Path:
    p = tmp_path / "vault.yaml"
    p.write_text(yaml.safe_dump(content))
    p.chmod(0o600)
    return p


def _minimal_config() -> Config:
    return load_config(_MINIMAL_FAKE_YAML)


def _outside_repo(tmp_path: Path) -> Path:
    return tmp_path.parent.parent / "somewhere-else"


def test_vault_positive_prompt_only_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    p = _write_vault(tmp_path, {"positive_prompt": "Cinematic shot of a sunrise"})
    v = load_vault(p)
    assert v.positive_prompt == "Cinematic shot of a sunrise"
    assert v.segments is None


def test_vault_segments_only_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    p = _write_vault(
        tmp_path,
        {"segments": [{"prompt": "wide shot"}, {"prompt": "close-up"}]},
    )
    v = load_vault(p)
    assert v.positive_prompt is None
    assert v.segments is not None
    assert len(v.segments) == 2
    assert v.segments[0].prompt == "wide shot"


def test_vault_both_positive_and_segments_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly-one-of validator catches the both-populated case."""
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    p = _write_vault(
        tmp_path,
        {
            "positive_prompt": "wide shot",
            "segments": [{"prompt": "close-up"}],
        },
    )
    with pytest.raises(VaultParseError, match="exactly one"):
        load_vault(p)


def test_vault_neither_positive_nor_segments_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty vault is an error — caller asked for confidentiality of WHAT?"""
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    p = _write_vault(tmp_path, {"negative_prompt": "blurry"})
    with pytest.raises(VaultEmptyError):
        load_vault(p)


def test_vault_extra_keys_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extra='forbid' — unknown top-level keys fail load."""
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    p = _write_vault(tmp_path, {"positive_prompt": "ok", "unknown_key": "x"})
    with pytest.raises(VaultParseError):
        load_vault(p)


def test_vault_alias_regex_lowercase_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alias must match ^[a-z0-9][a-z0-9-]{0,63}$ — uppercase rejected."""
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    p = _write_vault(tmp_path, {"positive_prompt": "ok", "alias": "BadAlias"})
    with pytest.raises(VaultParseError):
        load_vault(p)


def test_vault_path_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(VaultPathError):
        load_vault(tmp_path / "nonexistent.yaml")


def test_vault_under_repo_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vault resolved under the active git repo root is a hard error
    (otherwise the user might accidentally commit it). Would-fail-bug: not
    consulting `git rev-parse` would let a vault under the repo silently
    succeed."""
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    monkeypatch.setattr("kinoforge.core.vault._git_repo_root", lambda: tmp_path)
    with pytest.raises(VaultUnderRepoError, match=str(tmp_path)):
        load_vault(p)


def test_vault_outside_repo_passes_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    v = load_vault(p)
    assert v.positive_prompt == "ok"


def test_vault_outside_repo_when_no_git_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When not inside a git repo, the repo-root check is skipped."""
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    monkeypatch.setattr("kinoforge.core.vault._git_repo_root", lambda: None)
    v = load_vault(p)
    assert v.positive_prompt == "ok"


def test_vault_world_readable_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    p.chmod(0o644)
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root", lambda: _outside_repo(tmp_path)
    )
    with caplog.at_level("WARNING", logger="kinoforge.core.vault"):
        load_vault(p)
    assert any("chmod 600" in r.message for r in caplog.records)


def test_compute_alias_no_vault_uses_capability_hash() -> None:
    """Backward compat: no vault → existing CapabilityKey.derive() hash."""
    cfg = _minimal_config()
    alias_a = compute_profile_alias(cfg, vault=None)
    alias_b = compute_profile_alias(cfg, vault=None)
    assert alias_a == alias_b
    assert not alias_a.startswith("cfg-")


def test_compute_alias_explicit_override_wins() -> None:
    cfg = _minimal_config()
    v = Vault(positive_prompt="ok", alias="my-vault-id")
    assert compute_profile_alias(cfg, v) == "my-vault-id"


def test_compute_alias_auto_derive_stable_and_order_sensitive() -> None:
    """Auto-derived alias is sha256-based over canonical-JSON over
    (base, loras, engine, precision). LoRA order matters."""
    cfg = _minimal_config()
    v1 = Vault(
        positive_prompt="ok", loras=[VaultLoRA(ref="aaaa"), VaultLoRA(ref="bbbb")]
    )
    v2 = Vault(
        positive_prompt="ok", loras=[VaultLoRA(ref="aaaa"), VaultLoRA(ref="bbbb")]
    )
    v3 = Vault(
        positive_prompt="ok", loras=[VaultLoRA(ref="bbbb"), VaultLoRA(ref="aaaa")]
    )
    assert compute_profile_alias(cfg, v1) == compute_profile_alias(cfg, v2)
    assert compute_profile_alias(cfg, v1) != compute_profile_alias(cfg, v3)
    assert compute_profile_alias(cfg, v1).startswith("cfg-")
    assert len(compute_profile_alias(cfg, v1)) == 4 + 12  # "cfg-" + 12 hex


def test_register_vault_tokens_registers_all_sensitive_strings() -> None:
    v = Vault(
        positive_prompt="positive body",
        negative_prompt="negative body",
        loras=[VaultLoRA(ref="civitai:1234@5678", label="my-style")],
    )
    register_vault_tokens(v)
    r = RedactionRegistry.instance()
    out = r.redact(
        "got positive body and negative body and civitai:1234@5678 and my-style"
    )
    assert "positive body" not in out
    assert "negative body" not in out
    assert "civitai:1234@5678" not in out
    assert "my-style" not in out


def test_vault_segments_construct_via_pydantic() -> None:
    """VaultSegment accepts prompt + optional params."""
    seg = VaultSegment(prompt="hello", params={"frames": 81})
    assert seg.prompt == "hello"
    assert seg.params == {"frames": 81}
