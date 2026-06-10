# Plan — provision marker alias-keying under `--ephemeral`

**Date:** 2026-06-10
**Spec:** `docs/superpowers/specs/2026-06-10-provision-marker-alias-keying-design.md`
**Status:** completed locally; untracked

---

## Task checklist

- [x] Task 0 — write addendum spec (`…-design.md`)
- [x] Task 1 — write RED tests (`tests/core/test_provision_state_alias_keyed.py`)
  - [x] AC1 — STRICT + vault returns alias
  - [x] AC2 — DEFAULT mode unchanged
  - [x] AC3 — STRICT without vault falls back to raw hash
  - [x] AC4 — warm-reuse round-trip under STRICT + vault
  - [x] AC5 — marker schema unchanged regression lock
  - [x] AC6 (added during implementation) — cross-mode marker treated as stale
- [x] Task 2 — confirm RED (5 failed import, 1 passed regression lock)
- [x] Task 3 — add `vault` field to `EphemeralSession`
- [x] Task 4 — implement `provision_state.marker_key_for(cfg, *, default=None)`
- [x] Task 5 — wire orchestrator (`_provision_compute_once` call site)
- [x] Task 6 — wire CLI (retain `_load_vault_or_none` return; pass to `EphemeralSession`)
- [x] Task 7 — first GREEN pass for new tests (6/6)
- [x] Task 8 — regression: orchestrator_render_provision tests broke (MagicMock cfg chain leaked through `cfg.capability_key().derive()`); refactor `marker_key_for` to accept optional `default` kwarg so the orchestrator passes its already-derived `key.derive()` and tests don't see a chain mismatch
- [x] Task 9 — second GREEN pass (1530/1530 + 15 skipped across `tests/core tests/cli tests/engines tests/integration tests/stores tests/providers tests/tools`)
- [ ] Task 10 — append Appendix A row in parent spec (deferred — operator review first)
- [ ] Task 11 — commit (BLOCKED on operator approval — operator asked for untracked deliverable)

## Files touched

### New (untracked)

- `docs/superpowers/specs/2026-06-10-provision-marker-alias-keying-design.md`
- `docs/superpowers/plans/2026-06-10-provision-marker-alias-keying.md` (this file)
- `tests/core/test_provision_state_alias_keyed.py` (6 tests, all green)

### Modified (untracked changes)

- `src/kinoforge/core/ephemeral.py` — `EphemeralSession.__init__` accepts
  optional `vault: Vault | None = None`; stored on `self.vault`. TYPE_CHECKING-only
  import of `Vault` to avoid runtime cycle.
- `src/kinoforge/core/provision_state.py` — adds `marker_key_for(cfg, *, default=None)`
  function. Lazy imports `EphemeralSession` and `compute_profile_alias`.
- `src/kinoforge/core/orchestrator.py` — `_provision_compute_once` call site
  uses `marker_key_for(cfg, default=key.derive())` for `capability_key_hex`.
  `marker_key_for` added to imports.
- `src/kinoforge/cli/_main.py` — retains `_load_vault_or_none` return as
  `_loaded_vault`; passes it to `EphemeralSession(enabled=..., vault=_loaded_vault)`.

## Verification

```
pixi run pytest tests/core/test_provision_state_alias_keyed.py -v
  → 6 passed in 0.24s

pixi run pytest tests/core/ tests/cli/ tests/engines/ tests/integration/ \
                tests/stores/ tests/providers/ tests/tools/
  → 1530 passed, 15 skipped in 33.76s
```

Live re-fire of the `kinoforge --ephemeral generate ...` smoke is not
required to verify this fix — the round-trip is offline-testable via
AC4 and the call site is the same code path the live smoke already
exercised (it would now write the alias under STRICT + vault, but the
write still happens). A live re-fire is a "belt-and-braces" worth doing
post-commit if the operator wants extra confidence.

## Out of scope (logged)

- **Profile-cache alias-key gap** — `compute_profile_alias` is defined in
  `core/vault.py` but never called in source; `JsonProfileCache` still
  keys on the raw `CapabilityKey.derive()` hex. Same Appendix A
  precedent, same fix shape; promotes to a sibling PROGRESS C-row when
  this spec lands.
- **`capability_key` in log lines** (`orchestrator.py:629, 632`) — the
  `[:12]` slug is short but still derived material. Spec relies on the
  `RedactingLogFilter` to scrub. Add the slug to the registry on a
  future patch if log audit shows it leaking.
- **Lock file** (`_locks/provision:<pod_id>.lock`) — per spec privacy
  framing, pod_id is operator-known compute resource ID, not in scope.
  See §1 of the addendum spec.

## Notes for operator review

1. **API shape of `marker_key_for`.** The `default` kwarg was added in
   Task 8 to preserve the orchestrator-test contract (those tests mock
   `key` not `cfg`). The alternative — fixing every test to set up
   `cfg.capability_key().derive()` correctly via MagicMock — would have
   touched ~10 tests for cosmetic gain. The two-mode API (caller passes
   default OR helper derives from cfg) is also closer to the spec text,
   which describes both branches.

2. **Backward compat under cross-mode.** AC6 added during write
   (originally Out-of-Scope but the spec §5 already discussed it).
   STRICT-written markers are treated as stale by a DEFAULT session
   (raw hash mismatch); DEFAULT-written markers stale by STRICT (alias
   mismatch). False-stale → re-provision → safe. Never a false-current.

3. **No new `EphemeralPolicy` field.** The STRICT-vs-DEFAULT decision is
   encoded in `delete_on_completion AND vault is not None` per spec §7.6.
   If the operator wants a dedicated `compute_marker_alias_keyed` flag
   for explicitness, that's a five-line follow-up; the test fixtures
   only need a single `EphemeralPolicy` swap to verify.

4. **Sibling deferred follow-up — Appendix A amendment to parent spec.**
   Not done in this layer (operator chose untracked deliverable; touching
   tracked spec would dirty the working tree). When committing, add a
   row to the parent spec's Appendix A:

   | Surface | Default | `--ephemeral` |
   |---|---|---|
   | `.provisioned` marker `capability_key` field | Raw `CapabilityKey.derive()` hex | Alias-keyed via `compute_profile_alias(cfg, vault)` |
