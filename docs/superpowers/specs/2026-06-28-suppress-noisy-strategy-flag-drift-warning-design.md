# Suppress noisy strategy-flag drift warning on warm-attach

## Problem

Every warm-attach generation emits:

```
WARNING kinoforge.profiles engine no longer declares strategy flags for
cached key <hex>; either declared_flags_map regressed or the engine was
downgraded
```

The warning fires unconditionally for `DiffusersEngine` and `FALEngine`
because their registry factories construct them without a
`declared_flags_map`, so `engine.declared_flags(key)` returns `{}` for every
key. Operators have learned to ignore it, which defeats the warning's
purpose for the case it was actually designed to catch (a hosted engine that
regressed from declaring flags to no longer declaring them).

Observed 2026-06-28 on a Wan 2.2 14B warm-attach; the line was load-bearing
noise that masked the real bug we were debugging in the same transcript.

## Goal

Silence the warning when there is no actual drift, while keeping it for
genuine regressions where a previously cached profile encodes a strategy
flag the engine no longer overrides.

## Non-goals

- Adding declared_flags maps to diffusers / fal engines. They legitimately
  do not declare strategy flags today; treat that as the default state.
- Plumbing `cfg.declared_flags_map` into live engines. Pre-existing dead
  code, out of scope.

## Approach

Single-site change in `core/profiles.py:verify_against_backend`. The
existing check (~line 410-421) emits the warning whenever
`engine.declared_flags(key)` contains neither `supports_native_extension`
nor `supports_joint_audio`. Tighten that branch: only warn if the cached
profile's strategy-flag values also differ from the fresh probe's values.

Rationale:
- If cache and probe agree, no drift exists. Warning is a false positive.
- If cache and probe disagree and the engine has stopped declaring the
  flag, the warning is the regression signal we want — `discover()` would
  have overwritten probe with declared, so divergence means an older write
  used a declared value that is no longer present.

## Change

```python
# core/profiles.py — inside verify_against_backend
if engine is not None and key is not None:
    declared = engine.declared_flags(key)
    if (
        "supports_native_extension" not in declared
        and "supports_joint_audio" not in declared
    ):
        if (
            profile.supports_native_extension != probe.supports_native_extension
            or profile.supports_joint_audio != probe.supports_joint_audio
        ):
            _log.warning(...)  # existing message unchanged
```

## Tests (RED first)

1. `test_verify_no_warning_when_engine_declares_no_flags_and_cache_matches_probe`
   - Engine with `declared_flags_map={}`, profile flags equal probe flags
   - Bug it catches: today this combination fires the warning on every
     warm-attach, training operators to ignore the line.
   - Expected: zero warning records.

2. `test_verify_still_warns_when_cache_disagrees_with_probe_and_engine_stopped_declaring`
   - Engine with `declared_flags_map={}`, cached profile carries
     `supports_native_extension=True`, fresh probe returns `False`
   - Bug it catches: an over-eager fix that drops the warning entirely
     would lose the genuine regression signal.
   - Expected: exactly one warning record containing
     `"engine no longer declares strategy flags"`.

3. `test_verify_no_warning_when_engine_still_declares_flags`
   - Engine declares `{"supports_native_extension": True}`
   - Expected: zero warning records (existing behavior; pin it).

All three live in `tests/core/test_profiles.py`; use `caplog` to capture.

## Out-of-scope cleanup

The `cfg.declared_flags_map` field on `BedrockVideoConfig` is never read.
Not removing it here — separate refactor if/when bedrock wiring lands.

## Risk

Low. Single conditional, scoped to the warning branch. No call-graph
changes. Existing `CapabilityMismatch` exception path (probeable-field
drift) is untouched.
