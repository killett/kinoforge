# Provision marker alias-keying under `--ephemeral` — design addendum

**Date:** 2026-06-10
**Parent spec:** `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md`
**Status:** untracked draft — awaiting Dr. Twinklebrane spec review

---

## 1. What the parent spec missed

Live audit on 2026-06-09 of a successful `kinoforge --ephemeral generate`
run against a RunPod ComfyUI pod surfaced two on-disk files that survive
`EphemeralSession.__exit__`:

```
<state_dir>/instances/<pod_id>/.provisioned
<state_dir>/_locks/provision:<pod_id>.lock
```

Neither file appears in the parent spec's Appendix A (Full state matrix)
nor in the §10.4 self-hosted-engine handling section. Both files are
written by `core/orchestrator.py:_provision_compute_once`
(line ~213) at every provision — gated by no ephemeral policy field.

### Re-classifying the two files against the §1 privacy surface

The parent spec frames the sensitive surface as "prompts, negative
prompts, LoRA references, LoRA filenames, **derived hashes**."

**`.provisioned` marker** — partial leak.

* **Existence** of the file: NOT a leak. The marker is the warm-reuse
  skip-provision signal. Deleting it on `__exit__` would defeat exactly
  what the 2026-06-10 changelog protects (`PROGRESS B5→B3` roadmap).
* **Contents** of the file are the leak. Today's marker payload:

  ```json
  {"instance_id": "<pod_id>", "capability_key": "<raw sha256 hex>",
   "engine": "<kind>", "timestamp": <float>}
  ```

  The `capability_key` field is `CapabilityKey.derive()` over
  `[base_model, loras, engine, precision]` (`core/interfaces.py:227`).
  That hash IS a "derived hash" per §1 and a "fingerprint of secret
  material" — exactly the class of identifier Appendix A already
  alias-keys for the profile cache (`profiles/<alias>.json`).

**`_locks/provision:<pod_id>.lock`** — NOT a leak under §1.

* `pod_id` in filename: operator-known compute resource ID; not in the
  prompt + LoRA + derived-hash surface.
* Contents: nonce + holder_pid + expires_at. No prompt-derived material.
* File is needed for cross-process safety when warm-reuse session 2
  attaches to the same pod. Deleting it on `__exit__` would re-introduce
  the race the lock prevents.

No fix required for the lock file. The remainder of this spec addresses
the marker only.

## 2. Proposed fix — alias-key the marker payload under STRICT

Mirror the Appendix A precedent for the profile cache:

| Surface | Default | `--ephemeral` |
|---|---|---|
| Profile cache key (Appendix A row) | Raw `capability_key` | Alias-keyed |
| `.provisioned` marker `capability_key` field (**this spec**) | Raw `capability_key` | **Alias-keyed** |

Concretely:

* Under DEFAULT (no `EphemeralSession`, or session without vault) the
  marker stores `cfg.capability_key().derive()` as today — no behavior
  change.
* Under STRICT (`EphemeralSession.policy.delete_on_completion=True`)
  with a vault loaded, the marker stores
  `compute_profile_alias(cfg, vault)` — the vault's explicit alias or
  the deterministic `cfg-<sha256[:12]>` fallback derived only from
  `{base, loras, engine, precision}` already in scope for `Appendix A`.
* Warm-reuse session 2 (still in STRICT mode, same vault, same cfg):
  derives the same alias deterministically, reads the marker, compares,
  matches → skips re-provision. Roundtrip preserved.
* Cross-mode (DEFAULT after STRICT or vice-versa): markers don't match;
  marker treated as stale → re-provision. Safe; no incorrect skip.

## 3. Vault threading

Today's CLI loads the vault at `_main.py:437` via `_load_vault_or_none`
then discards it after `register_vault_tokens` runs. The orchestrator
has no path to the vault from inside `provision_state.write_marker`.

The cleanest threading channel is the existing `EphemeralSession`
context manager. Both pieces are already in scope at the same call
site, both are scoped to the lifetime of one `kinoforge` invocation,
and the session is already consulted at the relevant write sites
(`core/profiles.py:246`, `core/lifecycle.py:455`, etc.).

```python
class EphemeralSession:
    def __init__(self, *, enabled: bool, vault: Vault | None = None) -> None:
        ...
        self.vault = vault
```

CLI wiring (`cli/_main.py:490`):

```python
with EphemeralSession(enabled=args.ephemeral, vault=loaded_vault):
    return _DISPATCH[args.cmd](args, ctx)
```

`loaded_vault` is the existing `_load_vault_or_none(args.vault)` return
value — already computed at line 437, just retained.

## 4. New helper — `marker_key_for(cfg)`

A single derivation function consulted at both the write site and the
read-comparison site:

```python
# core/provision_state.py
def marker_key_for(cfg: Any) -> str:
    """Return the cache key the .provisioned marker should compare against.

    Under STRICT + vault: returns ``compute_profile_alias(cfg, vault)``.
    Otherwise: returns ``cfg.capability_key().derive()`` (today's behavior).

    Centralising this here keeps write_marker / is_marker_current in
    lockstep — a future maintainer changing one without the other would
    produce silently-stale markers (always treated as 'not provisioned'
    → unnecessary re-provision on every run).
    """
    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.vault import compute_profile_alias

    session = EphemeralSession.current()
    if (
        session is not None
        and session.policy.delete_on_completion
        and session.vault is not None
    ):
        return compute_profile_alias(cfg, session.vault)
    return cfg.capability_key().derive()
```

