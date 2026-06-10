"""Provision marker alias-keying under `--ephemeral` (spec addendum
``docs/superpowers/specs/2026-06-10-provision-marker-alias-keying-design.md``).

The `.provisioned` marker stored the raw ``CapabilityKey.derive()`` hash
in its ``capability_key`` field. That hash is "a fingerprint of secret
material" per the parent ephemeral spec §1. These tests pin the new
behavior: under STRICT + vault, the marker stores
``compute_profile_alias(cfg, vault)`` (the same alias the profile cache
is supposed to use per Appendix A); under DEFAULT or STRICT-without-vault
it falls back to the raw hash so today's behavior is preserved.

All offline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.provision_state import (
    is_marker_current,
    marker_path,
    read_marker,
    write_marker,
)


@dataclass
class _StubCapabilityKey:
    """Pure-stub for ``cfg.capability_key()``.

    ``derive()`` returns the constructor-supplied hash string verbatim
    — the tests want a known-distinct value to differentiate from the
    alias output without dragging Config wiring in.
    """

    derived: str

    def derive(self) -> str:
        return self.derived


@dataclass
class _StubCfg:
    """Minimal cfg satisfying both `.capability_key()` and the structural
    shape `compute_profile_alias` expects (`.models`, `.engine.kind`,
    `.engine.precision`).
    """

    derived_hash: str
    models_entries: list[Any]
    engine_kind: str = "comfyui"
    engine_precision: str = "fp16"

    def capability_key(self) -> _StubCapabilityKey:
        return _StubCapabilityKey(self.derived_hash)

    @property
    def models(self) -> list[Any]:
        return self.models_entries

    @property
    def engine(self) -> Any:
        return _StubEngine(self.engine_kind, self.engine_precision)


@dataclass
class _StubEngine:
    kind: str
    precision: str


@dataclass
class _StubModelEntry:
    kind: str
    ref: str


def _make_cfg() -> _StubCfg:
    """Construct a stub cfg with a base model so `compute_profile_alias`
    has a non-empty `base_ref` to hash over.
    """
    return _StubCfg(
        derived_hash="raw_capability_key_hex_aaaaaaaaaaaaaaaa",
        models_entries=[_StubModelEntry(kind="base", ref="hf:fake/base-model")],
    )


def _make_vault_with_alias(alias: str = "my-vault-alias") -> Any:
    """Build a Vault model with an explicit alias for deterministic
    comparison."""
    from kinoforge.core.vault import Vault

    return Vault(positive_prompt="hello", alias=alias)


def _make_vault_no_alias() -> Any:
    """Build a Vault model with no explicit alias — exercises the
    `cfg-<sha12>` derivation fallback in `compute_profile_alias`.
    """
    from kinoforge.core.vault import Vault

    return Vault(positive_prompt="hello")


# ---------------------------------------------------------------------------
# AC1: marker_key_for STRICT + vault returns alias
# ---------------------------------------------------------------------------


def test_marker_key_for_strict_with_vault_returns_alias() -> None:
    """``marker_key_for(cfg)`` inside a STRICT EphemeralSession that
    carries a vault must return the vault alias, NOT the raw
    `CapabilityKey.derive()` hash.

    Catches: a regression where the marker fell back to raw hash under
    STRICT, leaking a "fingerprint of secret material" to disk.
    """
    from kinoforge.core.provision_state import marker_key_for

    cfg = _make_cfg()
    vault = _make_vault_with_alias("my-vault-alias")

    with EphemeralSession(enabled=True, vault=vault):
        seen = marker_key_for(cfg)

    assert seen == "my-vault-alias", (
        f"marker_key_for returned {seen!r}; expected the vault alias. "
        f"Under STRICT + vault the marker MUST use the alias so the "
        f"on-disk file does not carry the raw capability_key hash."
    )
    assert seen != cfg.capability_key().derive(), (
        "marker_key_for returned the raw derive() hash under STRICT + vault — "
        "the spec amendment's whole point is to avoid that."
    )


# ---------------------------------------------------------------------------
# AC2: DEFAULT mode unchanged
# ---------------------------------------------------------------------------


def test_marker_key_for_default_mode_returns_raw_derive_hash() -> None:
    """``marker_key_for(cfg)`` with NO active EphemeralSession returns
    the raw ``cfg.capability_key().derive()`` hex — today's behavior.

    Catches: a regression where the alias-key path fired for default
    runs and broke warm-reuse on non-ephemeral invocations.
    """
    from kinoforge.core.provision_state import marker_key_for

    cfg = _make_cfg()
    # No EphemeralSession active.
    seen = marker_key_for(cfg)
    assert seen == cfg.capability_key().derive(), (
        f"marker_key_for returned {seen!r}; expected raw derive() hash "
        f"({cfg.capability_key().derive()!r}) outside any EphemeralSession."
    )


# ---------------------------------------------------------------------------
# AC3: STRICT without vault falls back to raw hash
# ---------------------------------------------------------------------------


def test_marker_key_for_strict_without_vault_falls_back_to_raw_hash() -> None:
    """STRICT mode without a vault returns the raw derived hash.

    Operator may run `kinoforge --ephemeral generate ...` without a
    vault for non-content-sensitive runs. With no vault there is no
    alias-derivation source, so the marker uses the raw hash. Not a
    leak per the spec privacy framing (no vault → no sensitive material
    in scope).
    """
    from kinoforge.core.provision_state import marker_key_for

    cfg = _make_cfg()

    with EphemeralSession(enabled=True, vault=None):
        seen = marker_key_for(cfg)

    assert seen == cfg.capability_key().derive(), (
        f"marker_key_for returned {seen!r}; expected the raw derive() "
        f"fallback when STRICT mode is active but no vault was provided."
    )


# ---------------------------------------------------------------------------
# AC4: warm-reuse round-trip under STRICT + vault
# ---------------------------------------------------------------------------


def test_write_marker_then_is_marker_current_round_trip_strict_with_vault(
    tmp_path: Path,
) -> None:
    """Two back-to-back STRICT sessions with the same cfg + same vault
    hit the marker-current branch on the second read.

    This is the warm-reuse roadmap roundtrip the parent spec's 2026-06-10
    changelog explicitly protects.
    """
    from kinoforge.core.provision_state import marker_key_for

    cfg = _make_cfg()
    vault = _make_vault_with_alias("warm-reuse-alias")
    instance_id = "abc123pod"
    state_dir = tmp_path
    path = marker_path(state_dir, instance_id)

    # Session 1 — write the marker under STRICT.
    with EphemeralSession(enabled=True, vault=vault):
        key_1 = marker_key_for(cfg)
        write_marker(
            path,
            instance_id=instance_id,
            capability_key=key_1,
            engine_name="comfyui",
            timestamp=time.time(),
        )

    # Session 2 — fresh STRICT session, same cfg + same vault.
    with EphemeralSession(enabled=True, vault=vault):
        key_2 = marker_key_for(cfg)
        marker = read_marker(path)

    assert marker is not None, "session 2 could not read the marker session 1 wrote"
    assert is_marker_current(marker, key_2), (
        "warm-reuse round-trip failed under STRICT + vault — session 2's "
        "alias did not match session 1's. The vault-driven derivation "
        "must be deterministic across sessions."
    )


# ---------------------------------------------------------------------------
# AC5: marker schema unchanged
# ---------------------------------------------------------------------------


def test_default_mode_marker_payload_unchanged_regression_lock(
    tmp_path: Path,
) -> None:
    """Marker written under DEFAULT mode keeps the exact field shape
    pre-spec — no field renames, no new fields, same JSON layout.

    Catches: a refactor that adds the alias as a NEW field next to
    `capability_key` and breaks every existing reader.
    """
    cfg = _make_cfg()
    instance_id = "default-mode-pod"
    state_dir = tmp_path
    path = marker_path(state_dir, instance_id)

    # No EphemeralSession active — DEFAULT path.
    write_marker(
        path,
        instance_id=instance_id,
        capability_key=cfg.capability_key().derive(),
        engine_name="comfyui",
        timestamp=1234.5,
    )

    raw = json.loads(path.read_text())
    assert set(raw.keys()) == {
        "instance_id",
        "capability_key",
        "engine",
        "timestamp",
    }, (
        f"DEFAULT marker shape regressed; keys={set(raw.keys())!r} != "
        f"the documented four-key payload"
    )
    assert raw["capability_key"] == cfg.capability_key().derive()


# ---------------------------------------------------------------------------
# Cross-mode mismatch yields stale (acceptable — false-stale is safe)
# ---------------------------------------------------------------------------


def test_strict_marker_treated_stale_when_read_under_default(
    tmp_path: Path,
) -> None:
    """Cross-mode read: a marker written under STRICT (alias) is
    treated as stale when read by a DEFAULT session (raw hash) — safe
    behavior (false-stale → re-provision; never a false-current).
    """
    from kinoforge.core.provision_state import marker_key_for

    cfg = _make_cfg()
    vault = _make_vault_with_alias()
    instance_id = "cross-mode-pod"
    state_dir = tmp_path
    path = marker_path(state_dir, instance_id)

    with EphemeralSession(enabled=True, vault=vault):
        write_marker(
            path,
            instance_id=instance_id,
            capability_key=marker_key_for(cfg),  # alias
            engine_name="comfyui",
            timestamp=time.time(),
        )

    # No EphemeralSession active — DEFAULT mode read.
    default_key = marker_key_for(cfg)  # raw hash
    marker = read_marker(path)
    assert marker is not None
    assert not is_marker_current(marker, default_key), (
        "STRICT-written marker should be treated as stale by a DEFAULT "
        "session — but is_marker_current returned True, which would "
        "incorrectly skip re-provision."
    )