Call sites in `core/orchestrator.py:_provision_compute_once`:

```python
# Was: capability_key_hex = cfg.capability_key().derive()
capability_key_hex = marker_key_for(cfg)
```

Pure function on the cfg; the EphemeralSession lookup is per-process
state. Zero additional wiring across the orchestrator.

## 5. Backward compatibility

The on-disk marker schema is unchanged — same field names, same shape.
The VALUE of `capability_key` field changes under STRICT only.

* DEFAULT run produces a marker with raw hash (today's value); a
  subsequent STRICT session sees mismatch → re-provisions; subsequent
  STRICT writes the alias. No corruption, just one round of warm-reuse
  forgone on the mode transition.
* STRICT run produces a marker with alias; a subsequent DEFAULT session
  sees mismatch → re-provisions. Symmetric.
* `is_marker_current` already does string equality — no change there.
* `read_marker` returns `dict | None`, same shape. No reader breaks.

## 6. Out of scope

* **`compute_profile_alias` integration into `JsonProfileCache`.** The
  Layer 5b plan listed this as Task 4 deliverable but no live code path
  calls `compute_profile_alias` today. Profile cache still keys on the
  raw `CapabilityKey.derive()` everywhere. Mirror gap to the marker.
  Tracked here for future-us; this spec addresses the marker only so the
  immediate Wan-on-RunPod ephemeral round-trip stops leaking. Log in
  PROGRESS as a sibling C-row item once the spec lands.
* **Lock file removal under STRICT.** Out of scope per §1 analysis above.
* **Pod destroyer on `__exit__`.** Explicitly out of scope per
  parent-spec changelog.
* **Output sink gating under STRICT.** Operator confirmed the
  `output/` artifact is allowed (parent spec §2 "sole exempt zone").

## 7. Acceptance criteria

1. **STRICT + vault writes alias.** With `EphemeralSession(enabled=True,
   vault=<loaded>)` active, `write_marker` produces a marker whose
   `capability_key` field equals
   `compute_profile_alias(cfg, vault)`, not `cfg.capability_key().derive()`.
2. **DEFAULT unchanged.** Without an EphemeralSession, marker contains
   the raw derived hash exactly as today (regression-lock).
3. **STRICT without vault falls back to raw hash.** Operator may run
   `kinoforge --ephemeral generate ...` without a vault for non-content-
   sensitive ephemeral runs; the marker contains the raw hash in that
   case (no alias derivation source available).
4. **Warm-reuse round-trip under STRICT + vault.** Two back-to-back
   `kinoforge --ephemeral generate ...` invocations against the same
   cfg + same vault hit the marker-current branch on the second run
   (no re-provision).
5. **Marker schema unchanged.** Field names, presence checks, and
   `_REQUIRED_KEYS` tuple in `provision_state.py` are not modified.
6. **No new EphemeralPolicy field required.** The decision is encoded
   in `delete_on_completion=True AND vault is not None`. Adding a
   dedicated flag would split policy state for a derivable condition.

## 8. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Wrong-mode marker round-trip causes silent re-provision | False-stale is safe; provision is idempotent. Documented in §5. |
| `compute_profile_alias` returns short `cfg-<sha12>` for vaults without explicit alias | 12-hex-char prefix is still 48 bits of entropy; collisions across cfg variations are astronomical. Same surface as the Appendix A profile cache row. |
| Future engine adds a new field to `CapabilityKey.derive()` that's not in `compute_profile_alias` material | `compute_profile_alias` material list (`{base, loras, engine, precision}`) is a subset of `CapabilityKey` fields by design. New fields will diverge → marker always stale under STRICT for that engine. Caught by AC4 if anyone runs the warm-reuse round-trip test. |
| `EphemeralSession.vault = None` accidentally crashes `marker_key_for` | `marker_key_for` guards with `session.vault is not None`. Explicit |

## 9. Testing plan

```
tests/core/test_provision_state_alias_keyed.py
  - test_marker_key_for_default_mode_returns_raw_derive_hash
  - test_marker_key_for_strict_with_vault_returns_alias
  - test_marker_key_for_strict_without_vault_falls_back_to_raw_hash
  - test_write_marker_then_is_marker_current_round_trip_strict_with_vault
  - test_default_mode_marker_payload_unchanged_regression_lock
```

All offline. No live spend.

## 10. Implementation order

1. **RED test 1:** `marker_key_for` STRICT + vault returns alias.
2. **Implement** `marker_key_for` + add `EphemeralSession.vault` field.
3. **GREEN test 1.**
4. **RED tests 2-5:** other ACs.
5. **GREEN tests 2-5** by wiring the orchestrator + CLI.
6. **Regression sweep:** `tests/core/`, `tests/cli/`, `tests/integration/`.
7. **Final:** spec amendment to parent doc § Appendix A + changelog
   entry. No commit; operator reviews then commits or discards.
